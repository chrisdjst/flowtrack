from sqlalchemy.orm import Session as DbSession

from flowtrack.core.exceptions import NoActiveIncidentError
from flowtrack.models.incident import Incident
from flowtrack.repositories.deployment_repo import DeploymentRepository
from flowtrack.repositories.incident_repo import IncidentRepository


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
