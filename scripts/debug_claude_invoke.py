"""Reproduce the spawner's claude invocation and dump stdout+stderr.

Use this to debug why claude exits 1 on the first real-Claude smoke. Costs
~$0.001 per run.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


async def main() -> int:
    # Minimal — only what the spawner uses (no append-system-prompt, no
    # allowed-tools). Build up from minimum so we can isolate the failure.
    cmd_minimal = [
        "claude",
        "--print", "say only 'ok'",
        "--output-format", "stream-json",
        "--max-budget-usd", "0.02",
        "--verbose",
    ]
    print("=== minimal: " + " ".join(cmd_minimal))
    with tempfile.TemporaryDirectory() as cwd:
        # Make it a git repo so claude is happy.
        await asyncio.create_subprocess_exec("git", "init", "-q", cwd=cwd)
        proc = await asyncio.create_subprocess_exec(
            *cmd_minimal,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            print("TIMEOUT")
            return 1
        print(f"rc={proc.returncode}")
        print("--- stdout events (parsed line-by-line) ---")
        import json
        for line in stdout.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                t = e.get("type", "?")
                sub = e.get("subtype", "")
                # condense long fields
                payload_keys = list(e.keys())
                print(f"  {t}/{sub:20s} keys={payload_keys}")
                if t == "result":
                    print(f"    {json.dumps(e, default=str)[:600]}")
                if t == "system" and sub == "init":
                    print(f"    cwd={e.get('cwd')} session_id={e.get('session_id')}")
            except json.JSONDecodeError:
                print(f"  RAW: {line[:200]}")
        print("--- stderr ---")
        print(stderr.decode("utf-8", "replace")[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
