"""Budget aggregation + circuit breaker.

Two responsibilities:

1. **Account** usage events into the current hour/day window
   (``budget_windows`` table). Called from ``stream_parser._persist_event``
   whenever it sees a ``usage`` event.

2. **Gate** new spawns: ``is_blocked()`` returns True when the current hour or
   day window has breached its configured cap. The orchestrator loop checks
   this before claiming a job. A breach is sticky for the rest of the window —
   the next window starts fresh.

Caps are configured via settings (``budget_{hour,day}_cap_usd``). 0 = disabled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from flowtrack.core.settings import settings
from flowtrack.models import BudgetWindow
from flowtrack.models.budget_window import BudgetWindowKind

log = logging.getLogger(__name__)


def _truncate(ts: datetime, kind: BudgetWindowKind) -> datetime:
    """Floor ``ts`` to the start of its hour/day/month window."""
    if kind == BudgetWindowKind.HOUR:
        return ts.replace(minute=0, second=0, microsecond=0)
    if kind == BudgetWindowKind.DAY:
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    # month
    return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _accumulate(
    db: Session, kind: BudgetWindowKind, *, cost: Decimal, tokens: int
) -> BudgetWindow:
    """Upsert the (window_start, window_kind) row, adding cost + tokens.

    Uses ON CONFLICT DO UPDATE to keep concurrent writers race-free.
    """
    start = _truncate(datetime.now(tz=timezone.utc), kind)
    stmt = (
        pg_insert(BudgetWindow.__table__)
        .values(
            window_start=start,
            window_kind=kind.value,
            tokens_used=tokens,
            cost_usd=cost,
            task_count=0,
        )
        .on_conflict_do_update(
            constraint="uq_budget_window",
            set_={
                "tokens_used": BudgetWindow.__table__.c.tokens_used + tokens,
                "cost_usd": BudgetWindow.__table__.c.cost_usd + cost,
            },
        )
    )
    db.execute(stmt)
    row = db.scalar(
        select(BudgetWindow).where(
            BudgetWindow.window_start == start,
            BudgetWindow.window_kind == kind,
        )
    )
    return row


def record_usage(db: Session, *, cost_usd: Decimal, tokens: int) -> None:
    """Increment all (HOUR, DAY, MONTH) windows in one call. Caller commits."""
    if cost_usd == 0 and tokens == 0:
        return
    for kind in (BudgetWindowKind.HOUR, BudgetWindowKind.DAY, BudgetWindowKind.MONTH):
        _accumulate(db, kind, cost=cost_usd, tokens=tokens)


def is_blocked(db: Session) -> tuple[bool, str | None]:
    """Return (blocked, reason). Cheap — single query."""
    now = datetime.now(tz=timezone.utc)
    if settings.budget_hour_cap_usd > 0:
        row = db.scalar(
            select(BudgetWindow).where(
                BudgetWindow.window_start == _truncate(now, BudgetWindowKind.HOUR),
                BudgetWindow.window_kind == BudgetWindowKind.HOUR,
            )
        )
        if row and float(row.cost_usd) >= settings.budget_hour_cap_usd:
            return True, f"hour cap ${settings.budget_hour_cap_usd} reached (spent ${row.cost_usd})"
    if settings.budget_day_cap_usd > 0:
        row = db.scalar(
            select(BudgetWindow).where(
                BudgetWindow.window_start == _truncate(now, BudgetWindowKind.DAY),
                BudgetWindow.window_kind == BudgetWindowKind.DAY,
            )
        )
        if row and float(row.cost_usd) >= settings.budget_day_cap_usd:
            return True, f"day cap ${settings.budget_day_cap_usd} reached (spent ${row.cost_usd})"
    return False, None


def current_windows(db: Session) -> dict[str, dict]:
    """Snapshot of the current hour + day + month windows for the API."""
    now = datetime.now(tz=timezone.utc)
    out: dict[str, dict] = {}
    for kind in (BudgetWindowKind.HOUR, BudgetWindowKind.DAY, BudgetWindowKind.MONTH):
        row = db.scalar(
            select(BudgetWindow).where(
                BudgetWindow.window_start == _truncate(now, kind),
                BudgetWindow.window_kind == kind,
            )
        )
        out[kind.value] = {
            "window_start": str(row.window_start) if row else None,
            "tokens_used": int(row.tokens_used) if row else 0,
            "cost_usd": str(row.cost_usd) if row else "0",
            "task_count": int(row.task_count) if row else 0,
        }
    out["caps"] = {
        "hour_usd": settings.budget_hour_cap_usd,
        "day_usd": settings.budget_day_cap_usd,
    }
    return out
