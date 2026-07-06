"""Mirrors FlowTrack task status changes to Jira column transitions."""
from __future__ import annotations

import logging

from flowtrack.integrations.jira_client import JiraClient

log = logging.getLogger(__name__)

# Ordered candidate list per FlowTrack status — first match wins.
# Covers the most common Jira workflow names across Simple, Scrum, and Kanban boards.
_CANDIDATES: dict[str, list[str]] = {
    "todo":        ["To Do", "Backlog", "Open", "New"],
    "in_progress": ["In Progress", "In Development", "Development", "Doing"],
    "in_review":   ["In Review", "Code Review", "Review", "Under Review", "Peer Review"],
    "in_qa":       ["Testing", "QA", "In QA", "Test", "Quality Assurance"],
    "done":        ["Done", "Closed", "Resolved", "Complete", "Merged"],
    "blocked":     ["Blocked", "On Hold", "Impediment", "Waiting"],
}

_jira = JiraClient()


def push_task_status(ticket_id: str | None, flowtrack_status: str) -> None:
    """Transition a Jira issue to the column that matches *flowtrack_status*.

    Silently no-ops when Jira is not configured or the ticket has no matching
    transition (e.g. the project workflow doesn't have a "Blocked" column).
    """
    if not ticket_id:
        return
    candidates = _CANDIDATES.get(flowtrack_status)
    if not candidates:
        return
    ok = _jira.transition_issue(ticket_id, *candidates)
    if ok:
        log.info("jira sync: %s -> %s", ticket_id, flowtrack_status)
    else:
        log.warning(
            "jira sync: no matching transition for %s (status=%s, tried=%s)",
            ticket_id, flowtrack_status, candidates,
        )
