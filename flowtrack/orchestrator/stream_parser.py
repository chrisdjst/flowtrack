"""Parse the JSONL stream emitted by ``claude --print --output-format stream-json``.

Each line is a JSON object. We classify it into an InstanceEventType, persist it
to ``instance_events``, and update aggregate columns on ``instances`` (tokens,
cost, heartbeat).

The exact shape of stream-json events evolves between Claude Code versions. We
parse defensively: unknown event types are stored as MESSAGE so we never drop
data, and the JSON payload is preserved verbatim for forensics.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from flowtrack.api.events import broker
from flowtrack.core.database import SessionLocal
from flowtrack.models import Instance, InstanceEvent
from flowtrack.models.instance_event import InstanceEventType
from flowtrack.orchestrator import budget
from flowtrack.orchestrator.pricing import cost_for

log = logging.getLogger(__name__)


_KNOWN_TYPES = {t.value for t in InstanceEventType}


async def consume_stream(
    *,
    stream: asyncio.StreamReader,
    instance_id: UUID,
    model: str,
) -> None:
    """Read ``stream`` line-by-line until EOF, persisting events as we go.

    Runs in the asyncio loop; DB writes happen via ``asyncio.to_thread`` so we
    don't stall the event loop on Postgres round-trips.

    ``asyncio.StreamReader.readline`` has a 64KB default buffer limit; a single
    big system/init event from real Claude Code can exceed that and raise
    ``LimitOverrunError``. We grow the limit at subprocess-creation time
    (see spawner) but also fall back to readuntil with a larger cap here in
    case of latent surprises.
    """
    line_count = 0
    while True:
        try:
            line = await stream.readline()
        except asyncio.LimitOverrunError as exc:
            # Salvage what we can — read until the newline with a bigger budget.
            log.warning("stream readline overran limit for instance %s; falling back", instance_id)
            try:
                line = await stream.readuntil(b"\n")
            except Exception:
                # Drop the oversized line, log, continue.
                await asyncio.to_thread(
                    _persist_event,
                    instance_id,
                    InstanceEventType.ERROR,
                    {"error": f"readline overrun, salvage failed: {exc}"},
                    model,
                )
                # Skip past the trouble line.
                await stream.read(exc.consumed)
                continue
        except Exception:
            log.exception("stream read error for instance %s", instance_id)
            return
        if not line:
            log.info("stream EOF for instance %s after %d lines", instance_id, line_count)
            return
        line_count += 1
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            # Non-JSON line (e.g. stderr leaked into stdout?). Store as raw error.
            await asyncio.to_thread(
                _persist_event,
                instance_id,
                InstanceEventType.ERROR,
                {"raw": line.decode("utf-8", "replace").rstrip()},
                model,
            )
            continue

        event_type_str = payload.get("type", "message")
        event_type = (
            InstanceEventType(event_type_str)
            if event_type_str in _KNOWN_TYPES
            else InstanceEventType.MESSAGE
        )
        await asyncio.to_thread(_persist_event, instance_id, event_type, payload, model)


def _persist_event(
    instance_id: UUID,
    event_type: InstanceEventType,
    payload: dict,
    model: str,
) -> None:
    """One DB transaction per event. Updates instance aggregates inline."""
    db: Session = SessionLocal()
    try:
        db.add(InstanceEvent(
            instance_id=instance_id,
            event_type=event_type,
            payload_json=payload,
        ))

        instance = db.get(Instance, instance_id)
        if instance is None:
            log.warning("event for unknown instance %s — dropping aggregate update", instance_id)
            db.commit()
            return

        # Every event counts as a heartbeat.
        instance.last_heartbeat_at = datetime.now(tz=timezone.utc)

        # Token / cost extraction. Multiple shapes in the wild:
        #
        #   mock_claude.py:
        #     {"type": "usage", "input_tokens": N, "output_tokens": M}
        #
        #   real Claude Code (stream-json):
        #     {"type": "assistant", "message": {..., "usage": {"input_tokens": N,
        #       "output_tokens": M, ...}}, ...}
        #     {"type": "result", "usage": {...}, "total_cost_usd": $, ...}
        #
        # The `result` event carries authoritative final totals (and a real $
        # figure from the Anthropic side), so when it's present we set instance
        # cost from it instead of summing our own pricing estimates.
        input_tokens, output_tokens = _extract_usage(event_type, payload)
        result_cost = _extract_result_cost(event_type, payload)
        result_usage = (
            payload.get("usage", {}) if event_type == InstanceEventType.RESULT else None
        )

        if input_tokens or output_tokens:
            delta_cost = cost_for(
                model, input_tokens=input_tokens, output_tokens=output_tokens,
            )
            instance.tokens_input += input_tokens
            instance.tokens_output += output_tokens
            instance.cost_usd = (instance.cost_usd or 0) + delta_cost
            budget.record_usage(
                db, cost_usd=delta_cost, tokens=input_tokens + output_tokens,
            )

        # Result event: overwrite with authoritative totals when supplied.
        if result_cost is not None or result_usage:
            if result_usage:
                # Real Claude sometimes reports zero usage on the result line
                # itself (the per-turn assistant events already accounted for
                # it). Only overwrite when result.usage has meaningful values.
                ri = int(result_usage.get("input_tokens", 0))
                ro = int(result_usage.get("output_tokens", 0))
                if ri or ro:
                    instance.tokens_input = max(instance.tokens_input, ri)
                    instance.tokens_output = max(instance.tokens_output, ro)
            if result_cost is not None:
                # If we've been accumulating estimates, reconcile the diff
                # into budget_windows so the daemon-wide cap reflects truth.
                old = Decimal(instance.cost_usd or 0)
                new = Decimal(str(result_cost))
                delta = new - old
                instance.cost_usd = new
                if delta > 0:
                    budget.record_usage(db, cost_usd=delta, tokens=0)

        db.commit()

        broker.publish_sync("instance_event", {
            "instance_id": str(instance_id),
            "event_type": event_type.value,
            "tokens_input": instance.tokens_input,
            "tokens_output": instance.tokens_output,
            "cost_usd": str(instance.cost_usd),
            "summary": _event_summary(event_type, payload),
            "text": _event_text(event_type, payload),
        })
    except Exception:
        db.rollback()
        log.exception("failed to persist event for instance %s", instance_id)
    finally:
        db.close()


def _extract_usage(
    event_type: InstanceEventType, payload: dict
) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) for events that carry incremental usage.

    Handles the two known shapes (mock flat, real-Claude nested in
    ``assistant.message.usage``). Returns (0, 0) for events with no usage.
    """
    if event_type == InstanceEventType.USAGE:
        return (
            int(payload.get("input_tokens", 0)),
            int(payload.get("output_tokens", 0)),
        )
    if event_type == InstanceEventType.ASSISTANT:
        message = payload.get("message") or {}
        usage = message.get("usage") or {}
        return (
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
        )
    return 0, 0


