from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.api.deps import db_session
from flowtrack.api.events import broker
from flowtrack.api.schemas import (
    AdvanceTaskRequest,
    AdvanceTaskResponse,
    AssignTaskRequest,
    InstanceDetail,
    InstanceEventDetail,
    JobResponse,
    TaskDetail,
    TaskTransitionDetail,
)
from flowtrack.integrations.jira_sync import push_task_status
from flowtrack.models import Instance, InstanceEvent, Job, Role, Task, TaskComment, TaskTransition
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.instance_event import InstanceEventType
from flowtrack.models.task import TaskStatus
from flowtrack.orchestrator.spawner import _AWAIT_APPROVAL_REASON, _REQUEST_CHANGES_REASON

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Statuses that automatically trigger a role when a task is advanced to them.
_STATUS_TO_AUTO_ROLE: dict[str, str] = {
    "in_progress": "dev",
    "in_review": "reviewer",
    "in_qa": "qa",
}

_LIVE_STATUSES = (InstanceStatus.SPAWNING, InstanceStatus.RUNNING, InstanceStatus.WAITING_INPUT)

# Event types worth surfacing in the modal (skip noise: heartbeat, system, rate_limit).
_DETAIL_EVENT_TYPES = (
    InstanceEventType.ASSISTANT,
    InstanceEventType.TOOL_USE,
    InstanceEventType.ERROR,
    InstanceEventType.RESULT,
    InstanceEventType.USAGE,
)


def _event_summary(event_type: InstanceEventType, payload: dict) -> str:
    if event_type == InstanceEventType.TOOL_USE:
        name = payload.get("name") or ""
        inp = payload.get("input") or {}
        brief = str(inp)[:120] if inp else ""
        return f"[{name}] {brief}".strip()
    if event_type == InstanceEventType.USAGE:
        tok_in = payload.get("input_tokens", 0)
        tok_out = payload.get("output_tokens", 0)
        cost = payload.get("cost_usd", "")
        return f"tokens in={tok_in} out={tok_out} cost=${cost}"
    # ASSISTANT, ERROR, RESULT — extract text recursively.
    parts: list[str] = []
    def _walk(obj):
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            txt = obj.get("text")
            if isinstance(txt, str):
                parts.append(txt)
            for key in ("content", "message"):
                if key in obj:
                    _walk(obj[key])
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
    _walk(payload)
    return " ".join(parts)[:500]


