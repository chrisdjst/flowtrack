"""CLI for the PO agent: preview the ready-queue ordering and admit tasks."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from flowtrack.core.database import get_db

app = typer.Typer(help="PO agent: rank and admit ready tasks")
console = Console()


@app.command()
def rank() -> None:
    """Show the ready queue in PO priority order with the score breakdown."""
    from flowtrack.agents.po import rank_ready_tasks

    with get_db() as db:
        ranked = rank_ready_tasks(db)

        table = Table(title="PO ready queue (highest first)")
        table.add_column("#", justify="right")
        table.add_column("ID")
        table.add_column("Title", max_width=44)
        table.add_column("Score", justify="right")
        table.add_column("Entry")
        table.add_column("Factors")

        for idx, r in enumerate(ranked, start=1):
            factors = " ".join(f"{k}={v}" for k, v in r.factors.items() if v)
            table.add_row(
                str(idx), str(r.task_id)[:8], r.title, str(r.score),
                r.entry_role, factors or "-",
            )

    if not ranked:
        console.print("[dim]No ready tasks (todo + acceptance criteria, not already queued).[/dim]")
        return
    console.print(table)


@app.command()
def admit(
    limit: int = typer.Option(3, "--limit", "-n", help="Max tasks to admit"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be admitted, change nothing"),
) -> None:
    """Enqueue dev jobs for the top-ranked ready tasks (in rank order)."""
    from flowtrack.agents.po import admit_ready_tasks, rank_ready_tasks

    with get_db() as db:
        if dry_run:
            preview = rank_ready_tasks(db)[:limit]
            for idx, r in enumerate(preview, start=1):
                console.print(f"  {idx}. {str(r.task_id)[:8]} score={r.score} {r.title[:60]}")
            if not preview:
                console.print("[dim]Nothing to admit.[/dim]")
            return

        admitted = admit_ready_tasks(db, limit=limit)

    if not admitted:
        console.print("[dim]Nothing to admit.[/dim]")
        return
    for idx, r in enumerate(admitted, start=1):
        console.print(
            f"[green]Admitted[/green] {idx}. {str(r.task_id)[:8]} "
            f"score={r.score} {r.title[:60]}"
        )
