"""Integration tests for GET /api/tasks/{task_id}."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from flowtrack.api.deps import db_session
from flowtrack.api.routers import tasks
from flowtrack.models.task import Task, TaskPriority, TaskStatus


@pytest.fixture
def client(db):
    mini = FastAPI()
    mini.include_router(tasks.router)

    def _override():
        yield db

    mini.dependency_overrides[db_session] = _override
    return TestClient(mini)


def test_get_task_not_found(client):
    r = client.get(f"/api/tasks/{uuid.uuid4()}")
    assert r.status_code == 404


def test_get_task_returns_detail(client, db):
    task = Task(
        title="Implement live event modal",
        status=TaskStatus.IN_PROGRESS,
        priority=TaskPriority.HIGH,
        ticket_id="KAN-16",
        module_hint="flowtrack/web",
        description="Show live WS events in the task detail modal.",
        acceptance_criteria="Modal opens on click; events stream in real time.",
    )
    db.add(task)
    db.flush()

    r = client.get(f"/api/tasks/{task.id}")
    assert r.status_code == 200

    data = r.json()
    assert data["title"] == "Implement live event modal"
    assert data["status"] == "in_progress"
    assert data["priority"] == "high"
    assert data["ticket_id"] == "KAN-16"
    assert data["module_hint"] == "flowtrack/web"
    assert data["description"] == "Show live WS events in the task detail modal."
    assert "acceptance_criteria" in data
    assert data["current_instance_id"] is None
    assert data["current_instance_status"] is None
    assert data["current_role_name"] is None
    assert data["recent_events"] == []
