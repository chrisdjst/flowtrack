"""DevOps agent: pickup of infra-blocked tasks + RCA context building.

Trigger surface: tasks in status=blocked with blocked_reason=infra_failure —
today that bucket is fed by QA's BLOCKED verdict; future sources (CI failure
webhooks, deployment failures) plug in by blocking tasks with the same
reason, and this sweep picks them up unchanged.

The RCA-fix-verify loop itself runs inside the spawned devops instance,
bounded by the role's max_turns/max_minutes. Outcome handling lives in the
spawner's verdict table: RESOLVED -> seeded chain (qa re-verifies, clearing
blocked_reason); BLOCKED -> blocked/manual_intervention.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.models import Instance, Job, Role, Task, TaskTransition
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.job import JobStatus
from flowtrack.models.task import BlockedReason, TaskStatus

log = logging.getLogger(__name__)

# Unblocking infra sits above rework (50) and admission (200+) in claim
# order — an infra failure is stalling in-flight work.
PICKUP_PRIORITY = 40

_LIVE_INSTANCE_STATUSES = (InstanceStatus.SPAWNING, InstanceStatus.RUNNING)
_OPEN_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.CLAIMED)


def _rca_context(db: Session, task: Task) -> str:
    """Assemble what the devops agent needs to start the RCA: which stage
    reported the infra failure and what it said."""
    from flowtrack.orchestrator.spawner import _rejection_feedback

    blocking = db.scalar(
        select(TaskTransition)
        .where(TaskTransition.task_id == task.id)
        .where(TaskTransition.to_status == TaskStatus.BLOCKED.value)
        .order_by(TaskTransition.transitioned_at.desc())
        .limit(1)
    )
    header = (
        "## Infra failure pickup\n"
        "This task is blocked with blocked_reason=infra_failure. Diagnose and "
        "fix the environment/infra problem (root cause, not symptom), then "
        "state RESOLVED — or BLOCKED if it needs a human.\n"
        f"Blocking transition: {blocking.reason if blocking else '(unknown)'}\n"
    )
    if blocking is not None and blocking.instance_id is not None:
        return header + "\n" + _rejection_feedback(
            db, instance_id=blocking.instance_id,
            role_name="qa", keyword="BLOCKED",
        )
    return header + "\n(see task comments for details)"


def pickup_infra_failures(
    db: Session, *, limit: int, worker_id: str | None = None
) -> list[Task]:
    """Enqueue devops jobs for infra-blocked tasks. Caller commits.

    Oldest task first (FIFO) — there's no scoring dimension like the PO's;
    every entry means "the pipeline is stuck here".
    """
    if limit <= 0:
        return []
    devops_role = db.scalar(select(Role).where(Role.name == "devops"))
    if devops_role is None:
        log.warning("devops pickup: no 'devops' role found — nothing picked up")
        return []

    busy_job_task_ids = set(db.scalars(
        select(Job.task_id).where(Job.status.in_(_OPEN_JOB_STATUSES))
    ))
    live_instance_task_ids = set(db.scalars(
        select(Instance.task_id).where(Instance.status.in_(_LIVE_INSTANCE_STATUSES))
    ))
    skip = busy_job_task_ids | live_instance_task_ids

    candidates = db.scalars(
        select(Task)
        .where(Task.status == TaskStatus.BLOCKED)
        .where(Task.blocked_reason == BlockedReason.INFRA_FAILURE)
        .order_by(Task.created_at.asc())
    )

    picked: list[Task] = []
    for task in candidates:
        if task.id in skip:
            continue
        db.add(Job(
            task_id=task.id,
            role_id=devops_role.id,
            priority=PICKUP_PRIORITY,
            worker_id=worker_id,
            payload_json={
                "picked_up_by": "devops",
                "resume_context": _rca_context(db, task),
            },
        ))
        log.info("devops pickup: task %s (%r) -> devops job", task.id, task.title[:40])
        picked.append(task)
        if len(picked) >= limit:
            break
    return picked


async def run_devops_pickup(stop) -> None:
    """Background sweep (same pattern as PO admission): when
    devops_auto_pickup is on, pick up infra-blocked tasks each interval."""
    import asyncio

    from flowtrack.api.events import broker
    from flowtrack.core.runtime_config import RuntimeConfig

    log.info("devops pickup loop started")
    while not stop.is_set():
        try:
            if RuntimeConfig.get("devops_auto_pickup"):
                picked = await asyncio.to_thread(_pickup_once)
                for task_id, title in picked:
                    broker.publish_sync("devops_task_picked_up", {
                        "task_id": task_id,
                        "title": title,
                    })
        except Exception:
            log.exception("devops pickup sweep failed; continuing")
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=RuntimeConfig.get("devops_pickup_interval_seconds"),
            )
        except asyncio.TimeoutError:
            pass
    log.info("devops pickup loop stopped")


def _pickup_once() -> list[tuple[str, str]]:
    from flowtrack.core.database import SessionLocal
    from flowtrack.core.runtime_config import RuntimeConfig

    db = SessionLocal()
    try:
        picked = pickup_infra_failures(
            db, limit=RuntimeConfig.get("devops_pickup_batch")
        )
        # Snapshot before the session closes (detached-instance gotcha).
        summary = [(str(t.id), t.title) for t in picked]
        db.commit()
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
