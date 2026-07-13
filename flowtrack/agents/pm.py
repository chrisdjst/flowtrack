"""PM agent: refine a DiscoveredItem into a Task spec.

Design note (sec 7.2 of ORCHESTRATOR.md): PM is NOT a Claude Code role; it's
a focused one-shot LLM call. Cheaper, deterministic shape, no worktree.

We still go through the ``claude`` CLI so this works with the user's existing
OAuth login (no API key required). ``--output-format json`` returns a single
envelope dict with ``.result`` containing the assistant text — we instruct
the model in the prompt to emit only a JSON object and strip optional
markdown fences before parsing. (``--json-schema`` exists but in the
installed CLI version it's validation-only and unreliable; prompt-shape is
more robust.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from flowtrack.core.database import SessionLocal
from flowtrack.core.settings import settings
from flowtrack.orchestrator import budget

log = logging.getLogger(__name__)


_SEVERITIES = ("P0-Critical", "P1-High", "P2-Medium", "P3-Low")
_ROUTINGS = ("design_dev", "dev_only")


@dataclass(slots=True, frozen=True)
class RefinementResult:
    acceptance_criteria: str
    module_hint: str | None
    recommendation: Literal["promote", "reject", "duplicate"]
    cost_usd: Decimal
    severity: str = "P2-Medium"
    pipeline_routing: str = "dev_only"
    task_spec: str | None = None
    # id-prefix of the open task this item duplicates; None when the model
    # said duplicate but pointed at nothing we listed (human inspects).
    duplicate_of: str | None = None


_SCHEMA_HINT = """{
  "acceptance_criteria": string   // numbered, testable criteria
  "module_hint": string | null    // module name or null if unclear
  "recommendation": "promote" | "reject" | "duplicate"
  "duplicate_of": string | null   // id of the OPEN TASK duplicated (only with recommendation=duplicate)
  "severity": "P0-Critical" | "P1-High" | "P2-Medium" | "P3-Low"
  "pipeline_routing": "design_dev" | "dev_only"
  "task_spec": string             // markdown task spec (see rules)
}"""


def _build_prompt(*, title: str, summary: str | None, kind: str, source: str,
                  source_ref: str, raw_payload: dict | None,
                  existing_tasks: list[tuple[str, str]] | None = None) -> str:
    parts = [
        "You are a Technical Product Manager refining a discovered work item "
        "into a task specification.",
        "",
        f"Title: {title}",
        f"Source: {source} ({source_ref})",
        f"Kind: {kind}",
    ]
    if summary:
        parts += ["", "Summary:", summary[:2000]]
    if raw_payload:
        parts += ["", "Raw payload (truncated):", json.dumps(raw_payload)[:2000]]
    if existing_tasks:
        parts += ["", "Open tasks already in the backlog (id | title):"]
        parts += [f"- {tid} | {ttitle[:120]}" for tid, ttitle in existing_tasks[:30]]
    parts += [
        "",
        "Respond with ONLY a JSON object — no prose, no markdown fences — "
        "matching this shape:",
        _SCHEMA_HINT,
        "",
        "Rules:",
        "- acceptance_criteria: checkable items like 'X returns Y when Z',",
        "  not vague ('improve performance').",
        "- module_hint: a single module/package name (e.g. 'auth', 'billing').",
        "  Use null when you can't infer it confidently.",
        "- recommendation='reject' for noise (transient errors, vague feature",
        "  requests with no acceptance bar).",
        "- recommendation='duplicate' ONLY when the item clearly describes the",
        "  same defect/feature as one of the open tasks listed above; put that",
        "  task's id in duplicate_of. When in doubt, promote — don't guess.",
        "- severity: P0-Critical = security issue, data loss, or full outage;",
        "  P1-High = major function broken with no workaround;",
        "  P2-Medium = default for everything else; P3-Low = cosmetic/nice-to-have.",
        "- pipeline_routing: 'design_dev' only when the work changes user-facing",
        "  UI/UX and needs a design pass first; 'dev_only' otherwise.",
        "- task_spec: a compact markdown spec with sections: ## Context (what/why,",
        "  2-4 lines), ## Acceptance Criteria (same items as above), ## Out of",
        "  Scope (1-3 explicit exclusions), ## Module Hints (paths/modules).",
    ]
    return "\n".join(parts)


def _extract_json_object(text: str) -> dict:
    """Parse ``text`` as JSON, peeling off markdown fences if present.

    Real responses sometimes come wrapped as ```json\n{...}\n```; be lenient.
    Raises ValueError when nothing parsable is found.
    """
    s = text.strip()
    # Strip ```json ... ``` or ``` ... ``` fences.
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    # Fast path.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Fallback: find the outermost JSON object in the text.
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(s[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise ValueError(f"no JSON object in: {text[:200]!r}")


async def refine_async(
    *,
    title: str,
    summary: str | None,
    kind: str,
    source: str,
    source_ref: str,
    raw_payload: dict | None = None,
    existing_tasks: list[tuple[str, str]] | None = None,
    model: str = "sonnet",
    timeout_seconds: int = 90,
) -> RefinementResult:
    """Async wrapper around the claude subprocess call. Raises on hard failure.

    Idempotent: callers can retry safely (each call is a fresh session, no
    side effects beyond budget recording on success).

    existing_tasks: (id-prefix, title) of open tasks, used for DUPLICATE
    detection. Empty/None disables the duplicate branch in practice.
    """
    prompt = _build_prompt(
        title=title, summary=summary, kind=kind,
        source=source, source_ref=source_ref, raw_payload=raw_payload,
        existing_tasks=existing_tasks,
    )

    # Allow injection of a fake executable for tests (mirrors spawner pattern).
    cmd_prefix = shlex.split(settings.claude_executable, posix=(os.name != "nt"))
    if os.name == "nt":
        cmd_prefix = [p.strip('"') for p in cmd_prefix]
    cmd = [
        *cmd_prefix,
        "--print", prompt,
        # NOT --verbose — that would give us an array of events instead of
        # a single envelope dict, which is harder to parse.
        "--output-format", "json",
        "--model", model,
        "--max-budget-usd", "0.30",  # per-call hard cap
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"pm agent timed out after {timeout_seconds}s")

    if proc.returncode != 0:
        snippet = stderr.decode("utf-8", "replace")[:500]
        raise RuntimeError(f"pm agent exited {proc.returncode}: {snippet}")

    try:
        envelope = json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"pm agent emitted invalid envelope JSON: {e}") from e

    if not isinstance(envelope, dict):
        raise RuntimeError(
            f"pm agent expected single envelope dict; got {type(envelope).__name__}. "
            "Did --verbose sneak into the command line?"
        )
    if envelope.get("is_error"):
        raise RuntimeError(
            f"pm agent envelope reports error: subtype={envelope.get('subtype')} "
            f"errors={envelope.get('errors')}"
        )

    cost = Decimal(str(envelope.get("total_cost_usd", 0)))
    result_str = envelope.get("result")
    if not isinstance(result_str, str):
        raise RuntimeError(f"pm agent envelope missing .result string: keys={list(envelope.keys())}")

    result = _extract_json_object(result_str)

    if "acceptance_criteria" not in result or "recommendation" not in result:
        raise RuntimeError(f"pm agent result missing required keys: {list(result.keys())}")

    recommendation = result["recommendation"]
    if recommendation not in ("promote", "reject", "duplicate"):
        raise RuntimeError(f"pm agent returned unknown recommendation: {recommendation!r}")

    # Lenient on the enrichment fields — a malformed value degrades to a safe
    # default rather than failing the whole refinement.
    severity = result.get("severity")
    if severity not in _SEVERITIES:
        severity = "P2-Medium"
    routing = result.get("pipeline_routing")
    if routing not in _ROUTINGS:
        routing = "dev_only"

    duplicate_of = result.get("duplicate_of") or None
    if recommendation == "duplicate" and duplicate_of is not None:
        known_ids = {tid for tid, _ in (existing_tasks or [])}
        if duplicate_of not in known_ids:
            # Model pointed at something we never listed — keep the duplicate
            # verdict but drop the bogus ref so a human resolves it.
            log.warning(
                "pm agent: duplicate_of=%r not among listed tasks — clearing ref",
                duplicate_of,
            )
            duplicate_of = None

    parsed = RefinementResult(
        acceptance_criteria=result["acceptance_criteria"],
        module_hint=result.get("module_hint") or None,
        recommendation=recommendation,
        cost_usd=cost,
        severity=severity,
        pipeline_routing=routing,
        task_spec=result.get("task_spec") or None,
        duplicate_of=duplicate_of,
    )

    # Record spend so the global cap reflects PM agent usage too.
    if cost > 0:
        db = SessionLocal()
        try:
            budget.record_usage(db, cost_usd=cost, tokens=0)
            db.commit()
        finally:
            db.close()

    log.info(
        "pm agent: source=%s ref=%s rec=%s cost=$%s",
        source, source_ref, parsed.recommendation, cost,
    )
    return parsed
