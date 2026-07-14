"""Smoke for the specialization dispatch layer ($0, mock claude in E2E).

In-process scenarios:
  PATTERN   — variant with match_patterns wins for a matching module_hint;
              non-matching hint falls back to the base role.
  PROJECT   — project-scoped variant (score 2) beats a pattern variant
              (score 1) for tasks of that project; is ineligible for tasks
              of another project (pattern variant wins there).
  AMBIGUITY — two variants tied -> base role + TaskComment flag.
  IDENTITY  — roles without variants resolve exactly like the old name
              lookup (task or no task).

E2E scenario:
  VARIANT CHAIN — a dispatched dev VARIANT runs the full pipeline: chain
  shape inherited from the base (variant row leaves next_role_name NULL),
  stage transitions recorded under the BASE name, task reaches done.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid as _uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

MOCK = REPO / "scripts" / "mock_claude.py"
WORKTREES = REPO / ".smoke-worktrees"
TAG = _uuid.uuid4().hex[:6]


def check_inprocess() -> bool:
    from sqlalchemy import delete, select

    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Project, Role, Task, TaskComment
    from flowtrack.models.task import TaskPriority, TaskStatus
    from flowtrack.orchestrator.dispatch import resolve_role

    def make_task(db, *, hint=None, project_id=None):
        t = Task(title=f"dispatch-smoke-{TAG}", status=TaskStatus.TODO,
                 priority=TaskPriority.MEDIUM, module_hint=hint,
                 project_id=project_id)
        db.add(t)
        db.flush()
        return t

    db = SessionLocal()
    ok = True
    try:
        project = Project(name=f"smoke-proj-{TAG}")
        db.add(project)
        db.flush()

        v_front = Role(name=f"dev-front-{TAG}", system_prompt="variant",
                       base_role_name="dev", specialization="frontend",
                       match_patterns=["web*"])
        v_proj = Role(name=f"dev-proj-{TAG}", system_prompt="variant",
                      base_role_name="dev", specialization="project-x",
                      project_id=project.id)
        db.add_all([v_front, v_proj])
        db.flush()

        base_dev = db.scalar(select(Role).where(
            Role.name == "dev", Role.base_role_name.is_(None)))
        base_reviewer = db.scalar(select(Role).where(
            Role.name == "reviewer", Role.base_role_name.is_(None)))

        checks: list[tuple[str, bool]] = []

        # PATTERN
        t = make_task(db, hint="web/login")
        checks.append(("pattern match -> variant",
                       resolve_role(db, "dev", task=t).id == v_front.id))
        t = make_task(db, hint="docs")
        checks.append(("no pattern match -> base",
                       resolve_role(db, "dev", task=t).id == base_dev.id))

        # PROJECT
        t = make_task(db, hint="web/login", project_id=project.id)
        checks.append(("project (2) beats pattern (1)",
                       resolve_role(db, "dev", task=t).id == v_proj.id))
        other = Project(name=f"smoke-proj-other-{TAG}")
        db.add(other)
        db.flush()
        t = make_task(db, hint="web/login", project_id=other.id)
        checks.append(("other project -> project variant ineligible, pattern wins",
                       resolve_role(db, "dev", task=t).id == v_front.id))

        # AMBIGUITY: second pattern variant that also matches 'web/login'
        v_front2 = Role(name=f"dev-front2-{TAG}", system_prompt="variant",
                        base_role_name="dev", specialization="frontend-2",
                        match_patterns=["web/*"])
        db.add(v_front2)
        db.flush()
        t_amb = make_task(db, hint="web/login")
        resolved = resolve_role(db, "dev", task=t_amb)
        comments = list(db.scalars(select(TaskComment).where(
            TaskComment.task_id == t_amb.id)))
        checks.append(("ambiguous tie -> base", resolved.id == base_dev.id))
        checks.append(("ambiguity flagged via TaskComment",
                       len(comments) == 1 and "Dispatch ambiguity" in comments[0].body))

        # IDENTITY (no variants for reviewer)
        t = make_task(db, hint="web/login")
        checks.append(("no variants -> identical to name lookup",
                       resolve_role(db, "reviewer", task=t).id == base_reviewer.id))
        checks.append(("task=None -> base",
                       resolve_role(db, "dev").id == base_dev.id))
        checks.append(("missing base -> None",
                       resolve_role(db, f"nonexistent-{TAG}") is None))

        for label, passed in checks:
            print(f"  [{'ok' if passed else 'FAIL'}] {label}")
            ok = ok and passed
    finally:
        db.rollback()  # discard scratch rows in one go — nothing was committed
        db.close()
    return ok


async def check_variant_chain() -> bool:
    """Committed dev variant + orchestrator subprocess: dispatched variant
    runs, chain shape falls back to the base, task reaches done."""
    from sqlalchemy import delete, select

    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Instance, Role, TaskTransition

    variant_name = f"dev-e2e-{TAG}"
    hint = f"smoke-dispatch-{TAG}"
    worker_id = f"smoke-dispatch-{TAG}"

    db = SessionLocal()
    try:
        db.add(Role(name=variant_name, system_prompt="specialized dev variant",
                    base_role_name="dev", specialization="e2e",
                    match_patterns=[f"{hint}*"]))
        db.commit()
    finally:
        db.close()

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
    }
    code = f"""
