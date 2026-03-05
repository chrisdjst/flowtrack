import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base, TimestampMixin


class TaskComment(TimestampMixin, Base):
    __tablename__ = "task_comments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    synced_to_jira: Mapped[bool] = mapped_column(default=False)

    task = relationship("Task", back_populates="comments")
