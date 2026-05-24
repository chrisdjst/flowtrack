import uuid
from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from flowtrack.models.base import Base, TimestampMixin


class Role(TimestampMixin, Base):
    """Catalog of agent roles (pm, po, dev, reviewer, qa, ...).

    System prompts and limits live in DB so they can be tuned without redeploy.
    """

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    system_prompt: Mapped[str] = mapped_column(Text)
    tools_allowed: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)))
    model: Mapped[str] = mapped_column(String(50), default="claude-sonnet-4-6")
    max_tokens: Mapped[int] = mapped_column(default=500_000)
    max_minutes: Mapped[int] = mapped_column(default=60)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
