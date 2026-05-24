"""End-to-end smoke for the role pipeline (dev → reviewer → qa → done).

Queues a single dev Job on a fresh Task and waits for the orchestrator to chain
the three roles automatically. Asserts:
  - 3 instances created, one per role
  - 3 jobs (all done)
  - 3 transitions: todo→in_review, in_review→in_qa, in_qa→done
  - task.status ends at 'done'

Uses scripts/mock_claude.py as the executable — no real Claude tokens spent.
Limitation: each role's worktree is forked from HEAD (not from the previous
role's branch). Mock doesn't commit so this doesn't matter for the test, but
real runs need branch-chaining (TODO in spawner.create_worktree call site).

Prereqs: Postgres at FLOWTRACK_DATABASE_URL, schema at 006.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOCK = REPO / "scripts" / "mock_claude.py"
WORKTREES = REPO / ".smoke-worktrees"

os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)
os.environ["FLOWTRACK_ORCHESTRATOR_DRY_RUN"] = "false"
os.environ["FLOWTRACK_MAX_CONCURRENT_INSTANCES"] = "1"
os.environ["FLOWTRACK_ORCHESTRATOR_LOOP_INTERVAL_SECONDS"] = "0.3"
os.environ["FLOWTRACK_CLAUDE_EXECUTABLE"] = f'"{sys.executable}" "{MOCK}"'
os.environ["FLOWTRACK_TARGET_REPO_PATH"] = str(REPO)
os.environ["FLOWTRACK_WORKTREE_ROOT"] = str(WORKTREES)

from sqlalchemy import func, select  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.models import (  # noqa: E402
    Instance,
    Job,
    Role,
    Task,
    TaskTransition,
)
from flowtrack.models.task import TaskPriority, TaskStatus  # noqa: E402
from flowtrack.orchestrator.loop import run_orchestrator  # noqa: E402


async def cleanup_worktrees_and_branches() -> None:
    """Best-effort cleanup of all auto/* branches and the worktree dir."""
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "prune", cwd=str(REPO),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    # Delete any auto/* branches
    proc = await asyncio.create_subprocess_exec(
        "git", "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/",
        cwd=str(REPO),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    for branch in out.decode().splitlines():
        branch = branch.strip()
        if not branch:
            continue
        proc = await asyncio.create_subprocess_exec(
            "git", "branch", "-D", branch, cwd=str(REPO),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    if WORKTREES.exists():
        shutil.rmtree(WORKTREES, ignore_errors=True)


async def main() -> int:
    await cleanup_worktrees_and_branches()

    # Seed task + initial dev job.
    db = SessionLocal()
    try:
        dev_role = db.scalar(select(Role).where(Role.name == "dev"))
        if dev_role is None:
            print("ERROR: 'dev' role missing — run alembic upgrade head.")
            return 1
        task = Task(
            title="Pipeline smoke",
            description="Verify dev → reviewer → qa chain runs end-to-end.",
            status=TaskStatus.TODO,
            priority=TaskPriority.HIGH,
            ticket_id="SMOKE-PIPE",
            module_hint="smoke",
            acceptance_criteria="Pipeline reaches status=done.",
        )
        db.add(task)
        db.flush()
        db.add(Job(task_id=task.id, role_id=dev_role.id, priority=10))
        db.commit()
        task_id = task.id
        print(f"queued: task={task_id} role=dev")
    finally:
        db.close()

    # Run loop until task.status == 'done' or timeout.
    stop = asyncio.Event()
    runner = asyncio.create_task(run_orchestrator(stop))
    deadline = 60.0
    waited = 0.0
    while waited < deadline:
        await asyncio.sleep(0.3)
        waited += 0.3
        db = SessionLocal()
        try:
            t = db.get(Task, task_id)
            if t.status == TaskStatus.DONE:
                break
        finally:
            db.close()
    else:
        print(f"WARN: task did not reach 'done' within {deadline}s")

    stop.set()
    try:
        await asyncio.wait_for(runner, timeout=10)
    except asyncio.TimeoutError:
        runner.cancel()

    # Inspect.
    db = SessionLocal()
    try:
        t = db.get(Task, task_id)
        jobs = list(db.scalars(
            select(Job).where(Job.task_id == task_id).order_by(Job.created_at)
        ))
        insts = list(db.scalars(
            select(Instance).where(Instance.task_id == task_id).order_by(Instance.spawned_at)
        ))
        trans = list(db.scalars(
            select(TaskTransition)
            .where(TaskTransition.task_id == task_id)
            .order_by(TaskTransition.transitioned_at)
        ))
        roles_by_id = {r.id: r.name for r in db.scalars(select(Role))}

        print()
        print(f"=== TASK ===  status={t.status.value}")
        print(f"=== JOBS ({len(jobs)}) ===")
        for j in jobs:
            print(f"  {roles_by_id[j.role_id]:9s} status={j.status.value} attempts={j.attempts}")
        print(f"=== INSTANCES ({len(insts)}) ===")
        total_cost = 0
        for i in insts:
            total_cost += float(i.cost_usd)
            print(f"  {roles_by_id[i.role_id]:9s} status={i.status.value} "
                  f"exit={i.exit_code} tokens={i.tokens_input}/{i.tokens_output} "
                  f"cost=${i.cost_usd}")
        print(f"  total cost: ${total_cost:.4f}")
        print(f"=== TRANSITIONS ({len(trans)}) ===")
        for tr in trans:
            print(f"  {tr.from_status or '-':12s} -> {tr.to_status:12s} reason='{tr.reason}'")

        ok = (
            t.status == TaskStatus.DONE
            and len(jobs) == 3
            and all(j.status.value == "done" for j in jobs)
            and len(insts) == 3
            and all(i.status.value == "completed" for i in insts)
            and len(trans) == 3
        )
    finally:
        db.close()

    await cleanup_worktrees_and_branches()
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
