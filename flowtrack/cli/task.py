import uuid
from typing import Optional

import typer
from rich.panel import Panel
from rich.table import Table

from flowtrack.core.console import console
from flowtrack.core.database import get_db
from flowtrack.core.exceptions import FlowTrackError
from flowtrack.models.task import TaskPriority, TaskStatus
from flowtrack.services.orchestrator_service import OrchestratorService, RoleNotFoundError
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

        # Enrich with the role currently working each task (if any).
        active_roles = OrchestratorService(db).active_roles_for([t.id for t in tasks])

        table = Table(title="Tasks", show_header=True)
        table.add_column("ID", style="dim", max_width=8)
        table.add_column("Title", style="bold")
        table.add_column("Status")
        table.add_column("Priority")
        table.add_column("Role", style="cyan")
        table.add_column("Module", style="dim")
        table.add_column("Ticket", style="cyan")

        for task in tasks:
            s_color = STATUS_COLORS.get(task.status, "white")
            p_color = PRIORITY_COLORS.get(task.priority, "white")
            role_name = active_roles.get(task.id, "")
            table.add_row(
                str(task.id)[:8],
                task.title,
                f"[{s_color}]{task.status.value}[/{s_color}]",
                f"[{p_color}]{task.priority.value}[/{p_color}]",
                f"[green]>{role_name}[/green]" if role_name else "",
                task.module_hint or "",
                task.ticket_id or "",
            )

    console.print(table)


