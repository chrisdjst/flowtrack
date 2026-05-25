"""Smoke test for Jira discovery + promote.

JiraClient is monkey-patched to return a fixed list of issues so we never hit
the network. Verifies:
  1. JiraBacklogSource.fetch() maps Jira payloads -> DiscoveryCandidate.
  2. manager._run_source persists 2 distinct items.
  3. Re-running the same source is idempotent (UNIQUE(source, source_ref)).
  4. GET /api/discovery returns the 'new' items.
  5. POST /api/discovery/{id}/promote creates a Task with discovered_from set.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import uuid as _uuid
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


# Patch JiraClient BEFORE the source class imports it.
from flowtrack.integrations import jira_client as _jc  # noqa: E402


_FIXTURE = [
    {
        "key": f"SMOKE-DISC-A-{_uuid.uuid4().hex[:6]}",
        "fields": {
            "summary": "Fix the broken login flow",
            "issuetype": {"name": "Bug"},
            "description": {"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Repro: log in -> 500."}]},
            ]},
        },
    },
    {
        "key": f"SMOKE-DISC-B-{_uuid.uuid4().hex[:6]}",
        "fields": {
            "summary": "Add dark mode to kanban",
            "issuetype": {"name": "Story"},
            "description": None,
        },
    },
]


def _patched_search(self, jql: str, max_results: int = 50):  # noqa: ARG001
    return _FIXTURE


_jc.JiraClient.search_issues = _patched_search


# Re-import the source so it picks up the patch through default JiraClient.
from sqlalchemy import select  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.discovery.manager import _run_source  # noqa: E402
from flowtrack.discovery.sources.jira_backlog import JiraBacklogSource  # noqa: E402
from flowtrack.models import DiscoveredItem, Task  # noqa: E402


async def main() -> int:
    source = JiraBacklogSource()
    # Run twice — second should be a no-op (idempotent).
    first = await asyncio.to_thread(_run_source, source)
    second = await asyncio.to_thread(_run_source, source)
    print(f"first run added: {first}; second run added: {second} (expect 2 then 0)")

    # Quickly stand up the API so we can hit /api/discovery.
    port = _free_port()
    env = {**os.environ, "FLOWTRACK_API_PORT": str(port),
           "FLOWTRACK_ORCHESTRATOR_DRY_RUN": "true",
           "FLOWTRACK_WORKER_ID": f"smoke-disc-{_uuid.uuid4().hex[:8]}"}
    server = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "flowtrack.api.app",
        cwd=str(REPO), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            await asyncio.sleep(0.2)
            try:
                async with httpx.AsyncClient(timeout=1.0) as c:
                    if (await c.get(f"{base}/healthz")).status_code == 200:
                        break
            except Exception:
                continue
        else:
            print("ERROR: server did not become healthy"); return 1

        async with httpx.AsyncClient(timeout=5.0) as c:
            items = (await c.get(f"{base}/api/discovery")).json()
            ours = [i for i in items if i["source_ref"].startswith("SMOKE-DISC-")]
            print(f"GET /api/discovery: {len(ours)} matching items returned")

            # Promote the first one.
            target = ours[0]
            r = await c.post(f"{base}/api/discovery/{target['id']}/promote")
            r.raise_for_status()
            new_task_id = r.json()["task_id"]
            print(f"promoted -> task {new_task_id}")

            # Re-promote should now 409.
            r2 = await c.post(f"{base}/api/discovery/{target['id']}/promote")
            print(f"re-promote: status {r2.status_code} (expect 409)")
    finally:
        server.terminate()
        try:
            await asyncio.wait_for(server.wait(), timeout=5)
        except asyncio.TimeoutError:
            server.kill()
            await server.wait()

    # Verify the task row is linked.
    db = SessionLocal()
    try:
        task = db.get(Task, _uuid.UUID(new_task_id))
        item = db.scalar(
            select(DiscoveredItem).where(DiscoveredItem.id == _uuid.UUID(target["id"]))
        )
        print()
        print(f"task.discovered_from = {task.discovered_from}")
        print(f"task.ticket_id       = {task.ticket_id}")
        print(f"item.status          = {item.status.value}")
        print(f"item.promoted_task_id= {item.promoted_task_id}")

        ok = (
            first == 2
            and second == 0
            and len(ours) >= 2  # could be more if other smokes seeded items
            and r2.status_code == 409
            and task.discovered_from == item.id
            and item.status.value == "promoted"
            and item.promoted_task_id == task.id
        )
    finally:
        db.close()

    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
