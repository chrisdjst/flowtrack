from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from flowtrack.api.deps import db_session
from flowtrack.api.events import broker
from flowtrack.api.schemas import InstanceCard
from flowtrack.models import Instance, InstanceEvent, Role
from flowtrack.models.instance_event import InstanceEventType

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


@router.post("/{instance_id}/hook", status_code=status.HTTP_202_ACCEPTED)
async def receive_hook(
    instance_id: UUID,
    request: Request,
    name: str = Query(..., description="Hook name (Stop, SubagentStop, etc.)"),
    db: Session = Depends(db_session),
) -> dict:
    """Callback from a Claude Code hook running inside a spawned worktree.

    The hook's stdin (forwarded by Claude) is captured verbatim into the event
    payload so we can correlate with stream-json later. Always 202 — Claude
    doesn't care about our response, only that we accepted.
    """
    inst = db.get(Instance, instance_id)
    if inst is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"instance {instance_id} not found")

    raw = await request.body()
    payload_text = raw.decode("utf-8", "replace") if raw else ""
    payload = {"hook_name": name, "raw": payload_text[:4000]}  # cap to keep DB tidy

    db.add(InstanceEvent(
        instance_id=instance_id,
        event_type=InstanceEventType.HEARTBEAT,
        payload_json=payload,
    ))
    inst.last_heartbeat_at = datetime.now(tz=timezone.utc)

    broker.publish_sync("hook_received", {
        "instance_id": str(instance_id),
        "hook": name,
    })
    return {"accepted": True}
