"""Mock ``claude`` CLI for orchestrator smoke testing.

Ignores all args. Emits a tiny stream-json sequence on stdout, exits 0.
Not for production use — the real ``claude`` is what production points at.
"""

import json
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
