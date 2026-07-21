"""Entry point for the `cassis` CLI."""

from __future__ import annotations

import typer
from cassis_cli import __version__
from cassis_cli.eval import app as eval_app
from cassis_cli.ontology import app as ontology_app

app = typer.Typer(
    no_args_is_help=True,
    help="Cassis CLI — run Cassis actions from your CI pipelines.",
)
app.add_typer(ontology_app, name="ontology")
app.add_typer(eval_app, name="eval")


@app.command()
def version() -> None:
    """Print the CLI version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
