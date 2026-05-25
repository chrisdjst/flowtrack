import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base, TimestampMixin


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(TimestampMixin, Base):
    """Work item the orchestrator pulls from the queue.

    Claimed via ``SELECT ... FOR UPDATE SKIP LOCKED`` to avoid double-pickup
    across workers without needing Redis/Celery.
    """

    __tablename__ = "job_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"))
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("roles.id"))
    priority: Mapped[int] = mapped_column(default=100)
    payload_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", values_callable=lambda x: [e.value for e in x]),
        default=JobStatus.QUEUED,
    )
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    claimed_at: Mapped[datetime | None]
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instances.id")
    )
    finished_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    # Isolation lane: NULL = any daemon can claim. When set, only a daemon
    # whose worker_id matches will pick it up. See alembic 007 + identity.py.
    worker_id: Mapped[str | None] = mapped_column(String(100))

    task = relationship("Task")
    role = relationship("Role")
    instance = relationship("Instance", foreign_keys=[claimed_by])
