import typer
from rich.table import Table

from flowtrack.core.console import console
from flowtrack.core.credentials import SENSITIVE_KEYS
from flowtrack.core.crypto import MASTER_KEY_PATH, CryptoService
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
    ("jira_project_key", "Jira project key for auto-creating issues"),
]


@app.callback(invoke_without_command=True)
def config(
    ctx: typer.Context,
    show: bool = typer.Option(False, "--show", help="Show current configuration"),
) -> None:
    """Configure FlowTrack credentials interactively."""
    if ctx.invoked_subcommand is not None:
        return

    with get_db() as db:
        svc = ConfigService(db)

        if show:
            _show_config(svc)
            return

        console.print("[bold]FlowTrack Configuration[/bold]\n")
        console.print("Press Enter to keep current value.")
        console.print(f"Sensitive keys are encrypted with AES (key: {MASTER_KEY_PATH})\n")

        for key, description in CONFIG_KEYS:
            current = svc.get(key) or ""
            display = _mask(current) if key in SENSITIVE_KEYS else current
            prompt = f"{description}"
            if display:
                prompt += f" [{display}]"

            value = typer.prompt(prompt, default="", show_default=False)
            if value:
                svc.set(key, value)
                encrypted_tag = " (encrypted)" if key in SENSITIVE_KEYS else ""
                console.print(f"  [green]Updated {key}[/green]{encrypted_tag}")

        console.print("\n[green]Configuration saved.[/green]")


@app.command("encrypt")
def encrypt_secrets() -> None:
    """Encrypt any plaintext sensitive values stored in the database."""
    with get_db() as db:
        svc = ConfigService(db)
        count = svc.encrypt_existing()

    if count:
        console.print(f"[green]Encrypted {count} sensitive value(s).[/green]")
    else:
        console.print("[dim]No plaintext sensitive values found.[/dim]")


@app.command("rotate-key")
def rotate_key() -> None:
    """Generate a new master key and re-encrypt all secrets."""
    with get_db() as db:
        svc = ConfigService(db)
        raw_configs = svc.get_all_raw()
        encrypted_count = sum(1 for c in raw_configs if c.encrypted)

        if encrypted_count == 0:
            console.print("[dim]No encrypted values to rotate.[/dim]")
            return

        console.print(f"This will re-encrypt {encrypted_count} value(s) with a new master key.")
        console.print(f"Current key: {MASTER_KEY_PATH}")
        typer.confirm("Proceed?", abort=True)

        backup_path = MASTER_KEY_PATH.with_suffix(".key.bak")
        MASTER_KEY_PATH.rename(backup_path)
        console.print(f"  Old key backed up to: {backup_path}")

        new_crypto = CryptoService()
        old_crypto = CryptoService(key_path=backup_path)

        svc.crypto = old_crypto
        count = svc.rotate_key(new_crypto)

    console.print(f"[green]Rotated {count} value(s) to new key.[/green]")
    console.print(f"  New key: {MASTER_KEY_PATH}")
    console.print(f"  Backup: {backup_path} (delete after verifying)")


def _show_config(svc: ConfigService) -> None:
    decrypted = svc.get_all()
    raw_configs = {c.key: c for c in svc.get_all_raw()}

    table = Table(title="FlowTrack Configuration")
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="green")
    table.add_column("Encrypted", justify="center")

    for key, _ in CONFIG_KEYS:
        value = decrypted.get(key, "")
        display = _mask(value) if key in SENSITIVE_KEYS else value
        raw = raw_configs.get(key)
        encrypted_icon = "[green]yes[/green]" if raw and raw.encrypted else "[dim]no[/dim]"

        table.add_row(
            key,
            display or "[dim]not set[/dim]",
            encrypted_icon if value else "",
        )

    console.print(table)
    console.print(f"\n[dim]Master key: {MASTER_KEY_PATH}[/dim]")


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return value[:4] + "****"
