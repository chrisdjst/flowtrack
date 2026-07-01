"""Add max_turns to roles table

Revision ID: 009
Revises: 008
Create Date: 2026-07-01

max_turns maps directly to Claude Code's --max-turns flag. NULL = no limit
(existing behaviour). Seed sensible defaults per role so the constraint takes
effect immediately after the migration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Defaults chosen from observed usage patterns:
#   dev      — complex multi-step work, allow up to 20 turns
#   reviewer — read-only review, should conclude in 5 turns
#   qa       — run tests + report, 8 turns enough
#   pm/po    — short analytical tasks, 5 turns
#   design   — produce a doc, 8 turns
#   devops   — targeted fix, 8 turns
_DEFAULTS: dict[str, int] = {
    "dev":      20,
    "reviewer":  5,
    "qa":        8,
    "pm":        5,
    "po":        5,
    "design":    8,
    "devops":    8,
}


def upgrade() -> None:
    op.add_column("roles", sa.Column("max_turns", sa.Integer(), nullable=True))
    conn = op.get_bind()
    for role_name, turns in _DEFAULTS.items():
        conn.execute(
            sa.text("UPDATE roles SET max_turns = :t WHERE name = :n"),
            {"t": turns, "n": role_name},
        )


def downgrade() -> None:
    op.drop_column("roles", "max_turns")
