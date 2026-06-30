from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.api.deps import db_session
from flowtrack.api.schemas import AssignTaskRequest, InstanceEventRow, JobResponse, TaskDetail
from flowtrack.models import Instance, InstanceEvent, Job, Role, Task
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.instance_event import InstanceEventType

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_LIVE_STATUSES = (
    InstanceStatus.SPAWNING,
    InstanceStatus.RUNNING,
    InstanceStatus.WAITING_INPUT,
)


def _ev_summary(event_type: InstanceEventType, payload: dict) -> str:
    if event_type == InstanceEventType.TOOL_USE:
        return f"tool: {payload.get('tool') or payload.get('name', '?')}"
    if event_type == InstanceEventType.USAGE:
        return f"+{payload.get('input_tokens', 0)}/{payload.get('output_tokens', 0)} tok"
    if event_type == InstanceEventType.RESULT:
        subtype = payload.get("subtype")
        cost = payload.get("total_cost_usd")
        if cost is not None:
            return f"result: {subtype or 'success'} (${cost})"
        return f"result: {subtype or payload.get('exit_reason', 'success')}"
    if event_type == InstanceEventType.ASSISTANT:
        return "assistant message"
    if event_type == InstanceEventType.SYSTEM:
        return f"system: {payload.get('subtype', 'event')}"
    if event_type == InstanceEventType.RATE_LIMIT:
        return "rate limit"
    if event_type == InstanceEventType.ERROR:
        return "error"
    if event_type == InstanceEventType.THINKING:
        return "thinking"
    return event_type.value


@router.get("/{task_id}", response_model=TaskDetail)
def get_task(task_id: UUID, db: Session = Depends(db_session)) -> TaskDetail:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"task {task_id} not found")

    live_inst = db.scalar(
        select(Instance)
        .where(Instance.task_id == task_id)
        .where(Instance.status.in_(_LIVE_STATUSES))
        .order_by(Instance.spawned_at.desc())
        .limit(1)
    )

    role_name = None
    recent_events: list[InstanceEventRow] = []
    if live_inst:
        role = db.get(Role, live_inst.role_id)
        role_name = role.name if role else None
        rows = list(
            db.scalars(
                select(InstanceEvent)
                .where(InstanceEvent.instance_id == live_inst.id)
                .order_by(InstanceEvent.recorded_at.desc())
                .limit(50)
            )
        )
        rows.reverse()
        recent_events = [
            InstanceEventRow(
                id=e.id,
                event_type=e.event_type.value,
                recorded_at=e.recorded_at,
                summary=_ev_summary(e.event_type, e.payload_json),
            )
            for e in rows
        ]

    return TaskDetail(
        id=task.id,
        title=task.title,
        description=task.description,
        acceptance_criteria=task.acceptance_criteria,
        status=task.status.value,
        priority=task.priority.value,
        ticket_id=task.ticket_id,
        module_hint=task.module_hint,
        created_at=task.created_at,
        current_instance_id=live_inst.id if live_inst else None,
        current_role_name=role_name,
        current_instance_status=live_inst.status.value if live_inst else None,
        recent_events=recent_events,
    )


@router.post(
    "/{task_id}/assign",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
)
def assign_task(
    task_id: UUID,
    payload: AssignTaskRequest,
    db: Session = Depends(db_session),
) -> Job:
    """Enqueue a Job binding (task, role). The orchestrator loop will pick it up.

    We do not check whether a previous Job for (task, role) is already running —
    that's a policy decision left to the orchestrator (idempotency at the spawn
    layer, not at enqueue).
    """
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"task {task_id} not found")

    role = db.scalar(select(Role).where(Role.name == payload.role_name))
    if role is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"role '{payload.role_name}' not found"
        )

    job = Job(
        task_id=task.id,
        role_id=role.id,
        priority=payload.priority,
        worker_id=payload.worker_id,
    )
    db.add(job)
    db.flush()  # populate id/created_at without ending the txn
    return job
