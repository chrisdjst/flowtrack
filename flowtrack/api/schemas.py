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
    last_instance_failed: bool  # derived; true when last instance exited with failure
    awaiting_approval: bool  # derived; true when blocked awaiting human approval to return to dev
    blocked_reason: str | None = None  # why the task is blocked (typed bucket)
    bounce_count: int = 0  # failure-routings accumulated since last human reset
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


class AssignTaskRequest(BaseModel):
    role_name: str
    priority: int = 100
    # Optional isolation lane. Defaults to NULL = any daemon can claim.
    # Tests / parallel daemons set this to a unique value to avoid cross-pickup.
    worker_id: str | None = None


class AdvanceTaskRequest(BaseModel):
    status: str


class AdvanceTaskResponse(BaseModel):
    task_id: UUID
    status: str
    job_id: UUID | None = None
    role_triggered: str | None = None


class TaskTransitionDetail(_Base):
    from_status: str | None
    to_status: str
    reason: str | None
    transitioned_at: datetime
    instance_id: UUID | None


class InstanceEventDetail(_Base):
    event_type: str
    summary: str
    recorded_at: datetime


class InstanceDetail(_Base):
    id: UUID
    role_name: str
    status: str
    tokens_input: int
    tokens_output: int
    cost_usd: Decimal
    spawned_at: datetime
    finished_at: datetime | None
    exit_code: int | None
    events: list[InstanceEventDetail]


class TaskDetail(_Base):
    id: UUID
    title: str
    description: str | None
    status: str
    priority: str
    ticket_id: str | None
    created_at: datetime
    transitions: list[TaskTransitionDetail]
    instances: list[InstanceDetail]


class JobResponse(_Base):
    id: UUID
    task_id: UUID
    role_id: UUID
    status: str
    priority: int
    attempts: int
    created_at: datetime


class RoleCard(_Base):
    id: UUID
    name: str
    model: str
    max_tokens: int
    max_turns: int | None
    max_minutes: int
    tools_allowed: list[str] | None
    system_prompt: str
    next_role_name: str | None
    task_status_on_success: str | None
    task_status_on_failure: str | None
    max_bounce_count: int | None
    updated_at: datetime


class RoleUpdate(BaseModel):
    model: str | None = None
    max_tokens: int | None = None
    max_turns: int | None = None
    max_minutes: int | None = None
    tools_allowed: list[str] | None = None
    system_prompt: str | None = None
    next_role_name: str | None = None
    task_status_on_success: str | None = None
    task_status_on_failure: str | None = None
    max_bounce_count: int | None = None
