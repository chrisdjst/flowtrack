"""role pipeline: add next_role_name + task_status_on_success to roles

Revision ID: 006
Revises: 005
Create Date: 2026-05-24

Enables the data-driven role chain. When an instance of role X completes
successfully, the orchestrator looks up X.next_role_name (if any) and enqueues
a Job for that next role on the same task, plus sets task.status to
X.task_status_on_success.

Default chain (conservative — only the dev→reviewer→qa flow auto-chains;
pm/po/design/devops stay manual until trust is established):

    dev       → next=reviewer, task→in_review
    reviewer  → next=qa,       task→in_qa
    qa        → next=NULL,     task→done
    (others)  → next=NULL,     task=NULL  (no change)

Tune via /api/roles when the API supports PATCH (TBD).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("roles", sa.Column("next_role_name", sa.String(50)))
    op.add_column("roles", sa.Column("task_status_on_success", sa.String(50)))

    # Seed the conservative default chain. Idempotent — re-run safe.
    op.execute(
        "UPDATE roles SET next_role_name='reviewer', task_status_on_success='in_review' "
        "WHERE name='dev'"
    )
    op.execute(
        "UPDATE roles SET next_role_name='qa', task_status_on_success='in_qa' "
        "WHERE name='reviewer'"
    )
    op.execute(
        "UPDATE roles SET task_status_on_success='done' WHERE name='qa'"
    )


def downgrade() -> None:
    op.drop_column("roles", "task_status_on_success")
    op.drop_column("roles", "next_role_name")
