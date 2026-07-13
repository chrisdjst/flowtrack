"""Common types and protocol for discovery sources.

A "source" is something that surfaces candidate work (bugs, features, etc.) from
an external system. Each source runs on its own interval, produces a list of
``DiscoveryCandidate`` objects, and the manager persists them as
``discovered_items`` (idempotent via UNIQUE(source, source_ref)).

Promotion to ``tasks`` is a separate, human-gated step (PM agent later) —
sources never write directly to ``tasks``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from flowtrack.models.discovered_item import (
    DiscoveryKind,
    DiscoverySource,
)


@dataclass(slots=True, frozen=True)
class DiscoveryCandidate:
    source: DiscoverySource
    source_ref: str
    kind: DiscoveryKind
    title: str
    summary: str | None = None
    raw_payload: dict | None = None
    signal_score: float | None = None
    # Critical findings bypass the human/TPM gate: the manager immediately
    # promotes them to a Task and blocks it with blocked_reason=security.
    # The source decides what counts as critical, the manager stays generic.
    critical: bool = False


@runtime_checkable
class DiscoveryWorker(Protocol):
    """Anything that produces DiscoveryCandidates on demand."""

    name: str
    interval_seconds: int

    def fetch(self) -> list[DiscoveryCandidate]: ...
