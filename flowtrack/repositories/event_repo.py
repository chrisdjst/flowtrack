import uuid
from datetime import datetime

from sqlalchemy.orm import Session as DbSession

from flowtrack.models.event import Event, EventType


class EventRepository:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def create(
        self,
        session_id: uuid.UUID,
        event_type: EventType,
        metadata_json: dict | None = None,
    ) -> Event:
        event = Event(
            session_id=session_id,
            event_type=event_type,
            started_at=datetime.now(),
            metadata_json=metadata_json,
        )
        self.db.add(event)
        self.db.flush()
        return event

    def end(self, event: Event) -> Event:
        event.ended_at = datetime.now()
        self.db.flush()
        return event

    def get_active_by_type(
        self, session_id: uuid.UUID, event_type: EventType
    ) -> Event | None:
        return (
            self.db.query(Event)
            .filter(
                Event.session_id == session_id,
                Event.event_type == event_type,
                Event.ended_at.is_(None),
            )
            .first()
        )

    def list_by_session(self, session_id: uuid.UUID) -> list[Event]:
        return (
            self.db.query(Event)
            .filter(Event.session_id == session_id)
            .order_by(Event.started_at)
            .all()
        )

    def list_by_period(self, start: datetime, end: datetime) -> list[Event]:
        return (
            self.db.query(Event)
            .filter(Event.started_at >= start, Event.started_at < end)
            .order_by(Event.started_at)
            .all()
        )
