"""worker_id on instances + job_queue for daemon isolation

Revision ID: 007
Revises: 006
Create Date: 2026-05-24

Background: while testing the orchestrator we caught a stale background
``flowtrack-server`` process (DRY_RUN=true) racing with a smoke run and
cancelling its jobs. The fix: each running daemon has a worker_id, and the
claim query filters to ``Job.worker_id IS NULL OR Job.worker_id == self``.

Tests (or any caller wanting an isolated lane) stamp a unique worker_id on
the jobs they create; production daemons ignore them. Production jobs stay
NULL and any daemon can claim them — backwards-compatible default.

The instance also stores the worker_id of the daemon that spawned it, purely
for audit / debug.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("job_queue", sa.Column("worker_id", sa.String(100)))
    op.add_column("instances", sa.Column("worker_id", sa.String(100)))
    # Claim query is `status='queued' AND (worker_id IS NULL OR worker_id = ?)`
    # ordered by (priority, created_at). The pending-only index already covers
    # the ordering; add a small index to speed the worker_id filter.
    op.create_index(
        "idx_job_queue_worker", "job_queue", ["worker_id"],
        postgresql_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_index("idx_job_queue_worker", table_name="job_queue")
    op.drop_column("instances", "worker_id")
    op.drop_column("job_queue", "worker_id")
