import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base


class TaskTransition(Base):
    """Audit trail of every task status change (= kanban column move).

    Feeds cycle-time / lead-time metrics and the per-task timeline on the UI.
    """

    __tablename__ = "task_transitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"))
    from_status: Mapped[str | None] = mapped_column(String(50))
    to_status: Mapped[str] = mapped_column(String(50))
    instance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instances.id")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    transitioned_at: Mapped[datetime] = mapped_column(server_default=func.now())

    task = relationship("Task")
    instance = relationship("Instance")
