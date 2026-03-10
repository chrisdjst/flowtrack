import json

import typer
from rich.panel import Panel
from rich.table import Table

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.services.report_service import Report, ReportService

app = typer.Typer(help="Generate metrics reports.")


@app.callback(invoke_without_command=True)
def report(
    period: str = typer.Option("week", "--period", help="Period: week|month|sprint"),
    fmt: str = typer.Option("table", "--format", "-f", help="Output format: table|json|csv"),
) -> None:
    """Generate a SPACE + DORA metrics report."""
    with get_db() as db:
        svc = ReportService(db)
        r = svc.generate(period=period)

    if fmt == "json":
        _output_json(r)
        return

    if fmt == "csv":
        _output_csv(r)
        return

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


def _output_json(r: Report) -> None:
    data = {
        "period": {
            "start": r.period_start.isoformat(),
            "end": r.period_end.isoformat(),
        },
        "space": {
            "total_sessions": r.space.total_sessions,
            "sessions_per_day": round(r.space.sessions_per_day, 2),
            "flow_time_ratio": round(r.space.flow_time_ratio, 4),
            "blocking_ratio": round(r.space.blocking_ratio, 4),
            "interrupts_per_session": round(r.space.interrupts_per_session, 2),
            "review_sessions": r.space.review_sessions,
            "avg_review_duration_min": round(r.space.avg_review_duration_min, 1),
            "test_sessions": r.space.test_sessions,
            "change_failure_rate": round(r.space.change_failure_rate, 4),
        },
        "dora": {
            "deployment_frequency": round(r.dora.deployment_frequency, 2),
            "lead_time_hours": round(r.dora.lead_time_hours, 1),
            "change_failure_rate": round(r.dora.change_failure_rate, 4),
            "mttr_hours": round(r.dora.mttr_hours, 1),
        },
    }
    typer.echo(json.dumps(data, indent=2))


def _output_csv(r: Report) -> None:
    lines = ["category,metric,value"]
    s = r.space
    lines.append(f"space,total_sessions,{s.total_sessions}")
    lines.append(f"space,sessions_per_day,{s.sessions_per_day:.2f}")
    lines.append(f"space,flow_time_ratio,{s.flow_time_ratio:.4f}")
    lines.append(f"space,blocking_ratio,{s.blocking_ratio:.4f}")
    lines.append(f"space,interrupts_per_session,{s.interrupts_per_session:.2f}")
    lines.append(f"space,review_sessions,{s.review_sessions}")
    lines.append(f"space,avg_review_duration_min,{s.avg_review_duration_min:.1f}")
    lines.append(f"space,test_sessions,{s.test_sessions}")
    lines.append(f"space,change_failure_rate,{s.change_failure_rate:.4f}")
    d = r.dora
    lines.append(f"dora,deployment_frequency,{d.deployment_frequency:.2f}")
    lines.append(f"dora,lead_time_hours,{d.lead_time_hours:.1f}")
    lines.append(f"dora,change_failure_rate,{d.change_failure_rate:.4f}")
    lines.append(f"dora,mttr_hours,{d.mttr_hours:.1f}")
    typer.echo("\n".join(lines))
