"""Wire the design role into the pipeline chain

Revision ID: 011
Revises: 010
Create Date: 2026-07-13

design -> dev, task -> in_progress on success. The design stage is
conditional: the PO admission targets `design` only for tasks with
pipeline_routing='design_dev' (see agents/po.py); dev_only tasks never
enter it. Pure seed — no schema change.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE roles SET next_role_name='dev', task_status_on_success='in_progress' "
        "WHERE name='design'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE roles SET next_role_name=NULL, task_status_on_success=NULL "
        "WHERE name='design'"
    )
