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


@dataclass(slots=True, frozen=True)
class RefinementResult:
    acceptance_criteria: str
    module_hint: str | None
    recommendation: Literal["promote", "reject"]
    cost_usd: Decimal


_SCHEMA_HINT = """{
  "acceptance_criteria": string  // numbered, testable criteria
  "module_hint": string | null   // module name or null if unclear
  "recommendation": "promote" | "reject"
}"""


def _build_prompt(*, title: str, summary: str | None, kind: str, source: str,
                  source_ref: str, raw_payload: dict | None) -> str:
    parts = [
        "You are a Product Manager refining a discovered work item.",
        "",
        f"Title: {title}",
        f"Source: {source} ({source_ref})",
        f"Kind: {kind}",
    ]
    if summary:
        parts += ["", "Summary:", summary[:2000]]
    if raw_payload:
        parts += ["", "Raw payload (truncated):", json.dumps(raw_payload)[:2000]]
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
        "  requests with no acceptance bar, duplicates).",
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
    model: str = "sonnet",
    timeout_seconds: int = 90,
) -> RefinementResult:
    """Async wrapper around the claude subprocess call. Raises on hard failure.

    Idempotent: callers can retry safely (each call is a fresh session, no
    side effects beyond budget recording on success).
    """
    prompt = _build_prompt(
        title=title, summary=summary, kind=kind,
        source=source, source_ref=source_ref, raw_payload=raw_payload,
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

    parsed = RefinementResult(
        acceptance_criteria=result["acceptance_criteria"],
        module_hint=result.get("module_hint") or None,
        recommendation=result["recommendation"],
        cost_usd=cost,
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
