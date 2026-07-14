"""Projects table + role specialization/dispatch fields

Revision ID: 015
Revises: 014
Create Date: 2026-07-13

Mechanism for the specialization dispatch layer (KAN-42):
- projects: minimal registry (a task/role may belong to one)
- tasks.project_id: the task-side dispatch signal
- roles.base_role_name: NULL = base role; set = this row is a variant of
  that base (e.g. name='dev-frontend', base_role_name='dev'). Explicit
  column instead of name-prefix inference.
- roles.project_id / specialization / match_patterns: variant eligibility
  and specificity inputs (patterns are fnmatch globs against module_hint)

No concrete specializations are seeded — with zero variant rows the
dispatcher resolves exactly like the old name lookup.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("repo_path", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.add_column("tasks", sa.Column(
        "project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True
    ))
    op.add_column("roles", sa.Column(
        "project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True
    ))
    op.add_column("roles", sa.Column("specialization", sa.String(100), nullable=True))
    op.add_column("roles", sa.Column("match_patterns", ARRAY(sa.String(255)), nullable=True))
    op.add_column("roles", sa.Column("base_role_name", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("roles", "base_role_name")
    op.drop_column("roles", "match_patterns")
    op.drop_column("roles", "specialization")
    op.drop_column("roles", "project_id")
    op.drop_column("tasks", "project_id")
    op.drop_table("projects")
