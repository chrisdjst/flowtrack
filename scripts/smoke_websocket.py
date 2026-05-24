"""Smoke test for /ws: subscribe, queue a job, count events.

Starts the FastAPI server in a background subprocess (because uvicorn + the
lifespan-managed background tasks behave best in their own process). Connects
a WebSocket client, queues a dev job via /api/tasks/{id}/assign, runs until
the chain completes, prints the events received per type.

Asserts at least: 1 task_transitioned, 1 instance_finalized, multiple
instance_event entries.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import uuid as _uuid
from collections import Counter
from pathlib import Path

import httpx
import websockets

REPO = Path(__file__).resolve().parents[1]
MOCK = REPO / "scripts" / "mock_claude.py"
WORKTREES = REPO / ".smoke-worktrees"

WORKER_ID = f"smoke-ws-{_uuid.uuid4().hex[:8]}"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def cleanup_branches() -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "prune", cwd=str(REPO),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    proc = await asyncio.create_subprocess_exec(
        "git", "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/",
        cwd=str(REPO),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    for branch in out.decode().splitlines():
        branch = branch.strip()
        if branch:
            p = await asyncio.create_subprocess_exec(
                "git", "branch", "-D", branch, cwd=str(REPO),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
    if WORKTREES.exists():
        shutil.rmtree(WORKTREES, ignore_errors=True)


async def main() -> int:
    await cleanup_branches()

    port = _free_port()
    env = {
        **os.environ,
        "FLOWTRACK_DATABASE_URL": "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
        "FLOWTRACK_ORCHESTRATOR_DRY_RUN": "false",
        "FLOWTRACK_MAX_CONCURRENT_INSTANCES": "1",
        "FLOWTRACK_ORCHESTRATOR_LOOP_INTERVAL_SECONDS": "0.3",
        "FLOWTRACK_CLAUDE_EXECUTABLE": f'"{sys.executable}" "{MOCK}"',
        "FLOWTRACK_TARGET_REPO_PATH": str(REPO),
        "FLOWTRACK_WORKTREE_ROOT": str(WORKTREES),
        "FLOWTRACK_API_PORT": str(port),
        "FLOWTRACK_WORKER_ID": WORKER_ID,
    }

    # Launch the server.
    server = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "flowtrack.api.app",
        cwd=str(REPO),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws"

    try:
        # Wait for /healthz.
        for _ in range(50):
            await asyncio.sleep(0.2)
            try:
                async with httpx.AsyncClient(timeout=1.0) as c:
                    r = await c.get(f"{base_url}/healthz")
                    if r.status_code == 200:
                        break
            except Exception:
                continue
        else:
            print("ERROR: server did not become healthy")
            return 1

        # Subscribe to WS BEFORE creating the task so we don't miss events.
        events: list[dict] = []

        async def reader(ws):
            try:
                async for msg in ws:
                    events.append(json.loads(msg))
            except Exception:
                pass

        async with websockets.connect(ws_url) as ws:
            reader_task = asyncio.create_task(reader(ws))
            await asyncio.sleep(0.2)  # let server register us

            # Insert task + queue dev job via the API.
            async with httpx.AsyncClient(timeout=5.0) as c:
                # We don't have POST /api/tasks yet — write directly via psql isn't possible
                # from here. Use the DB instead, then hit the assign endpoint.
                from sqlalchemy import select  # noqa: E402  (lazy to keep top imports clean)
                from flowtrack.core.database import SessionLocal
                from flowtrack.models import Role, Task
                from flowtrack.models.task import TaskPriority, TaskStatus

                db = SessionLocal()
                try:
                    dev_role = db.scalar(select(Role).where(Role.name == "dev"))
                    task = Task(
                        title="WS smoke",
                        status=TaskStatus.TODO,
                        priority=TaskPriority.HIGH,
                        ticket_id="SMOKE-WS",
                        module_hint="smoke-ws",
                        acceptance_criteria="Events should flow.",
                    )
                    db.add(task)
                    db.flush()
                    task_id = str(task.id)
                    db.commit()
                finally:
                    db.close()

                r = await c.post(
                    f"{base_url}/api/tasks/{task_id}/assign",
                    json={"role_name": "dev", "priority": 10, "worker_id": WORKER_ID},
                )
                r.raise_for_status()
                print(f"queued dev job for task={task_id} worker={WORKER_ID}")

                # Wait for pipeline completion (poll the kanban).
                deadline = 60.0
                waited = 0.0
                done = False
                while waited < deadline:
                    await asyncio.sleep(0.5)
                    waited += 0.5
                    board = (await c.get(f"{base_url}/api/kanban")).json()
                    if any(t["id"] == task_id for t in board["merged"]):
                        done = True
                        break

            # Give the broker a moment to drain the final events.
            await asyncio.sleep(1.0)
            reader_task.cancel()

        # Summarize received events.
        by_type = Counter(e["type"] for e in events)
        print()
        print(f"=== EVENTS RECEIVED ({len(events)}) ===")
        for t, n in sorted(by_type.items()):
            print(f"  {t:24s} x {n}")

        # Show a couple sample payloads per type.
        seen_types: set[str] = set()
        print()
        print("=== SAMPLE PAYLOADS ===")
        for e in events:
            if e["type"] not in seen_types:
                seen_types.add(e["type"])
                print(f"  {e['type']:24s} {json.dumps(e['payload'])[:140]}")

        ok = (
            done
            and by_type.get("instance_finalized", 0) >= 3
            and by_type.get("task_transitioned", 0) >= 3
            and by_type.get("instance_event", 0) >= 10
        )
        print()
        print("RESULT:", "PASS" if ok else "FAIL", "(pipeline_done=%s)" % done)
        return 0 if ok else 2

    finally:
        server.terminate()
        try:
            await asyncio.wait_for(server.wait(), timeout=5)
        except asyncio.TimeoutError:
            server.kill()
            await server.wait()
        await cleanup_branches()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
