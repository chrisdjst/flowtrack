"""Job queue claim/release using Postgres SELECT FOR UPDATE SKIP LOCKED.

This is the only piece of the orchestrator that *must* be careful about
concurrency. Everything else can be added later; if claim/release is wrong,
two instances pick up the same job and the whole pipeline gets corrupted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.models import Job
from flowtrack.models.job import JobStatus


def claim_next_job(db: Session) -> Job | None:
    """Atomically claim the next queued job.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers see disjoint rows.
    Returns None when the queue is empty.

    Caller is responsible for ``db.commit()`` (or ``release_job`` which doesn't
    commit either — both expect a session-per-tick pattern).
    """
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.QUEUED)
        .order_by(Job.priority.asc(), Job.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.scalar(stmt)
    if job is None:
        return None
    job.status = JobStatus.CLAIMED
    job.claimed_at = datetime.now(tz=timezone.utc)
    job.attempts += 1
    return job


def release_job(
    db: Session,
    job: Job,
    *,
    final_status: JobStatus,
    error: str | None = None,
    instance_id: UUID | None = None,
) -> None:
    """Mark the job as terminal (done/failed/cancelled) and optionally attach instance."""
    job.status = final_status
    job.finished_at = datetime.now(tz=timezone.utc)
    if error is not None:
        job.last_error = error
    if instance_id is not None:
        job.claimed_by = instance_id


def queue_depth(db: Session) -> int:
    """Count of queued (not yet claimed) jobs. Cheap — used for the heartbeat log."""
    from sqlalchemy import func

    return db.scalar(select(func.count(Job.id)).where(Job.status == JobStatus.QUEUED)) or 0
