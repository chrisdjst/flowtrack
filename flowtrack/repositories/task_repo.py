import uuid

from sqlalchemy.orm import Session as DbSession

from flowtrack.models.task import Task, TaskPriority, TaskStatus


class TaskRepository:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def create(
        self,
        title: str,
        description: str | None = None,
        status: TaskStatus = TaskStatus.TODO,
        priority: TaskPriority = TaskPriority.MEDIUM,
        ticket_id: str | None = None,
    ) -> Task:
        task = Task(
            title=title,
            description=description,
            status=status,
            priority=priority,
            ticket_id=ticket_id,
        )
        self.db.add(task)
        self.db.flush()
        return task

    def get_by_id(self, task_id: uuid.UUID) -> Task | None:
        return self.db.get(Task, task_id)

    def update_status(self, task: Task, status: TaskStatus) -> Task:
        task.status = status
        self.db.flush()
        return task

    def update(
        self,
        task: Task,
        title: str | None = None,
        description: str | None = None,
        status: TaskStatus | None = None,
        priority: TaskPriority | None = None,
    ) -> Task:
        if title is not None:
            task.title = title
        if description is not None:
            task.description = description
        if status is not None:
            task.status = status
        if priority is not None:
            task.priority = priority
        self.db.flush()
        return task

    def list_all(self, status: TaskStatus | None = None) -> list[Task]:
        query = self.db.query(Task)
        if status:
            query = query.filter(Task.status == status)
        return query.order_by(Task.created_at.desc()).all()

    def delete(self, task: Task) -> None:
        self.db.delete(task)
        self.db.flush()
