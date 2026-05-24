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
from fastapi import FastAPI

from flowtrack.api.routers import instances, kanban, tasks
from flowtrack.core.settings import settings
from flowtrack.orchestrator.loop import run_orchestrator

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
    task = asyncio.create_task(run_orchestrator(stop_event), name="orchestrator-loop")
    try:
        yield
    finally:
        log.info("stopping orchestrator...")
        stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=10)
        except asyncio.TimeoutError:
            log.warning("orchestrator did not stop in time, cancelling")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


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
