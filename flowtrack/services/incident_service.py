import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from flowtrack.core.exceptions import NoActiveIncidentError
from flowtrack.models.incident import Incident
from flowtrack.repositories.deployment_repo import DeploymentRepository
from flowtrack.repositories.incident_repo import IncidentRepository

# Description prefix that marks incidents opened by the orchestrator when a
# task lands in blocked/infra_failure. The prefix is the join key for
# resolution AND what keeps the human CLI's `incident end` away from them
# (see IncidentRepository.get_active).
PIPELINE_INCIDENT_PREFIX = "[pipeline]"


def open_pipeline_incident(db: DbSession, *, task_id: uuid.UUID, reason: str) -> Incident:
    """Open an MTTR-feeding incident for an infra-blocked task.

    deployment_id stays NULL on purpose: a pipeline infra failure is not a
    failed production deployment, so it must not count against the change
    failure rate.
    """
    return IncidentRepository(db).create(
        description=f"{PIPELINE_INCIDENT_PREFIX} task {task_id}: {reason}",
        severity="high",
    )


def resolve_pipeline_incidents(db: DbSession, task_id: uuid.UUID) -> int:
    """Resolve every open pipeline incident of a task (infra block cleared,
    by the pipeline itself or by a human unblock). Returns how many closed."""
    rows = list(db.scalars(
        select(Incident)
        .where(Incident.description.like(f"{PIPELINE_INCIDENT_PREFIX} task {task_id}%"))
        .where(Incident.resolved_at.is_(None))
    ))
    now = datetime.now()
    for incident in rows:
        incident.resolved_at = now
    return len(rows)


class IncidentService:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.repo = IncidentRepository(db)
        self.deploy_repo = DeploymentRepository(db)

    def start(self, description: str | None = None, severity: str | None = None) -> Incident:
        latest_deploy = self.deploy_repo.get_latest()
        return self.repo.create(
            deployment_id=latest_deploy.id if latest_deploy else None,
            description=description,
            severity=severity,
        )

    def end(self) -> Incident:
        incident = self.repo.get_active()
        if not incident:
            raise NoActiveIncidentError()
        return self.repo.resolve(incident)

    def list_incidents(self, open_only: bool = False, limit: int = 10) -> list[Incident]:
        return self.repo.list_all(open_only=open_only, limit=limit)
