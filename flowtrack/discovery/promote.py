"""Shared logic for turning a DiscoveredItem into a Task.

Reused by:
  - POST /api/discovery/{id}/promote (manual button click)
  - manager._auto_refine (when auto_refine_discovered=true)

Keeps both paths in sync — if we change what 'promote' means (e.g., also
auto-enqueue a dev job once acceptance_criteria are filled), one place to
edit.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import String, select
from sqlalchemy.orm import Session

from flowtrack.models import DiscoveredItem, Task, TaskComment, TaskTransition
from flowtrack.models.discovered_item import DiscoveryStatus
from flowtrack.models.task import (
    BlockedReason,
    PipelineRouting,
    TaskPriority,
    TaskSeverity,
    TaskStatus,
)

log = logging.getLogger(__name__)

# TPM severity drives the task's priority (the PO agent's main sort input).
_SEVERITY_PRIORITY: dict[str, TaskPriority] = {
    "P0-Critical": TaskPriority.URGENT,
    "P1-High": TaskPriority.HIGH,
    "P2-Medium": TaskPriority.MEDIUM,
    "P3-Low": TaskPriority.LOW,
}


def open_tasks_snapshot(db: Session, *, limit: int = 30) -> list[tuple[str, str]]:
    """(id-prefix, title) of non-done tasks, newest first — the TPM's
    duplicate-detection context. Capped to keep the prompt compact."""
    rows = db.scalars(
        select(Task)
        .where(Task.status != TaskStatus.DONE)
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    return [(str(t.id)[:8], t.title) for t in rows]


def promote_item(
    db: Session,
    item: DiscoveredItem,
    *,
    acceptance_criteria: str | None = None,
    module_hint: str | None = None,
    severity: str | None = None,
    pipeline_routing: str | None = None,
    task_spec: str | None = None,
) -> Task:
    """Create a Task from a DiscoveredItem and flip the item to PROMOTED.

    Caller is responsible for the commit. Raises if the item is already
    resolved (promoted/rejected/duplicate).
    """
    if item.status != DiscoveryStatus.NEW:
        raise ValueError(f"item {item.id} already {item.status.value}")

    task = Task(
        title=item.title,
        description=item.summary,
        status=TaskStatus.TODO,
        priority=_SEVERITY_PRIORITY.get(severity, TaskPriority.MEDIUM),
        ticket_id=item.source_ref if item.source.value in ("jira", "github_issue") else None,
        discovered_from=item.id,
        acceptance_criteria=acceptance_criteria,
        module_hint=module_hint,
        severity=TaskSeverity(severity) if severity in _SEVERITY_PRIORITY else None,
        pipeline_routing=(
            PipelineRouting(pipeline_routing)
            if pipeline_routing in tuple(r.value for r in PipelineRouting) else None
        ),
        task_spec=task_spec,
    )
    db.add(task)
    db.flush()

    item.status = DiscoveryStatus.PROMOTED
    item.promoted_task_id = task.id
    log.info("promoted item %s -> task %s", item.id, task.id)
    return task


def escalate_critical_item(db: Session, item: DiscoveredItem) -> Task:
    """CRITICAL security finding: promote AND block in one step.

    Bypasses the TPM/human gate — a critical finding must be a visible,
    blocking task now, not an inbox row. The task lands in blocked/security
    at P0/urgent; a human triages and moves it into the pipeline (the human
    unblock paths already reset bounce/blocked metadata).

    Caller commits and publishes events (manager pattern).
    """
    task = promote_item(
        db, item,
        severity="P0-Critical",
        task_spec=item.summary,
    )
    from_status = task.status.value
    task.status = TaskStatus.BLOCKED
    task.blocked_reason = BlockedReason.SECURITY
    db.add(TaskTransition(
        task_id=task.id, from_status=from_status, to_status=TaskStatus.BLOCKED.value,
        reason="secops: CRITICAL finding",
    ))
    db.add(TaskComment(
        task_id=task.id,
        body=(
            "SecOps scan reported a CRITICAL finding — auto-promoted and "
            "blocked for immediate human triage.\n\n"
            f"{item.summary or item.title}"
        ),
    ))
    log.warning("secops: CRITICAL item %s escalated to blocked task %s", item.id, task.id)
    return task


def reject_item(db: Session, item: DiscoveredItem, *, reason: str | None = None) -> None:
    if item.status != DiscoveryStatus.NEW:
        raise ValueError(f"item {item.id} already {item.status.value}")
    item.status = DiscoveryStatus.REJECTED
    log.info("rejected item %s (reason=%s)", item.id, reason)


def duplicate_item(
    db: Session, item: DiscoveredItem, *, duplicate_of: str | None = None
) -> None:
    """Flip the item to DUPLICATE. duplicate_of is the id-prefix of the open
    task it duplicates (None when the TPM couldn't point at a listed one).

    When the prefix resolves to a task, promoted_task_id is set to it — the FK
    is null for duplicates otherwise, and persisting the link keeps repeat
    reports auditable (and countable as an urgency signal) without a new column.
    """
    if item.status != DiscoveryStatus.NEW:
        raise ValueError(f"item {item.id} already {item.status.value}")
    item.status = DiscoveryStatus.DUPLICATE
    if duplicate_of:
        target_id = db.scalar(
            select(Task.id).where(Task.id.cast(String).like(f"{duplicate_of}%"))
        )
        if target_id is not None:
            item.promoted_task_id = target_id
    log.info("marked item %s duplicate (of=%s)", item.id, duplicate_of)
