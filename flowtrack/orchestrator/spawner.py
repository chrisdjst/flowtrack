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

from sqlalchemy import select

from flowtrack.api.events import broker
from flowtrack.core.database import SessionLocal
from flowtrack.core.settings import settings
from flowtrack.models import Instance, InstanceEvent, Job, Role, Task, TaskComment, TaskTransition
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.instance_event import InstanceEventType
from flowtrack.models.job import JobStatus
from flowtrack.models.task import TaskStatus
from flowtrack.orchestrator import hooks, locks, worktree
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
    # Branch to fork the worktree from. "HEAD" for the first role; for chained
    # roles (reviewer, qa), the previous role's branch so the new agent sees
    # the prior commits. Read from Job.payload_json.parent_branch.
    base_branch: str


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
            base_branch=ctx.base_branch,
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

    # 4b. Drop a .claude/settings.json with hooks that callback the daemon.
    api_base_url = f"http://{settings.api_host}:{settings.api_port}"
    try:
        await asyncio.to_thread(
            hooks.write_worktree_settings,
            worktree_path=wt_path,
            instance_id=instance_id,
            api_base_url=api_base_url,
        )
    except Exception:
        log.warning(
            "failed to write hook settings for instance %s — continuing without hooks",
            instance_id, exc_info=True,
        )

    # 5. Spawn subprocess.
    cmd = _build_command(ctx)
    env = _build_env(instance_id=instance_id)
    log.info("spawning instance %s: %s", instance_id, " ".join(cmd))
    # 4MB stdout buffer — the first system/init line from real Claude Code
    # serialises every available tool + slash command + skill into a single
    # JSON line and easily exceeds the default 64KB limit. LimitOverrunError
    # silently kills our consumer task otherwise.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(wt_path),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=4 * 1024 * 1024,
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
        payload = job.payload_json or {}
        parent_branch = payload.get("parent_branch")
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
            base_branch=parent_branch or "HEAD",
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
    task_id_for_event: UUID | None = None
    with _db() as db:
        inst = db.get(Instance, instance_id)
        if inst is not None:
            inst.status = inst_status
            inst.exit_code = exit_code
            inst.finished_at = datetime.now(tz=timezone.utc)
            task_id_for_event = inst.task_id
        locks.release_all(db, instance_id=instance_id)
        job = db.get(Job, job_id)
        if job is not None:
            release_job(db, job, final_status=job_status, error=error, instance_id=instance_id)
        # On success: advance the task to the next pipeline step (role + status).
        # Failure path stays put — caller can re-enqueue manually after triage.
        if inst_status == InstanceStatus.COMPLETED and inst is not None and inst.task_id:
            _advance_pipeline(db, task_id=inst.task_id, instance_id=instance_id, role_id=inst.role_id)

    broker.publish_sync("instance_finalized", {
        "instance_id": str(instance_id),
        "status": inst_status.value,
        "exit_code": exit_code,
        "error": error,
        "task_id": str(task_id_for_event) if task_id_for_event else None,
    })


