"""Optional Sentry integration for the FlowTrack daemon.

Active only when ``settings.sentry_dsn`` is non-empty. Wiring is idempotent —
the first ``init_sentry()`` call configures the SDK; subsequent calls are
no-ops so you can safely call it from both the CLI entry point and the API
``run()`` without double-initialising.

Captured automatically:
  - Uncaught exceptions in any thread.
  - ``logging.ERROR`` (and above) records via LoggingIntegration.
  - FastAPI request errors via FastApiIntegration.
  - SQLAlchemy exceptions via SqlalchemyIntegration.

Filtered out (in ``before_send``):
  - Anything tagged ``noisy=true`` in extra data — workaround for known
    flapping events you don't want to alert on yet.
"""

from __future__ import annotations

import logging

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from flowtrack.core.settings import settings

log = logging.getLogger(__name__)

_INITIALISED = False


def _before_send(event: dict, hint: dict) -> dict | None:
    extra = (event.get("extra") or {})
    if extra.get("noisy"):
        return None
    return event


def init_sentry() -> bool:
    """Wire Sentry if a DSN is configured. Returns True iff initialised.

    Safe to call multiple times.
    """
    global _INITIALISED
    if _INITIALISED:
        return True
    if not settings.sentry_dsn:
        log.debug("sentry: no DSN configured, skipping")
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        release=settings.sentry_release or None,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=settings.sentry_send_default_pii,
        integrations=[
            # Logging at ERROR level -> Sentry event; INFO+ left as breadcrumbs.
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
        ],
        before_send=_before_send,
    )
    _INITIALISED = True
    log.info(
        "sentry: initialised (env=%s, traces=%s)",
        settings.sentry_environment, settings.sentry_traces_sample_rate,
    )
    return True
