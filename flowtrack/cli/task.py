import uuid
from typing import Optional

import typer
from rich.table import Table

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.models.task import TaskPriority, TaskStatus
from flowtrack.services.task_service import NoActiveTaskError, TaskService

app = typer.Typer(help="Manage tasks.")

STATUS_CHOICES = [s.value for s in TaskStatus]
PRIORITY_CHOICES = [p.value for p in TaskPriority]

STATUS_COLORS = {
    TaskStatus.TODO: "dim",
    TaskStatus.IN_PROGRESS: "cyan",
    TaskStatus.BLOCKED: "red",
    TaskStatus.IN_REVIEW: "yellow",
    TaskStatus.DONE: "green",
}

PRIORITY_COLORS = {
    TaskPriority.LOW: "dim",
    TaskPriority.MEDIUM: "white",
    TaskPriority.HIGH: "yellow",
    TaskPriority.URGENT: "red bold",
}


@app.command()
def add(
    title: Optional[str] = typer.Argument(None, help="Task title"),
    description: Optional[str] = typer.Option(None, "--desc", "-d", help="Task description"),
    status: str = typer.Option("todo", "--status", "-s", help="Status: todo|in_progress|blocked|in_review|done"),
    priority: str = typer.Option("medium", "--priority", "-p", help="Priority: low|medium|high|urgent"),
    ticket: Optional[str] = typer.Option(None, "--ticket", "-t", help="Jira ticket ID (skips Jira creation)"),
    no_jira: bool = typer.Option(False, "--no-jira", help="Skip Jira issue creation"),
) -> None:
    """Create a new task. Creates a Jira issue if JIRA_PROJECT_KEY is configured."""
    interactive = title is None
    if interactive:
        title = typer.prompt("Title")
        desc_input = typer.prompt("Description (optional)", default="", show_default=False)
        description = desc_input if desc_input else None
        status = typer.prompt(
            f"Status ({', '.join(STATUS_CHOICES)})", default="todo", show_default=True,
        )
        priority = typer.prompt(
            f"Priority ({', '.join(PRIORITY_CHOICES)})", default="medium", show_default=True,
        )

    try:
        task_status = TaskStatus(status)
    except ValueError:
        console.print(f"[red]Invalid status:[/red] {status}. Use: {', '.join(STATUS_CHOICES)}")
        raise typer.Exit(1)

    try:
        task_priority = TaskPriority(priority)
    except ValueError:
        console.print(f"[red]Invalid priority:[/red] {priority}. Use: {', '.join(PRIORITY_CHOICES)}")
        raise typer.Exit(1)

    try:
        with get_db() as db:
            svc = TaskService(db)
            task, jira_key = svc.create(
                title=title,
                description=description,
                status=task_status,
                priority=task_priority,
                ticket_id=ticket,
                sync_jira=not no_jira,
            )
            console.print(f"[green]Task created[/green] ({task.id})")
            console.print(f"  Title: {task.title}")
            if task.description:
                console.print(f"  Description: {task.description}")
            console.print(f"  Status: {task.status.value}")
            console.print(f"  Priority: {task.priority.value}")
            if task.ticket_id:
                console.print(f"  Ticket: {task.ticket_id}")
            if jira_key:
                console.print(f"  [cyan]Jira issue created: {jira_key}[/cyan]")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("list")
def list_tasks(
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status"),
) -> None:
    """List all tasks."""
    task_status = None
    if status:
        try:
            task_status = TaskStatus(status)
        except ValueError:
            console.print(f"[red]Invalid status:[/red] {status}. Use: {', '.join(STATUS_CHOICES)}")
            raise typer.Exit(1)

    with get_db() as db:
        svc = TaskService(db)
        tasks = svc.list(status=task_status)

        if not tasks:
            console.print("[dim]No tasks found.[/dim]")
            return

        table = Table(title="Tasks", show_header=True)
        table.add_column("ID", style="dim", max_width=8)
        table.add_column("Title", style="bold")
        table.add_column("Status")
        table.add_column("Priority")
        table.add_column("Ticket", style="cyan")

        for task in tasks:
            s_color = STATUS_COLORS.get(task.status, "white")
            p_color = PRIORITY_COLORS.get(task.priority, "white")
            table.add_row(
                str(task.id)[:8],
                task.title,
                f"[{s_color}]{task.status.value}[/{s_color}]",
                f"[{p_color}]{task.priority.value}[/{p_color}]",
                task.ticket_id or "",
            )

    console.print(table)


