"""Pull GitHub issues into the discovery inbox.

Filters by a single label (default ``bot-pickup``). Excludes PRs — GitHub's
``/issues`` endpoint returns both, but a PR row has a ``pull_request`` key
that a plain issue doesn't.

Kind inference walks the issue's labels:
  - ``bug`` / ``defect``         → BUG
  - ``enhancement`` / ``feature`` → FEATURE
  - ``tech-debt`` / ``cleanup``  → TECH_DEBT
  - ``incident`` / ``outage``    → INCIDENT
  - everything else              → FEATURE
"""

from __future__ import annotations

import logging

from flowtrack.core.settings import settings
from flowtrack.discovery.base import DiscoveryCandidate
from flowtrack.integrations.github_client import GitHubClient
from flowtrack.models.discovered_item import DiscoveryKind, DiscoverySource

log = logging.getLogger(__name__)


_LABEL_TO_KIND: dict[str, DiscoveryKind] = {
    "bug": DiscoveryKind.BUG,
    "defect": DiscoveryKind.BUG,
    "enhancement": DiscoveryKind.FEATURE,
    "feature": DiscoveryKind.FEATURE,
    "tech-debt": DiscoveryKind.TECH_DEBT,
    "cleanup": DiscoveryKind.TECH_DEBT,
    "incident": DiscoveryKind.INCIDENT,
    "outage": DiscoveryKind.INCIDENT,
    "improvement": DiscoveryKind.IMPROVEMENT,
}


def _infer_kind(labels: list[dict]) -> DiscoveryKind:
    for label in labels:
        name = (label.get("name") or "").lower().strip()
        if name in _LABEL_TO_KIND:
            return _LABEL_TO_KIND[name]
    return DiscoveryKind.FEATURE


class GitHubIssuesSource:
    name = "github_issues"
    interval_seconds = 900  # 15 min — GitHub rate limits favour calm

    def __init__(self, label: str | None = None, client: GitHubClient | None = None) -> None:
        self.label = label or settings.github_discovery_label
        self.client = client or GitHubClient()

    def fetch(self) -> list[DiscoveryCandidate]:
        issues = self.client.list_issues(label=self.label, state="open")
        candidates: list[DiscoveryCandidate] = []
        for issue in issues:
            # Skip PRs: GitHub returns them through /issues but they have this key.
            if issue.get("pull_request") is not None:
                continue
            number = issue.get("number")
            if number is None:
                continue
            candidates.append(DiscoveryCandidate(
                source=DiscoverySource.GITHUB_ISSUE,
                source_ref=f"#{number}",
                kind=_infer_kind(issue.get("labels") or []),
                title=issue.get("title") or f"(no title) #{number}",
                summary=(issue.get("body") or "").strip()[:2000] or None,
                raw_payload=issue,
                signal_score=None,
            ))
        log.info("github_issues: %d candidates (label=%s)", len(candidates), self.label)
        return candidates
