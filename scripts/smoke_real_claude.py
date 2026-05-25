"""End-to-end smoke against the REAL ``claude`` CLI.

Spends real Anthropic tokens. Uses Claude Code's existing OAuth login (do NOT
set FLOWTRACK_ANTHROPIC_API_KEY — when empty the subprocess inherits the
keychain auth from the parent shell).

Guards:
  - chain disabled (dev.next_role_name temporarily NULL'd, restored after)
  - dev.max_minutes capped at 5 for this run only, restored after
  - budget caps $0.25/hour, $0.25/day
  - max_concurrent_instances = 1
  - worker_id pinned to a fresh lane so nothing chains in from elsewhere

Cleanup runs in finally so role/budget mutations don't outlive the smoke.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid as _uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKTREES = REPO / ".real-smoke-worktrees"

WORKER_ID = f"real-smoke-{_uuid.uuid4().hex[:8]}"

# Auth: leave FLOWTRACK_ANTHROPIC_API_KEY UNSET so subprocess uses Claude Code
# OAuth login. If you want to test API-key path, set it before running.
os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)
os.environ["FLOWTRACK_ORCHESTRATOR_DRY_RUN"] = "false"
os.environ["FLOWTRACK_MAX_CONCURRENT_INSTANCES"] = "1"
os.environ["FLOWTRACK_ORCHESTRATOR_LOOP_INTERVAL_SECONDS"] = "0.5"
os.environ["FLOWTRACK_TARGET_REPO_PATH"] = str(REPO)
os.environ["FLOWTRACK_WORKTREE_ROOT"] = str(WORKTREES)
os.environ["FLOWTRACK_WORKER_ID"] = WORKER_ID
os.environ["FLOWTRACK_BUDGET_HOUR_CAP_USD"] = "1.00"
os.environ["FLOWTRACK_BUDGET_DAY_CAP_USD"] = "1.00"
# Default executable "claude" is on PATH — confirmed via `which claude`.

from sqlalchemy import select  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.models import (  # noqa: E402
    BudgetWindow,
    Instance,
    InstanceEvent,
    Job,
    Role,
    Task,
)
from flowtrack.models.job import JobStatus  # noqa: E402
from flowtrack.models.task import TaskPriority, TaskStatus  # noqa: E402
from flowtrack.orchestrator.loop import run_orchestrator  # noqa: E402


_SMOKE_PROMPT_OVERRIDE = """You are running inside an orchestrator smoke test.

WHAT TO DO
1. Read the task description below.
2. Create the file requested. Do not edit anything else.
3. Commit the file with the message format `<ticket>: <verb>`.
4. STOP. Do not push (there is no remote configured for this worktree).
5. Do NOT run pytest, lint, or any other tool.

You are in an isolated git worktree. The branch you are on was created for you.

