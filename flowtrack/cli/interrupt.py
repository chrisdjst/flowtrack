from typing import Optional

import typer

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.services.event_service import EventService

app = typer.Typer(help="Manage interrupt events.")


@app.command()
def start(
    type: Optional[str] = typer.Option(
        None, "--type", help="Type of interrupt (meeting|slack|other)"
    ),
) -> None:
    """Start an interrupt event on the active session."""
    try:
        with get_db() as db:
            svc = EventService(db)
            event = svc.start_interrupt(interrupt_type=type)
            console.print(f"[yellow]Interrupt started[/yellow] ({event.id})")
            if type:
                console.print(f"  Type: {type}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def end() -> None:
    """End the active interrupt event."""
    try:
        with get_db() as db:
            svc = EventService(db)
            event = svc.end_interrupt()
            duration = (event.ended_at - event.started_at).total_seconds() / 60
            console.print(f"[green]Interrupt ended[/green] ({duration:.0f} min)")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
