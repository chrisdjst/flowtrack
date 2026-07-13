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

from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from flowtrack.api.auth import BearerAuthMiddleware, check_websocket_token
from flowtrack.api.events import broker
from flowtrack.api.routers import budget, discovery, instances, kanban, roles_router, tasks, settings_router
from flowtrack.core.runtime_config import RuntimeConfig
from flowtrack.core.sentry import init_sentry
from flowtrack.core.settings import settings
from flowtrack.agents.po import run_po_admission
from flowtrack.discovery.manager import run_discovery_manager
from flowtrack.orchestrator.loop import run_orchestrator
from flowtrack.orchestrator.watchdog import run_watchdog

_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "starting orchestrator (dry_run=%s, max_concurrent=%d, interval=%.1fs)",
        RuntimeConfig.get("orchestrator_dry_run"),
        RuntimeConfig.get("max_concurrent_instances"),
        RuntimeConfig.get("orchestrator_loop_interval_seconds"),
    )
    stop_event = asyncio.Event()
    bg_tasks = [
        asyncio.create_task(run_orchestrator(stop_event), name="orchestrator-loop"),
        asyncio.create_task(run_watchdog(stop_event), name="watchdog"),
        asyncio.create_task(broker.run_broadcaster(stop_event), name="ws-broadcaster"),
        asyncio.create_task(run_discovery_manager(stop_event), name="discovery-manager"),
        asyncio.create_task(run_po_admission(stop_event), name="po-admission"),
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

# Auth middleware. No-op when api_token is empty.
app.add_middleware(BearerAuthMiddleware)

app.include_router(kanban.router)
app.include_router(tasks.router)
app.include_router(instances.router)
app.include_router(budget.router)
app.include_router(discovery.router)
app.include_router(settings_router.router)
app.include_router(roles_router.router)

# Static assets (css/js) under /web/, and the index page at /.
if _STATIC_DIR.is_dir():
    app.mount("/web", StaticFiles(directory=_STATIC_DIR), name="web")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    return {
        "status": "ok",
        "dry_run": RuntimeConfig.get("orchestrator_dry_run"),
        "sentry_active": bool(settings.sentry_dsn),
        "sentry_environment": settings.sentry_environment,
    }


@app.get("/__sentry_test", include_in_schema=False)
def _sentry_test() -> dict:
    """Intentional crash to verify Sentry capture. Disabled in production envs.

    Returns 404 unless ``sentry_environment`` is ``development``. Calling it
    raises ZeroDivisionError, which Sentry should record as a new event.
    """
    from fastapi import HTTPException
    if settings.sentry_environment == "production":
        raise HTTPException(404)
    division_by_zero = 1 / 0  # noqa: F841  (intentional)
    return {"unreachable": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Live event stream for the kanban frontend.

    When ``settings.api_token`` is set, the client must pass it as
    ``Authorization: Bearer <token>`` OR ``?token=<token>`` (browsers don't
    let you set headers on WebSocket; query param is the escape hatch).

    The server never expects client -> server messages; reading just keeps
    the connection alive until close.
    """
    if not await check_websocket_token(ws):
        return
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
    # Sentry first so logging-config above is captured as breadcrumbs.
    init_sentry()
    uvicorn.run(
        "flowtrack.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
        # reload=False on purpose: lifespan + background task plays badly with reload.
    )


if __name__ == "__main__":
    run()
