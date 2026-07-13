"""Smoke for agent session auto-tracking + pipeline incidents (mock, $0).

Scenarios:
  CHAIN     — dev -> reviewer -> qa (PASS): one FlowTrack session per
              instance with the right SessionType (development / review /
              testing), ticket_id set, all ENDED with ended_at, and
              Instance.session_id linked.
  INVARIANT — an ACTIVE agent session (instance_id set) is invisible to
              SessionRepository.get_active(), so it can never satisfy or
              violate the CLI's one-active-session rule.
  INCIDENT  — qa BLOCKED opens a '[pipeline] task <id>' incident
              (deployment_id NULL); the devops RESOLVED -> qa PASS arc
              resolves it (resolved_at = the MTTR endpoint).
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

TICKET_PREFIX = "SMOKE-SESS"


def _base_env(worker_id: str) -> dict:
    return {
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


async def _run_subprocess(code: str, env: dict) -> _uuid.UUID | None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, cwd=str(REPO), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    for line in stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("TASK_ID="):
            return _uuid.UUID(line.split("=", 1)[1])
    print(f"  subprocess stdout: {stdout.decode('utf-8', 'replace')[:400]}")
    print(f"  subprocess stderr: {stderr.decode('utf-8', 'replace')[:800]}")
    return None


def _chain_code(*, tag: str, ticket: str, module_hint: str, worker_id: str,
                start_blocked: bool = False) -> str:
    """Subprocess body: seed a task (+ first job) and run until terminal."""
    if start_blocked:
        seed = f"""
        from flowtrack.agents.devops import pickup_infra_failures
        from flowtrack.models.task import BlockedReason
        t = Task(title='sess-smoke {tag}', status=TaskStatus.BLOCKED,
                 priority=TaskPriority.HIGH, ticket_id='{ticket}',
                 module_hint='{module_hint}', acceptance_criteria='ok',
                 blocked_reason=BlockedReason.INFRA_FAILURE)
        db.add(t); db.flush()
        task_id = t.id
        picked = pickup_infra_failures(db, limit=10, worker_id='{worker_id}')
        assert any(p.id == task_id for p in picked), picked
"""
    else:
        seed = f"""
        dev_id = db.scalar(select(Role.id).where(Role.name == 'dev'))
        t = Task(title='sess-smoke {tag}', status=TaskStatus.TODO,
                 priority=TaskPriority.HIGH, ticket_id='{ticket}',
                 module_hint='{module_hint}', acceptance_criteria='ok')
        db.add(t); db.flush()
        task_id = t.id
        db.add(Job(task_id=task_id, role_id=dev_id, priority=10,
                   worker_id='{worker_id}'))
"""
    return f"""
import asyncio
from sqlalchemy import select
from flowtrack.core.database import SessionLocal
from flowtrack.models import Job, Role, Task
from flowtrack.models.task import TaskPriority, TaskStatus
from flowtrack.orchestrator.loop import run_orchestrator


async def main():
    db = SessionLocal()
    try:
{seed}
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


def _sessions_for(db, task_id):
    from sqlalchemy import select
    from flowtrack.models import Instance
    from flowtrack.models.session import Session as AgentSession

    instances = list(db.scalars(
        select(Instance).where(Instance.task_id == task_id)
        .order_by(Instance.spawned_at)))
    sessions = list(db.scalars(
        select(AgentSession).where(AgentSession.instance_id.in_(
            [i.id for i in instances] or [task_id]))
        .order_by(AgentSession.started_at)))
    return instances, sessions


async def check_chain() -> bool:
    tag = "chain"
    worker_id = f"smoke-sess-{tag}-{_uuid.uuid4().hex[:6]}"
    ticket = f"{TICKET_PREFIX}-CHAIN-{_uuid.uuid4().hex[:6]}"
    env = _base_env(worker_id)
    task_id = await _run_subprocess(
        _chain_code(tag=tag, ticket=ticket,
                    module_hint=worker_id, worker_id=worker_id), env)
    if task_id is None:
        return False

    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Task
    from flowtrack.models.session import SessionStatus

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        instances, sessions = _sessions_for(db, task_id)
        types = [s.type.value for s in sessions]
        diag = {
            "task_status": task.status.value,
            "n_instances": len(instances),
            "session_types": types,
            "statuses": [s.status.value for s in sessions],
            "tickets": sorted({s.ticket_id for s in sessions}),
            "ended_at_set": all(s.ended_at is not None for s in sessions),
            "instance_links": all(i.session_id is not None for i in instances),
        }
        ok = (
            task.status.value == "done"
            and types == ["development", "review", "testing"]
            and all(s.status == SessionStatus.ENDED for s in sessions)
            and diag["ended_at_set"]
            and diag["tickets"] == [ticket]
            and diag["instance_links"]
        )
    finally:
        db.close()
    print("=== CHAIN SESSIONS ===")
    for k, v in diag.items():
        print(f"  {k} = {v}")
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return ok, task_id  # task_id reused by the INVARIANT check


