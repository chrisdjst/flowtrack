"""Discovery inbox endpoints.

Lists candidate items and promotes/rejects them. Promotion creates a Task
(with discovered_from set) and flips the item's status to 'promoted'.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.api.deps import db_session
from flowtrack.api.events import broker
from flowtrack.discovery.promote import promote_item, reject_item
from flowtrack.models import DiscoveredItem
from flowtrack.models.discovered_item import DiscoveryStatus

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


@router.get("")
def list_discovered(
    db: Session = Depends(db_session),
    include_resolved: bool = False,
) -> list[dict]:
    stmt = select(DiscoveredItem).order_by(DiscoveredItem.created_at.desc()).limit(200)
    if not include_resolved:
        stmt = stmt.where(DiscoveredItem.status == DiscoveryStatus.NEW)
    items = list(db.scalars(stmt))
    return [{
        "id": str(i.id),
        "source": i.source.value,
        "source_ref": i.source_ref,
        "kind": i.kind.value,
        "title": i.title,
        "summary": i.summary,
        "signal_score": str(i.signal_score) if i.signal_score is not None else None,
        "status": i.status.value,
        "promoted_task_id": str(i.promoted_task_id) if i.promoted_task_id else None,
        "created_at": i.created_at.isoformat(),
    } for i in items]


@router.post("/{item_id}/promote", status_code=status.HTTP_201_CREATED)
def promote(item_id: UUID, db: Session = Depends(db_session)) -> dict:
    """Materialise a DiscoveredItem into a Task.

    The new Task is created in status=todo without acceptance_criteria — the
    PM agent (or a human) fills those in next. Returns the created task id.
    """
    item = db.get(DiscoveredItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"item {item_id} not found")
    try:
        task = promote_item(db, item)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e))

    # Commit before returning so a subsequent request sees the new state.
    # Depends(db_session) commit runs AFTER response delivery and races with
    # rapid retries — see the smoke that previously double-promoted.
    db.commit()

    broker.publish_sync("discovered_item_promoted", {
        "item_id": str(item.id),
        "task_id": str(task.id),
        "title": task.title,
    })
    return {"task_id": str(task.id)}


@router.post("/{item_id}/refine", status_code=status.HTTP_200_OK)
async def refine(item_id: UUID, db: Session = Depends(db_session)) -> dict:
    """Run the PM agent over an item: returns refined criteria + a recommendation.

    Does NOT mutate the item or create a Task — caller decides whether to
    POST /promote afterwards. Run multiple times if needed; each call costs
    money so the API is explicit.
    """
    from flowtrack.agents.pm import refine_async
    from flowtrack.discovery.promote import open_tasks_snapshot

    item = db.get(DiscoveredItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"item {item_id} not found")
    try:
        result = await refine_async(
            title=item.title,
            summary=item.summary,
            kind=item.kind.value,
            source=item.source.value,
            source_ref=item.source_ref or "",
            raw_payload=item.raw_payload,
            existing_tasks=open_tasks_snapshot(db),
        )
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(e))

    broker.publish_sync("discovered_item_refined", {
        "item_id": str(item_id),
        "recommendation": result.recommendation,
        "cost_usd": str(result.cost_usd),
    })
    return {
        "acceptance_criteria": result.acceptance_criteria,
        "module_hint": result.module_hint,
        "recommendation": result.recommendation,
        "duplicate_of": result.duplicate_of,
        "severity": result.severity,
        "pipeline_routing": result.pipeline_routing,
        "task_spec": result.task_spec,
        "cost_usd": str(result.cost_usd),
    }


@router.post("/{item_id}/reject", status_code=status.HTTP_200_OK)
def reject(item_id: UUID, db: Session = Depends(db_session)) -> dict:
    item = db.get(DiscoveredItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"item {item_id} not found")
    try:
        reject_item(db, item)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e))
    db.commit()
    return {"ok": True}
