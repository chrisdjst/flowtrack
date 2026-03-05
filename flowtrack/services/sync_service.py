from datetime import datetime

from sqlalchemy.orm import Session as DbSession

from flowtrack.integrations.github_client import GitHubClient
from flowtrack.integrations.jira_client import JiraClient
from flowtrack.models.event import EventType
from flowtrack.models.session import Session
from flowtrack.repositories.event_repo import EventRepository


class SyncService:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.event_repo = EventRepository(db)
        self.github = GitHubClient()
        self.jira = JiraClient()

    def sync_session(self, session: Session) -> dict[str, bool]:
        summary = self._build_summary(session)
        results: dict[str, bool] = {"github": False, "jira": False}

        if session.pr_number:
            results["github"] = self.github.post_comment(session.pr_number, summary)

        if session.ticket_id:
            results["jira"] = self.jira.post_comment(session.ticket_id, summary)

        return results

    def _build_summary(self, session: Session) -> str:
        events = self.event_repo.list_by_session(session.id)
        now = datetime.now()
        session_end = session.ended_at or now
        total_mins = (session_end - session.started_at).total_seconds() / 60

        blocks = [e for e in events if e.event_type == EventType.BLOCK_START]
        interrupts = [e for e in events if e.event_type == EventType.INTERRUPT_START]

        block_mins = sum(
            ((e.ended_at or now) - e.started_at).total_seconds() / 60 for e in blocks
        )
        interrupt_mins = sum(
            ((e.ended_at or now) - e.started_at).total_seconds() / 60 for e in interrupts
        )
        active_mins = total_mins - block_mins - interrupt_mins

        lines = [
            f"## FlowTrack - {session.type.value.title()} Session",
            f"- **Duration:** {total_mins:.0f} min",
            f"- **Active time:** {active_mins:.0f} min",
            f"- **Blocks:** {len(blocks)} ({block_mins:.0f} min)",
            f"- **Interrupts:** {len(interrupts)} ({interrupt_mins:.0f} min)",
        ]

        if session.ticket_id:
            lines.append(f"- **Ticket:** {session.ticket_id}")
        if session.pr_number:
            lines.append(f"- **PR:** #{session.pr_number}")

        return "\n".join(lines)
