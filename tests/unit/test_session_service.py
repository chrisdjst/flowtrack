import pytest

from flowtrack.core.exceptions import NoActiveSessionError, SessionAlreadyActiveError
from flowtrack.models.session import SessionStatus, SessionType
from flowtrack.services.session_service import SessionService


class TestSessionService:
    def test_start_creates_active_session(self, db):
        svc = SessionService(db)
        session = svc.start(SessionType.DEVELOPMENT, ticket_id="TEST-1", pr_number=42)

        assert session.type == SessionType.DEVELOPMENT
        assert session.status == SessionStatus.ACTIVE
        assert session.ticket_id == "TEST-1"
        assert session.pr_number == 42

    def test_start_raises_if_already_active(self, db):
        svc = SessionService(db)
        svc.start(SessionType.DEVELOPMENT)

        with pytest.raises(SessionAlreadyActiveError):
            svc.start(SessionType.REVIEW)

    def test_end_session(self, db):
        svc = SessionService(db)
        svc.start(SessionType.DEVELOPMENT)
        session = svc.end()

        assert session.status == SessionStatus.ENDED
        assert session.ended_at is not None

    def test_end_raises_if_no_active(self, db):
        svc = SessionService(db)

        with pytest.raises(NoActiveSessionError):
            svc.end()

    def test_pause_and_resume(self, db):
        svc = SessionService(db)
        svc.start(SessionType.DEVELOPMENT)

        svc.pause()
        session = svc.get_active()
        assert session.status == SessionStatus.PAUSED

        svc.resume()
        session = svc.get_active()
        assert session.status == SessionStatus.ACTIVE

    def test_pause_raises_if_not_active(self, db):
        svc = SessionService(db)
        svc.start(SessionType.DEVELOPMENT)
        svc.pause()

        with pytest.raises(NoActiveSessionError):
            svc.pause()
