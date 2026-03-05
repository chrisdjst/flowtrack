import typer
from rich.table import Table

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.services.config_service import ConfigService

app = typer.Typer(help="Manage FlowTrack configuration.")

CONFIG_KEYS = [
    ("database_url", "PostgreSQL connection URL"),
    ("github_token", "GitHub personal access token"),
    ("github_owner", "GitHub repository owner"),
    ("github_repo", "GitHub repository name"),
    ("jira_base_url", "Jira base URL (e.g. https://company.atlassian.net)"),
    ("jira_email", "Jira account email"),
    ("jira_token", "Jira API token"),
]


@app.callback(invoke_without_command=True)
def config(
    show: bool = typer.Option(False, "--show", help="Show current configuration"),
) -> None:
    """Configure FlowTrack credentials interactively."""
    with get_db() as db:
        svc = ConfigService(db)

        if show:
            _show_config(svc)
            return

        console.print("[bold]FlowTrack Configuration[/bold]\n")
        console.print("Press Enter to keep current value.\n")

        for key, description in CONFIG_KEYS:
            current = svc.get(key) or ""
            display = _mask(current) if "token" in key else current
            prompt = f"{description}"
            if display:
                prompt += f" [{display}]"

            value = typer.prompt(prompt, default="", show_default=False)
            if value:
                svc.set(key, value)
                console.print(f"  [green]Updated {key}[/green]")

        console.print("\n[green]Configuration saved.[/green]")


def _show_config(svc: ConfigService) -> None:
    configs = svc.get_all()
    table = Table(title="FlowTrack Configuration")
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="green")

    for key, _ in CONFIG_KEYS:
        value = configs.get(key, "")
        display = _mask(value) if "token" in key else value
        table.add_row(key, display or "[dim]not set[/dim]")

    console.print(table)


def _mask(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return value[:4] + "****"
