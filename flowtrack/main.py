import typer

from flowtrack.cli.block import app as block_app
from flowtrack.cli.config import app as config_app
from flowtrack.cli.deploy import app as deploy_app
from flowtrack.cli.dev import app as dev_app
from flowtrack.cli.incident import app as incident_app
from flowtrack.cli.interrupt import app as interrupt_app
from flowtrack.cli.report import app as report_app
from flowtrack.cli.review import app as review_app
from flowtrack.cli.status import app as status_app
from flowtrack.cli.sync import app as sync_app
from flowtrack.cli.test_cmd import app as test_app

app = typer.Typer(name="flowtrack", help="CLI for capturing SPACE + DORA productivity metrics.")

app.add_typer(dev_app, name="dev")
app.add_typer(block_app, name="block")
app.add_typer(interrupt_app, name="interrupt")
app.add_typer(review_app, name="review")
app.add_typer(test_app, name="test")
app.add_typer(deploy_app, name="deploy")
app.add_typer(incident_app, name="incident")
app.add_typer(sync_app, name="sync")
app.add_typer(report_app, name="report")
app.add_typer(config_app, name="config")
app.add_typer(status_app, name="status")

if __name__ == "__main__":
    app()
