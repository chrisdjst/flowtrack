"""Smoke test for the watchdog sweeper.

Inserts:
  - one Instance with status=running and last_heartbeat_at=10 hours ago
  - one ResourceLock expired 1 hour ago
  - one in-flight Job claimed by that instance

Runs a single sweep, then asserts:
  - the stale instance is now KILLED with finished_at set
  - its lock was deleted (either via release_all OR sweep_expired)
  - the job is FAILED with error mentioning watchdog
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)

from sqlalchemy import select  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.models import (  # noqa: E402
    Instance,
    Job,
    ResourceLock,
    Role,
    Task,
)
from flowtrack.models.instance import InstanceStatus  # noqa: E402
from flowtrack.models.job import JobStatus  # noqa: E402
from flowtrack.models.task import TaskPriority, TaskStatus  # noqa: E402
from flowtrack.orchestrator.watchdog import _sweep_once  # noqa: E402


async def main() -> int:
    db = SessionLocal()
    try:
        dev_role = db.scalar(select(Role).where(Role.name == "dev"))
        if dev_role is None:
            print("ERROR: dev role missing — run alembic upgrade head")
            return 1

        task = Task(
            title="Watchdog target",
            description="Watchdog must reclaim this task.",
            status=TaskStatus.IN_PROGRESS,
            priority=TaskPriority.HIGH,
            ticket_id="SMOKE-WD",
            module_hint="smoke-wd",
        )
        db.add(task)
        db.flush()

        ten_hours_ago = datetime.now(tz=timezone.utc) - timedelta(hours=10)
        inst = Instance(
            id=uuid4(),
            role_id=dev_role.id,
            task_id=task.id,
            status=InstanceStatus.RUNNING,
            last_heartbeat_at=ten_hours_ago,
            spawned_at=ten_hours_ago,
        )
        db.add(inst)
        db.flush()

        lock = ResourceLock(
            resource_key=f"smoke-wd-{inst.id}",
            instance_id=inst.id,
            expires_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        )
        db.add(lock)

        job = Job(
            task_id=task.id,
            role_id=dev_role.id,
            status=JobStatus.CLAIMED,
            attempts=1,
            claimed_at=ten_hours_ago,
            claimed_by=inst.id,
            priority=10,
        )
        db.add(job)
        db.commit()

        instance_id, job_id = inst.id, job.id
        print(f"seeded: instance={instance_id} job={job_id} lock={lock.resource_key}")
    finally:
        db.close()

    # Sweep.
    await asyncio.to_thread(_sweep_once)

    # Inspect.
    db = SessionLocal()
    try:
        inst = db.get(Instance, instance_id)
        job = db.get(Job, job_id)
        lock_left = db.scalar(
            select(ResourceLock).where(ResourceLock.instance_id == instance_id)
        )

        print()
        print(f"instance status      = {inst.status.value}  (expect 'killed')")
        print(f"instance finished_at = {inst.finished_at}  (expect not-None)")
        print(f"job status           = {job.status.value}  (expect 'failed')")
        print(f"job last_error       = {job.last_error}")
        print(f"lock remaining       = {lock_left}  (expect None)")

        ok = (
            inst.status == InstanceStatus.KILLED
            and inst.finished_at is not None
            and job.status == JobStatus.FAILED
            and (job.last_error or "").startswith("watchdog")
            and lock_left is None
        )
        print()
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
