"""Smoke for SentryIssuesSource. SentryClient.list_issues is monkey-patched.

Verifies:
  1. Issues above sentry_min_event_count become candidates; below get dropped.
  2. Kind inference: level='error' -> BUG, others -> INCIDENT.
  3. signal_score capped at 999 (Numeric(5,2) limit).
  4. Idempotent re-runs via UNIQUE(source, source_ref).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid as _uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)
# Pretend Sentry creds are set so SentryClient.is_configured passes (we don't
# actually hit the network — list_issues is patched out).
os.environ["FLOWTRACK_SENTRY_TOKEN"] = "smoke-token"
os.environ["FLOWTRACK_SENTRY_ORG"] = "smoke-org"
os.environ["FLOWTRACK_SENTRY_PROJECT"] = "smoke-project"
os.environ["FLOWTRACK_SENTRY_MIN_EVENT_COUNT"] = "3"

from flowtrack.integrations import sentry_client as _sc  # noqa: E402

_RUN = _uuid.uuid4().hex[:6]
_FIXTURE = [
    {  # loud error -> BUG, signal_score >= min
        "id": f"100-{_RUN}",
        "shortId": f"FLOW-{_RUN}-A",
        "title": f"NullPointerException in login flow ({_RUN})",
        "level": "error",
        "count": 42,
        "metadata": {"value": "Cannot read property 'id' of null"},
        "culprit": "auth.controllers.login_controller in handle",
    },
    {  # warning -> INCIDENT, high count clamped to 999
        "id": f"200-{_RUN}",
        "shortId": f"FLOW-{_RUN}-B",
        "title": f"Slow query detected ({_RUN})",
        "level": "warning",
        "count": 12345,
        "metadata": {"value": "query took > 5s"},
        "culprit": "db.queries.fetch_recent",
    },
    {  # tiny count -> dropped
        "id": f"300-{_RUN}",
        "shortId": f"FLOW-{_RUN}-C",
        "title": f"Rare blip ({_RUN})",
        "level": "error",
        "count": 1,  # below min_event_count=3
        "metadata": {"value": "Once-off"},
    },
]


def _patched_list(self, *, query=None, stats_period=None, limit=50):  # noqa: ARG001
    return _FIXTURE


_sc.SentryClient.list_issues = _patched_list


from sqlalchemy import select  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.discovery.manager import _run_source  # noqa: E402
from flowtrack.discovery.sources.sentry import SentryIssuesSource  # noqa: E402
from flowtrack.models import DiscoveredItem  # noqa: E402
from flowtrack.models.discovered_item import DiscoveryKind  # noqa: E402


async def main() -> int:
    source = SentryIssuesSource()
    first = await asyncio.to_thread(_run_source, source)
    second = await asyncio.to_thread(_run_source, source)
    print(f"first run added: {first}; second: {second} (expect 2 then 0; tiny dropped)")

    db = SessionLocal()
    try:
        items = list(db.scalars(
            select(DiscoveredItem).where(DiscoveredItem.source_ref.in_(
                [f"FLOW-{_RUN}-A", f"FLOW-{_RUN}-B", f"FLOW-{_RUN}-C"]
            ))
        ))
        by_ref = {i.source_ref: i for i in items}
        bug = by_ref.get(f"FLOW-{_RUN}-A")
        warn = by_ref.get(f"FLOW-{_RUN}-B")
        tiny = by_ref.get(f"FLOW-{_RUN}-C")

        print(f"  A error (count=42)    -> kind={bug.kind.value if bug else 'MISSING'} "
              f"score={bug.signal_score if bug else None}  (expect bug, 42)")
        print(f"  B warning (count=12345) -> kind={warn.kind.value if warn else 'MISSING'} "
              f"score={warn.signal_score if warn else None}  (expect incident, 999)")
        print(f"  C tiny (count=1)      -> present? {tiny is not None}  (expect False)")

        ok = (
            first == 2
            and second == 0
            and bug is not None and bug.kind == DiscoveryKind.BUG
            and warn is not None and warn.kind == DiscoveryKind.INCIDENT
            and int(warn.signal_score) == 999       # clamped
            and int(bug.signal_score) == 42
            and tiny is None
        )
    finally:
        db.close()

    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
