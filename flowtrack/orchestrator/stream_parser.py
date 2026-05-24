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
from uuid import UUID

from sqlalchemy.orm import Session

from flowtrack.api.events import broker
from flowtrack.core.database import SessionLocal
from flowtrack.models import Instance, InstanceEvent
from flowtrack.models.instance_event import InstanceEventType
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
    """
    while True:
        line = await stream.readline()
        if not line:
            return
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

        if event_type == InstanceEventType.USAGE:
            input_tokens = int(payload.get("input_tokens", 0))
            output_tokens = int(payload.get("output_tokens", 0))
            instance.tokens_input += input_tokens
            instance.tokens_output += output_tokens
            instance.cost_usd = (instance.cost_usd or 0) + cost_for(
                model, input_tokens=input_tokens, output_tokens=output_tokens
            )

        db.commit()

        broker.publish_sync("instance_event", {
            "instance_id": str(instance_id),
            "event_type": event_type.value,
            "tokens_input": instance.tokens_input,
            "tokens_output": instance.tokens_output,
            "cost_usd": str(instance.cost_usd),
            "summary": _event_summary(event_type, payload),
        })
    except Exception:
        db.rollback()
        log.exception("failed to persist event for instance %s", instance_id)
    finally:
        db.close()


def _event_summary(event_type: InstanceEventType, payload: dict) -> str:
    """Short, UI-friendly description of the event. Used by the kanban cards."""
    if event_type == InstanceEventType.TOOL_USE:
        return f"tool: {payload.get('tool', '?')}"
    if event_type == InstanceEventType.USAGE:
        return f"+{payload.get('input_tokens', 0)}/{payload.get('output_tokens', 0)} tok"
    if event_type == InstanceEventType.RESULT:
        return f"result: {payload.get('exit_reason', 'unknown')}"
    if event_type == InstanceEventType.ERROR:
        return "error"
    if event_type == InstanceEventType.THINKING:
        return "thinking"
    return event_type.value
