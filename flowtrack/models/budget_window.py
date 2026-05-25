import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from flowtrack.models.base import Base


class BudgetWindowKind(str, enum.Enum):
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"


class BudgetWindow(Base):
    """Aggregated spend per time window for the circuit breaker.

    Orchestrator increments the row for the current window on every usage event;
    spawn loop checks the row before launching new instances.
    """

    __tablename__ = "budget_windows"
    __table_args__ = (
        UniqueConstraint("window_start", "window_kind", name="uq_budget_window"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    window_start: Mapped[datetime]
    window_kind: Mapped[BudgetWindowKind] = mapped_column(
        Enum(
            BudgetWindowKind,
            name="budget_window_kind",
            values_callable=lambda x: [e.value for e in x],
        )
    )
    tokens_used: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    task_count: Mapped[int] = mapped_column(default=0)