@router.get("/{task_id}/detail", response_model=TaskDetail)
def get_task_detail(task_id: UUID, db: Session = Depends(db_session)) -> TaskDetail:
    """Full task detail: metadata, transition timeline, and per-instance event logs."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"task {task_id} not found")

    transitions = list(db.scalars(
        select(TaskTransition)
        .where(TaskTransition.task_id == task_id)
        .order_by(TaskTransition.transitioned_at.asc())
    ))

    instances_rows = list(db.scalars(
        select(Instance)
        .where(Instance.task_id == task_id)
        .order_by(Instance.spawned_at.asc())
    ))

    role_by_id = {r.id: r for r in db.scalars(select(Role))}

    instance_details: list[InstanceDetail] = []
    for inst in instances_rows:
        events_rows = list(db.scalars(
            select(InstanceEvent)
            .where(InstanceEvent.instance_id == inst.id)
            .where(InstanceEvent.event_type.in_(_DETAIL_EVENT_TYPES))
            .order_by(InstanceEvent.id.asc())
        ))
        events = [
            InstanceEventDetail(
                event_type=ev.event_type.value,
                summary=_event_summary(ev.event_type, ev.payload_json or {}),
                recorded_at=ev.recorded_at,
            )
            for ev in events_rows
            if _event_summary(ev.event_type, ev.payload_json or {}).strip()
        ]
        instance_details.append(InstanceDetail(
            id=inst.id,
            role_name=role_by_id[inst.role_id].name if inst.role_id in role_by_id else "?",
            status=inst.status.value,
            tokens_input=inst.tokens_input,
            tokens_output=inst.tokens_output,
            cost_usd=inst.cost_usd,
            spawned_at=inst.spawned_at,
            finished_at=inst.finished_at,
            exit_code=inst.exit_code,
            events=events,
        ))

    return TaskDetail(
        id=task.id,
        title=task.title,
        description=task.description,
        status=task.status.value,
        priority=task.priority.value,
        ticket_id=task.ticket_id,
        created_at=task.created_at,
        transitions=[
            TaskTransitionDetail(
                from_status=t.from_status,
                to_status=t.to_status,
                reason=t.reason,
                transitioned_at=t.transitioned_at,
                instance_id=t.instance_id,
            )
            for t in transitions
        ],
        instances=instance_details,
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


@router.patch("/{task_id}/status", response_model=AdvanceTaskResponse)
def advance_task_status(
    task_id: UUID,
    payload: AdvanceTaskRequest,
    db: Session = Depends(db_session),
) -> AdvanceTaskResponse:
    """Move a task to a new status and auto-trigger the matching role if configured.

    Auto-trigger is skipped when the task already has a live instance running.
    """
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"task {task_id} not found")

    try:
        new_status = TaskStatus(payload.status)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid status '{payload.status}'",
        )

    from_status = task.status.value if task.status else None
    task.status = new_status
    push_task_status(task.ticket_id, new_status.value)
    db.add(TaskTransition(
        task_id=task.id,
        from_status=from_status,
        to_status=new_status.value,
        reason="manual: frontend advance",
    ))

    job_id: UUID | None = None
    role_triggered: str | None = None

    auto_role_name = _STATUS_TO_AUTO_ROLE.get(new_status.value)
    if auto_role_name:
        live = db.scalars(
            select(Instance)
            .where(Instance.task_id == task.id)
            .where(Instance.status.in_(_LIVE_STATUSES))
        ).first()
        if live is None:
            role = db.scalar(select(Role).where(Role.name == auto_role_name))
            if role is not None:
                job = Job(task_id=task.id, role_id=role.id, priority=100)
                db.add(job)
                db.flush()
                job_id = job.id
                role_triggered = role.name

    db.flush()
    broker.publish_sync("task_transitioned", {
        "task_id": str(task.id),
        "from_status": from_status,
        "to_status": new_status.value,
        "by_role": role_triggered,
        "job_id": str(job_id) if job_id else None,
    })

    return AdvanceTaskResponse(
        task_id=task.id,
        status=new_status.value,
        job_id=job_id,
        role_triggered=role_triggered,
    )


@router.post("/{task_id}/approve-return-dev", response_model=AdvanceTaskResponse)
def approve_return_to_dev(
    task_id: UUID,
    db: Session = Depends(db_session),
) -> AdvanceTaskResponse:
    """Human approval: allow a task blocked at 2nd+ reviewer cycle to go back to dev.

    Only valid when the task is BLOCKED and the last transition reason is the
    awaiting-approval sentinel. Enqueues a dev Job and transitions to IN_PROGRESS.
    """
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"task {task_id} not found")

    if task.status != TaskStatus.BLOCKED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="task is not blocked — nothing to approve",
        )

    last_transition = db.scalar(
        select(TaskTransition)
        .where(TaskTransition.task_id == task_id)
        .order_by(TaskTransition.transitioned_at.desc())
        .limit(1)
    )
    if last_transition is None or last_transition.reason != _AWAIT_APPROVAL_REASON:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="task is not awaiting reviewer-cycle approval",
        )

    dev_role = db.scalar(select(Role).where(Role.name == "dev"))
    if dev_role is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'dev' role not found")

    from_status = task.status.value
    task.status = TaskStatus.IN_PROGRESS
    push_task_status(task.ticket_id, task.status.value)
    db.add(TaskTransition(
        task_id=task.id,
        from_status=from_status,
        to_status=task.status.value,
        reason=f"{_REQUEST_CHANGES_REASON} (human approved)",
    ))
    db.add(TaskComment(
        task_id=task.id,
        body="Human approved return to dev after repeated reviewer cycle.",
    ))
    job = Job(task_id=task.id, role_id=dev_role.id, priority=50)
    db.add(job)
    db.flush()

    broker.publish_sync("task_transitioned", {
        "task_id": str(task.id),
        "from_status": from_status,
        "to_status": task.status.value,
        "by_role": "human",
        "job_id": str(job.id),
    })

    return AdvanceTaskResponse(
        task_id=task.id,
        status=task.status.value,
        job_id=job.id,
        role_triggered="dev",
    )
