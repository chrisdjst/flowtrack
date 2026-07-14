import uuid

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from flowtrack.models.base import Base, TimestampMixin


class Project(TimestampMixin, Base):
    """Registry of codebases/products the pipeline works on.

    Tasks and role variants may point at one; the dispatcher prefers a
    variant whose project matches the task's (see orchestrator/dispatch.py).
    Mechanism only — nothing requires projects to exist.
    """

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    repo_path: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