def _send_back_to_dev(
    db: Session, *, task: Task, role: Role, instance_id: UUID
) -> None:
    """Reviewer rejected: route the task back to dev with the reviewer's
    parent_branch so dev's next worktree sees what was reviewed.

    Records a transition + a comment + a new dev Job. Stays in the same
    worker lane via inst.worker_id propagation.
    """
    inst = db.get(Instance, instance_id)
    dev_role = db.scalar(select(Role).where(Role.name == "dev"))
    if dev_role is None:
        log.warning("reviewer wants changes but no 'dev' role found — falling back to blocked")
        _block_for_human(db, task=task, role=role, instance_id=instance_id)
        return

    from_status = task.status.value if task.status else None
    task.status = TaskStatus.IN_PROGRESS
    db.add(TaskTransition(
        task_id=task.id, from_status=from_status, to_status=task.status.value,
        instance_id=instance_id, reason="reviewer: REQUEST_CHANGES",
    ))
    db.add(TaskComment(
        task_id=task.id,
        body="Reviewer requested changes. Sending back to dev. See instance events for the reviewer's comments.",
        author_role_id=role.id, instance_id=instance_id,
    ))
    parent_branch = inst.branch_name if inst is not None else None
    worker_id = inst.worker_id if inst is not None else None
    db.add(Job(
        task_id=task.id, role_id=dev_role.id, priority=50,
        worker_id=worker_id,
        payload_json={
            "parent_branch": parent_branch,
            "reviewer_feedback": "REQUEST_CHANGES — see prior task comments",
        } if parent_branch else {
            "reviewer_feedback": "REQUEST_CHANGES — see prior task comments",
        },
    ))
    broker.publish_sync("reviewer_request_changes", {
        "task_id": str(task.id),
        "from_role": role.name,
        "instance_id": str(instance_id),
    })
    broker.publish_sync("task_transitioned", {
        "task_id": str(task.id), "from_status": from_status,
        "to_status": task.status.value, "by_role": role.name,
        "instance_id": str(instance_id),
    })


def _block_for_human(
    db: Session, *, task: Task, role: Role, instance_id: UUID
) -> None:
    """Reviewer punted: task -> blocked, comment + WS event for the kanban."""
    from_status = task.status.value if task.status else None
    task.status = TaskStatus.BLOCKED
    db.add(TaskTransition(
        task_id=task.id, from_status=from_status, to_status=task.status.value,
        instance_id=instance_id, reason="reviewer: NEEDS_HUMAN",
    ))
    db.add(TaskComment(
        task_id=task.id,
        body="Reviewer flagged NEEDS_HUMAN. Pipeline halted — human triage required.",
        author_role_id=role.id, instance_id=instance_id,
    ))
    broker.publish_sync("reviewer_needs_human", {
        "task_id": str(task.id),
        "from_role": role.name,
        "instance_id": str(instance_id),
    })
    broker.publish_sync("task_transitioned", {
        "task_id": str(task.id), "from_status": from_status,
        "to_status": task.status.value, "by_role": role.name,
        "instance_id": str(instance_id),
    })


def _flatten_text(payload: dict) -> str:
    """Recursively pull text strings out of a stream-json event payload."""
    parts: list[str] = []

    def walk(obj):
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            # Common shape: {"type": "text", "text": "..."}
            txt = obj.get("text")
            if isinstance(txt, str):
                parts.append(txt)
            content = obj.get("content")
            if content is not None:
                walk(content)
            message = obj.get("message")
            if message is not None:
                walk(message)
        elif isinstance(obj, list):
            for child in obj:
                walk(child)
    walk(payload)
    return " ".join(parts)


def _reviewer_verdict(db: Session, instance_id: UUID) -> str:
    """Inspect recent assistant text for a verdict keyword.

    Searches the last ~20 assistant/result/message events for explicit
    REQUEST_CHANGES / NEEDS_HUMAN / APPROVE. Earlier mentions win — the
    reviewer prompt asks for the verdict at the end of its work.

    Default when nothing matches: 'needs_human' (fail-safe — the reviewer
    said nothing actionable, escalate instead of waving through).
    """
    rows = list(db.scalars(
        select(InstanceEvent)
        .where(InstanceEvent.instance_id == instance_id)
        .where(InstanceEvent.event_type.in_((
            InstanceEventType.ASSISTANT,
            InstanceEventType.RESULT,
            InstanceEventType.MESSAGE,
        )))
        .order_by(InstanceEvent.id.desc())
        .limit(20)
    ))
    for ev in rows:
        text = _flatten_text(ev.payload_json or {}).upper()
        if "REQUEST_CHANGES" in text:
            return "request_changes"
        if "NEEDS_HUMAN" in text:
            return "needs_human"
        if "APPROVE" in text:
            return "approve"
    return "needs_human"


