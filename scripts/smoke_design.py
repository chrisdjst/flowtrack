"""Smoke for the conditional design stage.

Scenarios (mock claude, $0):
  SPEC — pipeline_routing=design_dev task admitted via the PO (exercising
    routing): design instance emits UI_SPEC_START..UI_SPEC_END; assert
    tasks.ui_spec stored, job chain starts design -> dev, task reaches done.
  SKIP — same routing, design emits SKIP: ui_spec stays NULL, dev still
    runs, task reaches done.
  DEV_ONLY — pipeline_routing=dev_only: PO admits straight to dev, no
    design job at all (pure DB check, no orchestrator run).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid as _uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOCK = REPO / "scripts" / "mock_claude.py"
WORKTREES = REPO / ".smoke-worktrees"

_MOCK_SPEC = (
    "UI_SPEC_START Login form: username + password fields, submit button, "
    "error toast on 401, loading state on submit. UI_SPEC_END"
)


async def cleanup() -> None:
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
    from sqlalchemy import delete
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import ResourceLock
    db = SessionLocal()
    try:
        db.execute(delete(ResourceLock).where(ResourceLock.resource_key.like("module:smoke-design%")))
        db.commit()
    finally:
        db.close()


async def _drive_scenario(design_verdict: str) -> tuple[bool, dict]:
    """SPEC or SKIP scenario: PO-admit one design_dev task, run the chain."""
    tag = "spec" if "UI_SPEC_START" in design_verdict else "skip"
    worker_id = f"smoke-design-{tag}-{_uuid.uuid4().hex[:6]}"
    module_hint = f"smoke-design-{tag}-{_uuid.uuid4().hex[:6]}"

    env = {
        **os.environ,
        "FLOWTRACK_DATABASE_URL": "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
        "FLOWTRACK_ORCHESTRATOR_DRY_RUN": "false",
        "FLOWTRACK_MAX_CONCURRENT_INSTANCES": "1",
        "FLOWTRACK_ORCHESTRATOR_LOOP_INTERVAL_SECONDS": "0.3",
        "FLOWTRACK_CLAUDE_EXECUTABLE": f'"{sys.executable}" "{MOCK}"',
        "FLOWTRACK_TARGET_REPO_PATH": str(REPO),
        "FLOWTRACK_WORKTREE_ROOT": str(WORKTREES),
        "FLOWTRACK_WORKER_ID": worker_id,
        "FLOWTRACK_CLAUDE_MOCK_VERDICT_DESIGN": design_verdict,
        # reviewer/qa fall back to the default APPROVE PASS
    }

    code = f"""
import asyncio
from sqlalchemy import select
from flowtrack.agents.po import admit_ready_tasks
from flowtrack.core.database import SessionLocal
from flowtrack.models import Task
from flowtrack.models.task import PipelineRouting, TaskPriority, TaskStatus
from flowtrack.orchestrator.loop import run_orchestrator


async def main():
    db = SessionLocal()
    try:
        t = Task(title='design-smoke {tag}', status=TaskStatus.TODO,
                 priority=TaskPriority.URGENT, is_urgent=True, is_overdue=True,
                 ticket_id='SMOKE-DESIGN-{tag}', module_hint='{module_hint}',
                 acceptance_criteria='ok',
                 pipeline_routing=PipelineRouting.DESIGN_DEV)
        db.add(t); db.flush()
        task_id = t.id
        # PO admission with limit=1: our urgent+overdue task outranks
        # everything else in the dev DB, so exactly this one is admitted —
        # and routing must send it to the design role.
        admitted = admit_ready_tasks(db, limit=1, worker_id='{worker_id}')
        assert [a.task_id for a in admitted] == [task_id], admitted
        assert admitted[0].entry_role == 'design', admitted[0]
        db.commit()
    finally:
        db.close()
    print(f'TASK_ID={{task_id}}', flush=True)

    stop = asyncio.Event()
    runner = asyncio.create_task(run_orchestrator(stop))

    for _ in range(240):
        await asyncio.sleep(0.5)
        db = SessionLocal()
        try:
            status = db.scalar(select(Task.status).where(Task.id == task_id))
            if status and status.value in ('blocked', 'done'):
                await asyncio.sleep(1.0)
                break
        finally:
            db.close()

    stop.set()
    try:
        await asyncio.wait_for(runner, timeout=10)
    except asyncio.TimeoutError:
        runner.cancel()


