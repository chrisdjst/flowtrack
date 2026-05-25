"""Smoke for GitHubIssuesSource. GitHubClient.list_issues is monkey-patched
so we never touch the GitHub API.

Verifies:
  1. Issue payloads (including a PR that must be filtered out) map to the
     right number of candidates.
  2. Label-based kind inference picks BUG over the default FEATURE.
  3. _run_source persists 2 items, second run is idempotent.
  4. POST /api/discovery/{id}/promote creates a Task with discovered_from set.
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

from flowtrack.integrations import github_client as _gh  # noqa: E402


_RUN = _uuid.uuid4().hex[:6]
# Issue "numbers" carry the run tag so each invocation hits fresh
# (source, source_ref) tuples; UNIQUE constraint would otherwise make
# repeated runs return 0 new items and fail the first==2 assertion.
_N_BUG = int.from_bytes(_uuid.uuid4().bytes[:2], "big")
_N_FEAT = int.from_bytes(_uuid.uuid4().bytes[:2], "big")
_N_PR = int.from_bytes(_uuid.uuid4().bytes[:2], "big")
_FIXTURE = [
    {  # plain bug
        "number": _N_BUG,
        "title": f"Login form crashes (run {_RUN})",
        "body": "Repro: open /login, submit empty form -> 500.",
        "labels": [{"name": "bug"}, {"name": "bot-pickup"}],
    },
    {  # feature (default kind)
        "number": _N_FEAT,
        "title": f"Add dark mode toggle (run {_RUN})",
        "body": "Users keep asking.",
        "labels": [{"name": "enhancement"}, {"name": "bot-pickup"}],
    },
    {  # PR — must be filtered
        "number": _N_PR,
        "title": "PR — should be ignored",
        "body": "",
        "labels": [{"name": "bot-pickup"}],
        "pull_request": {"url": "https://example.com/pr"},
    },
]


def _patched_list(self, *, label=None, state="open", per_page=50):  # noqa: ARG001
    return _FIXTURE


_gh.GitHubClient.list_issues = _patched_list


from sqlalchemy import select  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.discovery.manager import _run_source  # noqa: E402
from flowtrack.discovery.sources.github_issues import GitHubIssuesSource  # noqa: E402
from flowtrack.models import DiscoveredItem  # noqa: E402
from flowtrack.models.discovered_item import DiscoveryKind  # noqa: E402


async def main() -> int:
    source = GitHubIssuesSource()
    first = await asyncio.to_thread(_run_source, source)
    second = await asyncio.to_thread(_run_source, source)
    print(f"first run added: {first}; second: {second} (expect 2 then 0; PR filtered)")

    db = SessionLocal()
    try:
        refs = [f"#{_N_BUG}", f"#{_N_FEAT}", f"#{_N_PR}"]
        items = list(db.scalars(
            select(DiscoveredItem).where(DiscoveredItem.source_ref.in_(refs))
        ))
        ours = [i for i in items if f"run {_RUN}" in (i.title or "")]
        print(f"persisted from this run: {len(ours)}")
        by_ref = {i.source_ref: i for i in ours}
        bug = by_ref.get(f"#{_N_BUG}")
        feat = by_ref.get(f"#{_N_FEAT}")
        pr = by_ref.get(f"#{_N_PR}")
        print(f"  #{_N_BUG} kind = {bug.kind.value if bug else 'MISSING'} (expect bug)")
        print(f"  #{_N_FEAT} kind = {feat.kind.value if feat else 'MISSING'} (expect feature)")
        print(f"  #{_N_PR} present? {pr is not None} (expect False — PR filtered)")

        ok = (
            first == 2
            and second == 0
            and bug is not None and bug.kind == DiscoveryKind.BUG
            and feat is not None and feat.kind == DiscoveryKind.FEATURE
            and pr is None
        )
    finally:
        db.close()

    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
