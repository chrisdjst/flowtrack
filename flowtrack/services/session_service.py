from sqlalchemy.orm import Session as DbSession

from flowtrack.core.exceptions import NoActiveSessionError, SessionAlreadyActiveError
from flowtrack.models.event import EventType
from flowtrack.models.session import Session, SessionStatus, SessionType
from flowtrack.repositories.event_repo import EventRepository
from flowtrack.repositories.session_repo import SessionRepository


class SessionService:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.repo = SessionRepository(db)
        self.event_repo = EventRepository(db)

    def start(
        self,
        session_type: SessionType,
        ticket_id: str | None = None,
        pr_number: int | None = None,
    ) -> Session:
        active = self.repo.get_active()
        if active:
            raise SessionAlreadyActiveError(active.type.value)
        return self.repo.create(session_type, ticket_id, pr_number)

    def end(self) -> Session:
        session = self._get_active_or_raise()
        # Close any open block/interrupt events
        for event_type in [EventType.BLOCK_START, EventType.INTERRUPT_START]:
            active_event = self.event_repo.get_active_by_type(session.id, event_type)
            if active_event:
                self.event_repo.end(active_event)
        return self.repo.end(session)

    def pause(self) -> Session:
        session = self._get_active_or_raise()
        if session.status != SessionStatus.ACTIVE:
            raise NoActiveSessionError()
        self.event_repo.create(session.id, EventType.PAUSE)
        return self.repo.pause(session)

    def resume(self) -> Session:
        session = self._get_active_or_raise()
        if session.status != SessionStatus.PAUSED:
            raise NoActiveSessionError()
        self.event_repo.create(session.id, EventType.RESUME)
        return self.repo.resume(session)

    def get_active(self) -> Session | None:
        return self.repo.get_active()

    def _get_active_or_raise(self) -> Session:
        session = self.repo.get_active()
        if not session:
            raise NoActiveSessionError()
        return session
