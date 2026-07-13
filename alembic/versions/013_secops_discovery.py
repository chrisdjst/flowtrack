"""SecOps as a discovery source: new enum values only

Revision ID: 013
Revises: 012
Create Date: 2026-07-13

Adds 'secops' to discovery_source and 'security' to discovery_kind so the
SecOps scan can emit discovered_items into the TPM funnel. ALTER TYPE ADD
VALUE must run outside the migration transaction (the value would be
unusable before commit), hence the autocommit block.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE discovery_source ADD VALUE IF NOT EXISTS 'secops'")
        op.execute("ALTER TYPE discovery_kind ADD VALUE IF NOT EXISTS 'security'")


def downgrade() -> None:
    # Postgres cannot drop enum values without rebuilding the type (and every
    # column using it). The stray values are harmless; leave them in place.
    pass
