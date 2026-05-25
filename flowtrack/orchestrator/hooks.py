"""Generate ``.claude/settings.json`` inside each spawned worktree.

The hooks call back into the daemon's HTTP API so the daemon learns about
events Claude Code emits via its own hook system (Stop, SubagentStop,
PreToolUse, etc.) — independent of the stream-json that we already parse.

Why both? The stream-json gives us tool_use/usage/result lines per turn. Hooks
give us higher-level lifecycle signals that survive even if the stream-json
pipe breaks. They're cheap belt-and-suspenders.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import UUID

log = logging.getLogger(__name__)


def write_worktree_settings(
    *,
    worktree_path: Path,
    instance_id: UUID,
    api_base_url: str,
) -> None:
    """Drop a ``.claude/settings.json`` inside the worktree.

    The hooks POST JSON to /api/instances/{id}/hook with the hook name +
    payload. Failures are silent (curl exits non-zero but Claude continues —
    we don't want a flaky daemon to wedge the agent).
    """
    claude_dir = worktree_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # ``curl --fail-with-body`` is GNU/recent curl; ``-sS`` keeps it quiet but
    # surfaces errors. ``--max-time 5`` guards against a hung daemon.
    callback = (
        f'curl -sS --max-time 5 -X POST '
        f'-H "Content-Type: application/json" '
        f'-d "@-" '
        f'{api_base_url}/api/instances/{instance_id}/hook'
    )

    settings_data = {
        "hooks": {
            # Stop: agent ended its turn (final assistant message done).
            "Stop": [{"command": f'{callback}?name=Stop'}],
            # SubagentStop: a Task subagent finished.
            "SubagentStop": [{"command": f'{callback}?name=SubagentStop'}],
        },
    }

    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(settings_data, indent=2), encoding="utf-8")
    log.debug("wrote %s with daemon callback", settings_path)
