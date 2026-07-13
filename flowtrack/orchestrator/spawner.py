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
from flowtrack.core.runtime_config import RuntimeConfig
from flowtrack.core.settings import settings
from flowtrack.models import Instance, InstanceEvent, Job, Role, Task, TaskComment, TaskTransition
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.instance_event import InstanceEventType
from flowtrack.models.job import JobStatus
from flowtrack.models.task import BlockedReason, TaskStatus
from flowtrack.integrations.jira_sync import push_task_status
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
    role_max_tokens: int
    role_max_turns: int | None
    task_id: UUID
    task_title: str
    task_description: str | None
    task_ticket_id: str | None
    task_acceptance_criteria: str | None
    task_module_hint: str | None
    # TPM's full spec and (when the design stage ran) the Design agent's UI
    # spec — every downstream role gets both in its prompt.
    task_spec: str | None
    task_ui_spec: str | None
    branch_name: str
    # Branch to fork the worktree from. "HEAD" for the first role; for chained
    # roles (reviewer, qa), the previous role's branch so the new agent sees
    # the prior commits. Read from Job.payload_json.parent_branch.
    base_branch: str
    # Summary of what the previous instance accomplished. Injected into the
    # prompt on retries so the agent continues instead of restarting from scratch.
    resume_context: str | None


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
    target_repo = Path(RuntimeConfig.get("target_repo_path") or os.getcwd()).resolve()
    worktree_root = Path(RuntimeConfig.get("worktree_root")).resolve()
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
    lock_keys = locks.derive_locks_for(
        module_hint=ctx.task_module_hint,
        task_id=str(ctx.task_id),
    )
    if not await asyncio.to_thread(
        _try_acquire_locks, instance_id, lock_keys, ctx.role_max_minutes
    ):
        # Lock held by another instance on the same module — requeue so the
        # orchestrator retries on the next tick once the lock is released.
        await asyncio.to_thread(_requeue_on_contention, instance_id, job_id)
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
    env = _build_env(instance_id=instance_id, role_name=ctx.role_name)
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

    if rc == 0:
        await asyncio.to_thread(
            _finalize, instance_id, job_id,
            InstanceStatus.COMPLETED, JobStatus.DONE,
            exit_code=0, error=None,
        )
    else:
        await asyncio.to_thread(_handle_nonzero_exit, instance_id, job_id, rc)


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
            role_max_tokens=role.max_tokens,
            role_max_turns=role.max_turns,
            task_id=task.id,
            task_title=task.title,
            task_description=task.description,
            task_ticket_id=task.ticket_id,
            task_acceptance_criteria=task.acceptance_criteria,
            task_module_hint=task.module_hint,
            task_spec=task.task_spec,
            task_ui_spec=task.ui_spec,
            branch_name=f"auto/{role.name}-{short}-{ishort}",
            base_branch=parent_branch or "HEAD",
            resume_context=payload.get("resume_context"),
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


_REQUEST_CHANGES_REASON = "reviewer: REQUEST_CHANGES"
# Legacy sentinel — the bounce cap replaced the approval-at-2nd-cycle rule, so
# the spawner no longer writes this reason. The approve endpoint still accepts
# it for tasks blocked before migration 010.
_AWAIT_APPROVAL_REASON = "reviewer: REQUEST_CHANGES (awaiting approval)"

# Verdict keyword -> outcome, per role. Per event, first keyword found wins,
# so rejection keywords come before approval ones. Outcomes:
#   failure — route via role.task_status_on_failure (bounce-capped)
#   human   — blocked, blocked_reason=manual_intervention
#   infra   — blocked, blocked_reason=infra_failure (DevOps pickup)
#   success — fall through to the role's default success chain
_ROLE_VERDICTS: dict[str, tuple[tuple[str, str], ...]] = {
    "reviewer": (
        ("REQUEST_CHANGES", "failure"),
        ("NEEDS_HUMAN", "human"),
        ("APPROVE", "success"),
    ),
    "qa": (
        ("FAIL", "failure"),
        ("BLOCKED", "infra"),
        ("PASS", "success"),
    ),
}