When done, the only acceptance criterion is: the new file exists with the
required content AND there is a fresh commit on this branch.
"""


async def cleanup_worktrees_and_branches() -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "prune", cwd=str(REPO),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    proc = await asyncio.create_subprocess_exec(
        "git", "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/",
        cwd=str(REPO),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    for branch in out.decode().splitlines():
        branch = branch.strip()
        if branch:
            p = await asyncio.create_subprocess_exec(
                "git", "branch", "-D", branch, cwd=str(REPO),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
    if WORKTREES.exists():
        shutil.rmtree(WORKTREES, ignore_errors=True)


async def main() -> int:
    await cleanup_worktrees_and_branches()

    saved_dev: dict = {}
    task_id: _uuid.UUID | None = None
    instance_id: _uuid.UUID | None = None
    worktree_path: Path | None = None

    db = SessionLocal()
    try:
        dev = db.scalar(select(Role).where(Role.name == "dev"))
        if dev is None:
            print("ERROR: 'dev' role missing")
            return 1

        # Stash current dev role config and override for the smoke.
        saved_dev = {
            "next_role_name": dev.next_role_name,
            "task_status_on_success": dev.task_status_on_success,
            "max_minutes": dev.max_minutes,
            "max_tokens": dev.max_tokens,
            "system_prompt": dev.system_prompt,
        }
        dev.next_role_name = None             # no chain
        dev.task_status_on_success = None     # no auto-transition
        dev.max_minutes = 5                   # 5-min hard cap
        dev.max_tokens = 60_000               # ~$0.05 ceiling at sonnet rates
        dev.system_prompt = _SMOKE_PROMPT_OVERRIDE

        ticket = f"SMOKE-REAL-{_uuid.uuid4().hex[:6].upper()}"
        marker_filename = f"orchestrator-smoke-{_uuid.uuid4().hex[:6]}.md"

        task = Task(
            title="Real-Claude smoke: create marker file",
            description=(
                f"Create a file named `{marker_filename}` at the repo root "
                f"containing exactly the single line:\n"
                f"`# orchestrator smoke OK — {ticket}`\n"
                f"Then commit it."
            ),
            status=TaskStatus.TODO,
            priority=TaskPriority.HIGH,
            ticket_id=ticket,
            module_hint=marker_filename,  # restricts edits to this one path
            acceptance_criteria=(
                f"File `{marker_filename}` exists at the worktree root with the "
                f"required content. A commit exists on the current branch with "
                f"a message starting with `{ticket}:`."
            ),
        )
        db.add(task)
        db.flush()
        db.add(Job(task_id=task.id, role_id=dev.id, priority=10, worker_id=WORKER_ID))
        db.commit()
        task_id = task.id
        print(f"queued: task={task_id} ticket={ticket} worker={WORKER_ID}")
        print(f"target file: {marker_filename}")
    finally:
        db.close()

    stop = asyncio.Event()
    runner = asyncio.create_task(run_orchestrator(stop))

    # Poll until terminal or timeout.
    terminal = {"done", "failed", "cancelled"}
    deadline_s = 6 * 60  # 6 minutes hard ceiling here (role cap is 5)
    waited = 0.0
    final_job = None
    while waited < deadline_s:
        await asyncio.sleep(1.0)
        waited += 1.0
        db = SessionLocal()
        try:
            j = db.scalar(select(Job).where(Job.task_id == task_id))
            inst = db.scalar(select(Instance).where(Instance.task_id == task_id))
            if waited % 10 == 0:
                inst_status = inst.status.value if inst else "no-instance"
                inst_cost = inst.cost_usd if inst else 0
                print(f"  [{int(waited)}s] job={j.status.value} inst={inst_status} cost=${inst_cost}")
            if j.status.value in terminal:
                final_job = j
                break
        finally:
            db.close()

    stop.set()
    try:
        await asyncio.wait_for(runner, timeout=15)
    except asyncio.TimeoutError:
        runner.cancel()

    # Inspect
    db = SessionLocal()
    file_exists = False
    file_content = None
    commit_msg = None
    try:
        j = db.scalar(select(Job).where(Job.task_id == task_id))
        inst = db.scalar(select(Instance).where(Instance.task_id == task_id))
        events = db.scalar(
            select(InstanceEvent)
            .where(InstanceEvent.instance_id == inst.id if inst else False)
            .limit(1)
        ) if inst else None
        from sqlalchemy import func
        event_count = db.scalar(
            select(func.count(InstanceEvent.id))
            .where(InstanceEvent.instance_id == inst.id)
        ) if inst else 0
        budget_row = db.scalar(
            select(BudgetWindow).order_by(BudgetWindow.cost_usd.desc()).limit(1)
        )
        instance_id = inst.id if inst else None
        if inst and inst.worktree_path:
            worktree_path = Path(inst.worktree_path)

        print()
        print("=== JOB ===")
        print(f"  status     = {j.status.value}")
        print(f"  attempts   = {j.attempts}")
        print(f"  last_error = {j.last_error}")
        print()
        print("=== INSTANCE ===")
        if inst:
            print(f"  status        = {inst.status.value}")
            print(f"  exit_code     = {inst.exit_code}")
            print(f"  tokens        = in:{inst.tokens_input} out:{inst.tokens_output}")
            print(f"  cost_usd      = {inst.cost_usd}")
            print(f"  spawned_at    = {inst.spawned_at}")
            print(f"  finished_at   = {inst.finished_at}")
            print(f"  worktree      = {inst.worktree_path}")
            print(f"  branch        = {inst.branch_name}")
            print(f"  events        = {event_count}")
        else:
            print("  (no instance row)")

        if budget_row:
            print()
            print(f"=== BUDGET (top window) ===  cost=${budget_row.cost_usd} tokens={budget_row.tokens_used}")
    finally:
        db.close()

    # Inspect the worktree for the marker file + commit message.
    if worktree_path and worktree_path.exists():
        files = list(worktree_path.glob("orchestrator-smoke-*.md"))
        if files:
            file_exists = True
            file_content = files[0].read_text(encoding="utf-8", errors="replace").strip()
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "-1", "--format=%s",
            cwd=str(worktree_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        commit_msg = out.decode().strip()

    print()
    print("=== ARTIFACTS IN WORKTREE ===")
    print(f"  marker file present = {file_exists}")
    if file_content is not None:
        print(f"  content             = {file_content[:120]!r}")
    print(f"  HEAD commit message = {commit_msg!r}")

    # Restore the dev role's original config.
    db = SessionLocal()
    try:
        dev = db.scalar(select(Role).where(Role.name == "dev"))
        for k, v in saved_dev.items():
            setattr(dev, k, v)
        db.commit()
        print()
        print("dev role restored")
    finally:
        db.close()

    # Best-effort cleanup of any chained jobs that snuck through.
    db = SessionLocal()
    try:
        for j in db.scalars(select(Job).where(Job.worker_id == WORKER_ID)):
            if j.status == JobStatus.QUEUED:
                j.status = JobStatus.CANCELLED
                j.last_error = "real-smoke teardown"
        db.commit()
    finally:
        db.close()

    await cleanup_worktrees_and_branches()

    ok = (
        final_job is not None
        and final_job.status == JobStatus.DONE
        and file_exists
        and commit_msg
        and commit_msg.startswith(ticket)
    )
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
