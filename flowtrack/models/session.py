import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base, TimestampMixin


class SessionType(str, enum.Enum):
    DEVELOPMENT = "development"
    REVIEW = "review"
    TESTING = "testing"


class SessionStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[SessionType] = mapped_column(Enum(SessionType, name="session_type"))
    ticket_id: Mapped[str | None] = mapped_column(String(100))
    pr_number: Mapped[int | None]
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status"), default=SessionStatus.ACTIVE
    )

    events = relationship("Event", back_populates="session")
    deployments = relationship("Deployment", back_populates="session")
