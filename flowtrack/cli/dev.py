from typing import Optional

import typer

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.core.settings import settings
from flowtrack.models.session import SessionType
from flowtrack.services.session_service import SessionService
from flowtrack.services.sync_service import SyncService

app = typer.Typer(help="Manage development sessions.")


@app.command()
def start(
    ticket: Optional[str] = typer.Option(None, "--ticket", help="Jira ticket ID"),
    pr: Optional[int] = typer.Option(None, "--pr", help="GitHub PR number"),
) -> None:
    """Start a development session."""
    try:
        with get_db() as db:
            svc = SessionService(db)
            session = svc.start(SessionType.DEVELOPMENT, ticket_id=ticket, pr_number=pr)
            console.print(f"[green]Dev session started[/green] ({session.id})")
            if ticket:
                console.print(f"  Ticket: {ticket}")
            if pr:
                console.print(f"  PR: #{pr}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def end(
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip auto-sync"),
) -> None:
    """End the active development session."""
    try:
        with get_db() as db:
            svc = SessionService(db)
            session = svc.end()
            console.print(f"[green]Dev session ended[/green] ({session.id})")

            if not no_sync and settings.auto_sync:
                sync = SyncService(db)
                results = sync.sync_session(session)
                for target, ok in results.items():
                    if ok:
                        console.print(f"  Synced to {target}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def pause() -> None:
    """Pause the active development session."""
    try:
        with get_db() as db:
            svc = SessionService(db)
            svc.pause()
            console.print("[yellow]Dev session paused[/yellow]")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def resume() -> None:
    """Resume a paused development session."""
    try:
        with get_db() as db:
            svc = SessionService(db)
            svc.resume()
            console.print("[green]Dev session resumed[/green]")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
