"""Background sweeper for stuck instances and expired locks.

Runs alongside the main orchestrator loop (separate asyncio task). Two jobs:

1. **Stale instances**: an instance whose ``last_heartbeat_at`` is older than
   ``role.max_minutes * 2`` (with a floor) is considered dead. We mark it as
   ``KILLED`` and release its locks. We do NOT try to ``proc.kill()`` — the
   spawner owns the process handle and will SIGKILL on its own timeout. The
   watchdog only cleans up DB state for instances whose supervisor process
   itself crashed without finalizing.

2. **Expired locks**: locks past ``expires_at`` are dropped. Normally
   ``release_all`` runs at finalize and locks never expire, but if a supervisor
   crashes we depend on TTL to free the resource.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.core.database import SessionLocal
from flowtrack.core.settings import settings
from flowtrack.models import Instance, Job, Role
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.job import JobStatus
from flowtrack.orchestrator import locks
from flowtrack.orchestrator.queue import release_job

log = logging.getLogger(__name__)

# How often to sweep, in seconds.
_SWEEP_INTERVAL = 30.0
# Floor on the heartbeat staleness threshold, regardless of role config.
_MIN_STALE_SECONDS = 120


async def run_watchdog(stop: asyncio.Event) -> None:
    """Top-level coroutine; returns when ``stop`` is set."""
    log.info("watchdog started (sweep every %.0fs)", _SWEEP_INTERVAL)
    while not stop.is_set():
        try:
            await asyncio.to_thread(_sweep_once)
        except Exception:
            log.exception("watchdog sweep crashed; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_INTERVAL)
        except asyncio.TimeoutError:
            pass
    log.info("watchdog stopped")


def _sweep_once() -> None:
    db: Session = SessionLocal()
    try:
        killed = _kill_stale_instances(db)
        recovered = _recover_orphaned_claims(db)
        expired = locks.sweep_expired(db)
        db.commit()
        if killed or expired or recovered:
            log.info(
                "watchdog sweep: killed=%d stale_instances, recovered=%d orphan_claims, "
                "expired=%d locks", killed, recovered, expired,
            )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _kill_stale_instances(db: Session) -> int:
    """Mark instances whose heartbeat is older than their role-derived threshold."""
    now = datetime.now(tz=timezone.utc)
    role_max_by_id = {r.id: r.max_minutes for r in db.scalars(select(Role))}

    live_statuses = (
        InstanceStatus.SPAWNING,
        InstanceStatus.RUNNING,
        InstanceStatus.WAITING_INPUT,
    )
    candidates = list(db.scalars(
        select(Instance).where(Instance.status.in_(live_statuses))
    ))

    killed = 0
    for inst in candidates:
        # No heartbeat yet (still spawning) — give it the floor grace period.
        if inst.last_heartbeat_at is None:
            stale_after = inst.spawned_at + timedelta(seconds=_MIN_STALE_SECONDS)
        else:
            role_minutes = role_max_by_id.get(inst.role_id, settings.lock_default_ttl_minutes)
            grace = max(role_minutes * 2 * 60, _MIN_STALE_SECONDS)
            stale_after = inst.last_heartbeat_at + timedelta(seconds=grace)

        if now < stale_after:
            continue

        log.warning(
            "watchdog: killing stale instance %s (last_heartbeat=%s spawned=%s)",
            inst.id, inst.last_heartbeat_at, inst.spawned_at,
        )
        inst.status = InstanceStatus.KILLED
        inst.finished_at = now
        locks.release_all(db, instance_id=inst.id)

        # Best-effort: fail any job still attached to this instance.
        for job in db.scalars(
            select(Job).where(
                Job.claimed_by == inst.id,
                Job.status.in_((JobStatus.CLAIMED, JobStatus.RUNNING)),
            )
        ):
            release_job(
                db, job, final_status=JobStatus.FAILED,
                error="watchdog: stale heartbeat",
                instance_id=inst.id,
            )
        killed += 1
    return killed


def _recover_orphaned_claims(db: Session) -> int:
    """Recover jobs claimed but whose supervisor never reached _finalize.

    Two cases this covers:
      1. ``_claim_one`` crashed between job.status='claimed' and instance.add().
         Job has claimed_by=NULL but claimed_at is set.
      2. Instance died without _finalize and watchdog already killed it; the
         instance is now terminal but the job was somehow not released.

    Threshold: claimed_at older than ``_MIN_STALE_SECONDS``. If the supervisor
    hasn't progressed within that window, something went wrong.

    Recovery policy: if ``job.attempts < job.max_attempts``, requeue (status
    back to QUEUED, clear claimed_*); otherwise fail with a clear reason.
    Transient supervisor crashes (process killed, host reboot) shouldn't
    permanently consume a Job slot.
    """
    threshold = datetime.now(tz=timezone.utc) - timedelta(seconds=_MIN_STALE_SECONDS)
    candidates = list(db.scalars(
        select(Job).where(
            Job.status.in_((JobStatus.CLAIMED, JobStatus.RUNNING)),
            Job.claimed_at.isnot(None),
            Job.claimed_at < threshold,
        )
    ))
    recovered = 0
    for job in candidates:
        # If there is an instance and it's still live, leave it alone — the
        # instance sweep will handle it on this or a later tick.
        if job.claimed_by is not None:
            inst = db.get(Instance, job.claimed_by)
            if inst is not None and inst.status in (
                InstanceStatus.SPAWNING,
                InstanceStatus.RUNNING,
                InstanceStatus.WAITING_INPUT,
            ):
                continue

        if job.attempts < job.max_attempts:
            log.warning(
                "watchdog: requeueing orphaned job %s (attempt %d/%d, claimed_at=%s)",
                job.id, job.attempts, job.max_attempts, job.claimed_at,
            )
            job.status = JobStatus.QUEUED
            job.claimed_at = None
            job.claimed_by = None
            job.last_error = f"watchdog: requeued after orphaned attempt {job.attempts}"
        else:
            log.warning(
                "watchdog: failing orphaned job %s (attempts exhausted %d/%d)",
                job.id, job.attempts, job.max_attempts,
            )
            release_job(
                db, job, final_status=JobStatus.FAILED,
                error=f"watchdog: orphaned claim, attempts exhausted ({job.attempts})",
                instance_id=None,
            )
        recovered += 1
    return recovered
