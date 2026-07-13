"""Smoke for the SecOps discovery source ($0, no network, no orchestrator).

Scenarios:
  SECRET SCANNER — a planted GitHub-token-shaped string in a throwaway git
    repo is found, severity CRITICAL, and the stored summary/raw never
    contain the full credential.
  PIN COLLECTION — _collect_pins reads exact pins from requirements.txt.
  ESCALATION — manager._run_source with a fake source: the CRITICAL
    candidate becomes a promoted item + task blocked/security at P0/urgent
    (with transition + comment) and is NOT returned for TPM refinement; the
    MODERATE candidate stays NEW and IS returned.
  IDEMPOTENCY — a second _run_source run inserts nothing and creates no
    second task.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid as _uuid
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

RUN_TAG = _uuid.uuid4().hex[:8]
REF_PREFIX = f"smoke-secops-{RUN_TAG}"


def check_secret_scanner() -> bool:
    from flowtrack.discovery.sources.secops_scan import scan_secrets

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        fake_token = "ghp_" + "a1B2" * 10  # shape-valid, not a real credential
        (repo / "config.py").write_text(f'TOKEN = "{fake_token}"\n', encoding="utf-8")
        (repo / "clean.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)

        findings = scan_secrets(repo)
        ok = (
            len(findings) == 1
            and findings[0].severity == "CRITICAL"
            and findings[0].raw["file"] == "config.py"
            and fake_token not in (findings[0].summary or "")
            and fake_token not in str(findings[0].raw)
        )
        print(f"=== SECRET SCANNER ===\n  findings = {[f.ref for f in findings]}\n  -> {'PASS' if ok else 'FAIL'}")
        return ok


def check_pin_collection() -> bool:
    from flowtrack.discovery.sources.secops_scan import _collect_pins

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "requirements.txt").write_text(
            "requests==2.31.0\nflask>=2.0\n# comment\nurllib3==1.26.5  # pinned\n",
            encoding="utf-8",
        )
        pins = _collect_pins(repo)
        ok = pins == {"requests": "2.31.0", "urllib3": "1.26.5"}
        print(f"=== PIN COLLECTION ===\n  pins = {pins}\n  -> {'PASS' if ok else 'FAIL'}")
        return ok


class FakeSecOpsSource:
    name = "secops"
    interval_seconds = 3600

    def fetch(self):
        from flowtrack.discovery.base import DiscoveryCandidate
        from flowtrack.models.discovered_item import DiscoveryKind, DiscoverySource

        return [
            DiscoveryCandidate(
                source=DiscoverySource.SECOPS,
                source_ref=f"{REF_PREFIX}:secret:critical",
                kind=DiscoveryKind.SECURITY,
                title=f"[{RUN_TAG}] Exposed github-token in config.py",
                summary="config.py:3 matches the github-token pattern. Rotate it.",
                raw_payload={"scanner": "secrets", "secops_severity": "CRITICAL"},
                signal_score=Decimal(999),
                critical=True,
            ),
            DiscoveryCandidate(
                source=DiscoverySource.SECOPS,
                source_ref=f"{REF_PREFIX}:dep:moderate",
                kind=DiscoveryKind.SECURITY,
                title=f"[{RUN_TAG}] GHSA-xxxx: urllib3 1.26.5 is vulnerable",
                summary="Moderate severity advisory.",
                raw_payload={"scanner": "dependencies", "secops_severity": "MODERATE"},
                signal_score=Decimal(400),
                critical=False,
            ),
        ]


def check_escalation() -> bool:
    from sqlalchemy import select

    from flowtrack.core.database import SessionLocal
    from flowtrack.discovery.manager import _run_source
    from flowtrack.models import DiscoveredItem, Task, TaskComment, TaskTransition
    from flowtrack.models.discovered_item import DiscoveryStatus
    from flowtrack.models.task import BlockedReason, TaskPriority, TaskSeverity, TaskStatus

    first_ids = _run_source(FakeSecOpsSource())
    second_ids = _run_source(FakeSecOpsSource())  # idempotency

    db = SessionLocal()
    try:
        items = {
            i.source_ref: i for i in db.scalars(
                select(DiscoveredItem).where(DiscoveredItem.source_ref.like(f"{REF_PREFIX}%"))
            )
        }
        critical = items.get(f"{REF_PREFIX}:secret:critical")
        moderate = items.get(f"{REF_PREFIX}:dep:moderate")
        task = db.get(Task, critical.promoted_task_id) if critical and critical.promoted_task_id else None
        transitions = comments = []
        if task is not None:
            transitions = list(db.scalars(
                select(TaskTransition).where(TaskTransition.task_id == task.id)))
            comments = list(db.scalars(
                select(TaskComment).where(TaskComment.task_id == task.id)))
        n_tasks = len(list(db.scalars(
            select(Task).where(Task.title.like(f"[{RUN_TAG}]%")))))

        diag = {
            "first_ids": len(first_ids),
            "second_ids": len(second_ids),
            "critical_status": critical.status.value if critical else None,
            "moderate_status": moderate.status.value if moderate else None,
            "task_status": task.status.value if task else None,
            "blocked_reason": task.blocked_reason.value if task and task.blocked_reason else None,
            "priority": task.priority.value if task else None,
            "severity": task.severity.value if task and task.severity else None,
            "transitions": [(t.from_status, t.to_status, t.reason) for t in transitions],
            "n_comments": len(comments),
            "n_tasks": n_tasks,
        }
        ok = (
            critical is not None and moderate is not None and task is not None
            # moderate flows to TPM, critical does not
            and first_ids == [moderate.id]
            and second_ids == []
            and n_tasks == 1
            and critical.status == DiscoveryStatus.PROMOTED
            and moderate.status == DiscoveryStatus.NEW
            and task.status == TaskStatus.BLOCKED
            and task.blocked_reason == BlockedReason.SECURITY
            and task.priority == TaskPriority.URGENT
            and task.severity == TaskSeverity.P0_CRITICAL
            and any(t.to_status == "blocked" and t.reason == "secops: CRITICAL finding"
                    for t in transitions)
            and len(comments) >= 1
        )
    finally:
        db.close()

    print("=== ESCALATION + IDEMPOTENCY ===")
    for k, v in diag.items():
        print(f"  {k} = {v}")
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return ok


def cleanup() -> None:
    from sqlalchemy import delete, select, update

    from flowtrack.core.database import SessionLocal
    from flowtrack.models import DiscoveredItem, Task, TaskComment, TaskTransition

    db = SessionLocal()
    try:
        # Sweep every smoke-secops run (this one + stale leftovers).
        # Tasks first: tasks.discovered_from references discovered_items.
        task_ids = list(db.scalars(
            select(DiscoveredItem.promoted_task_id)
            .where(DiscoveredItem.source_ref.like("smoke-secops-%"))
            .where(DiscoveredItem.promoted_task_id.is_not(None))
        ))
        if task_ids:
            # Break the FK cycle (items.promoted_task_id <-> tasks.discovered_from)
            db.execute(
                update(DiscoveredItem)
                .where(DiscoveredItem.promoted_task_id.in_(task_ids))
                .values(promoted_task_id=None)
            )
            db.execute(delete(TaskComment).where(TaskComment.task_id.in_(task_ids)))
            db.execute(delete(TaskTransition).where(TaskTransition.task_id.in_(task_ids)))
            db.execute(delete(Task).where(Task.id.in_(task_ids)))
        db.execute(delete(DiscoveredItem).where(
            DiscoveredItem.source_ref.like("smoke-secops-%")))
        db.commit()
    finally:
        db.close()


def main() -> int:
    ok = True
    try:
        ok = check_secret_scanner() and ok
        ok = check_pin_collection() and ok
        ok = check_escalation() and ok
    finally:
        cleanup()
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
