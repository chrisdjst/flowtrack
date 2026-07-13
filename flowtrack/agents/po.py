"""PO agent: deterministic priority ordering + admission of ready tasks.

Design note: the brief defines the PO as a *pure sort* over task rows
(jira_priority, age_days, is_urgent, is_overdue) with no verdict states —
so unlike the PM agent this is plain code, not an LLM call. Deterministic,
free, and instant; the scoring weights are the "prompt".

Admission enqueues dev jobs with priority 200+rank — deliberately BELOW the
pipeline chain (100) and rework (50) in claim order (lower number claims
first), so in-flight tasks always finish before new work is started.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.models import Instance, Job, Role, Task
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.job import JobStatus
from flowtrack.models.task import TaskPriority

log = logging.getLogger(__name__)

# Claim-order base for admitted jobs (see module docstring).
ADMISSION_BASE_PRIORITY = 200

_PRIORITY_WEIGHT: dict[TaskPriority, int] = {
    TaskPriority.URGENT: 1000,
    TaskPriority.HIGH: 300,
    TaskPriority.MEDIUM: 100,
    TaskPriority.LOW: 30,
}

# Raw Jira priority names, additive on top of the task's own priority
# (which TPM already derives from severity).
_JIRA_PRIORITY_BONUS: dict[str, int] = {
    "highest": 400,
    "high": 200,
    "medium": 0,
    "low": -20,
    "lowest": -50,
}

_URGENT_BONUS = 500
_OVERDUE_BONUS = 250
_AGE_BONUS_PER_DAY = 10
_AGE_BONUS_CAP = 300

_LIVE_INSTANCE_STATUSES = (InstanceStatus.SPAWNING, InstanceStatus.RUNNING)
_OPEN_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.CLAIMED)


@dataclass(slots=True, frozen=True)
class RankedTask:
    task_id: UUID
    title: str
    score: int
    factors: dict[str, int]
    # Which role admission should enqueue: "design" for pipeline_routing=
    # design_dev tasks that don't have a ui_spec yet, "dev" otherwise.
    entry_role: str = "dev"


def score_task(task: Task, *, now: datetime | None = None) -> tuple[int, dict[str, int]]:
    """Score one task. Returns (score, factor breakdown) — the breakdown is
    what `flowtrack po rank` shows so the ordering is explainable."""
    now = now or datetime.now(tz=timezone.utc)

    factors: dict[str, int] = {
        "priority": _PRIORITY_WEIGHT.get(task.priority, 100),
        "urgent": _URGENT_BONUS if task.is_urgent else 0,
        "overdue": _OVERDUE_BONUS if task.is_overdue else 0,
    }

    created = task.created_at
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = max(0, (now - created).days)
        factors["age"] = min(age_days * _AGE_BONUS_PER_DAY, _AGE_BONUS_CAP)
    else:
        factors["age"] = 0

    if task.jira_priority:
        factors["jira"] = _JIRA_PRIORITY_BONUS.get(task.jira_priority.strip().lower(), 0)
    else:
        factors["jira"] = 0

    return sum(factors.values()), factors


def rank_ready_tasks(db: Session, *, now: datetime | None = None) -> list[RankedTask]:
    """Ready tasks ordered by score desc (ties: oldest first).

    Ready = status TODO with acceptance_criteria filled (the dev gate — see
    models/task.py) and not already admitted: no queued/claimed job and no
    live instance.
    """
    from flowtrack.models.task import TaskStatus

    busy_job_task_ids = set(db.scalars(
        select(Job.task_id).where(Job.status.in_(_OPEN_JOB_STATUSES))
    ))
    live_instance_task_ids = set(db.scalars(
        select(Instance.task_id).where(Instance.status.in_(_LIVE_INSTANCE_STATUSES))
    ))
    skip = busy_job_task_ids | live_instance_task_ids

    candidates = db.scalars(
        select(Task)
        .where(Task.status == TaskStatus.TODO)
        .where(Task.acceptance_criteria.isnot(None))
        .order_by(Task.created_at.asc())
    )

    from flowtrack.models.task import PipelineRouting

    ranked: list[RankedTask] = []
    for t in candidates:
        if t.id in skip:
            continue
        score, factors = score_task(t, now=now)
        needs_design = (
            t.pipeline_routing == PipelineRouting.DESIGN_DEV and t.ui_spec is None
        )
        ranked.append(RankedTask(
            task_id=t.id, title=t.title, score=score, factors=factors,
            entry_role="design" if needs_design else "dev",
        ))
    ranked.sort(key=lambda r: r.score, reverse=True)  # stable: ties stay oldest-first
    return ranked


def admit_ready_tasks(
    db: Session, *, limit: int, worker_id: str | None = None
) -> list[RankedTask]:
    """Enqueue dev jobs for the top `limit` ready tasks, in rank order.

    Job.priority encodes the rank (ADMISSION_BASE_PRIORITY + index) so the
    claim query's priority-asc ordering serves the PO's decision. Caller
    commits. Returns the admitted subset.
    """
    if limit <= 0:
        return []
    role_ids = {
        r.name: r.id
        for r in db.scalars(select(Role).where(Role.name.in_(("dev", "design"))))
    }
    if "dev" not in role_ids:
        log.warning("po admission: no 'dev' role found — nothing admitted")
        return []

    admitted = rank_ready_tasks(db)[:limit]
    for idx, ranked in enumerate(admitted):
        # design_dev tasks enter at the design stage; if the design role is
        # missing, degrade to dev rather than stranding the task.
        role_id = role_ids.get(ranked.entry_role) or role_ids["dev"]
        db.add(Job(
            task_id=ranked.task_id,
            role_id=role_id,
            priority=ADMISSION_BASE_PRIORITY + idx,
            worker_id=worker_id,
            payload_json={"admitted_by": "po", "score": ranked.score},
        ))
        log.info(
            "po admission: task %s (%r) score=%d -> %s job (priority=%d)",
            ranked.task_id, ranked.title[:40], ranked.score,
            ranked.entry_role, ADMISSION_BASE_PRIORITY + idx,
        )
    return admitted


async def run_po_admission(stop) -> None:
    """Background sweep (same pattern as the discovery manager): when
    po_auto_admit is on, admit up to po_admission_batch ready tasks per
    interval. Off by default — flip it in Settings once trusted."""
    import asyncio

    from flowtrack.api.events import broker
    from flowtrack.core.runtime_config import RuntimeConfig

    log.info("po admission loop started")
    while not stop.is_set():
        try:
            if RuntimeConfig.get("po_auto_admit"):
                admitted = await asyncio.to_thread(_admit_once)
                for r in admitted:
                    broker.publish_sync("po_task_admitted", {
                        "task_id": str(r.task_id),
                        "title": r.title,
                        "score": r.score,
                    })
        except Exception:
            log.exception("po admission sweep failed; continuing")
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=RuntimeConfig.get("po_admission_interval_seconds"),
            )
        except asyncio.TimeoutError:
            pass
    log.info("po admission loop stopped")


def _admit_once() -> list[RankedTask]:
    from flowtrack.core.database import SessionLocal
    from flowtrack.core.runtime_config import RuntimeConfig

    db = SessionLocal()
    try:
        admitted = admit_ready_tasks(db, limit=RuntimeConfig.get("po_admission_batch"))
        db.commit()
        return admitted
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
