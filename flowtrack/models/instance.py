import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base


class InstanceStatus(str, enum.Enum):
    SPAWNING = "spawning"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


class Instance(Base):
    """A single Claude Code process spawned by the orchestrator.

    One row per process (alive or historical). Used by the kanban frontend to
    show what each instance is currently doing.
    """

    __tablename__ = "instances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("roles.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id")
    )
    claude_session_id: Mapped[str | None] = mapped_column(String(100))
    pid: Mapped[int | None]
    worktree_path: Mapped[str | None] = mapped_column(Text)
    branch_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[InstanceStatus] = mapped_column(
        Enum(
            InstanceStatus,
            name="instance_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=InstanceStatus.SPAWNING,
    )
    spawned_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    exit_code: Mapped[int | None]
    tokens_input: Mapped[int] = mapped_column(default=0)
    tokens_output: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0"))
    last_heartbeat_at: Mapped[datetime | None]
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    # The daemon process that spawned this instance (audit/debug).
    worker_id: Mapped[str | None] = mapped_column(String(100))

    role = relationship("Role")
    task = relationship("Task", foreign_keys=[task_id])
    session = relationship("Session", foreign_keys=[session_id])
    events = relationship("InstanceEvent", back_populates="instance", cascade="all, delete-orphan")
