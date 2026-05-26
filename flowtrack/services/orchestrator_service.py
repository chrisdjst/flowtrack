"""Service helpers that bridge the CLI to orchestrator state.

CLI is the primary interface (`flowtrack task assign`, `flowtrack task show`,
`flowtrack discovery promote`, ...). The kanban frontend is read-only
visualization. Both call into the same domain via services — this module
groups the lookups + mutations that span the orchestrator tables (Instance,
Job, TaskTransition).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from flowtrack.core.exceptions import FlowTrackError
from flowtrack.models import Instance, Job, Role, Task, TaskTransition
from flowtrack.models.instance import InstanceStatus


_LIVE_INSTANCE_STATUSES = (
    InstanceStatus.SPAWNING,
    InstanceStatus.RUNNING,
    InstanceStatus.WAITING_INPUT,
)


class RoleNotFoundError(FlowTrackError):
    def __init__(self, role_name: str) -> None:
        super().__init__(f"Role '{role_name}' not found.")


class OrchestratorService:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    # ----- assign -----

    def assign(
        self,
        task_id: uuid.UUID,
        role_name: str,
        *,
        priority: int = 100,
        worker_id: str | None = None,
    ) -> Job:
        """Enqueue a Job binding (task, role). Raises if the role doesn't exist.

        Returns the new Job (status=queued). The orchestrator loop will pick
        it up on its next tick.
        """
        role = self.db.scalar(select(Role).where(Role.name == role_name))
        if role is None:
            raise RoleNotFoundError(role_name)
        job = Job(
            task_id=task_id,
            role_id=role.id,
            priority=priority,
            worker_id=worker_id,
        )
        self.db.add(job)
        self.db.flush()
        return job

    # ----- enrichment (used by `task list` / `task show`) -----

    def active_roles_for(self, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Bulk lookup: which role currently owns each task (NULL if idle).

        Returns dict {task_id: role_name}. Tasks without a live instance are
        absent from the dict.
        """
        if not task_ids:
            return {}
        stmt = (
            select(Instance, Role.name)
            .join(Role, Role.id == Instance.role_id)
            .where(
                Instance.task_id.in_(task_ids),
                Instance.status.in_(_LIVE_INSTANCE_STATUSES),
            )
        )
        out: dict[uuid.UUID, str] = {}
        for inst, role_name in self.db.execute(stmt):
            # If a task has multiple live instances (shouldn't with concurrency
            # caps, but defensive), the last write wins.
            out[inst.task_id] = role_name
        return out

    def instances_for(self, task_id: uuid.UUID) -> list[tuple[Instance, str]]:
        """Return (Instance, role_name) tuples for a task, newest first."""
        stmt = (
            select(Instance, Role.name)
            .join(Role, Role.id == Instance.role_id)
            .where(Instance.task_id == task_id)
            .order_by(Instance.spawned_at.desc())
        )
        return [(inst, role_name) for inst, role_name in self.db.execute(stmt)]

    def transitions_for(self, task_id: uuid.UUID) -> list[TaskTransition]:
        stmt = (
            select(TaskTransition)
            .where(TaskTransition.task_id == task_id)
            .order_by(TaskTransition.transitioned_at.asc())
        )
        return list(self.db.scalars(stmt))

    def jobs_for(self, task_id: uuid.UUID) -> list[tuple[Job, str]]:
        """Return (Job, role_name) tuples for a task, newest first."""
        stmt = (
            select(Job, Role.name)
            .join(Role, Role.id == Job.role_id)
            .where(Job.task_id == task_id)
            .order_by(Job.created_at.desc())
        )
        return [(job, role_name) for job, role_name in self.db.execute(stmt)]
