import typer

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.services.session_service import SessionService
from flowtrack.services.sync_service import SyncService

app = typer.Typer(help="Sync data to GitHub/Jira.")


@app.callback(invoke_without_command=True)
def sync() -> None:
    """Manually sync the active or last session to GitHub/Jira."""
    try:
        with get_db() as db:
            session_svc = SessionService(db)
            session = session_svc.get_active()
            if not session:
                console.print("[yellow]No active session to sync.[/yellow]")
                raise typer.Exit(1)

            sync_svc = SyncService(db)
            results = sync_svc.sync_session(session)

            synced = False
            for target, ok in results.items():
                if ok:
                    console.print(f"[green]Synced to {target}[/green]")
                    synced = True

            if not synced:
                console.print("[yellow]Nothing to sync (no PR or ticket linked).[/yellow]")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