def _extract_result_cost(event_type: InstanceEventType, payload: dict) -> float | None:
    """Return ``total_cost_usd`` from a result event, or None for other events."""
    if event_type != InstanceEventType.RESULT:
        return None
    raw = payload.get("total_cost_usd")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _event_text(event_type: InstanceEventType, payload: dict) -> str:
    """Richer text for live modal display — extracts actual content from payload."""
    if event_type == InstanceEventType.ASSISTANT:
        # Real Claude Code sends ASSISTANT with message.content = list of blocks.
        # Each block is {"type": "text", "text": "..."} or {"type": "tool_use", ...}.
        parts: list[str] = []
        message = payload.get("message") or payload
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    t = block.get("text", "")
                    if t:
                        parts.append(t)
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input") or {}
                    brief = str(inp)[:200] if inp else ""
                    parts.append(f"[tool: {name}] {brief}".strip())
        return "\n".join(parts)[:1000]

    if event_type == InstanceEventType.MESSAGE:
        # Streaming delta: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "..."}}
        delta = payload.get("delta") or {}
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            return delta.get("text", "")
        # content_block_start carries partial tool_use input
        block = payload.get("content_block") or {}
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return f"[tool: {block.get('name', '?')}]"
        return ""

    if event_type == InstanceEventType.TOOL_USE:
        name = payload.get("name") or payload.get("tool", "?")
        inp = payload.get("input") or {}
        brief = str(inp)[:300] if inp else ""
        return f"[{name}] {brief}".strip()

    if event_type == InstanceEventType.ERROR:
        return payload.get("error") or payload.get("raw", "error")

    if event_type == InstanceEventType.RESULT:
        subtype = payload.get("subtype", "")
        cost = payload.get("total_cost_usd")
        return f"{subtype} — cost ${cost}" if cost else subtype

    return ""


def _event_summary(event_type: InstanceEventType, payload: dict) -> str:
    """Short, UI-friendly description of the event. Used by the kanban cards."""
    if event_type == InstanceEventType.TOOL_USE:
        # Mock shape has 'tool' at top level; real Claude nests tool_use inside
        # a content item — fall back to anything resembling a name.
        return f"tool: {payload.get('tool') or payload.get('name', '?')}"
    if event_type == InstanceEventType.USAGE:
        return f"+{payload.get('input_tokens', 0)}/{payload.get('output_tokens', 0)} tok"
    if event_type == InstanceEventType.RESULT:
        subtype = payload.get("subtype")
        cost = payload.get("total_cost_usd")
        if cost is not None:
            return f"result: {subtype or 'success'} (${cost})"
        return f"result: {subtype or payload.get('exit_reason', 'success')}"
    if event_type == InstanceEventType.ASSISTANT:
        return "assistant message"
    if event_type == InstanceEventType.SYSTEM:
        return f"system: {payload.get('subtype', 'event')}"
    if event_type == InstanceEventType.RATE_LIMIT:
        return "rate limit"
    if event_type == InstanceEventType.ERROR:
        return "error"
    if event_type == InstanceEventType.THINKING:
        return "thinking"
    return event_type.value
