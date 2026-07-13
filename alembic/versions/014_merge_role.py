"""Seed the builtin 'merge' pipeline role

Revision ID: 014
Revises: 013
Create Date: 2026-07-13

KAN-40 decision: merge & deploy is a deterministic builtin executor
(orchestrator/merger.py), not a 9th LLM agent — there is no judgment left
once Review=APPROVE and QA=PASS. The Role row exists so the stage flows
through the normal Job/Instance audit trail; the spawner diverts it to the
executor instead of spawning Claude (model='builtin' is a marker, never
sent to any API).

qa is NOT rewired here: entry into the merge stage is gated in code by the
merge_enabled runtime config (default off), keeping the default chain
(qa -> done) intact.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO roles (id, name, system_prompt, model, max_tokens, "
        "max_minutes, task_status_on_success) "
        "VALUES (gen_random_uuid(), 'merge', "
        "'builtin: deterministic merge & deploy executor (orchestrator/merger.py); "
        "never spawned as an LLM instance', "
        "'builtin', 0, 10, 'done') "
        "ON CONFLICT (name) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM roles WHERE name='merge' AND model='builtin'")
