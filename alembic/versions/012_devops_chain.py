"""Wire the devops role into the pipeline chain

Revision ID: 012
Revises: 011
Create Date: 2026-07-13

devops -> qa, task -> in_qa on success (verdict RESOLVED): an infra fix is
verified by re-running QA, which is also what clears blocked_reason via the
normal success path. devops BLOCKED maps to blocked/manual_intervention in
the spawner's verdict table, not here. Pure seed — no schema change.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE roles SET next_role_name='qa', task_status_on_success='in_qa' "
        "WHERE name='devops'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE roles SET next_role_name=NULL, task_status_on_success=NULL "
        "WHERE name='devops'"
    )
