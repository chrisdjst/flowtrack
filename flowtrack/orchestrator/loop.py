"""Orchestrator main loop.

Each tick:
  1. Reap finished supervisor tasks from the active set.
  2. If under the concurrency cap, try to claim one queued job.
  3. Dry-run mode → cancel the claimed job with reason 'dry-run' and continue.
  4. Real mode → create an Instance row, spawn a supervisor asyncio task that
     owns the full lifecycle (worktree, locks, subprocess, stream parsing,
     teardown). Loop returns to step 1 immediately so it can claim more.

On shutdown the loop signals stop, then waits up to 30s for in-flight
supervisors to finish; remaining tasks are cancelled.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy.orm import Session

from flowtrack.core.database import SessionLocal
from flowtrack.core.runtime_config import RuntimeConfig
from flowtrack.models import Instance, Job
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.job import JobStatus
from flowtrack.orchestrator import budget
from flowtrack.orchestrator.identity import worker_id
from flowtrack.orchestrator.queue import claim_next_job, queue_depth, release_job
from flowtrack.orchestrator.spawner import supervise

log = logging.getLogger(__name__)


async def run_orchestrator(stop: asyncio.Event) -> None:
    """Top-level loop. Returns when ``stop`` is set."""
    log.info("orchestrator loop started")
    tick_no = 0
    active: set[asyncio.Task] = set()

    while not stop.is_set():
        tick_no += 1
        active = {t for t in active if not t.done()}
        interval = RuntimeConfig.get("orchestrator_loop_interval_seconds")
        heartbeat_every = max(1, int(30 / interval))

        if len(active) < RuntimeConfig.get("max_concurrent_instances"):
            try:
                dispatch = await asyncio.to_thread(_claim_one, tick_no, heartbeat_every)
            except Exception:
                log.exception("orchestrator tick %d failed during claim", tick_no)
                dispatch = None
            if dispatch is not None:
                job_id, instance_id = dispatch
                t = asyncio.create_task(
                    supervise(job_id, instance_id),
                    name=f"supervise-{instance_id}",
                )
                active.add(t)
                continue  # try to claim more immediately

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    log.info("orchestrator shutdown — waiting for %d in-flight supervisors", len(active))
    if active:
        done, pending = await asyncio.wait(active, timeout=30)
        for t in pending:
            log.warning("cancelling supervisor %s (shutdown timeout)", t.get_name())
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    log.info("orchestrator stopped after %d ticks", tick_no)


def _claim_one(tick_no: int, heartbeat_every: int) -> tuple[UUID, UUID] | None:
    """Atomic: claim next job + create Instance row (real mode) or cancel (dry-run).

    Returns (job_id, instance_id) when the caller should spawn a supervisor;
    None when nothing to do.
    """
    with _db() as db:
        if tick_no % heartbeat_every == 0:
            depth = queue_depth(db)
            log.info("tick %d alive; queue_depth=%d", tick_no, depth)

        # Budget circuit breaker: don't even claim if we're over cap. Existing
        # in-flight supervisors keep running; only NEW spawns are gated.
        blocked, reason = budget.is_blocked(db)
        if blocked:
            if tick_no % heartbeat_every == 0:  # don't spam the log
                log.warning("budget gate: %s — skipping spawn", reason)
            return None

        job = claim_next_job(db)
        if job is None:
            return None

        if RuntimeConfig.get("orchestrator_dry_run"):
            log.info(
                "DRY RUN: would spawn role_id=%s for task_id=%s (job=%s)",
                job.role_id, job.task_id, job.id,
            )
            release_job(db, job, final_status=JobStatus.CANCELLED, error="dry-run")
            return None

        instance = Instance(
            role_id=job.role_id,
            task_id=job.task_id,
            status=InstanceStatus.SPAWNING,
            worker_id=worker_id(),
        )
        db.add(instance)
        db.flush()
        job.claimed_by = instance.id
        log.info("claimed job %s -> instance %s (worker=%s)", job.id, instance.id, worker_id())
        return job.id, instance.id


class _db:
    def __enter__(self) -> Session:
        self._db = SessionLocal()
        return self._db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._db.commit()
            else:
                self._db.rollback()
        finally:
            self._db.close()
