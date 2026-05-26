"""CLI for the discovery inbox (the gateway between external signals and Tasks).

Discovery items live in ``discovered_items`` and are produced by Jira/GitHub/
Sentry sources (or seeded manually). Promotion creates a Task; rejection
marks them for filtering; refine runs the PM agent (real Claude, spends $).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

import typer
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import select

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.discovery.promote import promote_item, reject_item
from flowtrack.models import DiscoveredItem
from flowtrack.models.discovered_item import DiscoveryStatus

app = typer.Typer(help="Manage the discovery inbox (Jira/GitHub/Sentry candidates).")


_STATUS_COLORS = {
    DiscoveryStatus.NEW: "yellow",
    DiscoveryStatus.PROMOTED: "green",
    DiscoveryStatus.REJECTED: "dim",
    DiscoveryStatus.DUPLICATE: "dim",
}


def _resolve_item_id(short_or_full: str) -> uuid.UUID:
    """Accept full UUID or first-8 prefix. Raises FlowTrackError on no/many match."""
    try:
        return uuid.UUID(short_or_full)
    except ValueError:
        pass
    if len(short_or_full) < 4:
        raise FlowTrackError(
            f"Identifier '{short_or_full}' too short; need a UUID prefix of 4+ chars."
        )
    with get_db() as db:
        rows = list(db.scalars(select(DiscoveredItem)))
        matches = [r for r in rows if str(r.id).startswith(short_or_full)]
        if not matches:
            raise FlowTrackError(f"No discovered item matching '{short_or_full}'.")
        if len(matches) > 1:
            ids = ", ".join(str(r.id)[:12] for r in matches[:5])
            raise FlowTrackError(
                f"Ambiguous prefix '{short_or_full}' — matches: {ids}..."
            )
        return matches[0].id


@app.command("list")
def list_items(
    show_all: bool = typer.Option(
        False, "--all", "-a", help="Include promoted/rejected items, not just new ones",
    ),
    source: Optional[str] = typer.Option(None, "--source", "-s", help="Filter by source (sentry|jira|github_issue|...)"),
) -> None:
    """List discovery items (default: only status=new)."""
    # Build the table INSIDE the session block — once the context manager
    # closes, ORM attribute access raises DetachedInstanceError (expire on
    # commit). Rich.Table caches the rendered strings so printing later is OK.
    with get_db() as db:
        stmt = select(DiscoveredItem)
        if not show_all:
            stmt = stmt.where(DiscoveredItem.status == DiscoveryStatus.NEW)
        if source:
            stmt = stmt.where(DiscoveredItem.source == source)
        stmt = stmt.order_by(DiscoveredItem.created_at.desc()).limit(200)
        items = list(db.scalars(stmt))

        if not items:
            console.print("[dim]No discovered items match.[/dim]")
            return

        table = Table(title="Discovery inbox", show_header=True)
        table.add_column("ID", style="dim", max_width=8)
        table.add_column("Source", style="cyan")
        table.add_column("Ref")
        table.add_column("Kind")
        table.add_column("Title", style="bold")
        table.add_column("Score", justify="right")
        table.add_column("Status")
        for i in items:
            sc = _STATUS_COLORS.get(i.status, "white")
            table.add_row(
                str(i.id)[:8],
                i.source.value,
                i.source_ref or "",
                i.kind.value,
                i.title[:60],
                str(i.signal_score) if i.signal_score is not None else "",
                f"[{sc}]{i.status.value}[/{sc}]",
            )
    console.print(table)


@app.command()
def show(item_id: str = typer.Argument(help="Discovery item ID (prefix or full UUID)")) -> None:
    """Show full details of a discovered item, including the raw source payload."""
    try:
        full_id = _resolve_item_id(item_id)
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    with get_db() as db:
        item = db.get(DiscoveredItem, full_id)
        if item is None:
            console.print(f"[red]Item {full_id} not found.[/red]")
            raise typer.Exit(1)

        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column("Key", style="bold cyan")
        t.add_column("Value")
        t.add_row("ID", str(item.id)[:8])
        t.add_row("Source", f"{item.source.value} / {item.source_ref or '-'}")
        t.add_row("Kind", item.kind.value)
        t.add_row("Title", item.title)
        if item.summary:
            t.add_row("Summary", item.summary[:400])
        if item.signal_score is not None:
            t.add_row("Signal score", str(item.signal_score))
        sc = _STATUS_COLORS.get(item.status, "white")
        t.add_row("Status", f"[{sc}]{item.status.value}[/{sc}]")
        if item.promoted_task_id:
            t.add_row("Promoted to task", str(item.promoted_task_id)[:8])
        t.add_row("Created", f"{item.created_at:%Y-%m-%d %H:%M}")
        console.print(Panel(t, title="Discovery item", expand=False))


@app.command()
def promote(item_id: str = typer.Argument(help="Discovery item ID (prefix or full UUID)")) -> None:
    """Turn a discovered item into a Task (status=todo, no criteria yet).

    The new Task lands in the ``Refinement`` kanban column. Fill in criteria
    via ``flowtrack task update --criteria '...'`` or run the PM agent with
    ``flowtrack discovery refine`` BEFORE promoting (refine just previews
    though — it doesn't create the task).
    """
    try:
        full_id = _resolve_item_id(item_id)
        with get_db() as db:
            item = db.get(DiscoveredItem, full_id)
            if item is None:
                console.print(f"[red]Item {full_id} not found.[/red]")
                raise typer.Exit(1)
            task = promote_item(db, item)
        console.print(
            f"[green]Promoted[/green] {str(full_id)[:8]} -> task {str(task.id)[:8]} "
            f"([yellow]{task.status.value}[/yellow])"
        )
        console.print(
            f"[dim]Next: flowtrack task update {str(task.id)[:8]} "
            "--criteria '...' --module-hint ... "
            f"&& flowtrack task assign {str(task.id)[:8]} dev[/dim]"
        )
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def reject(
    item_id: str = typer.Argument(help="Discovery item ID (prefix or full UUID)"),
    reason: Optional[str] = typer.Option(None, "--reason", "-r"),
) -> None:
    """Mark a discovered item as rejected (won't show in default lists)."""
    try:
        full_id = _resolve_item_id(item_id)
        with get_db() as db:
            item = db.get(DiscoveredItem, full_id)
            if item is None:
                console.print(f"[red]Item {full_id} not found.[/red]")
                raise typer.Exit(1)
            reject_item(db, item, reason=reason)
        console.print(f"[green]Rejected[/green] {str(full_id)[:8]}")
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def refine(
    item_id: str = typer.Argument(help="Discovery item ID (prefix or full UUID)"),
    apply: bool = typer.Option(
        False, "--apply",
        help="Apply the PM recommendation: promote (with criteria filled) or reject",
    ),
) -> None:
    """Run the PM agent (real Claude, spends ~$0.04) to refine an item.

    Without --apply: prints the recommendation and acceptance_criteria for
    review. With --apply: also creates the Task (if recommendation=promote)
    or rejects the item, in the same call.
    """
    # Lazy import — pulls in the claude subprocess agent.
    from flowtrack.agents.pm import refine_async

    try:
        full_id = _resolve_item_id(item_id)
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    with get_db() as db:
        item = db.get(DiscoveredItem, full_id)
        if item is None:
            console.print(f"[red]Item {full_id} not found.[/red]")
            raise typer.Exit(1)
        snapshot = {
            "title": item.title,
            "summary": item.summary,
            "kind": item.kind.value,
            "source": item.source.value,
            "source_ref": item.source_ref or "",
            "raw_payload": item.raw_payload,
        }
        item_status = item.status

    console.print(f"[dim]Calling PM agent on {str(full_id)[:8]} (spends real tokens)...[/dim]")
    try:
        result = asyncio.run(refine_async(**snapshot))
    except Exception as e:
        console.print(f"[red]PM agent failed:[/red] {e}")
        raise typer.Exit(2)

    rec_color = "green" if result.recommendation == "promote" else "yellow"
    console.print(f"  Recommendation: [{rec_color}]{result.recommendation}[/{rec_color}]")
    console.print(f"  Module hint:    {result.module_hint or '(none)'}")
    console.print(f"  Cost:           ${result.cost_usd}")
    console.print()
    console.print("[bold]Acceptance criteria:[/bold]")
    console.print(result.acceptance_criteria)

    if not apply:
        console.print()
        console.print("[dim]Run with --apply to materialize (promote or reject).[/dim]")
        return

    if item_status != DiscoveryStatus.NEW:
        console.print(f"[yellow]Item already {item_status.value}, skipping apply.[/yellow]")
        return

    with get_db() as db:
        item = db.get(DiscoveredItem, full_id)
        try:
            if result.recommendation == "promote":
                task = promote_item(
                    db, item,
                    acceptance_criteria=result.acceptance_criteria,
                    module_hint=result.module_hint,
                )
                console.print(
                    f"[green]Applied: promoted -> task {str(task.id)[:8]} "
                    f"(criteria + module_hint filled)[/green]"
                )
            else:
                reject_item(db, item, reason="pm: refine recommended reject")
                console.print(f"[yellow]Applied: rejected[/yellow]")
        except ValueError as e:
            console.print(f"[red]Apply failed:[/red] {e}")
            raise typer.Exit(1)
