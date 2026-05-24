"""FastAPI application entrypoint for the FlowTrack orchestrator daemon.

Run with: `flowtrack-server` (or `uv run flowtrack-server`).

The orchestrator loop runs in the same process as the API, started/stopped via
FastAPI lifespan. When you outgrow this (>50 jobs/h or you want horizontal
scaling), split the loop into a separate `flowtrack worker` process — the only
shared state is the Postgres queue, so splitting is straightforward.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from flowtrack.api.events import broker
from flowtrack.api.routers import instances, kanban, tasks
from flowtrack.core.settings import settings
from flowtrack.orchestrator.loop import run_orchestrator
from flowtrack.orchestrator.watchdog import run_watchdog

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "starting orchestrator (dry_run=%s, max_concurrent=%d, interval=%.1fs)",
        settings.orchestrator_dry_run,
        settings.max_concurrent_instances,
        settings.orchestrator_loop_interval_seconds,
    )
    stop_event = asyncio.Event()
    bg_tasks = [
        asyncio.create_task(run_orchestrator(stop_event), name="orchestrator-loop"),
        asyncio.create_task(run_watchdog(stop_event), name="watchdog"),
        asyncio.create_task(broker.run_broadcaster(stop_event), name="ws-broadcaster"),
    ]
    try:
        yield
    finally:
        log.info("stopping background tasks...")
        stop_event.set()
        try:
            await asyncio.wait_for(asyncio.gather(*bg_tasks, return_exceptions=True), timeout=15)
        except asyncio.TimeoutError:
            log.warning("background tasks did not stop in time, cancelling")
            for t in bg_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*bg_tasks, return_exceptions=True)


app = FastAPI(
    title="FlowTrack Orchestrator",
    version="0.1.0",
    description="Multi-instance Claude Code orchestrator + kanban board.",
    lifespan=lifespan,
)

app.include_router(kanban.router)
app.include_router(tasks.router)
app.include_router(instances.router)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    return {"status": "ok", "dry_run": settings.orchestrator_dry_run}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Live event stream for the kanban frontend.

    No auth yet — bind to 127.0.0.1 in production until that ships. The
    server never expects client → server messages; reading just keeps the
    connection alive until close.
    """
    await ws.accept()
    await broker.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await broker.remove(ws)


def run() -> None:
    """Entrypoint for the `flowtrack-server` console script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    uvicorn.run(
        "flowtrack.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
        # reload=False on purpose: lifespan + background task plays badly with reload.
    )


if __name__ == "__main__":
    run()
