class FlowTrackError(Exception):
    pass


class NoActiveSessionError(FlowTrackError):
    def __init__(self) -> None:
        super().__init__("No active session found.")


class SessionAlreadyActiveError(FlowTrackError):
    def __init__(self, session_type: str) -> None:
        super().__init__(f"A {session_type} session is already active.")


class NoActiveEventError(FlowTrackError):
    def __init__(self, event_type: str) -> None:
        super().__init__(f"No active {event_type} event found.")


class EventAlreadyActiveError(FlowTrackError):
    def __init__(self, event_type: str) -> None:
        super().__init__(f"A {event_type} event is already active.")


class NoActiveIncidentError(FlowTrackError):
    def __init__(self) -> None:
        super().__init__("No active incident found.")


class ConfigNotFoundError(FlowTrackError):
    def __init__(self, key: str) -> None:
        super().__init__(f"Config key '{key}' not found.")
