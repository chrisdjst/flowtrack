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
    max_turns: Mapped[int | None] = mapped_column(default=None)
    max_minutes: Mapped[int] = mapped_column(default=60)
    # Pipeline: when this role completes successfully, enqueue next_role_name
    # for the same task and set tasks.status = task_status_on_success.
    # NULL on either field = pipeline ends here.
    next_role_name: Mapped[str | None] = mapped_column(String(50))
    task_status_on_success: Mapped[str | None] = mapped_column(String(50))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
