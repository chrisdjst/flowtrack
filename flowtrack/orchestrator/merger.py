"""Deterministic merge & deploy executor — the 'merge' pipeline stage.

KAN-40 decision: this is a builtin step, not a 9th LLM agent. Once
Review=APPROVE and QA=PASS there is no judgment left — branch, target and
gate are all decided; an agent would only re-narrate git's exit code.
Where judgment DOES reappear (merge conflict), the right move is routing
to a human/dev, not asking an LLM to resolve conflicts blind.

Flow (per claimed 'merge' job; supervise() diverts here by role name):
  1. Gate: TaskTransitions must prove 'pipeline: reviewer completed' AND
     'pipeline: qa completed'. Structural in the normal chain, but a merge
     job can be enqueued by hand — the gate makes Review+QA a hard rule.
  2. CAS merge: detached temp worktree at the target branch's current sha,
     `git merge --no-ff` the task branch there, then `git update-ref
     <target> <new> <old>` — atomic compare-and-swap, so the daemon's own
     checkout is never touched and concurrent merges can't stomp each
     other (loser retries from the fresh sha).
  3. Optional deploy_command in the target repo. Non-zero/timeout ->
     blocked/infra_failure, which the DevOps agent sweep picks up.
  4. Record a Deployment row (DORA: deployment frequency + lead time),
     task -> done.

Failure buckets: gate/CAS problems -> manual_intervention; merge conflict
-> code_failure (a dev must rebase); deploy failure -> infra_failure.

deploy_command caveat: the CAS moves the branch REF without touching the
target repo's primary working tree, so a deploy that builds from the
working tree must sync first (e.g. `git checkout -f <branch> && ...`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from flowtrack.api.events import broker
from flowtrack.core.database import SessionLocal
from flowtrack.core.runtime_config import RuntimeConfig
from flowtrack.models import Deployment, Instance, Job, Role, Task, TaskComment, TaskTransition
from flowtrack.models.deployment import Environment
from flowtrack.models.instance import InstanceStatus
from flowtrack.models.job import JobStatus
from flowtrack.models.task import BlockedReason, TaskStatus
from flowtrack.orchestrator.queue import release_job
from flowtrack.orchestrator.worktree import _run_git

log = logging.getLogger(__name__)

MERGE_ROLE_NAME = "merge"
_CAS_ATTEMPTS = 3

_GATE_REASONS = ("pipeline: reviewer completed", "pipeline: qa completed")


async def run_merge(job_id: UUID, instance_id: UUID) -> None:
    """Executor entrypoint. Always returns; failures land on the instance/task."""
    try:
        await _run_merge_inner(job_id, instance_id)
    except Exception:
        log.exception("merge executor crashed for instance %s", instance_id)
        await asyncio.to_thread(
            _finalize, job_id, instance_id,
            inst_status=InstanceStatus.FAILED, job_status=JobStatus.FAILED,
            error="merge executor crash",
        )


async def _run_merge_inner(job_id: UUID, instance_id: UUID) -> None:
    ctx = await asyncio.to_thread(_load_context, job_id, instance_id)
    if ctx is None:
        log.error("merge: instance/job/task missing for %s", instance_id)
        return

    if not ctx["gate_ok"]:
        await asyncio.to_thread(
            _finalize_blocked, job_id, instance_id,
            blocked_reason=BlockedReason.MANUAL_INTERVENTION,
            reason="merge: gate failed",
            body=(
                "Merge stage refused: could not verify Review=APPROVE and "
                "QA=PASS from the task's transitions. If this merge job was "
                "enqueued manually, run the task through the pipeline instead."
            ),
        )
        return

    if not ctx["source_branch"]:
        await asyncio.to_thread(
            _finalize_blocked, job_id, instance_id,
            blocked_reason=BlockedReason.MANUAL_INTERVENTION,
            reason="merge: no source branch",
            body="Merge stage has no parent_branch to merge (job payload empty). Human triage required.",
        )
        return

    target_repo = Path(RuntimeConfig.get("target_repo_path") or os.getcwd()).resolve()
    target_branch = RuntimeConfig.get("merge_target_branch")
    message = (
        f"auto-merge: {ctx['task_title']}"
        + (f" ({ctx['ticket_id']})" if ctx["ticket_id"] else "")
        + f"\n\nTask {ctx['task_id']}, branch {ctx['source_branch']}. Review=APPROVE, QA=PASS."
    )

    status, detail = await _cas_merge(
        repo=target_repo, target_branch=target_branch,
        source_branch=ctx["source_branch"], message=message,
    )
    if status == "conflict":
        await asyncio.to_thread(
            _finalize_blocked, job_id, instance_id,
            blocked_reason=BlockedReason.CODE_FAILURE,
            reason="merge: conflict",
            body=(
                f"Merge of {ctx['source_branch']} into {target_branch} hit "
                f"conflicts — a dev must rebase onto {target_branch}.\n\n"
                f"Conflicting files:\n{detail}"
            ),
        )
        return
    if status != "merged":
        await asyncio.to_thread(
            _finalize_blocked, job_id, instance_id,
            blocked_reason=BlockedReason.MANUAL_INTERVENTION,
            reason=f"merge: {status}",
            body=f"Merge stage failed ({status}): {detail}",
        )
        return
    new_sha = detail

    deploy_cmd = (RuntimeConfig.get("deploy_command") or "").strip()
    if deploy_cmd:
        rc, output = await _run_deploy(deploy_cmd, target_repo)
        if rc != 0:
            await asyncio.to_thread(
                _finalize_blocked, job_id, instance_id,
                blocked_reason=BlockedReason.INFRA_FAILURE,
                reason="merge: deploy failed",
                body=(
                    f"Merged {new_sha[:12]} into {target_branch} but deploy_command "
                    f"exited {rc}. Waiting for DevOps pickup.\n\n"
                    f"Output tail:\n{output[-1500:]}"
                ),
            )
            return

    await asyncio.to_thread(
        _finalize_success, job_id, instance_id,
        commit_sha=new_sha, target_branch=target_branch, deployed=bool(deploy_cmd),
    )


async def _cas_merge(
    *, repo: Path, target_branch: str, source_branch: str, message: str
) -> tuple[str, str]:
    """Merge source into target without touching any existing checkout.

    Returns (status, detail): ('merged', new_sha) | ('conflict', files) |
    ('cas_failed'|'error', description).
    """
    worktree_root = Path(RuntimeConfig.get("worktree_root")).resolve()
    worktree_root.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, _CAS_ATTEMPTS + 1):
        rc, out, err = await _run_git(
            repo, "rev-parse", "--verify", f"refs/heads/{target_branch}"
        )
        if rc != 0:
            return "error", f"target branch '{target_branch}' not found: {err.strip()}"
        old_sha = out.strip()

        path = worktree_root / f"merge-{uuid4().hex[:12]}"
        rc, out, err = await _run_git(
            repo, "worktree", "add", "--detach", str(path), old_sha
        )
        if rc != 0:
            return "error", f"merge worktree add failed: {err.strip() or out.strip()}"
        try:
            rc, out, err = await _run_git(
                path, "merge", "--no-ff", "-m", message, source_branch
            )
            if rc != 0:
                rc2, files, _ = await _run_git(
                    path, "diff", "--name-only", "--diff-filter=U"
                )
                await _run_git(path, "merge", "--abort")
                return "conflict", (files.strip() if rc2 == 0 and files.strip()
                                    else err.strip() or out.strip())
            rc, out, err = await _run_git(path, "rev-parse", "HEAD")
            if rc != 0:
                return "error", f"rev-parse after merge failed: {err.strip()}"
            new_sha = out.strip()

            # Atomic CAS: only move the ref if nobody else did meanwhile.
            rc, out, err = await _run_git(
                repo, "update-ref", f"refs/heads/{target_branch}", new_sha, old_sha
            )
            if rc == 0:
                log.info("merged %s into %s: %s -> %s",
                         source_branch, target_branch, old_sha[:12], new_sha[:12])
                return "merged", new_sha
            log.info("merge CAS lost on %s (attempt %d/%d) — retrying from fresh sha",
                     target_branch, attempt, _CAS_ATTEMPTS)
        finally:
            await _run_git(repo, "worktree", "remove", "--force", str(path))

    return "cas_failed", (
        f"'{target_branch}' kept moving during {_CAS_ATTEMPTS} merge attempts"
    )


async def _run_deploy(command: str, cwd: Path) -> tuple[int, str]:
    """Run deploy_command via the shell. Returns (rc, combined output)."""
    timeout = RuntimeConfig.get("deploy_timeout_seconds")
    log.info("deploy: running %r in %s (timeout %ds)", command, cwd, timeout)
    proc = await asyncio.create_subprocess_shell(
        command, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"deploy_command timed out after {timeout}s"
    return proc.returncode or 0, stdout.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Sync helpers (run inside asyncio.to_thread)                                  #
# --------------------------------------------------------------------------- #

def _load_context(job_id: UUID, instance_id: UUID) -> dict | None:
    with _db() as db:
        inst = db.get(Instance, instance_id)
        job = db.get(Job, job_id)
        task = db.get(Task, inst.task_id) if inst and inst.task_id else None
        if inst is None or job is None or task is None:
            return None
        payload = job.payload_json or {}
        source_branch = payload.get("parent_branch")

        reasons = set(db.scalars(
            select(TaskTransition.reason).where(TaskTransition.task_id == task.id)
        ))
        gate_ok = all(r in reasons for r in _GATE_REASONS)

        inst.status = InstanceStatus.RUNNING
        inst.branch_name = source_branch

        return {
            "task_id": str(task.id),
            "task_title": task.title,
            "ticket_id": task.ticket_id,
            "source_branch": source_branch,
            "gate_ok": gate_ok,
        }


def _finalize_blocked(
    job_id: UUID, instance_id: UUID, *,
    blocked_reason: BlockedReason, reason: str, body: str,
) -> None:
    # Lazy import: spawner imports merger's entrypoint at supervise time.
    from flowtrack.orchestrator.spawner import _block_task

    with _db() as db:
        inst = db.get(Instance, instance_id)
        job = db.get(Job, job_id)
        task = db.get(Task, inst.task_id) if inst and inst.task_id else None
        role = db.get(Role, inst.role_id) if inst else None
        if task is not None and role is not None:
            _block_task(
                db, task=task, role=role, instance_id=instance_id,
                reason=reason, body=body, blocked_reason=blocked_reason,
            )
        if inst is not None:
            inst.status = InstanceStatus.FAILED
            inst.finished_at = datetime.now(tz=timezone.utc)
        if job is not None:
            release_job(db, job, final_status=JobStatus.FAILED,
                        error=reason, instance_id=instance_id)


def _finalize_success(
    job_id: UUID, instance_id: UUID, *,
    commit_sha: str, target_branch: str, deployed: bool,
) -> None:
    with _db() as db:
        inst = db.get(Instance, instance_id)
        job = db.get(Job, job_id)
        task = db.get(Task, inst.task_id) if inst and inst.task_id else None
        role = db.get(Role, inst.role_id) if inst else None
        if task is None or role is None:
            return

        env_raw = RuntimeConfig.get("deploy_environment")
        try:
            environment = Environment(env_raw)
        except ValueError:
            log.warning("invalid deploy_environment=%r — recording as development", env_raw)
            environment = Environment.DEVELOPMENT
        db.add(Deployment(
            environment=environment,
            deployed_at=datetime.now(tz=timezone.utc),
            commit_sha=commit_sha,
            ticket_id=task.ticket_id,
        ))

        from_status = task.status.value if task.status else None
        task.status = TaskStatus.DONE
        task.blocked_reason = None
        db.add(TaskTransition(
            task_id=task.id, from_status=from_status, to_status=TaskStatus.DONE.value,
            instance_id=instance_id, reason="pipeline: merge completed",
        ))
        db.add(TaskComment(
            task_id=task.id,
            body=(
                f"Merged into {target_branch} at {commit_sha[:12]}"
                + (" and deployed" if deployed else " (merge-only, no deploy_command)")
                + ". Deployment recorded for DORA."
            ),
            author_role_id=role.id, instance_id=instance_id,
        ))

        if inst is not None:
            inst.status = InstanceStatus.COMPLETED
            inst.finished_at = datetime.now(tz=timezone.utc)
            inst.exit_code = 0
        if job is not None:
            release_job(db, job, final_status=JobStatus.DONE, instance_id=instance_id)

        task_id, ticket_id = str(task.id), task.ticket_id
        broker.publish_sync("merge_completed", {
            "task_id": task_id,
            "ticket_id": ticket_id,
            "commit_sha": commit_sha,
            "target_branch": target_branch,
            "deployed": deployed,
            "instance_id": str(instance_id),
        })
        broker.publish_sync("task_transitioned", {
            "task_id": task_id, "from_status": from_status,
            "to_status": TaskStatus.DONE.value, "by_role": MERGE_ROLE_NAME,
            "instance_id": str(instance_id),
        })

    # Push the terminal status to Jira outside the tx (same helper the
    # spawner uses; it no-ops when integrations are unconfigured).
    from flowtrack.integrations.jira_sync import push_task_status
    push_task_status(ticket_id, TaskStatus.DONE.value)


def _finalize(
    job_id: UUID, instance_id: UUID, *,
    inst_status: InstanceStatus, job_status: JobStatus, error: str | None,
) -> None:
    with _db() as db:
        inst = db.get(Instance, instance_id)
        job = db.get(Job, job_id)
        if inst is not None:
            inst.status = inst_status
            inst.finished_at = datetime.now(tz=timezone.utc)
        if job is not None:
            release_job(db, job, final_status=job_status,
                        error=error, instance_id=instance_id)


class _db:
    def __enter__(self):
        self._session = SessionLocal()
        return self._session

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
