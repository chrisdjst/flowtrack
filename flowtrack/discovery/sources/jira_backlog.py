"""Pull Jira issues into the discovery inbox.

JQL filter is configurable; the default ``labels = "auto"`` is a conservative
"only what you've explicitly marked" approach. Tune via settings or replace JQL
entirely.

Issue kind inference is intentionally simple — issuetype lowered, mapped to our
enum, fallback to FEATURE. Tighten when you see real data.
"""

from __future__ import annotations

import logging

from flowtrack.core.settings import settings
from flowtrack.discovery.base import DiscoveryCandidate
from flowtrack.integrations.jira_client import JiraClient
from flowtrack.models.discovered_item import DiscoveryKind, DiscoverySource

log = logging.getLogger(__name__)


_KIND_BY_ISSUETYPE = {
    "bug": DiscoveryKind.BUG,
    "task": DiscoveryKind.FEATURE,
    "story": DiscoveryKind.FEATURE,
    "improvement": DiscoveryKind.IMPROVEMENT,
    "tech debt": DiscoveryKind.TECH_DEBT,
    "incident": DiscoveryKind.INCIDENT,
}


def _adf_to_text(adf: object) -> str | None:
    """Flatten an Atlassian Document Format payload to plain text.

    We only persist a short summary, so naive recursion is fine. Returns None
    when the input is empty/unstructured.
    """
    if adf is None:
        return None
    if isinstance(adf, str):
        return adf
    if isinstance(adf, dict):
        out: list[str] = []
        if "text" in adf and isinstance(adf["text"], str):
            out.append(adf["text"])
        for child in adf.get("content", []) or []:
            sub = _adf_to_text(child)
            if sub:
                out.append(sub)
        joined = " ".join(out).strip()
        return joined or None
    if isinstance(adf, list):
        out = []
        for child in adf:
            sub = _adf_to_text(child)
            if sub:
                out.append(sub)
        return " ".join(out).strip() or None
    return None


def _infer_kind(fields: dict) -> DiscoveryKind:
    issuetype = (fields.get("issuetype") or {}).get("name", "").lower()
    return _KIND_BY_ISSUETYPE.get(issuetype, DiscoveryKind.FEATURE)


class JiraBacklogSource:
    name = "jira_backlog"
    interval_seconds = 600  # 10 min — chatty but cheap

    def __init__(self, label: str | None = None, client: JiraClient | None = None) -> None:
        self.label = label or settings.jira_discovery_label
        self.client = client or JiraClient()

    def fetch(self) -> list[DiscoveryCandidate]:
        jql = f'labels = "{self.label}" AND statusCategory != Done ORDER BY created DESC'
        issues = self.client.search_issues(jql)
        candidates: list[DiscoveryCandidate] = []
        for issue in issues:
            key = issue.get("key")
            fields = issue.get("fields") or {}
            if not key:
                continue
            candidates.append(DiscoveryCandidate(
                source=DiscoverySource.JIRA,
                source_ref=key,
                kind=_infer_kind(fields),
                title=fields.get("summary") or f"(no summary) {key}",
                summary=_adf_to_text(fields.get("description")),
                raw_payload=issue,
                signal_score=None,
            ))
        log.info("jira_backlog: %d candidates from JQL: %s", len(candidates), jql)
        return candidates