asyncio.run(main())
"""

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code,
        cwd=str(REPO), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out_str = stdout.decode("utf-8", "replace")
    err_str = stderr.decode("utf-8", "replace")
    task_id = None
    for line in out_str.splitlines():
        if line.startswith("TASK_ID="):
            task_id = _uuid.UUID(line.split("=", 1)[1])
            break
    if task_id is None:
        print(f"  subprocess stdout: {out_str[:400]}")
        print(f"  subprocess stderr: {err_str[:800]}")
        return False, {"error": "no TASK_ID emitted"}
    if err_str and ("Error" in err_str or "Traceback" in err_str):
        print(f"  subprocess stderr: {err_str[:800]}")

    from sqlalchemy import select
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Job, Role, Task, TaskTransition

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        roles_by_id = {r.id: r.name for r in db.scalars(select(Role))}
        jobs = list(db.scalars(
            select(Job).where(Job.task_id == task_id).order_by(Job.created_at)
        ))
        transitions = list(db.scalars(
            select(TaskTransition).where(TaskTransition.task_id == task_id)
            .order_by(TaskTransition.transitioned_at)
        ))
        job_roles = [roles_by_id[j.role_id] for j in jobs]
        diag = {
            "tag": tag,
            "task_status": task.status.value,
            "ui_spec": (task.ui_spec or "")[:60] or None,
            "jobs": job_roles,
            "transitions": [(t.from_status, t.to_status, t.reason) for t in transitions],
        }
    finally:
        db.close()

    if tag == "spec":
        ok = (
            task.status.value == "done"
            and job_roles[:2] == ["design", "dev"]
            and task.ui_spec is not None
            and "Login form" in task.ui_spec
            and "UI_SPEC_START" not in task.ui_spec  # markers stripped
        )
    else:  # skip
        ok = (
            task.status.value == "done"
            and job_roles[:2] == ["design", "dev"]
            and task.ui_spec is None
        )
    return ok, diag


def _dev_only_check() -> tuple[bool, dict]:
    """dev_only task: PO admits straight to dev, no design job. Pure DB."""
    from sqlalchemy import delete, select
    from flowtrack.agents.po import admit_ready_tasks
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Job, Role, Task
    from flowtrack.models.task import PipelineRouting, TaskPriority, TaskStatus

    db = SessionLocal()
    try:
        t = Task(title=f"design-smoke dev-only {_uuid.uuid4().hex[:6]}",
                 status=TaskStatus.TODO, priority=TaskPriority.URGENT,
                 is_urgent=True, is_overdue=True, acceptance_criteria="ok",
                 pipeline_routing=PipelineRouting.DEV_ONLY)
        db.add(t)
        db.flush()
        admitted = admit_ready_tasks(db, limit=1)
        db.commit()

        dev_role_id = db.scalar(select(Role.id).where(Role.name == "dev"))
        jobs = list(db.scalars(select(Job).where(Job.task_id == t.id)))
        diag = {
            "admitted": [str(a.task_id)[:8] for a in admitted],
            "entry_role": admitted[0].entry_role if admitted else None,
            "job_roles": ["dev" if j.role_id == dev_role_id else "OTHER" for j in jobs],
        }
        ok = (
            len(admitted) == 1 and admitted[0].task_id == t.id
            and admitted[0].entry_role == "dev"
            and len(jobs) == 1 and jobs[0].role_id == dev_role_id
        )
        # cleanup fixture
        db.execute(delete(Job).where(Job.task_id == t.id))
        db.delete(t)
        db.commit()
        return ok, diag
    finally:
        db.close()


async def main() -> int:
    await cleanup()
    overall_ok = True

    for verdict in (_MOCK_SPEC, "SKIP"):
        ok, diag = await _drive_scenario(verdict)
        print()
        print(f"=== DESIGN {diag.get('tag', '?').upper()} ===")
        for k, v in diag.items():
            print(f"  {k} = {v}")
        print(f"  -> {'PASS' if ok else 'FAIL'}")
        overall_ok = overall_ok and ok

    ok, diag = _dev_only_check()
    print()
    print("=== DEV_ONLY ===")
    for k, v in diag.items():
        print(f"  {k} = {v}")
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    overall_ok = overall_ok and ok

    await cleanup()
    print()
    print("RESULT:", "PASS" if overall_ok else "FAIL")
    return 0 if overall_ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
