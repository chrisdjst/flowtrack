import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, Text
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

    comments = relationship("TaskComment", back_populates="task", order_by="TaskComment.created_at")
    parent = relationship("Task", remote_side=[id], foreign_keys=[parent_task_id])
