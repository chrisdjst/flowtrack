"""Pydantic v2 response schemas for the orchestrator API.

Kept thin on purpose — these are not domain entities, just wire formats. Anything
business-logic-shaped belongs in services/.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DiscoveredItemCard(_Base):
    id: UUID
    source: str
    kind: str
    title: str
    summary: str | None
    signal_score: Decimal | None
    created_at: datetime


class TaskCard(_Base):
    id: UUID
    title: str
    description: str | None
    status: str
    priority: str
    ticket_id: str | None
    module_hint: str | None
    has_acceptance_criteria: bool  # derived; see kanban router
    current_instance_id: UUID | None  # derived
    current_role_name: str | None  # derived
    created_at: datetime


class InstanceCard(_Base):
    id: UUID
    role_name: str
    task_id: UUID | None
    task_title: str | None
    status: str
    tokens_input: int
    tokens_output: int
    cost_usd: Decimal
    spawned_at: datetime
    last_heartbeat_at: datetime | None


class KanbanBoard(_Base):
    discovery: list[DiscoveredItemCard]
    refinement: list[TaskCard]
    ready: list[TaskCard]
    in_progress: list[TaskCard]
    blocked: list[TaskCard]
    in_review: list[TaskCard]
    qa: list[TaskCard]
    merged: list[TaskCard]
    active_instances: list[InstanceCard]


class InstanceEventRow(_Base):
    id: int
    event_type: str
    recorded_at: datetime
    summary: str


class TaskDetail(_Base):
    id: UUID
    title: str
    description: str | None
    acceptance_criteria: str | None
    status: str
    priority: str
    ticket_id: str | None
    module_hint: str | None
    created_at: datetime
    current_instance_id: UUID | None
    current_role_name: str | None
    current_instance_status: str | None
    recent_events: list[InstanceEventRow]


class AssignTaskRequest(BaseModel):
    role_name: str
    priority: int = 100
    # Optional isolation lane. Defaults to NULL = any daemon can claim.
    # Tests / parallel daemons set this to a unique value to avoid cross-pickup.
    worker_id: str | None = None


class JobResponse(_Base):
    id: UUID
    task_id: UUID
    role_id: UUID
    status: str
    priority: int
    attempts: int
    created_at: datetime
