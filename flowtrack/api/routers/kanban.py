from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.api.deps import db_session
from flowtrack.api.schemas import (
    DiscoveredItemCard,
    InstanceCard,
    KanbanBoard,
    TaskCard,
)
from flowtrack.models import DiscoveredItem, Instance, Role, Task, TaskTransition
from flowtrack.models.discovered_item import DiscoveryStatus
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.task import TaskStatus
from flowtrack.orchestrator.spawner import _AWAIT_APPROVAL_REASON

_FAILED_STATUSES = (InstanceStatus.FAILED, InstanceStatus.KILLED)

router = APIRouter(prefix="/api", tags=["kanban"])


# Statuses considered "alive" for the current_instance_id derivation.
_LIVE_INSTANCE_STATUSES = (
    InstanceStatus.SPAWNING,
    InstanceStatus.RUNNING,
    InstanceStatus.WAITING_INPUT,
)


@router.get("/kanban", response_model=KanbanBoard)
def get_kanban(db: Session = Depends(db_session)) -> KanbanBoard:
    """Single endpoint that returns the full board.

    Designed for the frontend to render in one round-trip. If the board grows
    big, paginate per column rather than splitting into many endpoints.
    """
    # All live instances, indexed by task_id for card enrichment.
    live_instances = list(
        db.scalars(
            select(Instance).where(Instance.status.in_(_LIVE_INSTANCE_STATUSES))
        )
    )
    role_by_id = {r.id: r for r in db.scalars(select(Role))}
    instance_by_task: dict = {}
    for inst in live_instances:
        if inst.task_id is not None:
            instance_by_task[inst.task_id] = inst

    # Most recent finished instance per task — used to flag failed cards.
    from sqlalchemy import func as sqlfunc
    latest_subq = (
        select(Instance.task_id, sqlfunc.max(Instance.spawned_at).label("latest"))
        .where(Instance.finished_at.isnot(None))
        .group_by(Instance.task_id)
        .subquery()
    )
    failed_task_ids: set = set(db.scalars(
        select(Instance.task_id)
        .join(latest_subq, (Instance.task_id == latest_subq.c.task_id) &
              (Instance.spawned_at == latest_subq.c.latest))
        .where(Instance.status.in_(_FAILED_STATUSES))
    ))

    # Tasks in BLOCKED state whose last transition reason is the awaiting-approval sentinel.
    latest_transition_subq = (
        select(TaskTransition.task_id, sqlfunc.max(TaskTransition.transitioned_at).label("latest"))
        .group_by(TaskTransition.task_id)
        .subquery()
    )
    awaiting_approval_task_ids: set = set(db.scalars(
        select(TaskTransition.task_id)
        .join(
            latest_transition_subq,
            (TaskTransition.task_id == latest_transition_subq.c.task_id) &
            (TaskTransition.transitioned_at == latest_transition_subq.c.latest),
        )
        .where(TaskTransition.reason == _AWAIT_APPROVAL_REASON)
    ))

    def to_card(t: Task) -> TaskCard:
        inst = instance_by_task.get(t.id)
        role_name = role_by_id[inst.role_id].name if inst is not None else None
        return TaskCard(
            id=t.id,
            title=t.title,
            description=t.description,
            status=t.status.value,
            priority=t.priority.value,
            ticket_id=t.ticket_id,
            module_hint=t.module_hint,
            has_acceptance_criteria=t.acceptance_criteria is not None,
            current_instance_id=inst.id if inst else None,
            current_role_name=role_name,
            last_instance_failed=t.id in failed_task_ids,
            awaiting_approval=t.id in awaiting_approval_task_ids,
            created_at=t.created_at,
        )

    def tasks_with(status: TaskStatus, *, criteria: bool | None = None) -> list[TaskCard]:
        stmt = select(Task).where(Task.status == status)
        if criteria is True:
            stmt = stmt.where(Task.acceptance_criteria.isnot(None))
        elif criteria is False:
            stmt = stmt.where(Task.acceptance_criteria.is_(None))
        stmt = stmt.order_by(Task.created_at.desc())
        return [to_card(t) for t in db.scalars(stmt)]

    discovery_items = db.scalars(
        select(DiscoveredItem)
        .where(DiscoveredItem.status == DiscoveryStatus.NEW)
        .order_by(DiscoveredItem.created_at.desc())
    )

    active_instance_cards = [
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
        for inst in live_instances
    ]

    return KanbanBoard(
        discovery=[DiscoveredItemCard.model_validate(d) for d in discovery_items],
        refinement=tasks_with(TaskStatus.TODO, criteria=False),
        ready=tasks_with(TaskStatus.TODO, criteria=True),
        in_progress=tasks_with(TaskStatus.IN_PROGRESS),
        blocked=tasks_with(TaskStatus.BLOCKED),
        in_review=tasks_with(TaskStatus.IN_REVIEW),
        qa=tasks_with(TaskStatus.IN_QA),
        merged=tasks_with(TaskStatus.DONE),
        active_instances=active_instance_cards,
    )
