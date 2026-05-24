"""Logical resource locks so two instances do not edit the same module concurrently.

A "lock" is just a row in ``resource_locks`` with a unique ``resource_key``. The
DB UNIQUE constraint enforces mutex. Locks expire on their own — watchdog (TBD)
sweeps stale rows whose ``expires_at`` passed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from flowtrack.models import ResourceLock

log = logging.getLogger(__name__)


def derive_locks_for(*, module_hint: str | None) -> list[str]:
    """Return the resource_keys a task needs to hold while running.

    Conservative-first policy — see docs/ORCHESTRATOR.md §5.3 for granularity
    discussion. MVP: module_hint if set, else a coarse "_unknown_" lock that
    serializes execution (preferable to silent conflict).
    """
    if module_hint:
        return [f"module:{module_hint}"]
    return ["module:_unknown_"]


def try_acquire(
    db: Session,
    keys: list[str],
    *,
    instance_id: UUID,
    ttl: timedelta,
) -> bool:
    """Try to acquire ALL keys atomically. Returns False on first conflict.

    Acquires within the caller's transaction — if False is returned, caller MUST
    rollback so partially-inserted rows are discarded. Returns True with rows
    inserted but not yet committed.
    """
    expires_at = datetime.now(tz=timezone.utc) + ttl
    for key in keys:
        try:
            db.add(ResourceLock(
                resource_key=key,
                instance_id=instance_id,
                expires_at=expires_at,
            ))
            db.flush()
        except IntegrityError:
            log.info(
                "lock contention on %s for instance %s — will retry later", key, instance_id
            )
            return False
    return True


def release_all(db: Session, *, instance_id: UUID) -> int:
    """Drop every lock held by an instance. Returns count deleted."""
    result = db.execute(delete(ResourceLock).where(ResourceLock.instance_id == instance_id))
    return result.rowcount or 0


def renew(db: Session, *, instance_id: UUID, ttl: timedelta) -> int:
    """Extend expires_at for all locks held by instance. Returns count touched."""
    from sqlalchemy import update

    new_exp = datetime.now(tz=timezone.utc) + ttl
    result = db.execute(
        update(ResourceLock)
        .where(ResourceLock.instance_id == instance_id)
        .values(expires_at=new_exp)
    )
    return result.rowcount or 0


def sweep_expired(db: Session) -> int:
    """Drop locks past their expires_at. Run from watchdog. Returns count."""
    now = datetime.now(tz=timezone.utc)
    result = db.execute(delete(ResourceLock).where(ResourceLock.expires_at < now))
    return result.rowcount or 0
