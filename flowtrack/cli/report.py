import typer
from rich.panel import Panel
from rich.table import Table

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.services.report_service import ReportService

app = typer.Typer(help="Generate metrics reports.")


@app.callback(invoke_without_command=True)
def report(
    period: str = typer.Option("week", "--period", help="Period: week|month|sprint"),
) -> None:
    """Generate a SPACE + DORA metrics report."""
    with get_db() as db:
        svc = ReportService(db)
        r = svc.generate(period=period)

    # SPACE metrics table
    space_table = Table(title="SPACE Metrics", show_header=True)
    space_table.add_column("Metric", style="cyan")
    space_table.add_column("Value", style="green", justify="right")

    s = r.space
    space_table.add_row("Total Sessions", str(s.total_sessions))
    space_table.add_row("Sessions/Day", f"{s.sessions_per_day:.1f}")
    space_table.add_row("Flow Time Ratio", f"{s.flow_time_ratio:.1%}")
    space_table.add_row("Blocking Ratio", f"{s.blocking_ratio:.1%}")
    space_table.add_row("Interrupts/Session", f"{s.interrupts_per_session:.2f}")
    space_table.add_row("Review Sessions", str(s.review_sessions))
    space_table.add_row("Avg Review Duration", f"{s.avg_review_duration_min:.0f} min")
    space_table.add_row("Test Sessions", str(s.test_sessions))
    space_table.add_row("Change Failure Rate", f"{s.change_failure_rate:.1%}")

    # DORA metrics table
    dora_table = Table(title="DORA Metrics", show_header=True)
    dora_table.add_column("Metric", style="cyan")
    dora_table.add_column("Value", style="green", justify="right")

    d = r.dora
    dora_table.add_row("Deployment Frequency", f"{d.deployment_frequency:.2f}/day")
    dora_table.add_row("Lead Time", f"{d.lead_time_hours:.1f} hours")
    dora_table.add_row("Change Failure Rate", f"{d.change_failure_rate:.1%}")
    dora_table.add_row("MTTR", f"{d.mttr_hours:.1f} hours")

    period_str = f"{r.period_start:%Y-%m-%d} to {r.period_end:%Y-%m-%d}"
    console.print(Panel(f"[bold]FlowTrack Report[/bold] - {period_str}", expand=False))
    console.print(space_table)
    console.print()
    console.print(dora_table)
