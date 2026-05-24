import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base


class ResourceLock(Base):
    """Logical lock so two instances do not edit the same module concurrently.

    ``resource_key`` is opaque: it can be a file path, a module name, or a
    sentinel like ``'migration'``. Uniqueness on ``resource_key`` enforces
    mutual exclusion. Watchdog clears stale rows whose ``expires_at`` passed.
    """

    __tablename__ = "resource_locks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resource_key: Mapped[str] = mapped_column(String(255), unique=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instances.id")
    )
    acquired_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]

    instance = relationship("Instance")
