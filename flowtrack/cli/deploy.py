import typer

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.models.deployment import Environment
from flowtrack.services.deploy_service import DeployService

app = typer.Typer(help="Record deployments.")


@app.callback(invoke_without_command=True)
def deploy(
    env: str = typer.Option("production", "--env", help="Environment (production|staging|development)"),
) -> None:
    """Record a deployment."""
    try:
        environment = Environment(env)
    except ValueError:
        console.print(f"[red]Invalid environment:[/red] {env}")
        raise typer.Exit(1)

    try:
        with get_db() as db:
            svc = DeployService(db)
            deploy = svc.record_deploy(environment=environment)
            console.print(f"[green]Deploy recorded[/green] ({deploy.id})")
            console.print(f"  Environment: {env}")
            if deploy.commit_sha:
                console.print(f"  Commit: {deploy.commit_sha[:8]}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
