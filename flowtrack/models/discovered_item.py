import enum
import uuid
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flowtrack.models.base import Base, TimestampMixin


class DiscoverySource(str, enum.Enum):
    SENTRY = "sentry"
    JIRA = "jira"
    GITHUB_ISSUE = "github_issue"
    PR_COMMENT = "pr_comment"
    LOG_PATTERN = "log_pattern"
    SECOPS = "secops"


class DiscoveryKind(str, enum.Enum):
    BUG = "bug"
    FEATURE = "feature"
    IMPROVEMENT = "improvement"
    TECH_DEBT = "tech_debt"
    INCIDENT = "incident"
    SECURITY = "security"


class DiscoveryStatus(str, enum.Enum):
    NEW = "new"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class DiscoveredItem(TimestampMixin, Base):
    """Candidate item surfaced by a DiscoverySource worker.

    Stays in the kanban "Discovery" column until a human (or the PM agent)
    promotes it to a real ``Task`` or rejects it.
    """

    __tablename__ = "discovered_items"
    __table_args__ = (UniqueConstraint("source", "source_ref", name="uq_discovered_source_ref"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[DiscoverySource] = mapped_column(
        Enum(DiscoverySource, name="discovery_source", values_callable=lambda x: [e.value for e in x])
    )
    source_ref: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[DiscoveryKind] = mapped_column(
        Enum(DiscoveryKind, name="discovery_kind", values_callable=lambda x: [e.value for e in x])
    )
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    signal_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    status: Mapped[DiscoveryStatus] = mapped_column(
        Enum(DiscoveryStatus, name="discovery_status", values_callable=lambda x: [e.value for e in x]),
        default=DiscoveryStatus.NEW,
    )
    promoted_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id")
    )

    promoted_task = relationship("Task", foreign_keys=[promoted_task_id])
