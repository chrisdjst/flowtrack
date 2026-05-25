from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from flowtrack.api.deps import db_session
from flowtrack.orchestrator import budget

router = APIRouter(prefix="/api/budget", tags=["budget"])


@router.get("")
def get_budget(db: Session = Depends(db_session)) -> dict:
    """Snapshot of current hour/day/month windows and configured caps.

    The frontend uses this for the live spend panel; the loop uses
    ``budget.is_blocked`` directly. Both reads are cheap (single index hit
    per window via UNIQUE constraint).
    """
    snapshot = budget.current_windows(db)
    blocked, reason = budget.is_blocked(db)
    snapshot["blocked"] = blocked
    snapshot["reason"] = reason
    return snapshot