async def check_invariant(task_id) -> bool:
    """An ACTIVE agent session must be invisible to the CLI's get_active()."""
    from sqlalchemy import select
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Instance
    from flowtrack.models.session import Session as AgentSession, SessionStatus, SessionType
    from flowtrack.repositories.session_repo import SessionRepository
    from datetime import datetime

    db = SessionLocal()
    try:
        instance = db.scalar(select(Instance).where(Instance.task_id == task_id).limit(1))
        probe = AgentSession(
            type=SessionType.DEVELOPMENT, ticket_id=f"{TICKET_PREFIX}-PROBE",
            started_at=datetime.now(), status=SessionStatus.ACTIVE,
            instance_id=instance.id,
        )
        db.add(probe)
        db.flush()
        active = SessionRepository(db).get_active()
        # The probe must never surface; whatever does surface must be a
        # human session (instance_id None) — e.g. the session tracking
        # this very implementation run.
        ok = (active is None or active.instance_id is None) and (
            active is None or active.id != probe.id)
        db.delete(probe)
        db.commit()
    finally:
        db.close()
    print("=== CLI INVARIANT ===")
    print(f"  active_is_agent_session = {not ok}")
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return ok


async def check_incident() -> bool:
    from sqlalchemy import select
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Incident, Task

    # Leg 1: chain with qa BLOCKED -> infra block opens the incident.
    tag = "incident"
    worker_id = f"smoke-sess-{tag}-{_uuid.uuid4().hex[:6]}"
    ticket = f"{TICKET_PREFIX}-INC-{_uuid.uuid4().hex[:6]}"
    env = _base_env(worker_id)
    env["FLOWTRACK_CLAUDE_MOCK_VERDICT_QA"] = "BLOCKED"
    task_id = await _run_subprocess(
        _chain_code(tag=tag, ticket=ticket,
                    module_hint=worker_id, worker_id=worker_id), env)
    if task_id is None:
        return False

    def _incidents(db):
        return list(db.scalars(select(Incident).where(
            Incident.description.like(f"[pipeline] task {task_id}%"))))

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        incidents = _incidents(db)
        opened = (
            task.status.value == "blocked"
            and len(incidents) == 1
            and incidents[0].resolved_at is None
            and incidents[0].deployment_id is None
        )
    finally:
        db.close()
    if not opened:
        print("=== INCIDENT ===")
        print(f"  open failed: status={task.status.value} incidents={len(incidents)}")
        print("  -> FAIL")
        return False

    # Leg 2: devops RESOLVED -> qa PASS resolves it (fresh worker lane).
    worker_id2 = f"smoke-sess-resolve-{_uuid.uuid4().hex[:6]}"
    env2 = _base_env(worker_id2)
    env2["FLOWTRACK_CLAUDE_MOCK_VERDICT_DEVOPS"] = "RESOLVED"
    code2 = f"""
import asyncio
from sqlalchemy import select
from flowtrack.agents.devops import pickup_infra_failures
from flowtrack.core.database import SessionLocal
from flowtrack.models import Task
from flowtrack.orchestrator.loop import run_orchestrator


async def main():
    task_id = '{task_id}'
    db = SessionLocal()
    try:
        picked = pickup_infra_failures(db, limit=10, worker_id='{worker_id2}')
        assert any(str(p.id) == task_id for p in picked), picked
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
            t = db.get(Task, task_id)
            reblocked = t.status.value == 'blocked' and (
                t.blocked_reason is None or t.blocked_reason.value != 'infra_failure')
            if t.status.value == 'done' or reblocked:
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
    resolved_task = await _run_subprocess(code2, env2)
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        incidents = _incidents(db)
        diag = {
            "task_status": task.status.value,
            "n_incidents": len(incidents),
            "resolved_at_set": all(i.resolved_at is not None for i in incidents),
            "deployment_id_null": all(i.deployment_id is None for i in incidents),
        }
        ok = (
            resolved_task is not None
            and task.status.value == "done"
            and len(incidents) == 1
            and diag["resolved_at_set"]
            and diag["deployment_id_null"]
        )
    finally:
        db.close()
    print("=== INCIDENT (open -> devops arc -> resolved) ===")
    for k, v in diag.items():
        print(f"  {k} = {v}")
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return ok


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
        if branch.strip():
            p = await asyncio.create_subprocess_exec(
                "git", "branch", "-D", branch.strip(), cwd=str(REPO),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
    if WORKTREES.exists():
        shutil.rmtree(WORKTREES, ignore_errors=True)
    from sqlalchemy import delete, select, update
    from flowtrack.core.database import SessionLocal
    from flowtrack.models import Instance, ResourceLock
    from flowtrack.models.session import Session as AgentSession
    db = SessionLocal()
    try:
        db.execute(delete(ResourceLock).where(
            ResourceLock.resource_key.like("module:smoke-sess%")))
        # instances.session_id references sessions — unlink before deleting.
        smoke_session_ids = select(AgentSession.id).where(
            AgentSession.ticket_id.like(f"{TICKET_PREFIX}%"))
        db.execute(update(Instance)
                   .where(Instance.session_id.in_(smoke_session_ids))
                   .values(session_id=None))
        db.execute(delete(AgentSession).where(
            AgentSession.ticket_id.like(f"{TICKET_PREFIX}%")))
        db.commit()
    finally:
        db.close()


async def main() -> int:
    await cleanup()
    ok_chain, task_id = await check_chain()
    ok_inv = await check_invariant(task_id) if task_id else False
    ok_inc = await check_incident()
    overall = ok_chain and ok_inv and ok_inc
    await cleanup()
    print()
    print("RESULT:", "PASS" if overall else "FAIL")
    return 0 if overall else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
