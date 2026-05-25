"""Discovery scheduler.

Runs each registered source on its own interval, persists candidates as
``discovered_items`` with ON CONFLICT DO NOTHING so re-runs are idempotent.
Each insert publishes a ``discovered_item_added`` event for the frontend.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.dialects.postgresql import insert as pg_insert

from flowtrack.api.events import broker
from flowtrack.core.database import SessionLocal
from flowtrack.core.settings import settings
from flowtrack.discovery.base import DiscoveryCandidate, DiscoveryWorker
from flowtrack.discovery.sources.github_issues import GitHubIssuesSource
from flowtrack.discovery.sources.jira_backlog import JiraBacklogSource
from flowtrack.discovery.sources.sentry import SentryIssuesSource
from flowtrack.models import DiscoveredItem

log = logging.getLogger(__name__)


def default_sources() -> list[DiscoveryWorker]:
    """Return the list of sources to run, gated by config + creds.

    We don't instantiate sources that aren't usable — Jira without creds would
    just hammer ``search_issues`` returning empty lists, which adds noise.
    """
    sources: list[DiscoveryWorker] = []
    if settings.jira_base_url and settings.jira_email and settings.jira_token:
        sources.append(JiraBacklogSource())
    if settings.github_token and settings.github_owner and settings.github_repo:
        sources.append(GitHubIssuesSource())
    if settings.sentry_token and settings.sentry_org and settings.sentry_project:
        sources.append(SentryIssuesSource())
    return sources


async def run_discovery_manager(stop: asyncio.Event) -> None:
    sources = default_sources()
    if not sources:
        log.info("discovery: no configured sources, skipping manager loop")
        await stop.wait()
        return

    log.info("discovery manager started; sources=%s", [s.name for s in sources])
    last_run: dict[str, float] = {s.name: 0.0 for s in sources}

    while not stop.is_set():
        now = time.monotonic()
        for source in sources:
            if now - last_run[source.name] < source.interval_seconds:
                continue
            try:
                added = await asyncio.to_thread(_run_source, source)
                if added:
                    log.info("discovery %s: %d new items", source.name, added)
            except Exception:
                log.exception("discovery source %s crashed; continuing", source.name)
            last_run[source.name] = now

        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
    log.info("discovery manager stopped")


def _run_source(source: DiscoveryWorker) -> int:
    candidates: list[DiscoveryCandidate] = source.fetch()
    if not candidates:
        return 0

    db = SessionLocal()
    added = 0
    try:
        for c in candidates:
            stmt = pg_insert(DiscoveredItem.__table__).values(
                source=c.source.value,
                source_ref=c.source_ref,
                kind=c.kind.value,
                title=c.title,
                summary=c.summary,
                raw_payload=c.raw_payload,
                signal_score=c.signal_score,
                status="new",
            ).on_conflict_do_nothing(constraint="uq_discovered_source_ref")
            result = db.execute(stmt)
            if result.rowcount:
                added += 1
                broker.publish_sync("discovered_item_added", {
                    "source": c.source.value,
                    "source_ref": c.source_ref,
                    "kind": c.kind.value,
                    "title": c.title,
                })
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return added
