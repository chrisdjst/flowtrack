"""orchestrator schema: roles, instances, jobs, locks, discovery, events, budgets

Revision ID: 005
Revises: 004
Create Date: 2026-05-23

Adds the foundation tables for the multi-instance Claude Code orchestrator.
See docs/ORCHESTRATOR.md for the full design rationale.

Tables created (in dependency order):
    roles, instances, job_queue, resource_locks, task_transitions,
    discovered_items, instance_events, budget_windows

Existing tables altered:
    sessions       (+instance_id)
    tasks          (+discovered_from, module_hint, acceptance_criteria, parent_task_id)
    task_comments  (+author_role_id, +instance_id)
    task_status enum (+in_qa)

Seeds 7 default roles (pm, po, design, dev, reviewer, qa, devops). Prompts are
deliberately concise — tune via /api/roles, not via new migrations.

NOTE on downgrade: the 'in_qa' enum value cannot be removed without recreating
the type. Downgrade leaves it in place (harmless — code stops emitting it).
"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Enums created in this migration (named for reuse in upgrade/downgrade).
INSTANCE_STATUS = postgresql.ENUM(
    "spawning", "running", "waiting_input", "completed", "failed", "killed",
    name="instance_status", create_type=False,
)
JOB_STATUS = postgresql.ENUM(
    "queued", "claimed", "running", "done", "failed", "cancelled",
    name="job_status", create_type=False,
)
DISCOVERY_SOURCE = postgresql.ENUM(
    "sentry", "jira", "github_issue", "pr_comment", "log_pattern",
    name="discovery_source", create_type=False,
)
DISCOVERY_KIND = postgresql.ENUM(
    "bug", "feature", "improvement", "tech_debt", "incident",
    name="discovery_kind", create_type=False,
)
DISCOVERY_STATUS = postgresql.ENUM(
    "new", "promoted", "rejected", "duplicate",
    name="discovery_status", create_type=False,
)
INSTANCE_EVENT_TYPE = postgresql.ENUM(
    "tool_use", "message", "error", "thinking", "heartbeat", "usage", "result",
    name="instance_event_type", create_type=False,
)
BUDGET_WINDOW_KIND = postgresql.ENUM(
    "hour", "day", "month",
    name="budget_window_kind", create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create enum types up-front (so we can reference them in CREATE TABLE).
    for enum_obj in (
        INSTANCE_STATUS, JOB_STATUS, DISCOVERY_SOURCE, DISCOVERY_KIND,
        DISCOVERY_STATUS, INSTANCE_EVENT_TYPE, BUDGET_WINDOW_KIND,
    ):
        enum_obj.create(bind, checkfirst=True)

    # 2. roles (no FKs).
    op.create_table(
        "roles",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("tools_allowed", postgresql.ARRAY(sa.String(50))),
        sa.Column("model", sa.String(50), nullable=False, server_default="claude-sonnet-4-6"),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="500000"),
        sa.Column("max_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # 3. instances (refs roles, tasks, sessions).
    op.create_table(
        "instances",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("task_id", sa.UUID(), sa.ForeignKey("tasks.id")),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("sessions.id")),
        sa.Column("claude_session_id", sa.String(100)),
        sa.Column("pid", sa.Integer()),
        sa.Column("worktree_path", sa.Text()),
        sa.Column("branch_name", sa.String(255)),
        sa.Column("status", INSTANCE_STATUS, nullable=False, server_default="spawning"),
        sa.Column(
            "spawned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("tokens_input", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tokens_output", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "idx_instances_status_active",
        "instances",
        ["status"],
        postgresql_where=sa.text("status IN ('spawning','running','waiting_input')"),
    )
    op.create_index("idx_instances_task", "instances", ["task_id"])

    # 4. job_queue (refs tasks, roles, instances).
    op.create_table(
        "job_queue",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("task_id", sa.UUID(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "payload_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", JOB_STATUS, nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by", sa.UUID(), sa.ForeignKey("instances.id")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_job_queue_pending",
        "job_queue",
        ["priority", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )

    # 5. resource_locks (refs instances). UNIQUE on resource_key is the mutex.
    op.create_table(
        "resource_locks",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("resource_key", sa.String(255), nullable=False, unique=True),
        sa.Column("instance_id", sa.UUID(), sa.ForeignKey("instances.id"), nullable=False),
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 6. task_transitions (refs tasks, instances).
    op.create_table(
        "task_transitions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("task_id", sa.UUID(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("from_status", sa.String(50)),
        sa.Column("to_status", sa.String(50), nullable=False),
        sa.Column("instance_id", sa.UUID(), sa.ForeignKey("instances.id")),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "transitioned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # 7. discovered_items (refs tasks).
    op.create_table(
        "discovered_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("source", DISCOVERY_SOURCE, nullable=False),
        sa.Column("source_ref", sa.String(255)),
        sa.Column("kind", DISCOVERY_KIND, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("raw_payload", postgresql.JSONB()),
        sa.Column("signal_score", sa.Numeric(5, 2)),
        sa.Column("status", DISCOVERY_STATUS, nullable=False, server_default="new"),
        sa.Column("promoted_task_id", sa.UUID(), sa.ForeignKey("tasks.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("source", "source_ref", name="uq_discovered_source_ref"),
    )

    # 8. instance_events (high volume, bigserial PK).
    op.create_table(
        "instance_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("instance_id", sa.UUID(), sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("event_type", INSTANCE_EVENT_TYPE, nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_instance_events_by_instance",
        "instance_events",
        ["instance_id", sa.text("recorded_at DESC")],
    )

    # 9. budget_windows.
    op.create_table(
        "budget_windows",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_kind", BUDGET_WINDOW_KIND, nullable=False),
        sa.Column("tokens_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("task_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("window_start", "window_kind", name="uq_budget_window"),
    )

    # 10. Alter existing tables: sessions, tasks, task_comments.
    op.add_column("sessions", sa.Column("instance_id", sa.UUID(), sa.ForeignKey("instances.id")))

    op.add_column(
        "tasks",
        sa.Column("discovered_from", sa.UUID(), sa.ForeignKey("discovered_items.id")),
    )
    op.add_column("tasks", sa.Column("module_hint", sa.String(100)))
    op.add_column("tasks", sa.Column("acceptance_criteria", sa.Text()))
    op.add_column(
        "tasks",
        sa.Column("parent_task_id", sa.UUID(), sa.ForeignKey("tasks.id")),
    )

    op.add_column(
        "task_comments",
        sa.Column("author_role_id", sa.UUID(), sa.ForeignKey("roles.id")),
    )
    op.add_column(
        "task_comments",
        sa.Column("instance_id", sa.UUID(), sa.ForeignKey("instances.id")),
    )

    # 11. Seed default roles. Must come BEFORE the autocommit_block below — that
    # block commits the current transaction, so any failure after it would leave
    # half the migration applied without alembic_version being updated.
    _seed_default_roles()

    # 12. Add 'in_qa' to task_status enum.
    # Postgres requires ALTER TYPE ADD VALUE outside a transaction.
    # Placed last so a seed failure rolls back cleanly above.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'in_qa' BEFORE 'done'")


def downgrade() -> None:
    # Drop in reverse dependency order. Enum 'in_qa' value not removable
    # without recreating task_status type — left in place (see header note).
    op.drop_column("task_comments", "instance_id")
    op.drop_column("task_comments", "author_role_id")

    op.drop_column("tasks", "parent_task_id")
    op.drop_column("tasks", "acceptance_criteria")
    op.drop_column("tasks", "module_hint")
    op.drop_column("tasks", "discovered_from")

    op.drop_column("sessions", "instance_id")

    op.drop_table("budget_windows")
    op.drop_index("idx_instance_events_by_instance", table_name="instance_events")
    op.drop_table("instance_events")
    op.drop_table("discovered_items")
    op.drop_table("task_transitions")
    op.drop_table("resource_locks")
    op.drop_index("idx_job_queue_pending", table_name="job_queue")
    op.drop_table("job_queue")
    op.drop_index("idx_instances_task", table_name="instances")
    op.drop_index("idx_instances_status_active", table_name="instances")
    op.drop_table("instances")
    op.drop_table("roles")

    bind = op.get_bind()
    for enum_obj in (
        BUDGET_WINDOW_KIND, INSTANCE_EVENT_TYPE, DISCOVERY_STATUS,
        DISCOVERY_KIND, DISCOVERY_SOURCE, JOB_STATUS, INSTANCE_STATUS,
    ):
        enum_obj.drop(bind, checkfirst=True)


# --------------------------------------------------------------------------- #
# Default role seed                                                           #
# --------------------------------------------------------------------------- #

_DEV_PROMPT = """You are a senior engineer working on a single isolated task.

