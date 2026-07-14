"""Specialization dispatch: resolve a base role name to the best variant.

Every place that used to do `select(Role).where(Role.name == X)` before
enqueueing a job now calls resolve_role(db, X, task=task). With zero
variant rows in the DB the result is identical to the old lookup — the
mechanism is structurally off until someone creates variants.

Eligibility (a variant is skipped entirely when):
  - it is scoped to a project different from the task's, or
  - it declares match_patterns and none of them fnmatch the task's
    module_hint.

Specificity (among eligible variants):
  +2  project match (variant.project_id == task.project_id, both set)
  +1  pattern match (some glob matched module_hint)
An unscoped, patternless variant scores 0 — eligible but never preferred
over a scoped/matching one.

Ambiguity: two eligible variants tied at the top score. We fall back to
the BASE role (never guess between specialists) and flag it for a human:
TaskComment + 'dispatch_ambiguous' WS event. The pipeline keeps moving.
"""

from __future__ import annotations

import logging
# fnmatchcase: plain fnmatch normcases per-OS (case-insensitive on Windows,
# sensitive on POSIX) — dispatch must behave identically everywhere.
from fnmatch import fnmatchcase

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.api.events import broker
from flowtrack.models import Role, Task, TaskComment

log = logging.getLogger(__name__)


def _specificity(variant: Role, task: Task) -> int | None:
    """Score a variant for a task. None = ineligible."""
    score = 0
    if variant.project_id is not None:
        if task.project_id != variant.project_id:
            return None
        score += 2
    if variant.match_patterns:
        hint = task.module_hint or ""
        if not any(fnmatchcase(hint, pattern) for pattern in variant.match_patterns):
            return None
        score += 1
    return score


def base_role_of(db: Session, role: Role) -> Role:
    """The base Role row a variant belongs to (itself for base roles).

    Chain fields a variant leaves NULL (next_role_name,
    task_status_on_success/_on_failure, max_bounce_count) fall back to the
    base's values — a variant specializes the PROMPT, not the pipeline
    shape, unless it explicitly overrides.
    """
    if role.base_role_name is None:
        return role
    base = db.scalar(
        select(Role)
        .where(Role.name == role.base_role_name)
        .where(Role.base_role_name.is_(None))
    )
    if base is None:
        log.warning("variant %s has no base role %r — using its own fields",
                    role.name, role.base_role_name)
        return role
    return base


def resolve_role(db: Session, base_role_name: str, *, task: Task | None = None) -> Role | None:
    """Most specific eligible variant of a base role, else the base itself.

    Returns None only when the base role does not exist (caller logs/halts,
    same as the old name lookup). task=None skips dispatch entirely —
    callers that have no task context always get the base role.
    """
    base = db.scalar(
        select(Role)
        .where(Role.name == base_role_name)
        .where(Role.base_role_name.is_(None))
    )
    if task is None:
        return base

    variants = list(db.scalars(
        select(Role).where(Role.base_role_name == base_role_name)
    ))
    scored: list[tuple[int, Role]] = []
    for variant in variants:
        score = _specificity(variant, task)
        if score is not None:
            scored.append((score, variant))
    if not scored:
        return base

    top = max(score for score, _ in scored)
    winners = [variant for score, variant in scored if score == top]
    if len(winners) == 1:
        winner = winners[0]
        log.info(
            "dispatch: %s -> %s (specialization=%s, score=%d) for task %s",
            base_role_name, winner.name, winner.specialization, top, task.id,
        )
        return winner

    # Tie between specialists: never guess. Base role + human flag.
    names = sorted(w.name for w in winners)
    log.warning(
        "dispatch: ambiguous variants %s for base %s on task %s — using base",
        names, base_role_name, task.id,
    )
    db.add(TaskComment(
        task_id=task.id,
        body=(
            f"Dispatch ambiguity: variants {', '.join(names)} tied for "
            f"'{base_role_name}' on this task. Proceeding with the base role — "
            "tighten match_patterns/project scoping to disambiguate."
        ),
    ))
    broker.publish_sync("dispatch_ambiguous", {
        "task_id": str(task.id),
        "base_role": base_role_name,
        "tied_variants": names,
    })
    return base
