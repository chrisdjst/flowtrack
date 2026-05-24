from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.api.deps import db_session
from flowtrack.api.schemas import AssignTaskRequest, JobResponse
from flowtrack.models import Job, Role, Task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


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