@app.command()
def update(
    task_id: str = typer.Argument(help="Task ID (first 8 chars or full UUID)"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="New status: todo|in_progress|blocked|in_review|in_qa|done"),
    title: Optional[str] = typer.Option(None, "--title", help="New title"),
    description: Optional[str] = typer.Option(None, "--desc", "-d", help="New description"),
    priority: Optional[str] = typer.Option(None, "--priority", "-p", help="New priority: low|medium|high|urgent"),
    acceptance_criteria: Optional[str] = typer.Option(
        None, "--acceptance-criteria", "--criteria", "-a",
        help="Set the acceptance criteria (required before dev can pick the task up)",
    ),
    module_hint: Optional[str] = typer.Option(
        None, "--module-hint", "-m",
        help="Module the task touches — used by the orchestrator for resource locking",
    ),
) -> None:
    """Update a task."""
    if not any([status, title, description, priority, acceptance_criteria, module_hint]):
        console.print(
            "[red]Provide at least one option to update "
            "(--status, --title, --desc, --priority, --criteria, --module-hint)[/red]"
        )
        raise typer.Exit(1)

    task_status = None
    if status:
        try:
            task_status = TaskStatus(status)
        except ValueError:
            console.print(f"[red]Invalid status:[/red] {status}. Use: {', '.join(STATUS_CHOICES)}")
            raise typer.Exit(1)

    task_priority = None
    if priority:
        try:
            task_priority = TaskPriority(priority)
        except ValueError:
            console.print(f"[red]Invalid priority:[/red] {priority}. Use: {', '.join(PRIORITY_CHOICES)}")
            raise typer.Exit(1)

    try:
        full_id = _resolve_task_id(task_id)
        with get_db() as db:
            svc = TaskService(db)
            task = svc.update(
                full_id, status=task_status, title=title,
                description=description, priority=task_priority,
                acceptance_criteria=acceptance_criteria,
                module_hint=module_hint,
            )
            console.print(f"[green]Task updated[/green] — {task.title}")
            if task_status:
                s_color = STATUS_COLORS.get(task.status, "white")
                console.print(f"  Status: [{s_color}]{task.status.value}[/{s_color}]")
            if priority:
                p_color = PRIORITY_COLORS.get(task.priority, "white")
                console.print(f"  Priority: [{p_color}]{task.priority.value}[/{p_color}]")
            if acceptance_criteria is not None:
                console.print(f"  Acceptance criteria: {task.acceptance_criteria[:80]}...")
            if module_hint is not None:
                console.print(f"  Module hint: {task.module_hint}")
    except FlowTrackError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def assign(
    task_id: str = typer.Argument(help="Task ID (first 8 chars or full UUID)"),
    role_name: str = typer.Argument(help="Role: dev|reviewer|qa|pm|po|design|devops"),
    priority: int = typer.Option(100, "--priority", "-p", help="Job priority (lower = sooner)"),
    worker_id: Optional[str] = typer.Option(
        None, "--worker", "-w",
        help="Pin job to a specific worker lane (default: any daemon)",
    ),
) -> None:
    """Enqueue a Job: the orchestrator will spawn the given role for this task.

    Useful to drive a task into the dev->reviewer->qa pipeline from the CLI
    without going through the kanban UI.
    """
    try:
        full_id = _resolve_task_id(task_id)
        with get_db() as db:
            svc = OrchestratorService(db)
            job = svc.assign(full_id, role_name, priority=priority, worker_id=worker_id)
            console.print(
                f"[green]Job queued[/green] {str(job.id)[:8]} "
                f"role=[cyan]{role_name}[/cyan] priority={priority}"
                + (f" worker=[dim]{worker_id}[/dim]" if worker_id else "")
            )
            console.print(
                "[dim]Tip: GET /api/instances or watch the kanban at http://"
                "127.0.0.1:8080/ to follow the run.[/dim]"
            )
    except RoleNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("[dim]Available roles: dev, reviewer, qa, pm, po, design, devops[/dim]")
        raise typer.Exit(1)
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
def show(
    task_id: str = typer.Argument(help="Task ID (first 8 chars or full UUID)"),
) -> None:
    """Show task details with comments, pipeline transitions, and instances."""
    try:
        full_id = _resolve_task_id(task_id)
        with get_db() as db:
            svc = TaskService(db)
            orc = OrchestratorService(db)
            task = svc._get_or_raise(full_id)
            task_comments = svc.get_comments(full_id)
            transitions = orc.transitions_for(full_id)
            instances = orc.instances_for(full_id)
            jobs = orc.jobs_for(full_id)

            s_color = STATUS_COLORS.get(task.status, "white")
            p_color = PRIORITY_COLORS.get(task.priority, "white")

            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column("Key", style="bold cyan")
            table.add_column("Value")

            table.add_row("ID", str(task.id)[:8])
            table.add_row("Title", task.title)
            if task.description:
                table.add_row("Description", task.description)
            table.add_row("Status", f"[{s_color}]{task.status.value}[/{s_color}]")
            table.add_row("Priority", f"[{p_color}]{task.priority.value}[/{p_color}]")
            if task.ticket_id:
                table.add_row("Ticket", task.ticket_id)
            if task.module_hint:
                table.add_row("Module hint", task.module_hint)
            if task.acceptance_criteria:
                table.add_row("Acceptance criteria", task.acceptance_criteria)
            table.add_row("Created", f"{task.created_at:%Y-%m-%d %H:%M}")

            console.print(Panel(table, title="Task Details", expand=False))

            if jobs:
                console.print()
                console.print("[bold]Jobs (newest first):[/bold]")
                jt = Table(show_header=True, box=None, padding=(0, 2))
                jt.add_column("Role", style="cyan")
                jt.add_column("Status")
                jt.add_column("Attempts")
                jt.add_column("Worker", style="dim")
                jt.add_column("Created", style="dim")
                for job, role_name in jobs:
                    jt.add_row(
                        role_name,
                        job.status.value,
                        str(job.attempts),
                        job.worker_id or "(any)",
                        f"{job.created_at:%Y-%m-%d %H:%M}",
                    )
                console.print(jt)

            if instances:
                console.print()
                console.print("[bold]Instances (newest first):[/bold]")
                it = Table(show_header=True, box=None, padding=(0, 2))
                it.add_column("Role", style="cyan")
                it.add_column("Status")
                it.add_column("Tokens", style="dim")
                it.add_column("Cost", style="dim")
                it.add_column("Spawned", style="dim")
                for inst, role_name in instances:
                    it.add_row(
                        role_name,
                        inst.status.value,
                        f"{inst.tokens_input}/{inst.tokens_output}",
                        f"${inst.cost_usd}",
                        f"{inst.spawned_at:%H:%M:%S}",
                    )
                console.print(it)

            if transitions:
                console.print()
                console.print("[bold]Pipeline transitions:[/bold]")
                for t in transitions:
                    when = f"{t.transitioned_at:%H:%M:%S}"
                    reason = f" — {t.reason}" if t.reason else ""
                    console.print(
                        f"  [dim]{when}[/dim] "
                        f"{t.from_status or '-'} -> [cyan]{t.to_status}[/cyan]{reason}"
                    )

            if task_comments:
                console.print()
                console.print("[bold]Comments:[/bold]")
                for c in task_comments:
                    jira_tag = " [cyan](jira)[/cyan]" if c.synced_to_jira else ""
                    console.print(f"  [{c.created_at:%Y-%m-%d %H:%M}]{jira_tag} {c.body}")
            elif not (jobs or instances or transitions):
                console.print("\n[dim]No comments, jobs, instances or transitions yet.[/dim]")
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
