"""Pipeline fields on tasks + failure routing on roles

Revision ID: 010
Revises: 009
Create Date: 2026-07-13

tasks gains the autonomous-pipeline columns (bounce_count, blocked_reason,
task_spec, ui_spec, severity, pipeline_routing, and the PO sort inputs);
roles gains task_status_on_failure + max_bounce_count so failure routing is
data-driven instead of hardcoded per verdict.

age_days is intentionally NOT a column — the PO agent derives it from
tasks.created_at at read time.

Seeds: reviewer and qa route failures back to in_progress (this schema's
equivalent of "in_dev") with a shared cap of 3 bounces per task.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

blocked_reason = sa.Enum(
    "code_failure", "infra_failure", "security", "manual_intervention",
    name="blocked_reason",
)
task_severity = sa.Enum(
    "P0-Critical", "P1-High", "P2-Medium", "P3-Low",
    name="task_severity",
)
pipeline_routing = sa.Enum("design_dev", "dev_only", name="pipeline_routing")


def upgrade() -> None:
    bind = op.get_bind()
    blocked_reason.create(bind, checkfirst=True)
    task_severity.create(bind, checkfirst=True)
    pipeline_routing.create(bind, checkfirst=True)

    op.add_column(
        "tasks",
        sa.Column("bounce_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("tasks", sa.Column("blocked_reason", blocked_reason, nullable=True))
    op.add_column("tasks", sa.Column("task_spec", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("ui_spec", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("severity", task_severity, nullable=True))
    op.add_column("tasks", sa.Column("pipeline_routing", pipeline_routing, nullable=True))
    op.add_column("tasks", sa.Column("jira_priority", sa.String(50), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("is_urgent", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "tasks",
        sa.Column("is_overdue", sa.Boolean(), nullable=False, server_default="false"),
    )

    op.add_column("roles", sa.Column("task_status_on_failure", sa.String(50), nullable=True))
    op.add_column("roles", sa.Column("max_bounce_count", sa.Integer(), nullable=True))

    # Seed failure routing for the roles whose prompts emit rejection verdicts.
    # NULL task_status_on_failure elsewhere keeps today's behaviour (-> blocked).
    op.execute(
        "UPDATE roles SET task_status_on_failure='in_progress', max_bounce_count=3 "
        "WHERE name IN ('reviewer', 'qa')"
    )


def downgrade() -> None:
    op.drop_column("roles", "max_bounce_count")
    op.drop_column("roles", "task_status_on_failure")
    op.drop_column("tasks", "is_overdue")
    op.drop_column("tasks", "is_urgent")
    op.drop_column("tasks", "jira_priority")
    op.drop_column("tasks", "pipeline_routing")
    op.drop_column("tasks", "severity")
    op.drop_column("tasks", "ui_spec")
    op.drop_column("tasks", "task_spec")
    op.drop_column("tasks", "blocked_reason")
    op.drop_column("tasks", "bounce_count")

    bind = op.get_bind()
    pipeline_routing.drop(bind, checkfirst=True)
    task_severity.drop(bind, checkfirst=True)
    blocked_reason.drop(bind, checkfirst=True)
