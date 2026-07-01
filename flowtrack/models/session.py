import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String
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
    type: Mapped[SessionType] = mapped_column(Enum(SessionType, name="session_type", values_callable=lambda x: [e.value for e in x]))
    ticket_id: Mapped[str | None] = mapped_column(String(100))
    pr_number: Mapped[int | None]
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status", values_callable=lambda x: [e.value for e in x]), default=SessionStatus.ACTIVE
    )
    # NULL = manual CLI session; set = session owned by an orchestrator instance.
    instance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instances.id")
    )

    events = relationship("Event", back_populates="session")
    deployments = relationship("Deployment", back_populates="session")
