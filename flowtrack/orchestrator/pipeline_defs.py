"""Single source of truth for pipeline vocabulary (KAN-42 consolidation).

Everything here used to be duplicated across modules and reviews kept
flagging the copies (spawner._STATUS_ROLE, tasks.py _STATUS_TO_AUTO_ROLE /
_ROLE_TO_STATUS, merger._GATE_REASONS vs spawner f-strings). Renaming a
role or a reason now happens in exactly one file.

With the specialization dispatch layer, a Role row may be a VARIANT
(base_role_name set). Pipeline semantics — verdict tables, session types,
stage hooks, status maps — always key on the base name; use base_name().
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flowtrack.models.task import TaskStatus

if TYPE_CHECKING:
    from flowtrack.models.role import Role

# Which base role works a task at a given status (rework routing, manual
# assign) and the inverse (status applied when a role is assigned by hand).
STATUS_ROLE: dict[TaskStatus, str] = {
    TaskStatus.IN_PROGRESS: "dev",
    TaskStatus.IN_REVIEW: "reviewer",
    TaskStatus.IN_QA: "qa",
}
ROLE_STATUS: dict[str, TaskStatus] = {v: k for k, v in STATUS_ROLE.items()}


def base_name(role: "Role") -> str:
    """Semantic identity of a role: the base name for variants."""
    return role.base_role_name or role.name


def stage_completed_reason(role_name: str) -> str:
    """TaskTransition.reason recorded when a stage completes successfully.
    The merge gate matches on these exact strings — never format inline."""
    return f"pipeline: {role_name} completed"
