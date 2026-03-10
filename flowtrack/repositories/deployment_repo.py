import uuid
from datetime import datetime

from sqlalchemy.orm import Session as DbSession

from flowtrack.models.deployment import Deployment, Environment


class DeploymentRepository:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def create(
        self,
        environment: Environment,
        session_id: uuid.UUID | None = None,
        commit_sha: str | None = None,
        pr_number: int | None = None,
        ticket_id: str | None = None,
    ) -> Deployment:
        deployment = Deployment(
            session_id=session_id,
            environment=environment,
            deployed_at=datetime.now(),
            commit_sha=commit_sha,
            pr_number=pr_number,
            ticket_id=ticket_id,
        )
        self.db.add(deployment)
        self.db.flush()
        return deployment

    def get_by_id(self, deployment_id: uuid.UUID) -> Deployment | None:
        return self.db.get(Deployment, deployment_id)

    def get_latest(self) -> Deployment | None:
        return self.db.query(Deployment).order_by(Deployment.deployed_at.desc()).first()

    def list_by_period(self, start: datetime, end: datetime) -> list[Deployment]:
        return (
            self.db.query(Deployment)
            .filter(Deployment.deployed_at >= start, Deployment.deployed_at < end)
            .order_by(Deployment.deployed_at)
            .all()
        )

    def list_by_ticket(self, ticket_id: str) -> list[Deployment]:
        return (
            self.db.query(Deployment)
            .filter(Deployment.ticket_id == ticket_id)
            .order_by(Deployment.deployed_at)
            .all()
        )

    def list_all(
        self,
        environment: Environment | None = None,
        limit: int = 10,
    ) -> list[Deployment]:
        query = self.db.query(Deployment)
        if environment:
            query = query.filter(Deployment.environment == environment)
        return query.order_by(Deployment.deployed_at.desc()).limit(limit).all()