@app.command()
def update(
    task_id: str = typer.Argument(help="Task ID (first 8 chars or full UUID)"),
    status: str = typer.Option(..., "--status", "-s", help="New status: todo|in_progress|blocked|in_review|done"),
) -> None:
    """Update a task's status."""
    try:
        task_status = TaskStatus(status)
    except ValueError:
        console.print(f"[red]Invalid status:[/red] {status}. Use: {', '.join(STATUS_CHOICES)}")
        raise typer.Exit(1)

    try:
        full_id = _resolve_task_id(task_id)
        with get_db() as db:
            svc = TaskService(db)
            task = svc.update_status(full_id, task_status)
            s_color = STATUS_COLORS.get(task.status, "white")
            console.print(f"[green]Task updated[/green] [{s_color}]{task.status.value}[/{s_color}] — {task.title}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def comment(
    body: Optional[str] = typer.Argument(None, help="Comment text"),
    task_id: Optional[str] = typer.Option(None, "--task", "-t", help="Task ID (defaults to current in_progress task)"),
    no_jira: bool = typer.Option(False, "--no-jira", help="Skip Jira sync"),
) -> None:
    """Add a comment to the current in-progress task (or a specific task)."""
    if not body:
        body = typer.prompt("Comment")

    try:
        with get_db() as db:
            svc = TaskService(db)

            if task_id:
                full_id = _resolve_task_id(task_id)
            else:
                active = svc.get_active()
                if not active:
                    raise NoActiveTaskError()
                full_id = active.id

            task_comment, synced = svc.add_comment(full_id, body, sync_jira=not no_jira)

            task = svc._get_or_raise(full_id)
            console.print(f"[green]Comment added[/green] to {task.title}")
            if synced:
                console.print(f"  [cyan]Synced to Jira ({task.ticket_id})[/cyan]")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def comments(
    task_id: Optional[str] = typer.Argument(None, help="Task ID (defaults to current in_progress task)"),
) -> None:
    """Show comments for a task."""
    try:
        with get_db() as db:
            svc = TaskService(db)

            if task_id:
                full_id = _resolve_task_id(task_id)
            else:
                active = svc.get_active()
                if not active:
                    raise NoActiveTaskError()
                full_id = active.id

            task = svc._get_or_raise(full_id)
            task_comments = svc.get_comments(full_id)

            if not task_comments:
                console.print(f"[dim]No comments on '{task.title}'.[/dim]")
                return

            console.print(f"[bold]Comments on:[/bold] {task.title}")
            if task.ticket_id:
                console.print(f"[cyan]Ticket: {task.ticket_id}[/cyan]")
            console.print()

            for c in task_comments:
                jira_tag = " [cyan](jira)[/cyan]" if c.synced_to_jira else ""
                console.print(f"  [{c.created_at:%Y-%m-%d %H:%M}]{jira_tag} {c.body}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def rm(
    task_id: str = typer.Argument(help="Task ID (first 8 chars or full UUID)"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Delete a task."""
    try:
        full_id = _resolve_task_id(task_id)
        with get_db() as db:
            svc = TaskService(db)
            tasks = svc.list()
            task = next((t for t in tasks if t.id == full_id), None)
            if not task:
                console.print(f"[red]Task not found:[/red] {task_id}")
                raise typer.Exit(1)

            if not force:
                typer.confirm(f"Delete task '{task.title}'?", abort=True)

            svc.delete(full_id)
            console.print(f"[green]Task deleted[/green] — {task.title}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _resolve_task_id(short_id: str) -> uuid.UUID:
    """Resolve a short ID (first 8 chars) to a full UUID by querying tasks."""
    try:
        return uuid.UUID(short_id)
    except ValueError:
        pass

    with get_db() as db:
        svc = TaskService(db)
        tasks = svc.list()
        matches = [t for t in tasks if str(t.id).startswith(short_id)]
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            raise FlowTrackError(f"Ambiguous ID '{short_id}', matches {len(matches)} tasks. Use more characters.")
        raise FlowTrackError(f"No task found matching '{short_id}'.")
