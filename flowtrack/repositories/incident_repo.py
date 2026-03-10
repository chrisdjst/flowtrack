import uuid
from datetime import datetime

from sqlalchemy.orm import Session as DbSession

from flowtrack.models.incident import Incident


class IncidentRepository:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def create(
        self,
        deployment_id: uuid.UUID | None = None,
        description: str | None = None,
        severity: str | None = None,
    ) -> Incident:
        incident = Incident(
            deployment_id=deployment_id,
            started_at=datetime.now(),
            description=description,
            severity=severity,
        )
        self.db.add(incident)
        self.db.flush()
        return incident

    def get_active(self) -> Incident | None:
        return (
            self.db.query(Incident)
            .filter(Incident.resolved_at.is_(None))
            .order_by(Incident.started_at.desc())
            .first()
        )

    def resolve(self, incident: Incident) -> Incident:
        incident.resolved_at = datetime.now()
        self.db.flush()
        return incident

    def list_by_period(self, start: datetime, end: datetime) -> list[Incident]:
        return (
            self.db.query(Incident)
            .filter(Incident.started_at >= start, Incident.started_at < end)
            .order_by(Incident.started_at)
            .all()
        )

    def list_resolved_by_period(self, start: datetime, end: datetime) -> list[Incident]:
        return (
            self.db.query(Incident)
            .filter(
                Incident.started_at >= start,
                Incident.started_at < end,
                Incident.resolved_at.is_not(None),
            )
            .order_by(Incident.started_at)
            .all()
        )

    def list_all(
        self,
        open_only: bool = False,
        limit: int = 10,
    ) -> list[Incident]:
        query = self.db.query(Incident)
        if open_only:
            query = query.filter(Incident.resolved_at.is_(None))
        return query.order_by(Incident.started_at.desc()).limit(limit).all()
