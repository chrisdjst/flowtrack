"""Smoke for the new CLI gaps:

  flowtrack task update --criteria/--module-hint
  flowtrack task assign <id> <role>
  flowtrack task list  (must show the role column)
  flowtrack task show  (must include transitions, instances, jobs)
  flowtrack discovery list / promote / reject / refine

Uses Typer's CliRunner so the test stays in-process (no subprocess) and the
refine_async is patched to avoid spending real Anthropic tokens. The PM
agent's spend is also exercised in scripts/smoke_pm_agent.py against real
Claude — this one keeps free.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid as _uuid
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "FLOWTRACK_DATABASE_URL",
    "postgresql://flowtrack:flowtrack@localhost:5433/flowtrack",
)

from sqlalchemy import select  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from flowtrack.core.database import SessionLocal  # noqa: E402
from flowtrack.main import app  # noqa: E402
from flowtrack.models import DiscoveredItem, Instance, Job, Role, Task, TaskTransition  # noqa: E402
from flowtrack.models.discovered_item import (  # noqa: E402
    DiscoveryKind,
    DiscoverySource,
    DiscoveryStatus,
)
from flowtrack.models.instance import InstanceStatus  # noqa: E402
from flowtrack.models.task import TaskPriority, TaskStatus  # noqa: E402


_RUN = _uuid.uuid4().hex[:6]
TICKET = f"CLI-{_RUN}"
SOURCE_REF = f"CLI-SMOKE-{_RUN}"
runner = CliRunner()


def _ok(result, *, label: str) -> bool:
    if result.exit_code != 0:
        exc = result.exception
        import traceback as _tb
        tb = ""
        if exc is not None:
            tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
        print(
            f"  [FAIL] {label}: exit={result.exit_code}\n"
            f"    stdout={result.stdout[:400]!r}\n"
            f"    exception={exc!r}\n"
            f"    traceback={tb[-800:] if tb else ''}"
        )
        return False
    return True


def _setup_task_and_instance() -> tuple[_uuid.UUID, _uuid.UUID]:
    """Seed a task + one completed instance + a transition + a job
    so we can verify `task show` displays each section."""
    db = SessionLocal()
    try:
        dev_id = db.scalar(select(Role.id).where(Role.name == "dev"))
        task = Task(
            title=f"CLI smoke task {_RUN}",
            status=TaskStatus.TODO,
            priority=TaskPriority.MEDIUM,
            ticket_id=TICKET,
        )
        db.add(task)
        db.flush()
        # Completed instance.
        inst = Instance(
            role_id=dev_id, task_id=task.id,
            status=InstanceStatus.COMPLETED,
            tokens_input=1234, tokens_output=567,
            cost_usd=Decimal("0.0191"),
        )
        db.add(inst)
        db.flush()
        # Transition.
        db.add(TaskTransition(
            task_id=task.id, from_status="todo", to_status="in_review",
            instance_id=inst.id, reason="smoke: pipeline dev completed",
        ))
        # Done job, plus a queued one.
        db.add(Job(task_id=task.id, role_id=dev_id, status="done",
                   attempts=1, priority=10))
        db.add(Job(task_id=task.id, role_id=dev_id, status="queued",
                   attempts=0, priority=20))
        db.commit()
        return task.id, inst.id
    finally:
        db.close()


def _seed_discovered() -> _uuid.UUID:
    db = SessionLocal()
    try:
        item = DiscoveredItem(
            source=DiscoverySource.GITHUB_ISSUE,
            source_ref=SOURCE_REF,
            kind=DiscoveryKind.BUG,
            title=f"CLI smoke discovered ({_RUN})",
            summary="A bug seen via the CLI smoke.",
            status=DiscoveryStatus.NEW,
        )
        db.add(item)
        db.flush()
        item_id = item.id
        db.commit()
        return item_id
    finally:
        db.close()


# Patch refine_async BEFORE any test invocation reads from flowtrack.agents.pm.
from flowtrack.agents import pm as _pm  # noqa: E402


async def _fake_refine(*, title, summary, kind, source, source_ref,
                       raw_payload=None, model="sonnet", timeout_seconds=90):  # noqa: ARG001
    return _pm.RefinementResult(
        acceptance_criteria="1. CLI smoke acceptance criteria (mocked).",
        module_hint="cli-smoke",
        recommendation="promote",
        cost_usd=Decimal("0.0000"),
    )


_pm.refine_async = _fake_refine


def main() -> int:
    task_id, inst_id = _setup_task_and_instance()
    item_id = _seed_discovered()
    short_task = str(task_id)[:8]
    short_item = str(item_id)[:8]

    results: list[tuple[str, bool]] = []

    # 1. task update with --criteria and --module-hint
    r = runner.invoke(app, [
        "task", "update", short_task,
        "--criteria", "1. The CLI updates this. 2. Show reflects it.",
        "--module-hint", "cli-test",
    ])
    ok = _ok(r, label="task update --criteria/--module-hint")
    if ok:
        db = SessionLocal()
        try:
            t = db.get(Task, task_id)
            ok = (
                t.acceptance_criteria.startswith("1. The CLI updates this.")
                and t.module_hint == "cli-test"
            )
            if not ok:
                print(f"  [FAIL] update fields didn't persist: criteria={t.acceptance_criteria!r} module={t.module_hint!r}")
        finally:
            db.close()
    results.append(("task update +criteria+module_hint", ok))

    # 2. task list shows Role + Module columns
    r = runner.invoke(app, ["task", "list", "--status", "todo"])
    ok = _ok(r, label="task list") and "Role" in r.stdout and "Module" in r.stdout \
         and "cli-test" in r.stdout
    if not ok:
        print(f"  list stdout (truncated):\n{r.stdout[:400]}")
    results.append(("task list shows Role+Module", ok))

    # 3. task show: must include Acceptance criteria + Jobs + Instances + Transitions sections
    r = runner.invoke(app, ["task", "show", short_task])
    ok = _ok(r, label="task show")
    if ok:
        for needle in [
            "Acceptance criteria",
            "Module hint",
            "Jobs",
            "Instances",
            "Pipeline transitions",
            "smoke: pipeline dev completed",
        ]:
            if needle not in r.stdout:
                print(f"  [FAIL] task show missing {needle!r}")
                ok = False
    results.append(("task show enriched", ok))

    # 4. task assign: enqueues a Job
    r = runner.invoke(app, ["task", "assign", short_task, "reviewer", "--priority", "30"])
    ok = _ok(r, label="task assign") and "queued" in r.stdout.lower()
    if ok:
        db = SessionLocal()
        try:
            jobs = list(db.scalars(select(Job).where(Job.task_id == task_id, Job.role_id.in_(
                select(Role.id).where(Role.name == "reviewer")
            ))))
            ok = len(jobs) >= 1 and any(j.priority == 30 for j in jobs)
            if not ok:
                print(f"  [FAIL] reviewer job missing or wrong priority: {[(j.priority, j.status.value) for j in jobs]}")
        finally:
            db.close()
    results.append(("task assign creates Job", ok))

    # 4b. task assign with invalid role -> exit 1
    r = runner.invoke(app, ["task", "assign", short_task, "no-such-role"])
    ok = r.exit_code != 0 and "not found" in r.stdout.lower()
    if not ok:
        print(f"  [FAIL] invalid role didn't error: exit={r.exit_code} stdout={r.stdout[:200]}")
    results.append(("task assign rejects unknown role", ok))

    # 5. discovery list
    # Scope to source=github_issue so this run's item is included even when
    # a noisy DB has >200 NEW items. Rich table truncates long cells with an
    # ellipsis — we only assert the unique RUN tag appears.
    r = runner.invoke(app, ["discovery", "list", "--source", "github_issue"])
    ok = _ok(r, label="discovery list")
    if ok and _RUN not in r.stdout:
        print(f"  [FAIL] discovery list: missing _RUN={_RUN!r} (item not in scoped list)")
        ok = False
    results.append(("discovery list", ok))

    # 6. discovery show
    r = runner.invoke(app, ["discovery", "show", short_item])
    ok = _ok(r, label="discovery show") and "CLI smoke discovered" in r.stdout
    results.append(("discovery show", ok))

    # 7. discovery refine (mocked refine_async returns promote)
    r = runner.invoke(app, ["discovery", "refine", short_item])
    ok = _ok(r, label="discovery refine (preview)") and "Recommendation" in r.stdout \
         and "promote" in r.stdout
    if ok:
        # Without --apply, item should still be NEW.
        db = SessionLocal()
        try:
            it = db.get(DiscoveredItem, item_id)
            ok = it.status == DiscoveryStatus.NEW
            if not ok:
                print(f"  [FAIL] refine without --apply mutated: {it.status.value}")
        finally:
            db.close()
    results.append(("discovery refine preview", ok))

    # 8. discovery refine --apply (should promote and create a Task)
    r = runner.invoke(app, ["discovery", "refine", short_item, "--apply"])
    ok = _ok(r, label="discovery refine --apply") and "Applied: promoted" in r.stdout
    promoted_task_id: _uuid.UUID | None = None
    if ok:
        db = SessionLocal()
        try:
            it = db.get(DiscoveredItem, item_id)
            ok = it.status == DiscoveryStatus.PROMOTED and it.promoted_task_id is not None
            if ok:
                promoted_task_id = it.promoted_task_id
                t = db.get(Task, promoted_task_id)
                ok = (
                    t.acceptance_criteria
                    and t.module_hint == "cli-smoke"
                )
            if not ok:
                print(f"  [FAIL] --apply didn't fully promote: status={it.status.value} task={promoted_task_id}")
        finally:
            db.close()
    results.append(("discovery refine --apply creates Task", ok))

    # 9. discovery reject (need a fresh item — the one above is promoted)
    fresh_id = _seed_discovered_again()
    r = runner.invoke(app, ["discovery", "reject", str(fresh_id)[:8], "--reason", "smoke"])
    ok = _ok(r, label="discovery reject") and "Rejected" in r.stdout
    if ok:
        db = SessionLocal()
        try:
            it = db.get(DiscoveredItem, fresh_id)
            ok = it.status == DiscoveryStatus.REJECTED
        finally:
            db.close()
    results.append(("discovery reject", ok))

    # Cleanup: cancel pending jobs we created so they don't get claimed by a
    # daemon running in the background.
    db = SessionLocal()
    try:
        from sqlalchemy import update
        from flowtrack.models.job import JobStatus
        db.execute(
            update(Job)
            .where(Job.task_id.in_([task_id, promoted_task_id] if promoted_task_id else [task_id]))
            .where(Job.status == JobStatus.QUEUED)
            .values(status=JobStatus.CANCELLED, last_error="cli-smoke teardown")
        )
        db.commit()
    finally:
        db.close()

    print()
    print(f"=== CLI SMOKE ({len(results)} checks) ===")
    all_ok = True
    for label, ok in results:
        marker = "ok" if ok else "FAIL"
        print(f"  [{marker}] {label}")
        if not ok:
            all_ok = False
    print()
    print("RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 2


def _seed_discovered_again() -> _uuid.UUID:
    """Another fresh item for the reject test (the first is already promoted)."""
    db = SessionLocal()
    try:
        item = DiscoveredItem(
            source=DiscoverySource.JIRA,
            source_ref=f"CLI-REJ-{_uuid.uuid4().hex[:6]}",
            kind=DiscoveryKind.IMPROVEMENT,
            title=f"CLI smoke reject ({_RUN})",
            summary="To be rejected via CLI",
            status=DiscoveryStatus.NEW,
        )
        db.add(item)
        db.flush()
        item_id = item.id
        db.commit()
        return item_id
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
