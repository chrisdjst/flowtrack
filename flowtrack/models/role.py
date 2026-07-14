import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from flowtrack.models.base import Base, TimestampMixin


class Role(TimestampMixin, Base):
    """Catalog of agent roles (pm, po, dev, reviewer, qa, ...).

    System prompts and limits live in DB so they can be tuned without redeploy.
    """

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    system_prompt: Mapped[str] = mapped_column(Text)
    tools_allowed: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)))
    model: Mapped[str] = mapped_column(String(50), default="claude-sonnet-4-6")
    max_tokens: Mapped[int] = mapped_column(default=500_000)
    max_turns: Mapped[int | None] = mapped_column(default=None)
    max_minutes: Mapped[int] = mapped_column(default=60)
    # Pipeline: when this role completes successfully, enqueue next_role_name
    # for the same task and set tasks.status = task_status_on_success.
    # NULL on either field = pipeline ends here.
    next_role_name: Mapped[str | None] = mapped_column(String(50))
    task_status_on_success: Mapped[str | None] = mapped_column(String(50))
    # Where the task goes when this role fails. NULL = blocked (legacy default).
    # e.g. reviewer REQUEST_CHANGES routes back to in_progress for a dev retry.
    task_status_on_failure: Mapped[str | None] = mapped_column(String(50))
    # How many failure-routings a task tolerates before the orchestrator forces
    # blocked (manual_intervention) regardless of task_status_on_failure.
    # NULL = no cap. Compared against tasks.bounce_count.
    max_bounce_count: Mapped[int | None] = mapped_column(default=None)
    # ----- Specialization dispatch (KAN-42) -----
    # NULL = this row is a base role. Set = this row is a variant of that
    # base (e.g. name='dev-frontend', base_role_name='dev'). The dispatcher
    # resolves a requested base name to the most specific eligible variant;
    # pipeline semantics (verdicts, session type, chain hooks) always key on
    # the base name.
    base_role_name: Mapped[str | None] = mapped_column(String(50))
    # Free label describing what the variant is specialized in ('frontend').
    specialization: Mapped[str | None] = mapped_column(String(100))
    # fnmatch globs tested against tasks.module_hint. Non-empty + no match
    # = variant ineligible for that task; a match adds specificity.
    match_patterns: Mapped[list[str] | None] = mapped_column(ARRAY(String(255)))
    # Variant scoped to a project: eligible only for tasks of that project
    # (match adds the most specificity). NULL = any project.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id")
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
