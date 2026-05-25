"""Optional bearer-token auth for the FlowTrack API + WebSocket.

Active only when ``settings.api_token`` is non-empty. When inactive, every
request passes through (localhost-only deployment is the assumed default).

Scope: all paths under ``/api/`` and the WebSocket upgrade at ``/ws`` require
``Authorization: Bearer <token>``. Static assets (``/web/*``), the index page
(``/``), and ``/healthz`` stay open so an unauthenticated tab can still tell
whether the daemon is alive.

This is **shared secret** auth — simplest possible thing that defends against
casual public-internet exposure. Replace with proper OIDC/JWT before multi-
user deployments.
"""

from __future__ import annotations

import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from flowtrack.core.settings import settings

log = logging.getLogger(__name__)


_OPEN_PATHS = ("/healthz", "/web/", "/docs", "/openapi.json", "/redoc")
_OPEN_EXACT = {"/"}


def _is_open(path: str) -> bool:
    if path in _OPEN_EXACT:
        return True
    for prefix in _OPEN_PATHS:
        if path.startswith(prefix):
            return True
    return False


def _extract_token(header_value: str | None, query_value: str | None) -> str | None:
    if header_value:
        # Accept "Bearer X" or raw "X" — be lenient.
        parts = header_value.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return header_value.strip()
    if query_value:
        return query_value.strip()
    return None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated /api/* requests when api_token is configured."""

    async def dispatch(self, request: Request, call_next):
        if not settings.api_token:
            return await call_next(request)

        path = request.url.path
        if _is_open(path):
            return await call_next(request)

        if not path.startswith("/api/"):
            return await call_next(request)

        provided = _extract_token(
            request.headers.get("authorization"),
            request.query_params.get("token"),
        )
        if provided != settings.api_token:
            return JSONResponse(
                {"detail": "missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="flowtrack"'},
            )
        return await call_next(request)


async def check_websocket_token(websocket) -> bool:
    """Hand-rolled gate for the ``/ws`` endpoint.

    WebSocket upgrades don't go through HTTPMiddleware's dispatch in a way
    that lets us reject cleanly with a 401 + Sec-WebSocket headers, so the
    endpoint itself calls this BEFORE accepting the upgrade. Returns True
    if the request is authorised (or auth is disabled).
    """
    if not settings.api_token:
        return True
    provided = _extract_token(
        websocket.headers.get("authorization"),
        websocket.query_params.get("token"),
    )
    if provided != settings.api_token:
        await websocket.close(code=1008, reason="invalid token")
        return False
    return True