def _advance_pipeline(
    db: Session, *, task_id: UUID, instance_id: UUID, role_id: UUID
) -> None:
    """Apply role.task_status_on_success + enqueue role.next_role_name job.

    Records a TaskTransition for the status change. If next_role_name points at
    a missing role, logs and stops — does NOT raise (we already committed the
    instance as completed; the chain is best-effort from here).

    Reviewer role is special: its verdict overrides the default chain — see
    _reviewer_verdict.
    """
    role = db.get(Role, role_id)
    task = db.get(Task, task_id)
    if role is None or task is None:
        return

    # Reviewer branching: override the default in_qa flow based on what the
    # reviewer wrote. APPROVE keeps the default; the other branches divert.
    if role.name == "reviewer":
        verdict = _reviewer_verdict(db, instance_id)
        log.info("reviewer verdict for task %s: %s", task.id, verdict)
        if verdict == "request_changes":
            _send_back_to_dev(db, task=task, role=role, instance_id=instance_id)
            return
        if verdict == "needs_human":
            _block_for_human(db, task=task, role=role, instance_id=instance_id)
            return
        # else: fall through to the default approve path below.

    if role.task_status_on_success:
        try:
            new_status = TaskStatus(role.task_status_on_success)
        except ValueError:
            log.warning(
                "role %s has invalid task_status_on_success=%s — skipping transition",
                role.name, role.task_status_on_success,
            )
        else:
            from_status = task.status.value if task.status else None
            task.status = new_status
            db.add(TaskTransition(
                task_id=task.id,
                from_status=from_status,
                to_status=new_status.value,
                instance_id=instance_id,
                reason=f"pipeline: {role.name} completed",
            ))
            broker.publish_sync("task_transitioned", {
                "task_id": str(task.id),
                "from_status": from_status,
                "to_status": new_status.value,
                "by_role": role.name,
                "instance_id": str(instance_id),
            })

    if role.next_role_name:
        next_role = db.scalar(select(Role).where(Role.name == role.next_role_name))
        if next_role is None:
            log.warning(
                "role %s.next_role_name=%s not found — pipeline halted for task %s",
                role.name, role.next_role_name, task.id,
            )
            return
        # Propagate the just-finished instance's worker_id so the chained job
        # stays in the same lane. Tests rely on this — without it, a chained
        # reviewer job would escape the smoke's lane and a production daemon
        # could claim it.
        #
        # Also stash the parent's branch_name in payload_json so the next role's
        # worktree forks from it (instead of HEAD) and the next agent actually
        # sees the previous role's commits.
        inst = db.get(Instance, instance_id)
        next_worker_id = inst.worker_id if inst is not None else None
        parent_branch = inst.branch_name if inst is not None else None
        db.add(Job(
            task_id=task.id,
            role_id=next_role.id,
            priority=100,
            worker_id=next_worker_id,
            payload_json={"parent_branch": parent_branch} if parent_branch else {},
        ))
        log.info("pipeline: %s -> %s enqueued for task %s (worker=%s, parent_branch=%s)",
                 role.name, next_role.name, task.id, next_worker_id, parent_branch)
        broker.publish_sync("job_enqueued", {
            "task_id": str(task.id),
            "role_name": next_role.name,
            "from_role": role.name,
            "worker_id": next_worker_id,
            "parent_branch": parent_branch,
        })


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
        # Claude Code refuses to combine --print with --output-format=stream-json
        # unless --verbose is also set (it gates the streaming output stream).
        "--verbose",
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


def _build_env(*, instance_id: UUID) -> dict[str, str]:
    env = dict(os.environ)
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    # Used by hooks inside the worktree to call back into the daemon.
    env["FLOWTRACK_API_URL"] = f"http://{settings.api_host}:{settings.api_port}"
    env["FLOWTRACK_INSTANCE_ID"] = str(instance_id)
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
