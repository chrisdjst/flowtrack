"""End-to-end smoke test of the orchestrator using a mock ``claude`` executable.

Runs the orchestrator loop directly (no FastAPI). Inserts a task + job, waits
for the supervisor to finish, prints the final state of:
    - the Job row (status, last_error)
    - the Instance row (status, exit_code, tokens, cost, worktree path)
    - InstanceEvent count
    - any leaked ResourceLock rows

Prereqs:
    - Postgres reachable at FLOWTRACK_DATABASE_URL
    - Schema migrated to 005

Usage:
    uv run python scripts/smoke_orchestrator.py
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

# Per-run isolation lane so stray daemons (or other smokes) don't race us
# for the jobs this script creates. See alembic 007.
WORKER_ID = f"smoke-orch-{_uuid.uuid4().hex[:8]}"

# Settings must be in env BEFORE importing the orchestrator.
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

from sqlalchemy import func, select  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.models import (  # noqa: E402
    Instance,
    InstanceEvent,
    Job,
    ResourceLock,
    Role,
    Task,
)
from flowtrack.models.task import TaskPriority, TaskStatus  # noqa: E402
from flowtrack.orchestrator.loop import run_orchestrator  # noqa: E402


async def main() -> int:
    # Clean prior smoke worktrees (best-effort).
    if WORKTREES.exists():
        shutil.rmtree(WORKTREES, ignore_errors=True)

    # Seed task + job.
    db = SessionLocal()
    try:
        dev_role = db.scalar(select(Role).where(Role.name == "dev"))
        if dev_role is None:
            print("ERROR: 'dev' role missing — run `alembic upgrade head`.")
            return 1
        task = Task(
            title="Smoke spawn",
            description="Verify the orchestrator spawns a (mock) Claude end-to-end.",
            status=TaskStatus.TODO,
            priority=TaskPriority.HIGH,
            ticket_id="SMOKE-SPAWN",
            module_hint="smoke",
            acceptance_criteria="Mock claude emits stream-json and exits 0.",
        )
        db.add(task)
        db.flush()
        job = Job(task_id=task.id, role_id=dev_role.id, priority=10, worker_id=WORKER_ID)
        db.add(job)
        db.commit()
        task_id, job_id = task.id, job.id
        print(f"queued: task={task_id} job={job_id} worker={WORKER_ID}")
    finally:
        db.close()

    # Run orchestrator until job is terminal (or timeout).
    stop = asyncio.Event()
    runner = asyncio.create_task(run_orchestrator(stop))
    terminal = {"done", "failed", "cancelled"}
    deadline = 30  # seconds
    waited = 0.0
    while waited < deadline:
        await asyncio.sleep(0.3)
        waited += 0.3
        db = SessionLocal()
        try:
            j = db.get(Job, job_id)
            if j.status.value in terminal:
                break
        finally:
            db.close()
    else:
        print(f"WARN: job did not reach terminal status within {deadline}s")

    stop.set()
    try:
        await asyncio.wait_for(runner, timeout=10)
    except asyncio.TimeoutError:
        runner.cancel()

    # Inspect.
    db = SessionLocal()
    try:
        j = db.get(Job, job_id)
        inst = db.scalar(select(Instance).where(Instance.task_id == task_id))
        ev_count = 0
        lock_count = 0
        if inst is not None:
            ev_count = db.scalar(
                select(func.count()).select_from(InstanceEvent)
                .where(InstanceEvent.instance_id == inst.id)
            )
            lock_count = db.scalar(
                select(func.count()).select_from(ResourceLock)
                .where(ResourceLock.instance_id == inst.id)
            )

        print()
        print("=== JOB ===")
        print(f"  status      = {j.status.value}")
        print(f"  attempts    = {j.attempts}")
        print(f"  last_error  = {j.last_error}")
        print()
        print("=== INSTANCE ===")
        if inst is None:
            print("  (no instance row created — supervisor never started)")
        else:
            print(f"  status      = {inst.status.value}")
            print(f"  exit_code   = {inst.exit_code}")
            print(f"  tokens      = in:{inst.tokens_input} out:{inst.tokens_output}")
            print(f"  cost_usd    = {inst.cost_usd}")
            print(f"  worktree    = {inst.worktree_path}")
            print(f"  branch      = {inst.branch_name}")
            print(f"  events      = {ev_count}")
            print(f"  locks_left  = {lock_count}  (should be 0 — released by supervisor)")
    finally:
        db.close()

    # Cleanup worktree (and its branch).
    if inst is not None and inst.worktree_path:
        wt = Path(inst.worktree_path)
        if wt.exists():
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "worktree", "remove", "--force", str(wt),
                    cwd=str(REPO),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except Exception:
                pass
        if inst.branch_name:
            proc = await asyncio.create_subprocess_exec(
                "git", "branch", "-D", inst.branch_name,
                cwd=str(REPO),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
    if WORKTREES.exists():
        shutil.rmtree(WORKTREES, ignore_errors=True)

    return 0 if (inst is not None and inst.status.value == "completed") else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
