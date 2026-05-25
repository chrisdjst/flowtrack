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

from sqlalchemy.orm import Session

from flowtrack.models import DiscoveredItem, Task
from flowtrack.models.discovered_item import DiscoveryStatus
from flowtrack.models.task import TaskPriority, TaskStatus

log = logging.getLogger(__name__)


def promote_item(
    db: Session,
    item: DiscoveredItem,
    *,
    acceptance_criteria: str | None = None,
    module_hint: str | None = None,
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
        priority=TaskPriority.MEDIUM,
        ticket_id=item.source_ref if item.source.value in ("jira", "github_issue") else None,
        discovered_from=item.id,
        acceptance_criteria=acceptance_criteria,
        module_hint=module_hint,
    )
    db.add(task)
    db.flush()

    item.status = DiscoveryStatus.PROMOTED
    item.promoted_task_id = task.id
    log.info("promoted item %s -> task %s", item.id, task.id)
    return task


def reject_item(db: Session, item: DiscoveredItem, *, reason: str | None = None) -> None:
    if item.status != DiscoveryStatus.NEW:
        raise ValueError(f"item {item.id} already {item.status.value}")
    item.status = DiscoveryStatus.REJECTED
    log.info("rejected item %s (reason=%s)", item.id, reason)
