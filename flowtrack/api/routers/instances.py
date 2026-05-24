from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from flowtrack.api.deps import db_session
from flowtrack.api.schemas import InstanceCard
from flowtrack.models import Instance, Role

router = APIRouter(prefix="/api/instances", tags=["instances"])


@router.get("", response_model=list[InstanceCard])
def list_instances(
    include_finished_minutes: int = Query(
        default=60, ge=0, le=24 * 60, description="Include finished instances within last N minutes"
    ),
    db: Session = Depends(db_session),
) -> list[InstanceCard]:
    """Return live instances + recently finished ones (default last hour).

    Live = spawning|running|waiting_input (no finished_at).
    Finished = has finished_at, kept if within the cutoff window.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=include_finished_minutes)
    role_by_id = {r.id: r for r in db.scalars(select(Role))}

    stmt = (
        select(Instance)
        .where(or_(Instance.finished_at.is_(None), Instance.finished_at >= cutoff))
        .order_by(Instance.spawned_at.desc())
    )

    return [
        InstanceCard(
            id=inst.id,
            role_name=role_by_id[inst.role_id].name,
            task_id=inst.task_id,
            task_title=(inst.task.title if inst.task is not None else None),
            status=inst.status.value,
            tokens_input=inst.tokens_input,
            tokens_output=inst.tokens_output,
            cost_usd=inst.cost_usd,
            spawned_at=inst.spawned_at,
            last_heartbeat_at=inst.last_heartbeat_at,
        )
        for inst in db.scalars(stmt)
    ]
