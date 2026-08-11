"""Entry point for the `cassis` CLI."""

from __future__ import annotations

import typer
from cassis_cli import __version__
from cassis_cli.eval import app as eval_app
from cassis_cli.issues import app as issues_app
from cassis_cli.ontology import app as ontology_app
from cassis_cli.projects import app as projects_app
from cassis_cli.schema import app as schema_app
from cassis_cli.status import status
from cassis_cli.verify import verify

app = typer.Typer(
    no_args_is_help=True,
    help="Cassis CLI — run Cassis actions from your CI pipelines.",
)
app.add_typer(ontology_app, name="ontology")
app.add_typer(eval_app, name="eval")
app.add_typer(schema_app, name="schema")
app.add_typer(projects_app, name="projects")
app.add_typer(issues_app, name="issues")
app.command()(status)
app.command()(verify)


@app.command()
def version() -> None:
    """Print the CLI version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
