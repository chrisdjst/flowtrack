"""Smoke for reviewer's REQUEST_CHANGES and NEEDS_HUMAN branches.

Mock honours FLOWTRACK_CLAUDE_MOCK_VERDICT — the spawner inherits the
daemon's env, so the same env value goes to every spawn this run. Dev
doesn't care; reviewer's branching code triggers.

Scenarios:
  - REQUEST_CHANGES: dev runs, reviewer runs and writes verdict, task is
    sent back to dev (status=in_progress), a fresh dev job is enqueued in
    the same lane.
  - NEEDS_HUMAN: same setup, task ends in 'blocked' with a comment.
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
    # Locks left behind by previous aborted scenarios.
    from sqlalchemy import delete
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import ResourceLock
    db = SessionLocal()
    try:
        db.execute(delete(ResourceLock).where(ResourceLock.resource_key.like("module:smoke-rev%")))
        db.commit()
    finally:
        db.close()


async def _drive_scenario(verdict: str) -> tuple[bool, dict]:
    """Spin up a fresh orchestrator process per scenario so env var changes
    apply. Each scenario uses its own worker lane AND module_hint so the
    resource lock from one scenario can't block the other.

    Returns (ok, diagnostic_dict).
    """
    worker_id = f"smoke-rev-{verdict.lower()}-{_uuid.uuid4().hex[:6]}"
    module_hint = f"smoke-rev-{verdict.lower()}-{_uuid.uuid4().hex[:6]}"

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
        "FLOWTRACK_CLAUDE_MOCK_VERDICT": verdict,
    }

    # Run the driver inline via a subprocess so it picks up the env above.
    code = f"""
import asyncio
from sqlalchemy import select, func
from flowtrack.core.database import SessionLocal
from flowtrack.models import Role, Task, Job
from flowtrack.models.task import TaskPriority, TaskStatus
from flowtrack.orchestrator.loop import run_orchestrator


async def main():
    db = SessionLocal()
    try:
        dev_id = db.scalar(select(Role.id).where(Role.name == 'dev'))
        rev_id = db.scalar(select(Role.id).where(Role.name == 'reviewer'))
        t = Task(title='reviewer-smoke {verdict}', status=TaskStatus.TODO,
                 priority=TaskPriority.HIGH, ticket_id='SMOKE-REV-{verdict}',
                 module_hint='{module_hint}', acceptance_criteria='ok')
        db.add(t); db.flush()
        task_id = t.id
        db.add(Job(task_id=task_id, role_id=dev_id, priority=10,
                   worker_id='{worker_id}'))
        db.commit()
    finally:
        db.close()
    print(f'TASK_ID={{task_id}}', flush=True)

    stop = asyncio.Event()
    runner = asyncio.create_task(run_orchestrator(stop))

    # Wait until: task in (blocked, done) OR we observe 2+ dev jobs (the
    # request_changes branch enqueued a re-do). 60s ceiling.
    for _ in range(120):
        await asyncio.sleep(0.5)
        db = SessionLocal()
        try:
            status = db.scalar(select(Task.status).where(Task.id == task_id))
            dev_count = db.scalar(
                select(func.count(Job.id)).where(
                    Job.task_id == task_id, Job.role_id == dev_id
                )
            )
            if (status and status.value in ('blocked', 'done')) or (dev_count or 0) >= 2:
                # Give the chain another beat to flush transitions/comments.
                await asyncio.sleep(2.0)
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

    # Inspect from this process.
    from sqlalchemy import select
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Instance, Job, Role, Task, TaskComment, TaskTransition

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        roles_by_id = {r.id: r.name for r in db.scalars(select(Role))}
        jobs = list(db.scalars(
            select(Job).where(Job.task_id == task_id).order_by(Job.created_at)
        ))
        instances = list(db.scalars(
            select(Instance).where(Instance.task_id == task_id).order_by(Instance.spawned_at)
        ))
        transitions = list(db.scalars(
            select(TaskTransition).where(TaskTransition.task_id == task_id)
            .order_by(TaskTransition.transitioned_at)
        ))
        comments = list(db.scalars(
            select(TaskComment).where(TaskComment.task_id == task_id)
        ))
        diag = {
            "verdict": verdict,
            "task_status": task.status.value,
            "jobs": [(roles_by_id[j.role_id], j.status.value) for j in jobs],
            "instances": [(roles_by_id[i.role_id], i.status.value) for i in instances],
            "transitions": [(t.from_status, t.to_status, t.reason) for t in transitions],
            "comment_count": len(comments),
        }
    finally:
        db.close()

    if verdict == "REQUEST_CHANGES":
        ok = (
            task.status.value in ("in_progress", "in_review", "in_qa", "done")
            # Specifically: there should be at least 2 dev jobs in the lane.
            and sum(1 for r, _ in diag["jobs"] if r == "dev") >= 2
            and any(t for t in transitions if t.reason and "REQUEST_CHANGES" in t.reason)
            and any("Reviewer requested changes" in (c.body or "") for c in comments)
        )
    elif verdict == "NEEDS_HUMAN":
        ok = (
            task.status.value == "blocked"
            and any(t for t in transitions if t.reason and "NEEDS_HUMAN" in t.reason)
            and any("NEEDS_HUMAN" in (c.body or "") for c in comments)
        )
    else:
        ok = False
    return ok, diag


async def main() -> int:
    await cleanup()
    overall_ok = True

    for verdict in ("REQUEST_CHANGES", "NEEDS_HUMAN"):
        ok, diag = await _drive_scenario(verdict)
        print()
        print(f"=== {verdict} ===")
        print(f"  task_status   = {diag.get('task_status')}")
        print(f"  jobs          = {diag.get('jobs')}")
        print(f"  instances     = {diag.get('instances')}")
        print(f"  transitions   = {diag.get('transitions')}")
        print(f"  comment_count = {diag.get('comment_count')}")
        print(f"  -> {'PASS' if ok else 'FAIL'}")
        overall_ok = overall_ok and ok

    await cleanup()
    print()
    print("RESULT:", "PASS" if overall_ok else "FAIL")
    return 0 if overall_ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
