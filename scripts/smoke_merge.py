"""Smoke for the merge & deploy stage (builtin executor, mock claude, $0).

All git activity happens in a throwaway scratch repo (FLOWTRACK_TARGET_REPO_PATH)
— the flowtrack repo itself is never merged into.

Scenarios:
  MERGED   — merge job with both gate transitions seeded: work branch lands
             on scratch main (--no-ff), task -> done, Deployment row
             recorded, deploy_command marker written.
  GATE     — qa transition missing: blocked/manual_intervention, scratch
             main untouched.
  CONFLICT — branch conflicts with main: blocked/code_failure with the
             conflicting file named, scratch main untouched.
  E2E      — qa job + FLOWTRACK_MERGE_ENABLED=true: qa PASS diverts to the
             merge stage (audit transition 'pipeline: qa completed'), then
             merge completes -> done + Deployment.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import uuid as _uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOCK = REPO / "scripts" / "mock_claude.py"
WORKTREES = REPO / ".smoke-worktrees"

WORK_BRANCH = "auto/dev-fake-merge"


def make_scratch_repo(base: Path, *, conflict: bool) -> Path:
    repo = base / "scratch"
    repo.mkdir()
    run = lambda *a: subprocess.run(  # noqa: E731
        a, cwd=repo, check=True, capture_output=True, text=True
    )
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "smoke@flowtrack.local")
    run("git", "config", "user.name", "smoke")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    run("git", "add", ".")
    run("git", "commit", "-q", "-m", "base")
    run("git", "checkout", "-q", "-b", WORK_BRANCH)
    if conflict:
        (repo / "app.txt").write_text("feature version\n", encoding="utf-8")
    else:
        (repo / "feature.txt").write_text("the work\n", encoding="utf-8")
    run("git", "add", ".")
    run("git", "commit", "-q", "-m", "task work")
    run("git", "checkout", "-q", "main")
    if conflict:
        (repo / "app.txt").write_text("mainline version\n", encoding="utf-8")
        run("git", "add", ".")
        run("git", "commit", "-q", "-m", "mainline change")
    return repo


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


async def _drive(scenario: str, scratch: Path) -> tuple[bool, dict]:
    tag = scenario.lower()
    worker_id = f"smoke-merge-{tag}-{_uuid.uuid4().hex[:6]}"
    ticket = f"SMOKE-MERGE-{tag.upper()}-{_uuid.uuid4().hex[:6]}"
    marker = scratch / "deployed.marker"

    env = {
        **os.environ,
        "FLOWTRACK_DATABASE_URL": "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
        "FLOWTRACK_ORCHESTRATOR_DRY_RUN": "false",
        "FLOWTRACK_MAX_CONCURRENT_INSTANCES": "1",
        "FLOWTRACK_ORCHESTRATOR_LOOP_INTERVAL_SECONDS": "0.3",
        "FLOWTRACK_CLAUDE_EXECUTABLE": f'"{sys.executable}" "{MOCK}"',
        "FLOWTRACK_TARGET_REPO_PATH": str(scratch),
        "FLOWTRACK_WORKTREE_ROOT": str(WORKTREES),
        "FLOWTRACK_WORKER_ID": worker_id,
        "FLOWTRACK_MERGE_ENABLED": "true",
    }
    if scenario == "MERGED":
        env["FLOWTRACK_DEPLOY_COMMAND"] = (
            f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').write(\'x\')"'
        )

    seed_qa_transition = scenario in ("MERGED", "CONFLICT")
    job_role = "qa" if scenario == "E2E" else "merge"

    code = f"""
import asyncio
from sqlalchemy import select
from flowtrack.core.database import SessionLocal
from flowtrack.models import Job, Role, Task, TaskTransition
from flowtrack.models.task import TaskPriority, TaskStatus
from flowtrack.orchestrator.loop import run_orchestrator


async def main():
    db = SessionLocal()
    try:
        t = Task(title='merge-smoke {tag}', status=TaskStatus.IN_QA,
                 priority=TaskPriority.HIGH, ticket_id='{ticket}',
                 module_hint='smoke-merge-{tag}', acceptance_criteria='ok')
        db.add(t); db.flush()
        task_id = t.id
        db.add(TaskTransition(task_id=task_id, from_status='in_review',
                              to_status='in_qa', reason='pipeline: reviewer completed'))
        if {seed_qa_transition!r}:
            db.add(TaskTransition(task_id=task_id, from_status='in_qa',
                                  to_status='in_qa', reason='pipeline: qa completed'))
        role = db.scalar(select(Role).where(Role.name == '{job_role}'))
        assert role is not None, 'missing role {job_role}'
        db.add(Job(task_id=task_id, role_id=role.id, priority=100,
                   worker_id='{worker_id}',
                   payload_json={{'parent_branch': '{WORK_BRANCH}'}}))
        db.commit()
    finally:
        db.close()
    print(f'TASK_ID={{task_id}}', flush=True)

    stop = asyncio.Event()
    runner = asyncio.create_task(run_orchestrator(stop))
    for _ in range(240):
        await asyncio.sleep(0.5)
        db = SessionLocal()
        try:
            t = db.get(Task, task_id)
            if t.status.value in ('done', 'blocked'):
                await asyncio.sleep(1.0)
                break
        finally:
            db.close()
    stop.set()
    try:
        await asyncio.wait_for(runner, timeout=10)
    except asyncio.TimeoutError:
        runner.cancel()