CONTEXT
- You are in a git worktree separated from main. Your branch is auto-created.
- The task tells you WHAT to build; `acceptance_criteria` defines DONE.
- You may only edit files inside modules listed in `module_hint`.

HARD RULES
1. Do not merge. Commit + push your branch only.
2. Never run `git rebase`, `git reset --hard`, or any `--force` flag.
3. If you need something outside `module_hint`, comment on the task and stop.
4. Every commit message: `<task_ticket>: <imperative verb>`.
5. Before finishing: run `pytest` and report the result.

DELIVERY
- Push the branch.
- Post a final comment on the task: PR link + 3-line summary of changes.
"""

_REVIEWER_PROMPT = """You review a single open PR. You do not push commits.

CONTEXT
- A `dev` agent just opened the PR.
- Read the diff and the task's acceptance_criteria.

OUTPUT
- For each concern, post one comment on the PR with file:line reference.
- End with one of: APPROVE / REQUEST_CHANGES / NEEDS_HUMAN.
- APPROVE only if acceptance_criteria are met and you found no correctness bugs.
- Default to NEEDS_HUMAN when in doubt — humans cost less than a bad merge.
"""

_QA_PROMPT = """You verify a PR by running tests and a quick smoke check.

CONTEXT
- A `reviewer` just approved the PR.
- Branch is checked out in your worktree.

