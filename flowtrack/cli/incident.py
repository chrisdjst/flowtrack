from typing import Optional

import typer

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.services.incident_service import IncidentService

app = typer.Typer(help="Manage incidents.")


@app.command()
def start(
    description: Optional[str] = typer.Option(None, "--description", help="Incident description"),
) -> None:
    """Record an incident start."""
    try:
        with get_db() as db:
            svc = IncidentService(db)
            incident = svc.start(description=description)
            console.print(f"[red]Incident started[/red] ({incident.id})")
            if description:
                console.print(f"  Description: {description}")
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
