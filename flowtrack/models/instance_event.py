import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base


class InstanceEventType(str, enum.Enum):
    TOOL_USE = "tool_use"
    MESSAGE = "message"
    ERROR = "error"
    THINKING = "thinking"
    HEARTBEAT = "heartbeat"
    USAGE = "usage"
    RESULT = "result"
    # Real Claude Code stream-json variants we want to keep as first-class:
    SYSTEM = "system"               # init + reinit lifecycle pings
    ASSISTANT = "assistant"          # streamed assistant message chunks
    RATE_LIMIT = "rate_limit_event"  # quota / pacing pings from the API


class InstanceEvent(Base):
    """Single line parsed from Claude Code ``--output-format stream-json``.

    High-volume table — retention via cron job (default: drop > 30 days).
    bigserial PK because UUIDs would be wasteful at this volume.
    """

    __tablename__ = "instance_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instances.id")
    )
    event_type: Mapped[InstanceEventType] = mapped_column(
        Enum(
            InstanceEventType,
            name="instance_event_type",
            values_callable=lambda x: [e.value for e in x],
        )
    )
    payload_json: Mapped[dict] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())

    instance = relationship("Instance", back_populates="events")