# Which role works a task at a given status — used to enqueue the rework job
# when a failure routes the task back to an earlier stage.
_STATUS_ROLE: dict[TaskStatus, str] = {
    TaskStatus.IN_PROGRESS: "dev",
    TaskStatus.IN_REVIEW: "reviewer",
    TaskStatus.IN_QA: "qa",
}


def _block_task(
    db: Session, *, task: Task, role: Role, instance_id: UUID,
    reason: str, body: str, blocked_reason: BlockedReason,
    event_type: str = "task_blocked", event_extra: dict | None = None,
) -> None:
    """Task -> blocked with a typed blocked_reason, comment + WS events."""
    from_status = task.status.value if task.status else None
    task.status = TaskStatus.BLOCKED
    task.blocked_reason = blocked_reason
    push_task_status(task.ticket_id, task.status.value)
    db.add(TaskTransition(
        task_id=task.id, from_status=from_status, to_status=task.status.value,
        instance_id=instance_id, reason=reason,
    ))
    db.add(TaskComment(
        task_id=task.id, body=body,
        author_role_id=role.id, instance_id=instance_id,
    ))
    broker.publish_sync(event_type, {
        "task_id": str(task.id),
        "from_role": role.name,
        "instance_id": str(instance_id),
        "blocked_reason": blocked_reason.value,
        **(event_extra or {}),
    })
    broker.publish_sync("task_transitioned", {
        "task_id": str(task.id), "from_status": from_status,
        "to_status": task.status.value, "by_role": role.name,
        "instance_id": str(instance_id),
    })


def _rejection_feedback(
    db: Session, *, instance_id: UUID, role_name: str, keyword: str
) -> str:
    """Format the rejecting instance's assistant text as rework context.

    Injected into the next run's prompt via the job payload's resume_context
    slot so the reworking agent sees WHAT was rejected and why, instead of
    a bare "see prior task comments" pointer.
    """
    events = list(db.scalars(
        select(InstanceEvent)
        .where(InstanceEvent.instance_id == instance_id)
        .where(InstanceEvent.event_type.in_((
            InstanceEventType.ASSISTANT,
            InstanceEventType.MESSAGE,
        )))
        .order_by(InstanceEvent.id.asc())
    ))
    texts = [
        t for t in (_flatten_text(ev.payload_json or {}).strip() for ev in events)
        if len(t) > 30
    ]
    detail = "\n\n".join(texts[-4:])[-1500:] if texts else "(no detail captured — see task comments)"
    return (
        f"## Rework requested by {role_name} (verdict: {keyword})\n"
        "The previous delivery was rejected. Address ALL the points below, "
        "then finish the task.\n\n" + detail
    )


