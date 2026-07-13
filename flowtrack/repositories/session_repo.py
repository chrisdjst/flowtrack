import uuid
from datetime import datetime

from sqlalchemy.orm import Session as DbSession

from flowtrack.models.session import Session, SessionStatus, SessionType


class SessionRepository:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def create(
        self,
        session_type: SessionType,
        ticket_id: str | None = None,
        pr_number: int | None = None,
        instance_id: uuid.UUID | None = None,
    ) -> Session:
        session = Session(
            type=session_type,
            ticket_id=ticket_id,
            pr_number=pr_number,
            started_at=datetime.now(),
            status=SessionStatus.ACTIVE,
            instance_id=instance_id,
        )
        self.db.add(session)
        self.db.flush()
        return session

    def get_active(self) -> Session | None:
        """The human's active CLI session. Agent sessions (instance_id set)
        are excluded: they run concurrently and are owned by their instance,
        so they must never satisfy — or violate — the CLI's one-active-session
        invariant."""
        return (
            self.db.query(Session)
            .filter(Session.status.in_([SessionStatus.ACTIVE, SessionStatus.PAUSED]))
            .filter(Session.instance_id.is_(None))
            .first()
        )

    def get_by_id(self, session_id: uuid.UUID) -> Session | None:
        return self.db.get(Session, session_id)

    def end(self, session: Session) -> Session:
        session.status = SessionStatus.ENDED
        session.ended_at = datetime.now()
        self.db.flush()
        return session

    def end_for_instance(self, instance_id: uuid.UUID) -> Session | None:
        """Close an instance-owned agent session on any terminal path
        (spawner finalize, nonzero exit, watchdog kill). No-op when the
        instance never reached RUNNING (no session was created)."""
        session = (
            self.db.query(Session)
            .filter(Session.instance_id == instance_id)
            .filter(Session.status != SessionStatus.ENDED)
            .first()
        )
        if session is not None:
            session.status = SessionStatus.ENDED
            session.ended_at = datetime.now()
        return session

    def pause(self, session: Session) -> Session:
        session.status = SessionStatus.PAUSED
        self.db.flush()
        return session

    def resume(self, session: Session) -> Session:
        session.status = SessionStatus.ACTIVE
        self.db.flush()
        return session

    def list_by_period(self, start: datetime, end: datetime) -> list[Session]:
        return (
            self.db.query(Session)
            .filter(Session.started_at >= start, Session.started_at < end)
            .order_by(Session.started_at)
            .all()
        )

    def list_by_ticket(self, ticket_id: str) -> list[Session]:
        return (
            self.db.query(Session)
            .filter(Session.ticket_id == ticket_id)
            .order_by(Session.started_at)
            .all()
        )
