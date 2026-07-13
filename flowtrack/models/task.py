import enum
import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base, TimestampMixin


class TaskStatus(str, enum.Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    IN_REVIEW = "in_review"
    IN_QA = "in_qa"
    DONE = "done"


class TaskPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class BlockedReason(str, enum.Enum):
    """Why a task sits in status=blocked — different consumers fish in
    different buckets (DevOps picks up infra_failure; humans pick up
    manual_intervention/security)."""

    CODE_FAILURE = "code_failure"
    INFRA_FAILURE = "infra_failure"
    SECURITY = "security"
    MANUAL_INTERVENTION = "manual_intervention"


class TaskSeverity(str, enum.Enum):
    P0_CRITICAL = "P0-Critical"
    P1_HIGH = "P1-High"
    P2_MEDIUM = "P2-Medium"
    P3_LOW = "P3-Low"


class PipelineRouting(str, enum.Enum):
    # NULL on the column = not yet routed by TPM; orchestrator treats it as DEV_ONLY.
    DESIGN_DEV = "design_dev"
    DEV_ONLY = "dev_only"


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", values_callable=lambda x: [e.value for e in x]),
        default=TaskStatus.TODO,
    )
    priority: Mapped[TaskPriority] = mapped_column(
        Enum(TaskPriority, name="task_priority", values_callable=lambda x: [e.value for e in x]),
        default=TaskPriority.MEDIUM,
    )
    ticket_id: Mapped[str | None] = mapped_column(String(100))
    # Orchestrator extension columns.
    discovered_from: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovered_items.id")
    )
    # Heuristic for resource_locks: which module the task touches (e.g., "billing").
    module_hint: Mapped[str | None] = mapped_column(String(100))
    # Written by PM agent; required before role=dev picks the task up.
    acceptance_criteria: Mapped[str | None] = mapped_column(Text)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id")
    )
    # Pipeline fields (migration 010).
    # Incremented when a role routes the task via task_status_on_failure.
    # NOT reset on success advances (dev succeeding after each rework would
    # zero it every cycle and the cap could never trigger) — only a human
    # unblock (approve-return-dev / manual move out of blocked) resets it.
    bounce_count: Mapped[int] = mapped_column(default=0, server_default="0")
    blocked_reason: Mapped[BlockedReason | None] = mapped_column(
        Enum(BlockedReason, name="blocked_reason", values_callable=lambda x: [e.value for e in x])
    )
    # TPM output: full spec (title, AC, scope, module hints). acceptance_criteria
    # stays the dev-gate field; task_spec is the richer document.
    task_spec: Mapped[str | None] = mapped_column(Text)
    # Design agent output; NULL when routing skipped the design stage.
    ui_spec: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[TaskSeverity | None] = mapped_column(
        Enum(TaskSeverity, name="task_severity", values_callable=lambda x: [e.value for e in x])
    )
    pipeline_routing: Mapped[PipelineRouting | None] = mapped_column(
        Enum(PipelineRouting, name="pipeline_routing", values_callable=lambda x: [e.value for e in x])
    )
    # PO agent sort inputs (age_days is computed from created_at, not stored).
    jira_priority: Mapped[str | None] = mapped_column(String(50))
    is_urgent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_overdue: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    comments = relationship("TaskComment", back_populates="task", order_by="TaskComment.created_at")
    parent = relationship("Task", remote_side=[id], foreign_keys=[parent_task_id])
