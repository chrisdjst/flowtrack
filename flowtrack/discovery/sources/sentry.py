"""Pull unresolved Sentry issues into the discovery inbox.

Strategy: poll the project's issues endpoint with a configurable query
(default ``is:unresolved``) and statsPeriod (default 24h). Each unresolved
issue becomes a DiscoveryCandidate with signal_score = event ``count`` so
the kanban can show "how loud is this bug".

Drops issues below ``settings.sentry_min_event_count`` so one-off blips
don't generate task noise.

Kind inference: ``error`` level → BUG, others → INCIDENT.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from flowtrack.core.settings import settings
from flowtrack.discovery.base import DiscoveryCandidate
from flowtrack.integrations.sentry_client import SentryClient
from flowtrack.models.discovered_item import DiscoveryKind, DiscoverySource

log = logging.getLogger(__name__)


def _infer_kind(level: str) -> DiscoveryKind:
    return DiscoveryKind.BUG if (level or "").lower() == "error" else DiscoveryKind.INCIDENT


def _summary_for(issue: dict) -> str | None:
    """Build a short summary line: top frame + culprit, falling back to message."""
    meta = issue.get("metadata") or {}
    culprit = issue.get("culprit") or ""
    val = meta.get("value") or meta.get("title") or ""
    parts = [p for p in (val, culprit) if p]
    if not parts:
        return None
    return " | ".join(parts)[:1000]


class SentryIssuesSource:
    name = "sentry"
    interval_seconds = 600  # 10 min

    def __init__(
        self,
        *,
        client: SentryClient | None = None,
        min_event_count: int | None = None,
    ) -> None:
        self.client = client or SentryClient()
        self.min_event_count = (
            min_event_count
            if min_event_count is not None
            else settings.sentry_min_event_count
        )

    def fetch(self) -> list[DiscoveryCandidate]:
        issues = self.client.list_issues()
        candidates: list[DiscoveryCandidate] = []
        for issue in issues:
            issue_id = issue.get("id") or issue.get("shortId")
            if not issue_id:
                continue
            try:
                count = int(issue.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if count < self.min_event_count:
                continue
            candidates.append(DiscoveryCandidate(
                source=DiscoverySource.SENTRY,
                source_ref=str(issue.get("shortId") or issue_id),
                kind=_infer_kind(issue.get("level", "")),
                title=issue.get("title") or f"(no title) {issue_id}",
                summary=_summary_for(issue),
                raw_payload=issue,
                # signal_score = event count, capped at 999 (Numeric(5,2)
                # column has range -999.99..999.99).
                signal_score=Decimal(min(count, 999)),
            ))
        log.info("sentry: %d candidates from %d raw issues", len(candidates), len(issues))
        return candidates
