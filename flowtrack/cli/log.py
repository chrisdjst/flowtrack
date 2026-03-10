from datetime import datetime, timedelta
from typing import Optional

import typer
from rich.table import Table

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.repositories.deployment_repo import DeploymentRepository
from flowtrack.repositories.incident_repo import IncidentRepository
from flowtrack.repositories.session_repo import SessionRepository

app = typer.Typer(help="View activity history.")

TYPE_COLORS = {
    "development": "green",
    "review": "yellow",
    "testing": "blue",
    "deploy": "magenta",
    "incident": "red",
}

SESSION_TYPES = {"dev": "development", "review": "review", "test": "testing"}


@app.callback(invoke_without_command=True)
def log(
    activity_type: Optional[str] = typer.Option(
        None, "--type", "-t", help="Filter: dev|review|test|deploy|incident",
    ),
    period: str = typer.Option("week", "--period", help="Period: week|month|sprint"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max entries to show"),
) -> None:
    """Show unified activity history."""
    now = datetime.now()
    if period == "month":
        start = now - timedelta(days=30)
    elif period == "sprint":
        start = now - timedelta(days=14)
    else:
        start = now - timedelta(days=7)

    entries: list[tuple[datetime, str, str, str]] = []

    with get_db() as db:
        # Sessions
        if activity_type is None or activity_type in SESSION_TYPES:
            session_repo = SessionRepository(db)
            sessions = session_repo.list_by_period(start, now)
            target_type = SESSION_TYPES.get(activity_type) if activity_type else None

            for s in sessions:
                if target_type and s.type.value != target_type:
                    continue
                duration = ""
                if s.ended_at:
                    mins = (s.ended_at - s.started_at).total_seconds() / 60
                    duration = f" ({mins:.0f}min)"
                details_parts = []
                if s.ticket_id:
                    details_parts.append(f"[{s.ticket_id}]")
                if s.pr_number:
                    details_parts.append(f"PR#{s.pr_number}")
                entries.append((
                    s.started_at,
                    s.type.value,
                    f"{s.status.value}{duration}",
                    " ".join(details_parts),
                ))

        # Deployments
        if activity_type is None or activity_type == "deploy":
            deploy_repo = DeploymentRepository(db)
            deploys = deploy_repo.list_by_period(start, now)
            for d in deploys:
                details_parts = []
                if d.commit_sha:
                    details_parts.append(d.commit_sha[:8])
                if d.ticket_id:
                    details_parts.append(f"[{d.ticket_id}]")
                if d.pr_number:
                    details_parts.append(f"PR#{d.pr_number}")
                entries.append((
                    d.deployed_at,
                    "deploy",
                    d.environment.value,
                    " ".join(details_parts),
                ))

        # Incidents
        if activity_type is None or activity_type == "incident":
            incident_repo = IncidentRepository(db)
            incidents = incident_repo.list_by_period(start, now)
            for i in incidents:
                status = "resolved" if i.resolved_at else "OPEN"
                duration = ""
                if i.resolved_at:
                    mins = (i.resolved_at - i.started_at).total_seconds() / 60
                    duration = f" ({mins:.0f}min)"
                entries.append((
                    i.started_at,
                    "incident",
                    f"{status}{duration}",
                    i.description or "",
                ))

    entries.sort(key=lambda x: x[0], reverse=True)
    entries = entries[:limit]

    if not entries:
        console.print("[dim]No activity found.[/dim]")
        return

    table = Table(title=f"Activity Log ({period})", show_header=True)
    table.add_column("Timestamp", style="dim")
    table.add_column("Type")
    table.add_column("Summary")
    table.add_column("Details", style="cyan")

    for ts, entry_type, summary, details in entries:
        color = TYPE_COLORS.get(entry_type, "white")
        table.add_row(
            f"{ts:%Y-%m-%d %H:%M}",
            f"[{color}]{entry_type}[/{color}]",
            summary,
            details,
        )

    console.print(table)
