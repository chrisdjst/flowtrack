from __future__ import annotations

import uuid

from sqlalchemy.orm import Session as DbSession

from flowtrack.core.exceptions import FlowTrackError
from flowtrack.core.settings import settings
from flowtrack.integrations.jira_client import JiraClient
from flowtrack.models.task import Task, TaskPriority, TaskStatus
from flowtrack.models.task_comment import TaskComment
from flowtrack.repositories.task_comment_repo import TaskCommentRepository
from flowtrack.repositories.task_repo import TaskRepository


class TaskNotFoundError(FlowTrackError):
    def __init__(self, task_id: uuid.UUID | str) -> None:
        super().__init__(f"Task '{task_id}' not found.")


class NoActiveTaskError(FlowTrackError):
    def __init__(self) -> None:
        super().__init__("No task with status 'in_progress' found.")


PRIORITY_TO_JIRA = {
    TaskPriority.LOW: "Low",
    TaskPriority.MEDIUM: "Medium",
    TaskPriority.HIGH: "High",
    TaskPriority.URGENT: "Highest",
}


class TaskService:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.repo = TaskRepository(db)
        self.comment_repo = TaskCommentRepository(db)
        self.jira = JiraClient()

    def create(
        self,
        title: str,
        description: str | None = None,
        status: TaskStatus = TaskStatus.TODO,
        priority: TaskPriority = TaskPriority.MEDIUM,
        ticket_id: str | None = None,
        sync_jira: bool = True,
    ) -> tuple[Task, str | None]:
        """Create a task. Returns (task, jira_key) where jira_key is set if synced."""
        jira_key = None

        if sync_jira and not ticket_id and settings.jira_project_key:
            jira_key = self.jira.create_issue(
                project_key=settings.jira_project_key,
                summary=title,
                description=description,
                priority=PRIORITY_TO_JIRA.get(priority),
            )
            if jira_key:
                ticket_id = jira_key

        task = self.repo.create(
            title=title,
            description=description,
            status=status,
            priority=priority,
            ticket_id=ticket_id,
        )
        return task, jira_key

    def add_comment(
        self,
        task_id: uuid.UUID,
        body: str,
        sync_jira: bool = True,
    ) -> tuple[TaskComment, bool]:
        """Add a comment to a task. Returns (comment, synced_to_jira)."""
        task = self._get_or_raise(task_id)
        synced = False

        if sync_jira and task.ticket_id:
            synced = self.jira.post_comment(task.ticket_id, body)

        comment = self.comment_repo.create(task_id=task_id, body=body, synced_to_jira=synced)
        return comment, synced

    def get_active(self) -> Task | None:
        """Get the current in_progress task (most recently created)."""
        tasks = self.repo.list_all(status=TaskStatus.IN_PROGRESS)
        return tasks[0] if tasks else None

    def update_status(self, task_id: uuid.UUID, status: TaskStatus) -> Task:
        task = self._get_or_raise(task_id)
        return self.repo.update_status(task, status)

    def list(self, status: TaskStatus | None = None) -> list[Task]:
        return self.repo.list_all(status)

    def get_comments(self, task_id: uuid.UUID) -> list[TaskComment]:
        self._get_or_raise(task_id)
        return self.comment_repo.list_by_task(task_id)

    def delete(self, task_id: uuid.UUID) -> None:
        task = self._get_or_raise(task_id)
        self.repo.delete(task)

    def _get_or_raise(self, task_id: uuid.UUID) -> Task:
        task = self.repo.get_by_id(task_id)
        if not task:
            raise TaskNotFoundError(task_id)
        return task