def _route_failure(
    db: Session, *, task: Task, role: Role, instance_id: UUID, keyword: str
) -> None:
    """Config-driven failure routing with a persisted bounce cap.

    Increments tasks.bounce_count (survives across process runs, unlike the
    per-run max_turns budget). Over role.max_bounce_count the task is forced
    to blocked/manual_intervention regardless of task_status_on_failure —
    the human unblock path (approve-return-dev) resets the counter.
    NULL task_status_on_failure keeps the legacy behaviour: blocked.
    """
    reason = f"{role.name}: {keyword}"
    task.bounce_count = (task.bounce_count or 0) + 1
    cap = role.max_bounce_count

    if cap is not None and task.bounce_count > cap:
        _block_task(
            db, task=task, role=role, instance_id=instance_id,
            reason=f"{reason} (bounce cap {cap} exceeded)",
            body=(
                f"{role.name} rejected this task {task.bounce_count} times "
                f"(cap {cap}). Automatic rework disabled — human approval "
                "required. Click 'Aprovar → Dev' on the kanban card to continue."
            ),
            blocked_reason=BlockedReason.MANUAL_INTERVENTION,
            # Same event the old approval flow used — keeps the kanban toast.
            event_type="reviewer_return_approval_needed",
            event_extra={"task_title": task.title, "cycle": task.bounce_count},
        )
        return

    dest: TaskStatus | None = None
    if role.task_status_on_failure:
        try:
            dest = TaskStatus(role.task_status_on_failure)
        except ValueError:
            log.warning(
                "role %s has invalid task_status_on_failure=%s — blocking task %s",
                role.name, role.task_status_on_failure, task.id,
            )

    target_role_name = _STATUS_ROLE.get(dest) if dest else None
    target_role = (
        db.scalar(select(Role).where(Role.name == target_role_name))
        if target_role_name else None
    )
    if dest is None or dest == TaskStatus.BLOCKED or target_role is None:
        if dest is not None and dest != TaskStatus.BLOCKED and target_role is None:
            log.warning(
                "no role owns status %s (task_status_on_failure of %s) — blocking task %s",
                dest.value, role.name, task.id,
            )
        _block_task(
            db, task=task, role=role, instance_id=instance_id,
            reason=reason,
            body=f"{role.name} verdict {keyword} — no automatic rework route. Human triage required.",
            blocked_reason=BlockedReason.MANUAL_INTERVENTION,
        )
        return

    inst = db.get(Instance, instance_id)
    from_status = task.status.value if task.status else None
    task.status = dest
    task.blocked_reason = None
    push_task_status(task.ticket_id, dest.value)
    db.add(TaskTransition(
        task_id=task.id, from_status=from_status, to_status=dest.value,
        instance_id=instance_id, reason=reason,
    ))
    cap_note = f"/{cap}" if cap is not None else ""
    db.add(TaskComment(
        task_id=task.id,
        body=(
            f"{role.name} verdict: {keyword}. Routing back to {dest.value} "
            f"(bounce {task.bounce_count}{cap_note}). See instance events for details."
        ),
        author_role_id=role.id, instance_id=instance_id,
    ))
    payload: dict = {
        "resume_context": _rejection_feedback(
            db, instance_id=instance_id, role_name=role.name, keyword=keyword,
        ),
    }
    if inst is not None and inst.branch_name:
        payload["parent_branch"] = inst.branch_name
    db.add(Job(
        task_id=task.id, role_id=target_role.id, priority=50,
        worker_id=inst.worker_id if inst is not None else None,
        payload_json=payload,
    ))
    broker.publish_sync("role_failure_routed", {
        "task_id": str(task.id),
        "from_role": role.name,
        "to_role": target_role.name,
        "to_status": dest.value,
        "bounce_count": task.bounce_count,
        "instance_id": str(instance_id),
    })
    broker.publish_sync("task_transitioned", {
        "task_id": str(task.id), "from_status": from_status,
        "to_status": dest.value, "by_role": role.name,
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


_UI_SPEC_START = "UI_SPEC_START"
_UI_SPEC_END = "UI_SPEC_END"


def _capture_design_output(db: Session, *, instance_id: UUID, task: Task) -> str:
    """Store the design instance's UI spec on the task, honouring SKIP.

    Returns "skip" | "created" | "empty". The design prompt is asked to emit
    either the single keyword SKIP (no UI work needed) or the spec between
    UI_SPEC_START/UI_SPEC_END markers; without markers we fall back to the
    last substantial assistant text so a slightly off-format run still
    delivers something reviewable.
    """
    events = list(db.scalars(
        select(InstanceEvent)
        .where(InstanceEvent.instance_id == instance_id)
        .where(InstanceEvent.event_type.in_((
            InstanceEventType.ASSISTANT,
            InstanceEventType.MESSAGE,
        )))
        .order_by(InstanceEvent.id.asc())
    ))
    texts = [
        t for t in (_flatten_text(ev.payload_json or {}).strip() for ev in events)
        if t
    ]
    joined = "\n\n".join(texts)

    if "SKIP" in joined.upper() and _UI_SPEC_START not in joined:
        return "skip"

    start = joined.find(_UI_SPEC_START)
    end = joined.rfind(_UI_SPEC_END)
    if start >= 0 and end > start:
        spec = joined[start + len(_UI_SPEC_START):end].strip()
    else:
        # Fallback: last text block long enough to plausibly be a spec.
        substantial = [t for t in texts if len(t) > 80]
        spec = substantial[-1].strip() if substantial else ""

    if not spec:
        return "empty"
    task.ui_spec = spec
    return "created"


def _parse_verdict(
    db: Session, instance_id: UUID, verdicts: tuple[tuple[str, str], ...]
) -> tuple[str, str]:
    """Inspect recent assistant text for one of the role's verdict keywords.

    Searches the last ~20 assistant/result/message events, newest first —
    role prompts ask for the verdict at the end of their work. Per event,
    keywords are checked in table order (rejections before approvals).

    Returns (outcome, keyword). Default when nothing matches:
    ('human', 'NO_VERDICT') — fail-safe: the agent said nothing actionable,
    escalate instead of waving through.
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
        for keyword, outcome in verdicts:
            if keyword in text:
                return outcome, keyword
    return "human", "NO_VERDICT"


def _advance_pipeline(
    db: Session, *, task_id: UUID, instance_id: UUID, role_id: UUID
) -> None:
    """Apply role.task_status_on_success + enqueue role.next_role_name job.

    Records a TaskTransition for the status change. If next_role_name points at
    a missing role, logs and stops — does NOT raise (we already committed the
    instance as completed; the chain is best-effort from here).

    Verdict-bearing roles (see _ROLE_VERDICTS) can override the default chain:
    their failure/human/infra outcomes divert via _route_failure/_block_task;
    only a success verdict falls through to the success path below.
    """
    role = db.get(Role, role_id)
    task = db.get(Task, task_id)
    if role is None or task is None:
        return

    # Design stage: capture the UI spec (or honour SKIP) before the normal
    # success chain advances the task to dev. Design has no failure verdicts —
    # a crashed instance never reaches here (only COMPLETED instances do).
    if role.name == "design":
        outcome = _capture_design_output(db, instance_id=instance_id, task=task)
        log.info("design outcome for task %s: %s", task.id, outcome)
        db.add(TaskComment(
            task_id=task.id,
            body=(
                "Design: SKIP — no UI work needed; proceeding straight to dev."
                if outcome == "skip" else
                "Design: UI spec delivered (stored on the task)."
                if outcome == "created" else
                "Design: no usable output captured — proceeding to dev without a UI spec."
            ),
            author_role_id=role.id, instance_id=instance_id,
        ))
        # fall through: the seeded chain (design -> dev) advances either way.

    verdict_table = _ROLE_VERDICTS.get(role.name)
    if verdict_table:
        outcome, keyword = _parse_verdict(db, instance_id, verdict_table)
        log.info("%s verdict for task %s: %s (%s)", role.name, task.id, outcome, keyword)
        if outcome == "failure":
            _route_failure(db, task=task, role=role, instance_id=instance_id, keyword=keyword)
            return
        if outcome == "human":
            _block_task(
                db, task=task, role=role, instance_id=instance_id,
                reason=f"{role.name}: {keyword}",
                body=f"{role.name} flagged {keyword}. Pipeline halted — human triage required.",
                blocked_reason=BlockedReason.MANUAL_INTERVENTION,
            )
            return
        if outcome == "infra":
            _block_task(
                db, task=task, role=role, instance_id=instance_id,
                reason=f"{role.name}: {keyword}",
                body=(
                    f"{role.name} reported {keyword}: environment/infra problem, "
                    "not a code defect. Waiting for DevOps pickup."
                ),
                blocked_reason=BlockedReason.INFRA_FAILURE,
            )
            return
        # else: success — fall through to the default chain below.

    # Successful stage completion: stale block metadata no longer applies.
    # bounce_count is deliberately NOT reset here — dev succeeding after each
    # rework would zero it every cycle and the cap could never trigger. Only
    # the human unblock path resets it.
    if task.blocked_reason is not None:
        task.blocked_reason = None

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
            push_task_status(task.ticket_id, new_status.value)
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


def _build_resume_context(instance_id: UUID) -> str | None:
    """Summarise what the previous instance accomplished so the next run can continue.

    Extracts the last assistant text blocks and tool calls from the instance's
    event log and formats them as a compact handoff section injected into the
    next run's prompt. This prevents the agent from re-exploring the codebase
    or repeating work it already finished when it hit the turn limit.

    Returns None when there is nothing meaningful to extract.
    """
    text_blocks: list[str] = []
    tool_entries: list[str] = []

    with _db() as db:
        events = list(db.scalars(
            select(InstanceEvent)
            .where(InstanceEvent.instance_id == instance_id)
            .where(InstanceEvent.event_type == InstanceEventType.ASSISTANT)
            .order_by(InstanceEvent.id.asc())
        ))

        for ev in events:
            p = ev.payload_json or {}
            # Content can live at top level or nested under "message"
            content = p.get("message", {}).get("content", []) if "message" in p else p.get("content", [])
            if not isinstance(content, list):
                continue
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "text":
                    txt = (blk.get("text") or "").strip()
                    if len(txt) > 30:
                        text_blocks.append(txt)
                elif btype == "tool_use":
                    name = blk.get("name", "?")
                    inp = blk.get("input") or {}
                    cmd = inp.get("command", inp.get("pattern", inp.get("query", "")))
                    if not cmd:
                        cmd = str(inp)[:80]
                    tool_entries.append(f"[{name}] {str(cmd)[:100]}")

    if not text_blocks and not tool_entries:
        return None

    parts: list[str] = [
        "## Continuation from previous attempt",
        (
            "This is a **retry** — the previous attempt ran out of turns before finishing. "
            "The work below was already completed. Do NOT repeat it; pick up from where "
            "it left off and focus only on what remains."
        ),
    ]

    if text_blocks:
        # Show the last 4 reasoning blocks — most recent context is most useful.
        parts.append("\n### What was done (agent notes, last 4):")
        for txt in text_blocks[-4:]:
            # Cap each block so the prompt stays compact
            excerpt = txt[:400] + ("…" if len(txt) > 400 else "")
            parts.append(f"> {excerpt}")

    if tool_entries:
        # Show last 15 tool calls to give a concrete trail of actions.
        shown = tool_entries[-15:]
        parts.append(f"\n### Tool calls made (last {len(shown)}):")
        for entry in shown:
            parts.append(f"- {entry}")

    parts.append(
        "\n**Continue the task from the next logical step without re-reading "
        "files or re-running commands that are already reflected above.**"
    )
    return "\n".join(parts)


def _read_last_result_info(instance_id: UUID) -> tuple[str | None, int | None]:
    """Return (subtype, api_error_status) from the last RESULT event for an instance."""
    with _db() as db:
        last = db.scalar(
            select(InstanceEvent)
            .where(InstanceEvent.instance_id == instance_id)
            .where(InstanceEvent.event_type == InstanceEventType.RESULT)
            .order_by(InstanceEvent.id.desc())
            .limit(1)
        )
        if last is None:
            return None, None
        p = last.payload_json or {}
        return p.get("subtype"), p.get("api_error_status")


def _handle_nonzero_exit(instance_id: UUID, job_id: UUID, rc: int) -> None:
    """Decide between transient retry and permanent failure on exit code != 0.

    Claude Code exits 1 in two recoverable situations:
    - error_max_turns: the agent hit --max-turns; requeue for another attempt
    - api_error_status 429: five_hour rate limit exhausted; requeue to retry later

    Both cases are retried up to job.max_attempts times. On exhaustion the job
    becomes permanently FAILED so the task doesn't spin forever.
    """
    subtype, api_error = _read_last_result_info(instance_id)
    # error_during_execution: agent interrupted mid-tool (Docker hang, SIGTERM, etc.) — retriable
    should_requeue = subtype in ("error_max_turns", "error_during_execution") or api_error == 429

    if not should_requeue:
        _finalize_failed(instance_id, job_id, f"exit {rc}" + (f" ({subtype})" if subtype else ""))
        return

    # Build resume context before opening the DB write transaction so it runs
    # in its own read-only connection and doesn't hold locks during the HTTP
    # round-trip that _build_resume_context may do in the future.
    resume_ctx = _build_resume_context(instance_id) if subtype in ("error_max_turns", "error_during_execution") else None

    task_id_snapshot: UUID | None = None
    requeue_reason = "max_turns — requeued" if subtype == "error_max_turns" else "rate_limit 429 — requeued"
    permanent = False

    with _db() as db:
        inst = db.get(Instance, instance_id)
        if inst is not None:
            inst.status = InstanceStatus.FAILED
            inst.exit_code = rc
            inst.finished_at = datetime.now(tz=timezone.utc)
            task_id_snapshot = inst.task_id
        locks.release_all(db, instance_id=instance_id)
        job = db.get(Job, job_id)
        if job is not None:
            if job.attempts >= job.max_attempts:
                # Exhausted retries — fail permanently
                permanent = True
                release_job(db, job, final_status=JobStatus.FAILED,
                            error=f"{requeue_reason} (max attempts reached)", instance_id=instance_id)
            else:
                job.status = JobStatus.QUEUED
                job.claimed_at = None
                job.claimed_by = None
                if resume_ctx:
                    payload = dict(job.payload_json or {})
                    payload["resume_context"] = resume_ctx
                    job.payload_json = payload
                log.info(
                    "instance %s: %s (attempt %d/%d, resume_context=%s)",
                    instance_id, requeue_reason, job.attempts, job.max_attempts,
                    "yes" if resume_ctx else "no",
                )

    error_label = f"{requeue_reason} (permanent)" if permanent else requeue_reason
    broker.publish_sync("instance_finalized", {
        "instance_id": str(instance_id),
        "status": InstanceStatus.FAILED.value,
        "exit_code": rc,
        "error": error_label,
        "task_id": str(task_id_snapshot) if task_id_snapshot else None,
    })


def _finalize_failed(instance_id: UUID, job_id: UUID, error: str) -> None:
    _finalize(
        instance_id, job_id,
        InstanceStatus.FAILED, JobStatus.FAILED,
        exit_code=None, error=error,
    )


def _requeue_on_contention(instance_id: UUID, job_id: UUID) -> None:
    """Lock contention: mark instance FAILED, put the job back to QUEUED.

    The instance never ran, so the task stays in its current status. The
    orchestrator retries on the next tick once the conflicting lock is released.

    Respects job.max_attempts: if the job has already been attempted max_attempts
    times without ever getting the lock, it is failed permanently to prevent
    infinite spin-loops (e.g. when two jobs for the same task are both QUEUED).
    """
    task_id_snapshot: UUID | None = None
    permanent = False

    with _db() as db:
        inst = db.get(Instance, instance_id)
        if inst is not None:
            inst.status = InstanceStatus.FAILED
            inst.finished_at = datetime.now(tz=timezone.utc)
            task_id_snapshot = inst.task_id
        job = db.get(Job, job_id)
        if job is not None:
            if job.attempts >= job.max_attempts:
                permanent = True
                release_job(db, job, final_status=JobStatus.FAILED,
                            error="lock contention (max attempts reached)", instance_id=instance_id)
                log.warning(
                    "lock contention: job %s exhausted %d attempts — failing permanently",
                    job_id, job.max_attempts,
                )
            else:
                job.status = JobStatus.QUEUED
                job.claimed_at = None
                job.claimed_by = None
                log.info(
                    "lock contention: requeued job %s for next tick (attempt %d/%d, instance %s never ran)",
                    job_id, job.attempts, job.max_attempts, instance_id,
                )

    error = "lock contention (permanent)" if permanent else "lock contention — requeued"
    broker.publish_sync("instance_finalized", {
        "instance_id": str(instance_id),
        "status": InstanceStatus.FAILED.value,
        "exit_code": None,
        "error": error,
        "task_id": str(task_id_snapshot) if task_id_snapshot else None,
    })


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
    cmd_prefix = shlex.split(RuntimeConfig.get("claude_executable"), posix=(os.name != "nt"))
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
        "--session-id", str(ctx.task_id),
        "--model", ctx.role_model,
    ]
    if ctx.role_max_turns is not None:
        cmd += ["--max-turns", str(ctx.role_max_turns)]
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
    if ctx.task_spec:
        parts += ["", "## Task spec", ctx.task_spec]
    if ctx.task_ui_spec:
        parts += ["", "## UI spec", ctx.task_ui_spec]
    if ctx.task_module_hint:
        parts += ["", "## Module scope",
                  f"You may only edit files under: `{ctx.task_module_hint}`."]
    parts += ["", f"You are acting in the role of `{ctx.role_name}`."]
    if ctx.resume_context:
        parts += ["", ctx.resume_context]
    return "\n".join(parts)


def _build_env(*, instance_id: UUID, role_name: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if role_name:
        # Lets tooling (e.g. mock_claude in smokes) know which role is running.
        env["FLOWTRACK_ROLE_NAME"] = role_name
    api_key = RuntimeConfig.get("anthropic_api_key")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
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
