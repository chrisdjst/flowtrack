"""extend instance_event_type with system / assistant / rate_limit_event

Revision ID: 008
Revises: 007
Create Date: 2026-05-25

Real Claude Code emits ``system`` (init/reinit), ``assistant`` (streamed
messages), and ``rate_limit_event`` lines that our original enum (designed
against the mock) didn't include. They were being bucketed as ``message`` —
not incorrect, just lossy for the kanban "what is this instance doing now"
summary. Add them as first-class values.

ADD VALUE on a Postgres enum cannot run inside a transaction, so each call
lives in its own autocommit block.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_VALUES = ("system", "assistant", "rate_limit_event")


def upgrade() -> None:
    for value in _NEW_VALUES:
        with op.get_context().autocommit_block():
            op.execute(
                f"ALTER TYPE instance_event_type ADD VALUE IF NOT EXISTS '{value}'"
            )


def downgrade() -> None:
    # Removing enum values requires recreating the type — see 005's note.
    # Left as no-op; the unused values are harmless.
    pass
