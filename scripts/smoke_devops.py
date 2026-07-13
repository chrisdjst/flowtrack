"""Smoke for the DevOps agent (infra-failure pickup + RCA outcome routing).

Scenarios (mock claude, $0):
  RESOLVED — a task blocked with blocked_reason=infra_failure is picked up
    (devops job at PICKUP_PRIORITY), devops says RESOLVED -> seeded chain
    sends it to qa (blocked_reason cleared), qa PASSes -> done.
  BLOCKED — devops says BLOCKED -> task lands in blocked with
    blocked_reason=manual_intervention (human triage).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid as _uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOCK = REPO / "scripts" / "mock_claude.py"
WORKTREES = REPO / ".smoke-worktrees"


async def cleanup() -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "prune", cwd=str(REPO),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    proc = await asyncio.create_subprocess_exec(
        "git", "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/",
        cwd=str(REPO),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    for branch in out.decode().splitlines():
        branch = branch.strip()
        if branch:
            p = await asyncio.create_subprocess_exec(
                "git", "branch", "-D", branch, cwd=str(REPO),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
    if WORKTREES.exists():
        shutil.rmtree(WORKTREES, ignore_errors=True)
    from sqlalchemy import delete
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import ResourceLock
    db = SessionLocal()
    try:
        db.execute(delete(ResourceLock).where(ResourceLock.resource_key.like("module:smoke-devops%")))
        db.commit()
    finally:
        db.close()


async def _drive_scenario(devops_verdict: str) -> tuple[bool, dict]:
    tag = devops_verdict.lower()
    worker_id = f"smoke-devops-{tag}-{_uuid.uuid4().hex[:6]}"
    module_hint = f"smoke-devops-{tag}-{_uuid.uuid4().hex[:6]}"

    env = {
        **os.environ,
        "FLOWTRACK_DATABASE_URL": "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
        "FLOWTRACK_ORCHESTRATOR_DRY_RUN": "false",
        "FLOWTRACK_MAX_CONCURRENT_INSTANCES": "1",
        "FLOWTRACK_ORCHESTRATOR_LOOP_INTERVAL_SECONDS": "0.3",
        "FLOWTRACK_CLAUDE_EXECUTABLE": f'"{sys.executable}" "{MOCK}"',
        "FLOWTRACK_TARGET_REPO_PATH": str(REPO),
        "FLOWTRACK_WORKTREE_ROOT": str(WORKTREES),
        "FLOWTRACK_WORKER_ID": worker_id,
        "FLOWTRACK_CLAUDE_MOCK_VERDICT_DEVOPS": devops_verdict,
        # qa falls back to the default APPROVE PASS after a RESOLVED handoff
    }

    code = f"""
import asyncio
from sqlalchemy import select
from flowtrack.agents.devops import PICKUP_PRIORITY, pickup_infra_failures
from flowtrack.core.database import SessionLocal
from flowtrack.models import Task
from flowtrack.models.task import BlockedReason, TaskPriority, TaskStatus
from flowtrack.orchestrator.loop import run_orchestrator


async def main():
    db = SessionLocal()
    try:
        t = Task(title='devops-smoke {tag}', status=TaskStatus.BLOCKED,
                 priority=TaskPriority.HIGH, ticket_id='SMOKE-DEVOPS-{tag}',
                 module_hint='{module_hint}', acceptance_criteria='ok',
                 blocked_reason=BlockedReason.INFRA_FAILURE)
        db.add(t); db.flush()
        task_id = t.id
        picked = pickup_infra_failures(db, limit=10, worker_id='{worker_id}')
        assert any(p.id == task_id for p in picked), picked
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
            done = t.status.value == 'done'
            reblocked = (
                t.status.value == 'blocked'
                and t.blocked_reason is not None
                and t.blocked_reason.value == 'manual_intervention'
            )
            if done or reblocked:
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
        sys.executable, "-c", code,
        cwd=str(REPO), env=env,
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
    if err_str and ("Error" in err_str or "Traceback" in err_str):
        print(f"  subprocess stderr: {err_str[:800]}")

    from sqlalchemy import select
    from flowtrack.agents.devops import PICKUP_PRIORITY
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Job, Role, Task, TaskTransition
    from flowtrack.models.task import BlockedReason

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        roles_by_id = {r.id: r.name for r in db.scalars(select(Role))}
        jobs = list(db.scalars(
            select(Job).where(Job.task_id == task_id).order_by(Job.created_at)
        ))
        transitions = list(db.scalars(
            select(TaskTransition).where(TaskTransition.task_id == task_id)
            .order_by(TaskTransition.transitioned_at)
        ))
        job_info = [(roles_by_id[j.role_id], j.priority) for j in jobs]
        diag = {
            "verdict": devops_verdict,
            "task_status": task.status.value,
            "blocked_reason": task.blocked_reason.value if task.blocked_reason else None,
            "jobs": job_info,
            "transitions": [(t.from_status, t.to_status, t.reason) for t in transitions],
        }
    finally:
        db.close()

    if devops_verdict == "RESOLVED":
        ok = (
            task.status.value == "done"
            and task.blocked_reason is None
            and job_info[0] == ("devops", PICKUP_PRIORITY)
            and [r for r, _ in job_info] == ["devops", "qa"]
            and any(t.reason == "pipeline: devops completed" for t in transitions)
        )
    else:  # BLOCKED
        ok = (
            task.status.value == "blocked"
            and task.blocked_reason == BlockedReason.MANUAL_INTERVENTION
            and any(t.reason == "devops: BLOCKED" for t in transitions)
        )
    return ok, diag


async def main() -> int:
    await cleanup()
    overall_ok = True

    for verdict in ("RESOLVED", "BLOCKED"):
        ok, diag = await _drive_scenario(verdict)
        print()
        print(f"=== DEVOPS {verdict} ===")
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
