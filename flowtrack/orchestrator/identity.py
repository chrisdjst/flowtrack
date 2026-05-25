"""Daemon identity (``worker_id``).

Picked once per process at first call and cached. Used by the claim query to
isolate test lanes from production lanes — see alembic 007.

Format when auto-generated: ``<hostname>:<pid>:<6hex>``. The hex suffix avoids
collisions when two daemons happen to get the same pid on different hosts
that share a name (rare, but free to defend against).
"""

from __future__ import annotations

import os
import socket
import uuid

from flowtrack.core.settings import settings

_cached: str | None = None


def worker_id() -> str:
    global _cached
    if _cached is not None:
        return _cached
    if settings.worker_id:
        _cached = settings.worker_id
    else:
        _cached = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    return _cached
