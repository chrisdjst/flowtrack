"""Smoke for Sentry wiring without hitting the real Sentry servers.

Uses a fake transport that captures events into a list. Asserts:
  - init_sentry() activates the SDK when DSN is set.
  - An uncaught exception inside a captured block produces an event.
  - A logging.error() call produces an event (LoggingIntegration wired).
  - The ``noisy=true`` extra-data filter in before_send drops events.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.environ["FLOWTRACK_SENTRY_DSN"] = (
    # Test DSN — never sent over the wire because we replace the transport.
    "https://public@sentry.test/1"
)
os.environ["FLOWTRACK_SENTRY_ENVIRONMENT"] = "smoketest"

import sentry_sdk  # noqa: E402
from sentry_sdk.transport import Transport  # noqa: E402

CAPTURED: list[dict] = []


class _FakeTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options or {})

    def capture_envelope(self, envelope):
        for item in envelope.items:
            try:
                payload = item.payload.json
            except Exception:
                continue
            if isinstance(payload, dict) and payload.get("type") != "transaction":
                CAPTURED.append(payload)

    def flush(self, timeout=None, callback=None):
        pass


# Patch the SDK BEFORE init_sentry runs so the configured client uses our transport.
import flowtrack.core.sentry as ft_sentry  # noqa: E402

_orig_init = sentry_sdk.init


def _init_with_fake(*args, **kwargs):
    kwargs["transport"] = _FakeTransport
    return _orig_init(*args, **kwargs)


sentry_sdk.init = _init_with_fake


def main() -> int:
    activated = ft_sentry.init_sentry()
    print(f"init_sentry returned: {activated} (expect True)")

    # Case 1: uncaught exception via with_scope -> capture_exception.
    try:
        raise RuntimeError("smoke: deliberate failure")
    except RuntimeError:
        sentry_sdk.capture_exception()

    # Case 2: logging.error captured by LoggingIntegration.
    logging.basicConfig(level=logging.INFO)
    logging.error("smoke: error-level log line")

    # Case 3: filtered event (noisy=true in extra).
    with sentry_sdk.push_scope() as scope:
        scope.set_extra("noisy", True)
        sentry_sdk.capture_message("smoke: should be filtered out")

    sentry_sdk.flush(timeout=2)

    print(f"captured events: {len(CAPTURED)}")
    types = [e.get("level") or e.get("type") for e in CAPTURED]
    print(f"event levels: {types}")

    # Expect: 1 from RuntimeError + 1 from logging.error = 2.
    # The "noisy" event should have been dropped by before_send.
    ok = (
        activated
        and len(CAPTURED) == 2
        and any("smoke: deliberate failure" in str(e.get("exception", "")) for e in CAPTURED)
        and not any("should be filtered" in str(e.get("message", "")) for e in CAPTURED)
    )
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
