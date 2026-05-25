"""Smoke for auto_refine_discovered.

Patches both the source's fetch (so we don't hit Jira) and refine_async (so
we don't hit Claude). Verifies the manager's wire-up:

  - _run_source inserts 2 items.
  - Because auto_refine_discovered=true, _auto_refine is scheduled per new
    item.
  - One item is recommended 'promote' -> ends up as a Task.
  - Other is 'reject' -> item.status = rejected, no Task.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid as _uuid
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)
os.environ["FLOWTRACK_AUTO_REFINE_DISCOVERED"] = "true"
# Pretend Jira is configured so JiraBacklogSource gets registered (we patch
# search_issues so no real network).
os.environ["FLOWTRACK_JIRA_BASE_URL"] = "https://smoke.example"
os.environ["FLOWTRACK_JIRA_EMAIL"] = "smoke@example"
os.environ["FLOWTRACK_JIRA_TOKEN"] = "smoke-token"

_RUN = _uuid.uuid4().hex[:6]
_PROMOTE_KEY = f"SMOKE-AUTO-A-{_RUN}"
_REJECT_KEY = f"SMOKE-AUTO-B-{_RUN}"

# Patch the Jira client BEFORE the source imports it.
from flowtrack.integrations import jira_client as _jc  # noqa: E402

_FIXTURE = [
    {
        "key": _PROMOTE_KEY,
        "fields": {
            "summary": "Login throws NullPointerException",
            "issuetype": {"name": "Bug"},
            "description": None,
        },
    },
    {
        "key": _REJECT_KEY,
        "fields": {
            "summary": "Make it pop",
            "issuetype": {"name": "Story"},
            "description": None,
        },
    },
]


def _patched_search(self, jql, max_results=50):  # noqa: ARG001
    return _FIXTURE


_jc.JiraClient.search_issues = _patched_search

# Patch the PM agent BEFORE the manager imports it.
from flowtrack.agents import pm as _pm  # noqa: E402


async def _fake_refine(*, title, summary, kind, source, source_ref,
                       raw_payload=None, model="sonnet", timeout_seconds=90):  # noqa: ARG001
    rec = "promote" if "NullPointerException" in title else "reject"
    return _pm.RefinementResult(
        acceptance_criteria="1. Mock acceptance criteria.",
        module_hint="auth" if rec == "promote" else None,
        recommendation=rec,
        cost_usd=Decimal("0.01"),
    )


_pm.refine_async = _fake_refine

from sqlalchemy import select  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.discovery.manager import _auto_refine, _run_source  # noqa: E402
from flowtrack.discovery.sources.jira_backlog import JiraBacklogSource  # noqa: E402
from flowtrack.models import DiscoveredItem, Task  # noqa: E402


async def main() -> int:
    source = JiraBacklogSource()
    new_ids = await asyncio.to_thread(_run_source, source)
    print(f"inserted: {len(new_ids)} items")

    # Drive _auto_refine directly (in production the manager loop kicks them
    # off; here we await them to assert deterministically).
    await asyncio.gather(*[_auto_refine(item_id) for item_id in new_ids])

    db = SessionLocal()
    try:
        items = list(db.scalars(
            select(DiscoveredItem).where(
                DiscoveredItem.source_ref.in_([_PROMOTE_KEY, _REJECT_KEY])
            )
        ))
        by_ref = {i.source_ref: i for i in items}
        promote = by_ref.get(_PROMOTE_KEY)
        reject = by_ref.get(_REJECT_KEY)

        promote_task = None
        if promote and promote.promoted_task_id:
            promote_task = db.get(Task, promote.promoted_task_id)

        print(f"  {_PROMOTE_KEY} status={promote.status.value if promote else 'MISSING'}")
        if promote_task:
            print(f"    -> task id={promote_task.id}")
            print(f"       acceptance_criteria starts with: {(promote_task.acceptance_criteria or '')[:50]!r}")
            print(f"       module_hint={promote_task.module_hint}")
        print(f"  {_REJECT_KEY} status={reject.status.value if reject else 'MISSING'}")

        ok = (
            len(new_ids) == 2
            and promote is not None and promote.status.value == "promoted"
            and promote_task is not None
            and promote_task.acceptance_criteria
            and promote_task.module_hint == "auth"
            and reject is not None and reject.status.value == "rejected"
        )
    finally:
        db.close()

    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
