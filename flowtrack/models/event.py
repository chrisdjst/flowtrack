import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base, TimestampMixin


class EventType(str, enum.Enum):
    BLOCK_START = "block_start"
    BLOCK_END = "block_end"
    INTERRUPT_START = "interrupt_start"
    INTERRUPT_END = "interrupt_end"
    PAUSE = "pause"
    RESUME = "resume"


class Event(TimestampMixin, Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id")
    )
    event_type: Mapped[EventType] = mapped_column(Enum(EventType, name="event_type"))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)

    session = relationship("Session", back_populates="events")
