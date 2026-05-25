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
import subprocess
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


def make_commit() -> None:
    """Make a tiny commit so smokes can verify branch chaining.

    The dev role's commit must be visible from the reviewer's branch, and the
    reviewer's commit must be visible from qa's. If branch chaining is broken,
    each role forks from HEAD and the descendant checks fail.
    """
    instance_short = (INSTANCE_ID or "no-instance")[:8]
    marker = Path(f"mock-{instance_short}.txt")
    marker.write_text(f"instance={INSTANCE_ID}\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "mock", "GIT_AUTHOR_EMAIL": "mock@flowtrack",
           "GIT_COMMITTER_NAME": "mock", "GIT_COMMITTER_EMAIL": "mock@flowtrack"}
    subprocess.run(["git", "add", str(marker)], check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"mock: {instance_short}", "--no-verify"],
        check=True, env=env, capture_output=True,
    )


def main() -> int:
    # Assert the spawner wrote .claude/settings.json into our cwd (the worktree).
    settings_path = Path(".claude/settings.json")
    if not settings_path.exists():
        sys.stderr.write("mock: expected .claude/settings.json in cwd, not found\n")
        return 1

    try:
        make_commit()
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"mock: git commit failed: {e}\n")
        return 2

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
