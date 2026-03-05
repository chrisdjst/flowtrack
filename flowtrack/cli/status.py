from datetime import datetime

import typer
from rich.panel import Panel
from rich.table import Table

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.models.event import EventType
from flowtrack.repositories.event_repo import EventRepository
from flowtrack.services.session_service import SessionService

app = typer.Typer(help="Show current session status.")


@app.callback(invoke_without_command=True)
def status() -> None:
    """Show the status of the active session."""
    with get_db() as db:
        svc = SessionService(db)
        session = svc.get_active()

        if not session:
            console.print("[dim]No active session.[/dim]")
            return

        now = datetime.now()
        elapsed = (now - session.started_at).total_seconds() / 60

        event_repo = EventRepository(db)
        events = event_repo.list_by_session(session.id)

        blocks = [e for e in events if e.event_type == EventType.BLOCK_START]
        interrupts = [e for e in events if e.event_type == EventType.INTERRUPT_START]
        active_block = next(
            (e for e in blocks if e.ended_at is None), None
        )
        active_interrupt = next(
            (e for e in interrupts if e.ended_at is None), None
        )

        block_mins = sum(
            ((e.ended_at or now) - e.started_at).total_seconds() / 60 for e in blocks
        )
        interrupt_mins = sum(
            ((e.ended_at or now) - e.started_at).total_seconds() / 60 for e in interrupts
        )

        status_color = {
            "active": "green",
            "paused": "yellow",
            "ended": "dim",
        }
        color = status_color.get(session.status.value, "white")

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="bold cyan")
        table.add_column("Value")

        table.add_row("Type", session.type.value.title())
        table.add_row("Status", f"[{color}]{session.status.value.upper()}[/{color}]")
        table.add_row("Elapsed", f"{elapsed:.0f} min")
        if session.ticket_id:
            table.add_row("Ticket", session.ticket_id)
        if session.pr_number:
            table.add_row("PR", f"#{session.pr_number}")
        table.add_row("Blocks", f"{len(blocks)} ({block_mins:.0f} min)")
        table.add_row("Interrupts", f"{len(interrupts)} ({interrupt_mins:.0f} min)")

        if active_block:
            reason = (active_block.metadata_json or {}).get("reason", "")
            table.add_row("Active Block", f"[red]{reason or 'Yes'}[/red]")
        if active_interrupt:
            itype = (active_interrupt.metadata_json or {}).get("type", "")
            table.add_row("Active Interrupt", f"[yellow]{itype or 'Yes'}[/yellow]")

        console.print(Panel(table, title="FlowTrack Status", expand=False))
