from typing import Optional

import typer

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.services.event_service import EventService

app = typer.Typer(help="Manage block events.")


@app.command()
def start(
    reason: Optional[str] = typer.Option(None, "--reason", help="Reason for the block"),
) -> None:
    """Start a block event on the active session."""
    try:
        with get_db() as db:
            svc = EventService(db)
            event = svc.start_block(reason=reason)
            console.print(f"[red]Block started[/red] ({event.id})")
            if reason:
                console.print(f"  Reason: {reason}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def end() -> None:
    """End the active block event."""
    try:
        with get_db() as db:
            svc = EventService(db)
            event = svc.end_block()
            duration = (event.ended_at - event.started_at).total_seconds() / 60
            console.print(f"[green]Block ended[/green] ({duration:.0f} min)")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
