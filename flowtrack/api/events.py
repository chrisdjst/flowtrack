"""In-process event broker for the WebSocket fan-out.

Why not Redis pub/sub: the orchestrator runs in one process today, so module-
global state is sufficient. When we scale to multiple workers (different
processes / hosts), replace this with Redis or NATS — the public API
(``publish_sync`` / ``add`` / ``remove``) stays the same.

Producers (stream_parser, spawner) call ``broker.publish_sync(...)`` from sync
code. The async ``run_broadcaster`` task pulls from the queue and writes to
every connected WebSocket. Slow/dead WebSockets are dropped, not retried — the
frontend reconnects on disconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)

_QUEUE_MAX = 1000


class EventBroker:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAX)

    async def add(self, ws: WebSocket) -> None:
        self._connections.add(ws)
        log.info("ws connected; %d total", len(self._connections))

    async def remove(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        log.info("ws disconnected; %d remaining", len(self._connections))

    def publish_sync(self, event_type: str, payload: dict[str, Any]) -> None:
        """Non-blocking publish from any thread / sync code path.

        Drops the event on queue overflow rather than blocking the caller —
        events are advisory UI signals, never source of truth (DB is).
        """
        try:
            self._queue.put_nowait({"type": event_type, "payload": payload})
        except asyncio.QueueFull:
            log.warning("event queue full (%d); dropping event %s", _QUEUE_MAX, event_type)

    async def run_broadcaster(self, stop: asyncio.Event) -> None:
        """Consumer task: read queue, fan out. Returns when ``stop`` set."""
        log.info("ws broadcaster started")
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            message = json.dumps(event, default=str)
            dead: list[WebSocket] = []
            for ws in list(self._connections):
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._connections.discard(ws)
        log.info("ws broadcaster stopped")


# Module-level singleton. Importing this from anywhere is fine.
broker = EventBroker()
