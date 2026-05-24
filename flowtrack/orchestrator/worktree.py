"""Git worktree lifecycle for orchestrator instances.

Each instance gets its own worktree so two ``dev`` agents on different tasks do
not stomp on each other's files. Cheap: ``git worktree add`` is metadata + a
checkout, not a full clone.

Cleanup is intentionally NOT automatic when the instance finishes — the worktree
is forensic data (you may want to inspect what the agent did). Use
``remove_worktree`` from a separate cron / CLI job.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import UUID

log = logging.getLogger(__name__)


class WorktreeError(RuntimeError):
    pass


async def create_worktree(
    *,
    target_repo: Path,
    worktree_root: Path,
    instance_id: UUID,
    branch_name: str,
    base_branch: str = "HEAD",
) -> Path:
    """Create ``<worktree_root>/<instance_id>/`` on a fresh branch.

    Returns the absolute path to the new worktree. Raises WorktreeError on any
    git failure (caller marks instance as failed).
    """
    worktree_root.mkdir(parents=True, exist_ok=True)
    path = (worktree_root / str(instance_id)).resolve()

    if path.exists():
        # Should never happen (instance_id is fresh) but be defensive.
        raise WorktreeError(f"worktree path already exists: {path}")

    rc, out, err = await _run_git(
        target_repo,
        "worktree", "add", "-b", branch_name, str(path), base_branch,
    )
    if rc != 0:
        raise WorktreeError(
            f"git worktree add failed (rc={rc}): {err.strip() or out.strip()}"
        )
    log.info("created worktree %s on branch %s", path, branch_name)
    return path


async def remove_worktree(*, target_repo: Path, worktree_path: Path) -> None:
    """Best-effort remove. Logs but does not raise on failure."""
    rc, out, err = await _run_git(
        target_repo, "worktree", "remove", "--force", str(worktree_path)
    )
    if rc != 0:
        log.warning(
            "git worktree remove failed for %s (rc=%d): %s",
            worktree_path, rc, err.strip() or out.strip(),
        )
    else:
        log.info("removed worktree %s", worktree_path)


async def _run_git(cwd: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
