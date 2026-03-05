"""add tasks table

Revision ID: 002
Revises: 001
Create Date: 2026-03-05

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    task_status = postgresql.ENUM(
        "todo", "in_progress", "blocked", "in_review", "done",
        name="task_status", create_type=True,
    )
    task_priority = postgresql.ENUM(
        "low", "medium", "high", "urgent",
        name="task_priority", create_type=True,
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", task_status, nullable=False, server_default="todo"),
        sa.Column("priority", task_priority, nullable=False, server_default="medium"),
        sa.Column("ticket_id", sa.String(100)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("tasks")
    sa.Enum(name="task_status").drop(op.get_bind())
    sa.Enum(name="task_priority").drop(op.get_bind())
