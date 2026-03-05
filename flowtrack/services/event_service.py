from sqlalchemy.orm import Session as DbSession

from flowtrack.core.exceptions import (
    EventAlreadyActiveError,
    NoActiveEventError,
    NoActiveSessionError,
)
from flowtrack.models.event import Event, EventType
from flowtrack.repositories.event_repo import EventRepository
from flowtrack.repositories.session_repo import SessionRepository


class EventService:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.session_repo = SessionRepository(db)
        self.event_repo = EventRepository(db)

    def start_block(self, reason: str | None = None) -> Event:
        session = self._get_active_session()
        active = self.event_repo.get_active_by_type(session.id, EventType.BLOCK_START)
        if active:
            raise EventAlreadyActiveError("block")
        metadata = {"reason": reason} if reason else None
        return self.event_repo.create(session.id, EventType.BLOCK_START, metadata)

    def end_block(self) -> Event:
        session = self._get_active_session()
        active = self.event_repo.get_active_by_type(session.id, EventType.BLOCK_START)
        if not active:
            raise NoActiveEventError("block")
        return self.event_repo.end(active)

    def start_interrupt(self, interrupt_type: str | None = None) -> Event:
        session = self._get_active_session()
        active = self.event_repo.get_active_by_type(session.id, EventType.INTERRUPT_START)
        if active:
            raise EventAlreadyActiveError("interrupt")
        metadata = {"type": interrupt_type} if interrupt_type else None
        return self.event_repo.create(session.id, EventType.INTERRUPT_START, metadata)

    def end_interrupt(self) -> Event:
        session = self._get_active_session()
        active = self.event_repo.get_active_by_type(session.id, EventType.INTERRUPT_START)
        if not active:
            raise NoActiveEventError("interrupt")
        return self.event_repo.end(active)

    def _get_active_session(self):  # noqa: ANN202
        session = self.session_repo.get_active()
        if not session:
            raise NoActiveSessionError()
        return session
