"""Mock ``claude`` CLI for orchestrator smoke testing.

Ignores all args. Emits a tiny stream-json sequence on stdout, exits 0.

Also exercises two integration points the spawner sets up:
  - asserts ``.claude/settings.json`` was written in the worktree (cwd)
  - posts to ``/api/instances/{id}/hook?name=Stop`` to simulate the real
    Claude Code Stop hook firing.

Not for production use.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

API_URL = os.environ.get("FLOWTRACK_API_URL")
INSTANCE_ID = os.environ.get("FLOWTRACK_INSTANCE_ID")


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def post_hook(name: str, body: dict) -> None:
    if not (API_URL and INSTANCE_ID):
        return
    url = f"{API_URL}/api/instances/{INSTANCE_ID}/hook?name={name}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:
        sys.stderr.write(f"mock: hook POST failed: {e}\n")


def main() -> int:
    # Assert the spawner wrote .claude/settings.json into our cwd (the worktree).
    settings_path = Path(".claude/settings.json")
    if not settings_path.exists():
        sys.stderr.write("mock: expected .claude/settings.json in cwd, not found\n")
        return 1

    emit({"type": "message", "role": "system", "content": "mock claude online"})
    time.sleep(0.1)
    emit({"type": "thinking", "content": "Looking at the task..."})
    emit({"type": "tool_use", "tool": "Read", "params": {"file_path": "README.md"}})
    emit({"type": "message", "role": "assistant", "content": "I read the file."})
    emit({"type": "usage", "input_tokens": 1234, "output_tokens": 567})
    time.sleep(0.1)
    emit({"type": "tool_use", "tool": "Edit", "params": {"file_path": "noop.txt"}})
    emit({"type": "usage", "input_tokens": 800, "output_tokens": 300})
    emit({"type": "result", "exit_reason": "end_turn"})

    # Simulate the Stop hook firing — what real Claude Code would do via the
    # settings.json command we just verified exists.
    post_hook("Stop", {"reason": "mock end_turn", "instance_id": INSTANCE_ID})
    return 0


if __name__ == "__main__":
    sys.exit(main())
