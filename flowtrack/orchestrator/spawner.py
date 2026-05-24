"""Spawn and supervise a Claude Code process for one job.

Lifecycle:
    spawning  → worktree created, locks acquired, subprocess starting
    running   → subprocess alive, stream being consumed
    completed → returncode == 0
    failed    → returncode != 0, OR setup failed
    killed    → we sent SIGTERM (timeout, watchdog, manual)

Anything that goes wrong in setup is logged + reflected in the instance row;
nothing here raises out to the supervisor caller — caller treats this function
as terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from flowtrack.core.database import SessionLocal
from flowtrack.core.settings import settings
from flowtrack.models import Instance, Job, Role, Task
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.job import JobStatus
from flowtrack.orchestrator import locks, worktree
from flowtrack.orchestrator.queue import release_job
from flowtrack.orchestrator.stream_parser import consume_stream

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class _SpawnContext:
    """Plain-data snapshot of role + task fields needed by the spawn pipeline.

    Avoids passing detached ORM objects across session boundaries — see
    SQLAlchemy DetachedInstanceError when default expire_on_commit is in play.
    """
    role_name: str
    role_system_prompt: str
    role_tools_allowed: list[str] | None
    role_max_minutes: int
    role_model: str
    task_id: UUID
    task_title: str
    task_description: str | None
    task_ticket_id: str | None
    task_acceptance_criteria: str | None
    task_module_hint: str | None
    branch_name: str


async def supervise(job_id: UUID, instance_id: UUID) -> None:
    """Run the full lifecycle for one (job, instance) pair.

    Always returns; failures are surfaced via the instance row.
    """
    try:
        await _supervise_inner(job_id, instance_id)
    except Exception:  # last-resort guard so the orchestrator loop never dies
        log.exception("supervisor crashed for instance %s", instance_id)
        await asyncio.to_thread(_finalize_failed, instance_id, job_id, "supervisor crash")


async def _supervise_inner(job_id: UUID, instance_id: UUID) -> None:
    # 1. Snapshot role + task into a detached dataclass.
    ctx = await asyncio.to_thread(_load_context, instance_id, job_id)
    if ctx is None:
        log.error("supervisor: instance/job/role/task missing for %s", instance_id)
        return

    # 2. Worktree.
    target_repo = Path(settings.target_repo_path or os.getcwd()).resolve()
    worktree_root = Path(settings.worktree_root).resolve()
    try:
        wt_path = await worktree.create_worktree(
            target_repo=target_repo,
            worktree_root=worktree_root,
            instance_id=instance_id,
            branch_name=ctx.branch_name,
        )
    except worktree.WorktreeError as e:
        log.error("worktree creation failed: %s", e)
        await asyncio.to_thread(_finalize_failed, instance_id, job_id, f"worktree: {e}")
        return

    # 3. Locks.
    lock_keys = locks.derive_locks_for(module_hint=ctx.task_module_hint)
    if not await asyncio.to_thread(
        _try_acquire_locks, instance_id, lock_keys, ctx.role_max_minutes
    ):
        # Conflict — let watchdog / next tick retry. Mark instance + job failed
        # softly so we surface the contention. (A future iteration can requeue.)
        await asyncio.to_thread(_finalize_failed, instance_id, job_id, "lock contention")
        return

    # 4. Persist worktree info + flip to RUNNING. Subprocess starts next.
    await asyncio.to_thread(
        _mark_running, instance_id, wt_path=str(wt_path), branch=ctx.branch_name
    )

    # 5. Spawn subprocess.
    cmd = _build_command(ctx)
    env = _build_env()
    log.info("spawning instance %s: %s", instance_id, " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(wt_path),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.to_thread(_attach_pid, instance_id, proc.pid)

    # 6. Stream + wait with timeout.
    timeout_seconds = ctx.role_max_minutes * 60
    consumer_task = asyncio.create_task(
        consume_stream(stream=proc.stdout, instance_id=instance_id, model=ctx.role_model)
    )
    stderr_task = asyncio.create_task(_drain_stderr(proc.stderr, instance_id))

    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        log.warning("instance %s exceeded %ds — killing", instance_id, timeout_seconds)
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        await _cleanup_tasks(consumer_task, stderr_task)
        await asyncio.to_thread(_finalize, instance_id, job_id, InstanceStatus.KILLED,
                                JobStatus.FAILED, exit_code=None, error="timeout")
        return

    await _cleanup_tasks(consumer_task, stderr_task)

    final_status = InstanceStatus.COMPLETED if rc == 0 else InstanceStatus.FAILED
    job_status = JobStatus.DONE if rc == 0 else JobStatus.FAILED
    await asyncio.to_thread(
        _finalize, instance_id, job_id, final_status, job_status,
        exit_code=rc, error=None if rc == 0 else f"exit {rc}",
    )


# --------------------------------------------------------------------------- #
# Sync helpers (run inside asyncio.to_thread)                                 #
# --------------------------------------------------------------------------- #

def _load_context(instance_id: UUID, job_id: UUID) -> _SpawnContext | None:
    with _db() as db:
        inst = db.get(Instance, instance_id)
        job = db.get(Job, job_id)
        if inst is None or job is None:
            return None
        role = db.get(Role, inst.role_id)
        task = db.get(Task, inst.task_id) if inst.task_id else None
        if role is None or task is None:
            return None
        short = str(task.id)[:8]
        ishort = str(instance_id)[:8]
        return _SpawnContext(
            role_name=role.name,
            role_system_prompt=role.system_prompt,
            role_tools_allowed=list(role.tools_allowed) if role.tools_allowed else None,
            role_max_minutes=role.max_minutes,
            role_model=role.model,
            task_id=task.id,
            task_title=task.title,
            task_description=task.description,
            task_ticket_id=task.ticket_id,
            task_acceptance_criteria=task.acceptance_criteria,
            task_module_hint=task.module_hint,
            branch_name=f"auto/{role.name}-{short}-{ishort}",
        )


def _try_acquire_locks(instance_id: UUID, keys: list[str], role_max_minutes: int) -> bool:
    with _db() as db:
        ok = locks.try_acquire(
            db, keys, instance_id=instance_id,
            ttl=timedelta(minutes=role_max_minutes),
        )
        if not ok:
            db.rollback()
        return ok


def _mark_running(instance_id: UUID, *, wt_path: str, branch: str) -> None:
    with _db() as db:
        inst = db.get(Instance, instance_id)
        if inst is None:
            return
        inst.worktree_path = wt_path
        inst.branch_name = branch
        inst.status = InstanceStatus.RUNNING
        inst.claude_session_id = str(instance_id)  # 1:1 with our instance id
        inst.last_heartbeat_at = datetime.now(tz=timezone.utc)


def _attach_pid(instance_id: UUID, pid: int) -> None:
    with _db() as db:
        inst = db.get(Instance, instance_id)
        if inst is not None:
            inst.pid = pid


def _finalize(
    instance_id: UUID,
    job_id: UUID,
    inst_status: InstanceStatus,
    job_status: JobStatus,
    *,
    exit_code: int | None,
    error: str | None,
) -> None:
    with _db() as db:
        inst = db.get(Instance, instance_id)
        if inst is not None:
            inst.status = inst_status
            inst.exit_code = exit_code
            inst.finished_at = datetime.now(tz=timezone.utc)
        locks.release_all(db, instance_id=instance_id)
        job = db.get(Job, job_id)
        if job is not None:
            release_job(db, job, final_status=job_status, error=error, instance_id=instance_id)


def _finalize_failed(instance_id: UUID, job_id: UUID, error: str) -> None:
    _finalize(
        instance_id, job_id,
        InstanceStatus.FAILED, JobStatus.FAILED,
        exit_code=None, error=error,
    )


# --------------------------------------------------------------------------- #
# Subprocess plumbing                                                         #
# --------------------------------------------------------------------------- #

def _build_command(ctx: _SpawnContext) -> list[str]:
    """Construct argv for the Claude Code subprocess.

    Flag names follow the headless mode documented for Claude Code; if your
    installed version uses different names, override via a wrapper script
    pointed at by settings.claude_executable.

    ``settings.claude_executable`` may be a single binary (``"claude"``) or a
    command line (``"python C:/path/mock.py"``). We shlex-split so tests can
    plug in a mock without packaging it as a real exe.
    """
    prompt = _build_prompt(ctx)
    cmd_prefix = shlex.split(settings.claude_executable, posix=(os.name != "nt"))
    if os.name == "nt":
        # shlex with posix=False keeps surrounding quotes as part of the token;
        # subprocess on Windows then can't find e.g. `"C:/python.exe"` literally.
        cmd_prefix = [p.strip('"') for p in cmd_prefix]
    cmd: list[str] = [
        *cmd_prefix,
        "--print", prompt,
        "--output-format", "stream-json",
        "--session-id", str(ctx.task_id),  # any UUID; we don't use it for resume yet
    ]
    if ctx.role_system_prompt:
        cmd += ["--append-system-prompt", ctx.role_system_prompt]
    if ctx.role_tools_allowed:
        cmd += ["--allowed-tools", ",".join(ctx.role_tools_allowed)]
    return cmd


def _build_prompt(ctx: _SpawnContext) -> str:
    parts: list[str] = [f"# Task: {ctx.task_title}"]
    if ctx.task_ticket_id:
        parts.append(f"Ticket: {ctx.task_ticket_id}")
    if ctx.task_description:
        parts += ["", "## Description", ctx.task_description]
    if ctx.task_acceptance_criteria:
        parts += ["", "## Acceptance criteria", ctx.task_acceptance_criteria]
    if ctx.task_module_hint:
        parts += ["", "## Module scope",
                  f"You may only edit files under: `{ctx.task_module_hint}`."]
    parts += ["", f"You are acting in the role of `{ctx.role_name}`."]
    return "\n".join(parts)


def _build_env() -> dict[str, str]:
    env = dict(os.environ)
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    # Used by hooks inside the worktree to call back into the daemon.
    env["FLOWTRACK_API_URL"] = f"http://{settings.api_host}:{settings.api_port}"
    return env


async def _drain_stderr(stream: asyncio.StreamReader, instance_id: UUID) -> None:
    """Log stderr lines without persisting them — they're usually noise."""
    while True:
        line = await stream.readline()
        if not line:
            return
        log.debug("instance %s stderr: %s", instance_id, line.decode("utf-8", "replace").rstrip())


async def _cleanup_tasks(*tasks: asyncio.Task) -> None:
    """Wait briefly for stream consumers to drain post-exit, then cancel stragglers.

    After ``proc.wait()`` returns, the OS pipe may still have buffered lines the
    consumer hasn't read yet — cancelling immediately would drop the final
    usage/result events. Give 2s grace; then force-cancel.
    """
    pending = [t for t in tasks if not t.done()]
    if pending:
        done, still_pending = await asyncio.wait(pending, timeout=2.0)
        for t in still_pending:
            t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


class _db:
    def __enter__(self) -> Session:
        self._db = SessionLocal()
        return self._db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._db.commit()
            else:
                self._db.rollback()
        finally:
            self._db.close()