asyncio.run(main())
"""

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, cwd=str(REPO), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out_str = stdout.decode("utf-8", "replace")
    err_str = stderr.decode("utf-8", "replace")
    task_id = None
    for line in out_str.splitlines():
        if line.startswith("TASK_ID="):
            task_id = _uuid.UUID(line.split("=", 1)[1])
            break
    if task_id is None:
        print(f"  subprocess stdout: {out_str[:400]}")
        print(f"  subprocess stderr: {err_str[:800]}")
        return False, {"error": "no TASK_ID emitted"}

    from sqlalchemy import select
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Deployment, Job, Role, Task, TaskTransition
    from flowtrack.models.task import BlockedReason

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        roles_by_id = {r.id: r.name for r in db.scalars(select(Role))}
        jobs = [(roles_by_id[j.role_id], j.status.value) for j in db.scalars(
            select(Job).where(Job.task_id == task_id).order_by(Job.created_at))]
        transitions = [(t.from_status, t.to_status, t.reason) for t in db.scalars(
            select(TaskTransition).where(TaskTransition.task_id == task_id)
            .order_by(TaskTransition.transitioned_at))]
        deployment = db.scalar(select(Deployment).where(Deployment.ticket_id == ticket))
    finally:
        db.close()

    main_sha = git_out(scratch, "rev-parse", "main")
    main_log = git_out(scratch, "log", "--oneline", "main")
    merged = "task work" in main_log

    diag = {
        "scenario": scenario,
        "task_status": task.status.value,
        "blocked_reason": task.blocked_reason.value if task.blocked_reason else None,
        "jobs": jobs,
        "transitions": transitions,
        "deployment_sha": deployment.commit_sha[:12] if deployment else None,
        "main_merged": merged,
        "deploy_marker": marker.exists(),
    }

    if scenario == "MERGED":
        ok = (
            task.status.value == "done" and task.blocked_reason is None
            and merged and deployment is not None
            and deployment.commit_sha == main_sha
            and marker.exists()
            and any(r == "pipeline: merge completed" for _, _, r in transitions)
        )
    elif scenario == "GATE":
        ok = (
            task.status.value == "blocked"
            and task.blocked_reason == BlockedReason.MANUAL_INTERVENTION
            and not merged and deployment is None
        )
    elif scenario == "CONFLICT":
        # The conflicting-file detail lands in the task comment; here the
        # bucket + untouched main are what matter.
        ok = (
            task.status.value == "blocked"
            and task.blocked_reason == BlockedReason.CODE_FAILURE
            and not merged and deployment is None
        )
    else:  # E2E
        ok = (
            task.status.value == "done" and merged and deployment is not None
            and [r for r, _ in jobs] == ["qa", "merge"]
            and any(r == "pipeline: qa completed" for _, _, r in transitions)
            and any(r == "pipeline: merge completed" for _, _, r in transitions)
        )
    return ok, diag


async def cleanup() -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "prune", cwd=str(REPO),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if WORKTREES.exists():
        shutil.rmtree(WORKTREES, ignore_errors=True)
    from sqlalchemy import delete
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Deployment, ResourceLock
    db = SessionLocal()
    try:
        db.execute(delete(ResourceLock).where(
            ResourceLock.resource_key.like("module:smoke-merge%")))
        db.execute(delete(Deployment).where(
            Deployment.ticket_id.like("SMOKE-MERGE-%")))
        db.commit()
    finally:
        db.close()


async def main() -> int:
    await cleanup()
    overall_ok = True
    for scenario in ("MERGED", "GATE", "CONFLICT", "E2E"):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = make_scratch_repo(Path(tmp), conflict=(scenario == "CONFLICT"))
            ok, diag = await _drive(scenario, scratch)
        print()
        print(f"=== MERGE {scenario} ===")
        for k, v in diag.items():
            print(f"  {k} = {v}")
        print(f"  -> {'PASS' if ok else 'FAIL'}")
        overall_ok = overall_ok and ok
    await cleanup()
    print()
    print("RESULT:", "PASS" if overall_ok else "FAIL")
    return 0 if overall_ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
