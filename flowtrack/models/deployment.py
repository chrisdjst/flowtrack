import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base, TimestampMixin


class Environment(str, enum.Enum):
    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"


class Deployment(TimestampMixin, Base):
    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id")
    )
    environment: Mapped[Environment] = mapped_column(Enum(Environment, name="environment"))
    deployed_at: Mapped[datetime]
    commit_sha: Mapped[str | None] = mapped_column(String(40))
    pr_number: Mapped[int | None]
    ticket_id: Mapped[str | None] = mapped_column(String(100))

    session = relationship("Session", back_populates="deployments")
    incidents = relationship("Incident", back_populates="deployment")
