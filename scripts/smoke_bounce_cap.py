"""Smoke for the persisted bounce cap + human unblock reset.

Reviewer always answers REQUEST_CHANGES. With reviewer.max_bounce_count
temporarily set to 1 the flow is:

    dev -> reviewer (bounce 1, auto-return to dev)
        -> dev -> reviewer (bounce 2 > cap)
        -> blocked, blocked_reason=manual_intervention

Then the approve-return-dev endpoint logic unblocks the task: status back to
in_progress, bounce_count reset to 0, blocked_reason cleared, dev job queued.

Cost: $0 (mock claude).
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

CAP = 1


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
        db.execute(delete(ResourceLock).where(ResourceLock.resource_key.like("module:smoke-cap%")))
        db.commit()
    finally:
        db.close()


def _set_reviewer_cap(value):
    """Set reviewer.max_bounce_count, returning the previous value."""
    from sqlalchemy import select
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Role
    db = SessionLocal()
    try:
        reviewer = db.scalar(select(Role).where(Role.name == "reviewer"))
        prev = reviewer.max_bounce_count
        reviewer.max_bounce_count = value
        db.commit()
        return prev
    finally:
        db.close()


async def _drive() -> tuple[bool, dict]:
    worker_id = f"smoke-cap-{_uuid.uuid4().hex[:6]}"
    module_hint = f"smoke-cap-{_uuid.uuid4().hex[:6]}"

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
        "FLOWTRACK_CLAUDE_MOCK_VERDICT": "REQUEST_CHANGES",
    }

    code = f"""
import asyncio
from sqlalchemy import select
from flowtrack.core.database import SessionLocal
from flowtrack.models import Role, Task, Job
from flowtrack.models.task import TaskPriority, TaskStatus
from flowtrack.orchestrator.loop import run_orchestrator


async def main():
    db = SessionLocal()
    try:
        dev_id = db.scalar(select(Role.id).where(Role.name == 'dev'))
        t = Task(title='bounce-cap smoke', status=TaskStatus.TODO,
                 priority=TaskPriority.HIGH, ticket_id='SMOKE-CAP',
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

    # Wait until the cap forces blocked. 120s ceiling.
    for _ in range(240):
        await asyncio.sleep(0.5)
        db = SessionLocal()
        try:
            status = db.scalar(select(Task.status).where(Task.id == task_id))
            if status and status.value == 'blocked':
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
        diag = {
            "task_status": task.status.value,
            "blocked_reason": task.blocked_reason.value if task.blocked_reason else None,
            "bounce_count": task.bounce_count,
            "jobs": [(roles_by_id[j.role_id], j.status.value) for j in jobs],
            "transitions": [(t.from_status, t.to_status, t.reason) for t in transitions],
        }

        capped_ok = (
            task.status.value == "blocked"
            and task.blocked_reason == BlockedReason.MANUAL_INTERVENTION
            and task.bounce_count == CAP + 1
            and any(t.reason and "bounce cap" in t.reason for t in transitions)
        )
        diag["capped_ok"] = capped_ok
        if not capped_ok:
            return False, diag
    finally:
        db.close()

    # Human unblock: exercise the approve-return-dev endpoint logic directly.
    from flowtrack.api.routers.tasks import approve_return_to_dev

    db = SessionLocal()
    try:
        resp = approve_return_to_dev(task_id, db)
        db.commit()
        task = db.get(Task, task_id)
        dev_jobs_after = db.scalar(
            select(Job.id).where(Job.task_id == task_id, Job.id == resp.job_id)
        )
        diag["after_approve"] = {
            "status": task.status.value,
            "blocked_reason": task.blocked_reason.value if task.blocked_reason else None,
            "bounce_count": task.bounce_count,
            "job_created": dev_jobs_after is not None,
        }
        approve_ok = (
            task.status.value == "in_progress"
            and task.blocked_reason is None
            and task.bounce_count == 0
            and dev_jobs_after is not None
        )
        diag["approve_ok"] = approve_ok
        # Cancel the queued job so the next daemon run doesn't pick it up.
        job = db.get(Job, resp.job_id)
        if job is not None:
            db.delete(job)
        db.commit()
    finally:
        db.close()

    return capped_ok and approve_ok, diag


async def main() -> int:
    await cleanup()
    prev_cap = _set_reviewer_cap(CAP)
    try:
        ok, diag = await _drive()
    finally:
        _set_reviewer_cap(prev_cap)
        await cleanup()

    print()
    print("=== BOUNCE CAP ===")
    for k, v in diag.items():
        print(f"  {k} = {v}")
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