import asyncio
from sqlalchemy import select
from flowtrack.core.database import SessionLocal
from flowtrack.models import Job, Task
from flowtrack.models.task import TaskPriority, TaskStatus
from flowtrack.orchestrator.dispatch import resolve_role
from flowtrack.orchestrator.loop import run_orchestrator


async def main():
    db = SessionLocal()
    try:
        t = Task(title='dispatch-e2e-{TAG}', status=TaskStatus.TODO,
                 priority=TaskPriority.HIGH, ticket_id='SMOKE-DISP-{TAG}',
                 module_hint='{hint}', acceptance_criteria='ok')
        db.add(t); db.flush()
        task_id = t.id
        role = resolve_role(db, 'dev', task=t)
        assert role.name == '{variant_name}', role.name
        db.add(Job(task_id=task_id, role_id=role.id, priority=10,
                   worker_id='{worker_id}'))
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
                await asyncio.sleep(1.5)
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
        sys.executable, "-c", code, cwd=str(REPO), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    task_id = None
    for line in stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("TASK_ID="):
            task_id = _uuid.UUID(line.split("=", 1)[1])
    if task_id is None:
        print(f"  subprocess stderr: {stderr.decode('utf-8', 'replace')[:800]}")
        return False

    from flowtrack.models import Task
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        roles_by_id = {r.id: r.name for r in db.scalars(select(Role))}
        instance_roles = [roles_by_id[i.role_id] for i in db.scalars(
            select(Instance).where(Instance.task_id == task_id)
            .order_by(Instance.spawned_at))]
        reasons = [t.reason for t in db.scalars(
            select(TaskTransition).where(TaskTransition.task_id == task_id)
            .order_by(TaskTransition.transitioned_at))]
        diag = {
            "task_status": task.status.value,
            "instance_roles": instance_roles,
            "transition_reasons": reasons,
        }
        ok = (
            task.status.value == "done"
            and instance_roles == [variant_name, "reviewer", "qa"]
            # transitions recorded under BASE names (merge gate compatible)
            and "pipeline: dev completed" in reasons
            and "pipeline: qa completed" in reasons
        )
    finally:
        db.close()

    print("=== VARIANT CHAIN (e2e) ===")
    for k, v in diag.items():
        print(f"  {k} = {v}")
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return ok


def cleanup() -> None:
    from sqlalchemy import delete, select, update

    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Instance, Job, Project, Role, Task, TaskComment

    if WORKTREES.exists():
        shutil.rmtree(WORKTREES, ignore_errors=True)
    db = SessionLocal()
    try:
        db.execute(delete(TaskComment).where(TaskComment.task_id.in_(
            select(Task.id).where(Task.title.like("dispatch-smoke-%")))))
        db.execute(delete(Task).where(Task.title.like("dispatch-smoke-%")))
        # E2E variant rows are referenced by instances/jobs (FK) — repoint
        # that smoke residue at the base dev role, then drop the variants.
        base_dev_id = db.scalar(select(Role.id).where(
            Role.name == "dev", Role.base_role_name.is_(None)))
        variant_ids = select(Role.id).where(Role.base_role_name.is_not(None),
                                            Role.name.like("dev-e2e-%"))
        if base_dev_id is not None:
            db.execute(update(Instance).where(Instance.role_id.in_(variant_ids))
                       .values(role_id=base_dev_id))
            db.execute(update(Job).where(Job.role_id.in_(variant_ids))
                       .values(role_id=base_dev_id))
        db.execute(delete(Role).where(Role.base_role_name.is_not(None),
                                      Role.name.like("dev-e2e-%")))
        db.execute(delete(Project).where(Project.name.like("smoke-proj-%")))
        db.commit()
    finally:
        db.close()


def main() -> int:
    cleanup()
    ok = check_inprocess()
    ok = asyncio.run(check_variant_chain()) and ok
    cleanup()
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
