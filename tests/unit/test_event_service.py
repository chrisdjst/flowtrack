import pytest

from flowtrack.core.exceptions import (
    EventAlreadyActiveError,
    NoActiveEventError,
    NoActiveSessionError,
)
from flowtrack.models.session import SessionType
from flowtrack.services.event_service import EventService
from flowtrack.services.session_service import SessionService


class TestEventService:
    def test_start_block(self, db):
        SessionService(db).start(SessionType.DEVELOPMENT)
        svc = EventService(db)

        event = svc.start_block(reason="waiting for API")
        assert event.metadata_json == {"reason": "waiting for API"}
        assert event.ended_at is None

    def test_end_block(self, db):
        SessionService(db).start(SessionType.DEVELOPMENT)
        svc = EventService(db)
        svc.start_block()

        event = svc.end_block()
        assert event.ended_at is not None

    def test_start_block_raises_if_no_session(self, db):
        svc = EventService(db)
        with pytest.raises(NoActiveSessionError):
            svc.start_block()

    def test_start_block_raises_if_already_active(self, db):
        SessionService(db).start(SessionType.DEVELOPMENT)
        svc = EventService(db)
        svc.start_block()

        with pytest.raises(EventAlreadyActiveError):
            svc.start_block()

    def test_end_block_raises_if_no_active(self, db):
        SessionService(db).start(SessionType.DEVELOPMENT)
        svc = EventService(db)

        with pytest.raises(NoActiveEventError):
            svc.end_block()

    def test_start_and_end_interrupt(self, db):
        SessionService(db).start(SessionType.DEVELOPMENT)
        svc = EventService(db)

        event = svc.start_interrupt(interrupt_type="meeting")
        assert event.metadata_json == {"type": "meeting"}

        event = svc.end_interrupt()
        assert event.ended_at is not None
