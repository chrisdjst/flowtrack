"""Minimal Sentry REST client used by the discovery source.

Different concern from ``flowtrack/core/sentry.py`` (which uses sentry-sdk to
SEND errors). This module READS issues via the HTTP API. Two distinct auth
artifacts:
  - DSN          → sentry-sdk (outbound errors)
  - Auth Token   → REST API (inbound discovery)
"""

from __future__ import annotations

import logging

import httpx

from flowtrack.core.settings import settings

log = logging.getLogger(__name__)


class SentryClient:
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.sentry_token}",
            "Accept": "application/json",
        }

    def is_configured(self) -> bool:
        return bool(
            settings.sentry_token and settings.sentry_org and settings.sentry_project
        )

    def list_issues(
        self,
        *,
        query: str | None = None,
        stats_period: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return unresolved issues for the configured project.

        Empty list on misconfig / non-200 / network error. The Sentry response
        already includes ``count`` (total events for the issue) which we use as
        the signal score.
        """
        if not self.is_configured():
            return []
        url = (
            f"{settings.sentry_api_base.rstrip('/')}"
            f"/api/0/projects/{settings.sentry_org}/{settings.sentry_project}/issues/"
        )
        params: dict[str, str | int] = {
            "query": query or settings.sentry_discovery_query,
            "statsPeriod": stats_period or settings.sentry_discovery_stats_period,
            "limit": limit,
        }
        try:
            response = httpx.get(url, params=params, headers=self._headers(), timeout=15)
        except httpx.HTTPError as e:
            log.warning("sentry list_issues HTTP error: %s", e)
            return []
        if response.status_code != 200:
            log.warning(
                "sentry list_issues failed: status=%d body=%s",
                response.status_code, response.text[:200],
            )
            return []
        return response.json() or []
