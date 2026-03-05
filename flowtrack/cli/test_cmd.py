from typing import Optional

import typer

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.core.settings import settings
from flowtrack.models.session import SessionType
from flowtrack.services.session_service import SessionService
from flowtrack.services.sync_service import SyncService

app = typer.Typer(help="Manage testing sessions.")


@app.command()
def start(
    ticket: Optional[str] = typer.Option(None, "--ticket", help="Jira ticket ID"),
) -> None:
    """Start a testing session."""
    try:
        with get_db() as db:
            svc = SessionService(db)
            session = svc.start(SessionType.TESTING, ticket_id=ticket)
            console.print(f"[green]Test session started[/green] ({session.id})")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def end(
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip auto-sync"),
) -> None:
    """End the active testing session."""
    try:
        with get_db() as db:
            svc = SessionService(db)
            session = svc.end()
            console.print(f"[green]Test session ended[/green] ({session.id})")

            if not no_sync and settings.auto_sync:
                sync = SyncService(db)
                results = sync.sync_session(session)
                for target, ok in results.items():
                    if ok:
                        console.print(f"  Synced to {target}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
