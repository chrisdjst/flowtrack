import subprocess

from sqlalchemy.orm import Session as DbSession

from flowtrack.models.deployment import Deployment, Environment
from flowtrack.repositories.deployment_repo import DeploymentRepository
from flowtrack.repositories.session_repo import SessionRepository


class DeployService:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.repo = DeploymentRepository(db)
        self.session_repo = SessionRepository(db)

    def record_deploy(
        self,
        environment: Environment,
        pr_number: int | None = None,
        ticket_id: str | None = None,
    ) -> Deployment:
        session = self.session_repo.get_active()
        commit_sha = self._get_current_commit()
        return self.repo.create(
            environment=environment,
            session_id=session.id if session else None,
            commit_sha=commit_sha,
            pr_number=pr_number or (session.pr_number if session else None),
            ticket_id=ticket_id or (session.ticket_id if session else None),
        )

    def _get_current_commit(self) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
