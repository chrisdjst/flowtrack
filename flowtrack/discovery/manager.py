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

from sqlalchemy import select
from uuid import UUID

from flowtrack.api.events import broker
from flowtrack.core.database import SessionLocal
from flowtrack.core.runtime_config import RuntimeConfig
from flowtrack.core.settings import settings
from flowtrack.discovery.base import DiscoveryCandidate, DiscoveryWorker
from flowtrack.discovery.promote import promote_item, reject_item
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
    if RuntimeConfig.get("jira_base_url") and RuntimeConfig.get("jira_email") and RuntimeConfig.get("jira_token"):
        sources.append(JiraBacklogSource())
    if RuntimeConfig.get("github_token") and RuntimeConfig.get("github_owner") and RuntimeConfig.get("github_repo"):
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
                new_ids = await asyncio.to_thread(_run_source, source)
                if new_ids:
                    log.info("discovery %s: %d new items", source.name, len(new_ids))
                    if RuntimeConfig.get("auto_refine_discovered"):
                        for item_id in new_ids:
                            asyncio.create_task(
                                _auto_refine(item_id), name=f"pm-refine-{item_id}"
                            )
            except Exception:
                log.exception("discovery source %s crashed; continuing", source.name)
            last_run[source.name] = now

        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
    log.info("discovery manager stopped")


def _run_source(source: DiscoveryWorker) -> list[UUID]:
    """Persist new candidates. Returns IDs of rows actually inserted."""
    candidates: list[DiscoveryCandidate] = source.fetch()
    if not candidates:
        return []

    db = SessionLocal()
    new_ids: list[UUID] = []
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
                # Re-query for the freshly-inserted row's id (RETURNING isn't
                # straightforward through on_conflict_do_nothing).
                fresh = db.scalar(
                    select(DiscoveredItem).where(
                        DiscoveredItem.source == c.source,
                        DiscoveredItem.source_ref == c.source_ref,
                    )
                )
                if fresh:
                    new_ids.append(fresh.id)
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
    return new_ids


async def _auto_refine(item_id: UUID) -> None:
    """Refine a fresh DiscoveredItem and promote/reject per PM recommendation.

    Runs as a fire-and-forget asyncio task. Failures are logged, never raised
    out (so a flaky PM call doesn't poison the manager loop).
    """
    # Lazy import — agents.pm pulls in claude subprocess machinery; not worth
    # paying that at module import time for the no-op path.
    from flowtrack.agents.pm import refine_async

    db = SessionLocal()
    try:
        item = db.get(DiscoveredItem, item_id)
        if item is None:
            log.warning("auto_refine: item %s not found", item_id)
            return
        # Snapshot fields the agent needs (avoids passing detached ORM across
        # the await — same pattern as spawner._SpawnContext).
        snapshot = {
            "title": item.title,
            "summary": item.summary,
            "kind": item.kind.value,
            "source": item.source.value,
            "source_ref": item.source_ref or "",
            "raw_payload": item.raw_payload,
        }
    finally:
        db.close()

    try:
        result = await refine_async(**snapshot)
    except Exception:
        log.exception("auto_refine: PM call failed for item %s; leaving item NEW", item_id)
        return

    db = SessionLocal()
    try:
        item = db.get(DiscoveredItem, item_id)
        if item is None:
            return
        try:
            if result.recommendation == "promote":
                task = promote_item(
                    db, item,
                    acceptance_criteria=result.acceptance_criteria,
                    module_hint=result.module_hint,
                )
                broker.publish_sync("discovered_item_promoted", {
                    "item_id": str(item.id),
                    "task_id": str(task.id),
                    "title": task.title,
                    "by": "auto_refine",
                })
            elif result.recommendation == "reject":
                reject_item(db, item, reason="auto_refine: PM rejected")
                broker.publish_sync("discovered_item_rejected", {
                    "item_id": str(item.id),
                    "title": item.title,
                    "by": "auto_refine",
                })
        except ValueError:
            # Race: someone already promoted/rejected manually. Fine.
            log.info("auto_refine: item %s already resolved, skipping", item_id)
        db.commit()
    finally:
        db.close()