ACTIONS
1. Run the full test suite (pytest). Report failures with file:line.
2. If the change touches user-facing behavior, run the affected CLI command(s)
   end-to-end and capture stdout.
3. Post a final comment: PASS / FAIL with evidence.
"""

_PM_PROMPT = """You refine a discovered_item into a Task ready for development.

INPUT: title + summary + raw_payload from the discovery source.
OUTPUT: write `acceptance_criteria` (numbered, testable) and `module_hint`.

RULES
- Acceptance criteria are checkable: "X returns Y when Z", not "improve X".
- If you cannot infer module_hint from the payload, leave it NULL.
- If the item looks like noise (transient error, duplicate), recommend REJECT.
"""

_PO_PROMPT = """You prioritize the Ready column.

INPUT: list of tasks with priority + age + ticket info.
OUTPUT: a single ordered list of task_ids representing the recommended queue.

RULES
- Urgent > overdue > stale-by-age > everything else.
- Tie-break by ticket priority from Jira.
"""

_DESIGN_PROMPT = """You produce a textual mockup or interaction spec for UI tasks.

OUTPUT: file under docs/design/<task_id>.md with sections:
- User story
- States (empty, loading, error, success)
- Component breakdown (ASCII or markdown table)
- Edge cases worth flagging to the dev agent
"""

_DEVOPS_PROMPT = """You handle CI/CD failures or deploy issues.

INPUT: failing job logs + last deploy info.
ACTIONS
- Identify root cause (config drift, secret rotation, broken dep, etc.).
- If fix is a config change in .github/, ci/, or docker/, make it.
- Otherwise comment on the task with the diagnosis and stop.
"""


def _seed_default_roles() -> None:
    roles_table = sa.table(
        "roles",
        sa.column("id", sa.UUID()),
        sa.column("name", sa.String()),
        sa.column("system_prompt", sa.Text()),
        sa.column("tools_allowed", postgresql.ARRAY(sa.String())),
        sa.column("model", sa.String()),
        sa.column("max_tokens", sa.Integer()),
        sa.column("max_minutes", sa.Integer()),
    )

    op.bulk_insert(
        roles_table,
        [
            {
                "id": uuid.uuid4(),
                "name": "pm",
                "system_prompt": _PM_PROMPT,
                "tools_allowed": ["Read", "Grep", "Glob"],
                "model": "claude-opus-4-7",
                "max_tokens": 200_000,
                "max_minutes": 15,
            },
            {
                "id": uuid.uuid4(),
                "name": "po",
                "system_prompt": _PO_PROMPT,
                "tools_allowed": ["Read"],
                "model": "claude-opus-4-7",
                "max_tokens": 100_000,
                "max_minutes": 10,
            },
            {
                "id": uuid.uuid4(),
                "name": "design",
                "system_prompt": _DESIGN_PROMPT,
                "tools_allowed": ["Read", "Write", "Grep"],
                "model": "claude-sonnet-4-6",
                "max_tokens": 200_000,
                "max_minutes": 20,
            },
            {
                "id": uuid.uuid4(),
                "name": "dev",
                "system_prompt": _DEV_PROMPT,
                "tools_allowed": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
                "model": "claude-sonnet-4-6",
                "max_tokens": 500_000,
                "max_minutes": 30,
            },
            {
                "id": uuid.uuid4(),
                "name": "reviewer",
                "system_prompt": _REVIEWER_PROMPT,
                "tools_allowed": ["Read", "Grep", "Bash"],
                "model": "claude-sonnet-4-6",
                "max_tokens": 200_000,
                "max_minutes": 15,
            },
            {
                "id": uuid.uuid4(),
                "name": "qa",
                "system_prompt": _QA_PROMPT,
                "tools_allowed": ["Read", "Bash", "Edit"],
                "model": "claude-sonnet-4-6",
                "max_tokens": 300_000,
                "max_minutes": 20,
            },
            {
                "id": uuid.uuid4(),
                "name": "devops",
                "system_prompt": _DEVOPS_PROMPT,
                "tools_allowed": ["Read", "Edit", "Bash"],
                "model": "claude-sonnet-4-6",
                "max_tokens": 300_000,
                "max_minutes": 20,
            },
        ],
    )
