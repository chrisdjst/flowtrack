"""Smoke for QA's verdict branches (FAIL and BLOCKED).

Per-role mock verdicts (FLOWTRACK_CLAUDE_MOCK_VERDICT_<ROLE>) let the
reviewer APPROVE while QA rejects in the same run.

Scenarios:
  - FAIL: dev -> reviewer (approve) -> qa FAIL -> task back to in_progress
    (qa.task_status_on_failure), bounce_count incremented, dev job enqueued,
    and — since code changes — the rework re-enters at REVIEWER, not qa.
  - BLOCKED: same chain, qa says BLOCKED -> task blocked with
    blocked_reason=infra_failure (DevOps pickup bucket, not a code bounce).

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
        db.execute(delete(ResourceLock).where(ResourceLock.resource_key.like("module:smoke-qa%")))
        db.commit()
    finally:
        db.close()


async def _drive_scenario(qa_verdict: str) -> tuple[bool, dict]:
    worker_id = f"smoke-qa-{qa_verdict.lower()}-{_uuid.uuid4().hex[:6]}"
    module_hint = f"smoke-qa-{qa_verdict.lower()}-{_uuid.uuid4().hex[:6]}"

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
        "FLOWTRACK_CLAUDE_MOCK_VERDICT_REVIEWER": "APPROVE",
        "FLOWTRACK_CLAUDE_MOCK_VERDICT_QA": qa_verdict,
    }

    # FAIL: stop once a 2nd reviewer job appears (rework re-entered review)
    # or the task blocks (would be a FAIL-branch bug). BLOCKED: stop at
    # status=blocked.
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
        t = Task(title='qa-verdict smoke {qa_verdict}', status=TaskStatus.TODO,
                 priority=TaskPriority.HIGH, ticket_id='SMOKE-QA-{qa_verdict}',
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

    for _ in range(240):
        await asyncio.sleep(0.5)
        db = SessionLocal()
        try:
            status = db.scalar(select(Task.status).where(Task.id == task_id))
            rev_count = db.scalar(
                select(func.count(Job.id)).where(
                    Job.task_id == task_id, Job.role_id == rev_id
                )
            )
            if (status and status.value in ('blocked', 'done')) or (rev_count or 0) >= 2:
                await asyncio.sleep(1.5)
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
        job_roles = [roles_by_id[j.role_id] for j in jobs]
        diag = {
            "qa_verdict": qa_verdict,
            "task_status": task.status.value,
            "blocked_reason": task.blocked_reason.value if task.blocked_reason else None,
            "bounce_count": task.bounce_count,
            "jobs": [(roles_by_id[j.role_id], j.status.value) for j in jobs],
            "transitions": [(t.from_status, t.to_status, t.reason) for t in transitions],
        }
    finally:
        db.close()

    if qa_verdict == "FAIL":
        ok = (
            any(t.reason == "qa: FAIL" and t.to_status == "in_progress" for t in transitions)
            and task.bounce_count >= 1
            and task.blocked_reason is None
            # rework chain re-enters at reviewer: dev, reviewer, qa, dev, reviewer
            and job_roles.count("dev") >= 2
            and job_roles.count("reviewer") >= 2
        )
    else:  # BLOCKED
        ok = (
            task.status.value == "blocked"
            and task.blocked_reason == BlockedReason.INFRA_FAILURE
            and any(t.reason == "qa: BLOCKED" for t in transitions)
            # infra block is NOT a code bounce
            and task.bounce_count == 0
        )
    return ok, diag


async def main() -> int:
    await cleanup()
    overall_ok = True

    for qa_verdict in ("FAIL", "BLOCKED"):
        ok, diag = await _drive_scenario(qa_verdict)
        print()
        print(f"=== QA {qa_verdict} ===")
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
