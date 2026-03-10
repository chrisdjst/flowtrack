from typing import Optional

import typer
from rich.table import Table

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.models.deployment import Environment
from flowtrack.services.deploy_service import DeployService

app = typer.Typer(help="Record deployments.")

ENV_COLORS = {
    Environment.PRODUCTION: "red",
    Environment.STAGING: "yellow",
    Environment.DEVELOPMENT: "green",
}


@app.callback(invoke_without_command=True)
def deploy(
    ctx: typer.Context,
    env: str = typer.Option("production", "--env", help="Environment (production|staging|development)"),
    pr: Optional[int] = typer.Option(None, "--pr", help="GitHub PR number"),
    ticket: Optional[str] = typer.Option(None, "--ticket", help="Jira ticket ID"),
) -> None:
    """Record a deployment."""
    if ctx.invoked_subcommand is not None:
        return

    try:
        environment = Environment(env)
    except ValueError:
        console.print(f"[red]Invalid environment:[/red] {env}")
        raise typer.Exit(1)

    try:
        with get_db() as db:
            svc = DeployService(db)
            d = svc.record_deploy(environment=environment, pr_number=pr, ticket_id=ticket)
            console.print(f"[green]Deploy recorded[/green] ({d.id})")
            console.print(f"  Environment: {env}")
            if d.commit_sha:
                console.print(f"  Commit: {d.commit_sha[:8]}")
            if d.pr_number:
                console.print(f"  PR: #{d.pr_number}")
            if d.ticket_id:
                console.print(f"  Ticket: {d.ticket_id}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("list")
def list_deploys(
    env: Optional[str] = typer.Option(None, "--env", help="Filter by environment"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of deploys to show"),
) -> None:
    """List recent deployments."""
    environment = None
    if env:
        try:
            environment = Environment(env)
        except ValueError:
            console.print(f"[red]Invalid environment:[/red] {env}")
            raise typer.Exit(1)

    with get_db() as db:
        svc = DeployService(db)
        deploys = svc.list_deploys(environment=environment, limit=limit)

        if not deploys:
            console.print("[dim]No deployments found.[/dim]")
            return

        table = Table(title="Deployments", show_header=True)
        table.add_column("ID", style="dim", max_width=8)
        table.add_column("Environment")
        table.add_column("Deployed At")
        table.add_column("Commit", max_width=8)
        table.add_column("PR")
        table.add_column("Ticket", style="cyan")

        for d in deploys:
            color = ENV_COLORS.get(d.environment, "white")
            table.add_row(
                str(d.id)[:8],
                f"[{color}]{d.environment.value}[/{color}]",
                f"{d.deployed_at:%Y-%m-%d %H:%M}",
                d.commit_sha[:8] if d.commit_sha else "",
                f"#{d.pr_number}" if d.pr_number else "",
                d.ticket_id or "",
            )

    console.print(table)
