"""Smoke test for the budget circuit breaker.

Scenario:
  - Configure budget_hour_cap_usd=0.01 (mock spends ~$0.0191 per instance,
    so a single run blows the cap).
  - Queue 2 dev jobs.
  - Expect: job_1 runs to completion (claimed BEFORE any spend recorded);
    after its usage events accumulate past $0.01, the loop's circuit breaker
    refuses to claim job_2. Job_2 stays QUEUED.
  - Assert: 1 completed instance, 1 still-queued job.
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

WORKER_ID = f"smoke-budget-{_uuid.uuid4().hex[:8]}"

os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)
os.environ["FLOWTRACK_ORCHESTRATOR_DRY_RUN"] = "false"
os.environ["FLOWTRACK_MAX_CONCURRENT_INSTANCES"] = "1"
os.environ["FLOWTRACK_ORCHESTRATOR_LOOP_INTERVAL_SECONDS"] = "0.3"
os.environ["FLOWTRACK_CLAUDE_EXECUTABLE"] = f'"{sys.executable}" "{MOCK}"'
os.environ["FLOWTRACK_TARGET_REPO_PATH"] = str(REPO)
os.environ["FLOWTRACK_WORKTREE_ROOT"] = str(WORKTREES)
os.environ["FLOWTRACK_WORKER_ID"] = WORKER_ID
# Tiny cap: mock spends ~$0.0191 per instance so the first run breaches it.
os.environ["FLOWTRACK_BUDGET_HOUR_CAP_USD"] = "0.01"

from sqlalchemy import func, select  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.models import (  # noqa: E402
    BudgetWindow,
    Instance,
    Job,
    Role,
    Task,
)
from flowtrack.models.job import JobStatus  # noqa: E402
from flowtrack.models.task import TaskPriority, TaskStatus  # noqa: E402
from flowtrack.orchestrator.loop import run_orchestrator  # noqa: E402


async def cleanup_worktrees_and_branches() -> None:
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


async def main() -> int:
    await cleanup_worktrees_and_branches()

    # Wipe budget windows from previous runs so caps are honoured deterministically.
    db = SessionLocal()
    try:
        db.execute(BudgetWindow.__table__.delete())
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        dev_role = db.scalar(select(Role).where(Role.name == "dev"))
        # Two independent tasks (don't chain via pipeline — we want plain queue gate).
        t1 = Task(title="budget t1", status=TaskStatus.TODO, priority=TaskPriority.HIGH,
                  ticket_id="BUDGET-1", module_hint="budget-1")
        t2 = Task(title="budget t2", status=TaskStatus.TODO, priority=TaskPriority.HIGH,
                  ticket_id="BUDGET-2", module_hint="budget-2")
        db.add_all([t1, t2])
        db.flush()
        # Lower priority number = claimed first.
        db.add(Job(task_id=t1.id, role_id=dev_role.id, priority=1, worker_id=WORKER_ID))
        db.add(Job(task_id=t2.id, role_id=dev_role.id, priority=2, worker_id=WORKER_ID))
        # Mark dev role as terminal for this smoke (no chaining) so dev doesn't
        # enqueue a reviewer job and dilute our assertion.
        # We can't mutate the seeded role here without affecting other smokes,
        # so instead we just count and ignore chained jobs.
        db.commit()
        t1_id, t2_id = t1.id, t2.id
        print(f"queued 2 jobs (lane={WORKER_ID}, cap=$0.01)")
    finally:
        db.close()

    stop = asyncio.Event()
    runner = asyncio.create_task(run_orchestrator(stop))

    # Run long enough for one instance to complete + the budget check to kick in.
    await asyncio.sleep(8.0)

    stop.set()
    try:
        await asyncio.wait_for(runner, timeout=10)
    except asyncio.TimeoutError:
        runner.cancel()

    db = SessionLocal()
    try:
        # Only count jobs from our lane.
        jobs = list(db.scalars(
            select(Job).where(Job.worker_id == WORKER_ID).order_by(Job.priority)
        ))
        completed = [j for j in jobs if j.status == JobStatus.DONE]
        queued = [j for j in jobs if j.status == JobStatus.QUEUED]

        budget_row = db.scalar(
            select(BudgetWindow)
            .order_by(BudgetWindow.cost_usd.desc())
            .limit(1)
        )
        print()
        print(f"=== JOBS ({len(jobs)} total in lane) ===")
        for j in jobs:
            print(f"  task={j.task_id} priority={j.priority} status={j.status.value}")
        print(f"completed={len(completed)} queued={len(queued)}")
        print(f"top budget window: cost=${budget_row.cost_usd if budget_row else 0}")

        # We expect at least 1 done (t1) and at least 1 still queued (t2 gated).
        ok = (
            any(j.task_id == t1_id and j.status == JobStatus.DONE for j in jobs)
            and any(j.task_id == t2_id and j.status == JobStatus.QUEUED for j in jobs)
        )
        print()
        print("RESULT:", "PASS" if ok else "FAIL")
    finally:
        db.close()

    await cleanup_worktrees_and_branches()
    # Clean up the gated queued job so subsequent smokes don't claim it.
    db = SessionLocal()
    try:
        for j in db.scalars(select(Job).where(Job.worker_id == WORKER_ID)):
            if j.status == JobStatus.QUEUED:
                j.status = JobStatus.CANCELLED
                j.last_error = "budget-smoke teardown"
        db.commit()
    finally:
        db.close()

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
