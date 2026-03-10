from typing import Optional

import typer
from rich.table import Table

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.services.incident_service import IncidentService

app = typer.Typer(help="Manage incidents.")


@app.command()
def start(
    description: Optional[str] = typer.Option(None, "--description", help="Incident description"),
    severity: Optional[str] = typer.Option(None, "--severity", help="Severity: low|medium|high|critical"),
) -> None:
    """Record an incident start."""
    try:
        with get_db() as db:
            svc = IncidentService(db)
            incident = svc.start(description=description, severity=severity)
            console.print(f"[red]Incident started[/red] ({incident.id})")
            if description:
                console.print(f"  Description: {description}")
            if severity:
                console.print(f"  Severity: {severity}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def end() -> None:
    """Resolve the active incident."""
    try:
        with get_db() as db:
            svc = IncidentService(db)
            incident = svc.end()
            duration = (incident.resolved_at - incident.started_at).total_seconds() / 60
            console.print(f"[green]Incident resolved[/green] ({duration:.0f} min)")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("list")
def list_incidents(
    open_only: bool = typer.Option(False, "--open", help="Show only unresolved incidents"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of incidents to show"),
) -> None:
    """List incidents."""
    with get_db() as db:
        svc = IncidentService(db)
        incidents = svc.list_incidents(open_only=open_only, limit=limit)

        if not incidents:
            console.print("[dim]No incidents found.[/dim]")
            return

        table = Table(title="Incidents", show_header=True)
        table.add_column("ID", style="dim", max_width=8)
        table.add_column("Description")
        table.add_column("Severity")
        table.add_column("Started")
        table.add_column("Resolved")
        table.add_column("Duration")

        for i in incidents:
            duration = ""
            resolved = "[yellow]OPEN[/yellow]"
            if i.resolved_at:
                mins = (i.resolved_at - i.started_at).total_seconds() / 60
                duration = f"{mins:.0f} min"
                resolved = f"{i.resolved_at:%Y-%m-%d %H:%M}"

            table.add_row(
                str(i.id)[:8],
                i.description or "",
                i.severity or "",
                f"{i.started_at:%Y-%m-%d %H:%M}",
                resolved,
                duration,
            )

    console.print(table)
