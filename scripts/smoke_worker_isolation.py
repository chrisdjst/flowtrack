"""Smoke test for worker_id isolation (alembic 007).

Scenario:
  - Insert 3 jobs:
      job_A    -> worker_id='lane-A'
      job_B    -> worker_id='lane-B'
      job_null -> worker_id=NULL
  - Run claim_next_job as if we were the lane-A daemon, three times.
  - Expect to claim job_A and job_null (in priority order) but NEVER job_B.

This proves the regression that bit us during pipeline smoke (a zombie
daemon stole the reviewer job) cannot repeat once smokes opt into a lane.
"""

from __future__ import annotations

import os
import sys
import uuid as _uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)
os.environ["FLOWTRACK_WORKER_ID"] = f"smoke-iso-{_uuid.uuid4().hex[:8]}"

# claim_next_job reads worker_id() — pin it.
LANE_A = os.environ["FLOWTRACK_WORKER_ID"]
LANE_B = f"smoke-iso-other-{_uuid.uuid4().hex[:8]}"

from sqlalchemy import select  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.models import Job, Role, Task  # noqa: E402
from flowtrack.models.job import JobStatus  # noqa: E402
from flowtrack.models.task import TaskPriority, TaskStatus  # noqa: E402
from flowtrack.orchestrator.queue import claim_next_job  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        dev = db.scalar(select(Role).where(Role.name == "dev"))
        if dev is None:
            print("ERROR: dev role missing")
            return 1

        # Seed a task per lane to keep things readable.
        def make(label: str, worker: str | None, priority: int) -> _uuid.UUID:
            t = Task(
                title=f"iso-{label}",
                status=TaskStatus.TODO,
                priority=TaskPriority.HIGH,
                ticket_id=f"ISO-{label.upper()}",
            )
            db.add(t)
            db.flush()
            j = Job(task_id=t.id, role_id=dev.id, priority=priority, worker_id=worker)
            db.add(j)
            db.flush()
            return j.id

        j_a = make("a", LANE_A, priority=20)
        j_b = make("b", LANE_B, priority=10)  # higher priority but wrong lane
        j_null = make("null", None, priority=30)
        db.commit()
        print(f"seeded: A={j_a} B={j_b} (lane {LANE_B}) NULL={j_null}")
    finally:
        db.close()

    # Claim as lane-A three times.
    claimed: list[_uuid.UUID] = []
    for _ in range(3):
        db = SessionLocal()
        try:
            job = claim_next_job(db)
            if job is None:
                db.commit()
                break
            claimed.append(job.id)
            # release so we don't leave them CLAIMED for future test runs
            job.status = JobStatus.CANCELLED
            job.last_error = "iso-smoke"
            db.commit()
        finally:
            db.close()

    print(f"claimed by lane A in order: {claimed}")
    print(f"j_b ({j_b}) should NOT appear above")

    ok = j_b not in claimed and set(claimed) == {j_a, j_null}
    print()
    print("RESULT:", "PASS" if ok else "FAIL")

    # Cleanup: cancel the j_b too so it doesn't pollute future runs.
    db = SessionLocal()
    try:
        job_b = db.get(Job, j_b)
        if job_b and job_b.status == JobStatus.QUEUED:
            job_b.status = JobStatus.CANCELLED
            job_b.last_error = "iso-smoke teardown"
            db.commit()
    finally:
        db.close()

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
