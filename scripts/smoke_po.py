"""Smoke for the PO agent (deterministic ranking + admission).

Pure DB test — no orchestrator, no Claude, $0.

Seeds five tasks in a recognizable pattern and asserts:
  1. rank_ready_tasks orders by score: urgent+overdue > urgent > high > medium,
     and excludes (a) tasks without acceptance criteria, (b) tasks that
     already have a queued job.
  2. admit_ready_tasks(limit=2) enqueues dev jobs for the top 2 only, with
     ascending Job.priority starting at ADMISSION_BASE_PRIORITY.
  3. A second admit sweep does not double-admit (idempotence): the two
     admitted tasks now have open jobs, so the next batch picks the rest.
"""

from __future__ import annotations

import os
import sys
import uuid as _uuid

os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)

from sqlalchemy import delete, select  # noqa: E402

from flowtrack.agents.po import (  # noqa: E402
    ADMISSION_BASE_PRIORITY,
    admit_ready_tasks,
    rank_ready_tasks,
)
from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.models import Job, Role, Task  # noqa: E402
from flowtrack.models.task import TaskPriority, TaskStatus  # noqa: E402

_RUN = _uuid.uuid4().hex[:6]


def _title(tag: str) -> str:
    return f"po-smoke {tag} {_RUN}"


def main() -> int:
    db = SessionLocal()
    created_ids: list = []
    ok = True
    try:
        dev_role_id = db.scalar(select(Role.id).where(Role.name == "dev"))
        if dev_role_id is None:
            print("no dev role — run migrations/seeds first")
            return 2

        def add_task(tag: str, *, priority=TaskPriority.MEDIUM, urgent=False,
                     overdue=False, criteria="1. ok", status=TaskStatus.TODO) -> Task:
            t = Task(
                title=_title(tag), status=status, priority=priority,
                is_urgent=urgent, is_overdue=overdue,
                acceptance_criteria=criteria,
            )
            db.add(t)
            db.flush()
            created_ids.append(t.id)
            return t

        t_top = add_task("urgent+overdue", priority=TaskPriority.URGENT, urgent=True, overdue=True)
        t_urgent = add_task("urgent", priority=TaskPriority.URGENT)
        t_high = add_task("high", priority=TaskPriority.HIGH)
        t_med = add_task("medium")
        t_nocrit = add_task("no-criteria", priority=TaskPriority.URGENT, criteria=None)
        t_busy = add_task("already-queued", priority=TaskPriority.URGENT, urgent=True)
        db.add(Job(task_id=t_busy.id, role_id=dev_role_id, priority=10))
        db.commit()

        # --- 1. ranking ---
        ranked = rank_ready_tasks(db)
        ours = [r for r in ranked if r.task_id in set(created_ids)]
        order = [r.task_id for r in ours]
        print("rank order (ours):")
        for r in ours:
            print(f"  {str(r.task_id)[:8]} score={r.score} {r.title}")

        rank_ok = (
            order[:4] == [t_top.id, t_urgent.id, t_high.id, t_med.id]
            and t_nocrit.id not in order
            and t_busy.id not in order
        )
        print(f"rank_ok = {rank_ok}")
        ok = ok and rank_ok

        # --- 2. admission (limit=2) ---
        # Isolate to our fixtures: the dev DB may hold other ready tasks that
        # would outrank ours; park them as blocked for the duration.
        parked = list(db.scalars(
            select(Task).where(
                Task.status == TaskStatus.TODO,
                Task.acceptance_criteria.isnot(None),
                Task.id.notin_(created_ids),
            )
        ))
        for p in parked:
            p.status = TaskStatus.BLOCKED
        db.flush()

        try:
            admitted = admit_ready_tasks(db, limit=2)
            db.commit()
            admitted_ids = [r.task_id for r in admitted]
            jobs = list(db.scalars(
                select(Job).where(Job.task_id.in_(created_ids), Job.priority >= ADMISSION_BASE_PRIORITY)
                .order_by(Job.priority)
            ))
            print(f"admitted: {[str(i)[:8] for i in admitted_ids]}")
            print(f"admission jobs: {[(str(j.task_id)[:8], j.priority) for j in jobs]}")
            admit_ok = (
                admitted_ids == [t_top.id, t_urgent.id]
                and [j.task_id for j in jobs] == [t_top.id, t_urgent.id]
                and [j.priority for j in jobs] == [ADMISSION_BASE_PRIORITY, ADMISSION_BASE_PRIORITY + 1]
                and all(j.role_id == dev_role_id for j in jobs)
                and all((j.payload_json or {}).get("admitted_by") == "po" for j in jobs)
            )
            print(f"admit_ok = {admit_ok}")
            ok = ok and admit_ok

            # --- 3. idempotence: next sweep skips the two just admitted ---
            admitted2 = admit_ready_tasks(db, limit=10)
            db.commit()
            admitted2_ids = [r.task_id for r in admitted2 if r.task_id in set(created_ids)]
            print(f"second sweep admitted: {[str(i)[:8] for i in admitted2_ids]}")
            idem_ok = admitted2_ids == [t_high.id, t_med.id]
            print(f"idem_ok = {idem_ok}")
            ok = ok and idem_ok
        finally:
            # restore parked tasks even if an assertion path blew up
            for p in parked:
                p.status = TaskStatus.TODO
            db.commit()
    finally:
        # cleanup fixtures
        try:
            db.execute(delete(Job).where(Job.task_id.in_(created_ids)))
            db.execute(delete(Task).where(Task.id.in_(created_ids)))
            db.commit()
        except Exception:
            db.rollback()
        db.close()

    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
