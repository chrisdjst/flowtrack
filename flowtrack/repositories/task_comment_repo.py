import uuid

from sqlalchemy.orm import Session as DbSession

from flowtrack.models.task_comment import TaskComment


class TaskCommentRepository:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def create(self, task_id: uuid.UUID, body: str, synced_to_jira: bool = False) -> TaskComment:
        comment = TaskComment(task_id=task_id, body=body, synced_to_jira=synced_to_jira)
        self.db.add(comment)
        self.db.flush()
        return comment

    def list_by_task(self, task_id: uuid.UUID) -> list[TaskComment]:
        return (
            self.db.query(TaskComment)
            .filter(TaskComment.task_id == task_id)
            .order_by(TaskComment.created_at)
            .all()
        )
