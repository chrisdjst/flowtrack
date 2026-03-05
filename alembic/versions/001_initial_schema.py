"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-04

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    session_type = postgresql.ENUM(
        "development", "review", "testing", name="session_type", create_type=True
    )
    session_status = postgresql.ENUM(
        "active", "paused", "ended", name="session_status", create_type=True
    )
    event_type = postgresql.ENUM(
        "block_start", "block_end", "interrupt_start", "interrupt_end", "pause", "resume",
        name="event_type", create_type=True,
    )
    environment = postgresql.ENUM(
        "production", "staging", "development", name="environment", create_type=True
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("type", session_type, nullable=False),
        sa.Column("ticket_id", sa.String(100)),
        sa.Column("pr_number", sa.Integer()),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("status", session_status, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("event_type", event_type, nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("metadata_json", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "deployments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("sessions.id")),
        sa.Column("environment", environment, nullable=False),
        sa.Column("deployed_at", sa.DateTime(), nullable=False),
        sa.Column("commit_sha", sa.String(40)),
        sa.Column("pr_number", sa.Integer()),
        sa.Column("ticket_id", sa.String(100)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "incidents",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("deployment_id", sa.UUID(), sa.ForeignKey("deployments.id")),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime()),
        sa.Column("description", sa.Text()),
        sa.Column("severity", sa.String(20)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "config",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("config")
    op.drop_table("incidents")
    op.drop_table("deployments")
    op.drop_table("events")
    op.drop_table("sessions")
    sa.Enum(name="session_type").drop(op.get_bind())
    sa.Enum(name="session_status").drop(op.get_bind())
    sa.Enum(name="event_type").drop(op.get_bind())
    sa.Enum(name="environment").drop(op.get_bind())
