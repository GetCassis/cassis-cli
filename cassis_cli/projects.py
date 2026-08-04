"""`cassis projects` — discover the projects an API key can reach.

Every other CI route is project-scoped, so the very first thing a pipeline or
checkout agent needs is a project id. `projects list` answers that from the
terminal instead of fishing the UUID out of a webapp URL.
"""

from __future__ import annotations

import json
from typing import Optional

import typer
from cassis_cli.api import DEFAULT_API_URL, ApiError, AuthError, get_projects
from cassis_cli.common import EXIT_OK, EXIT_TRANSPORT, require_api_key

app = typer.Typer(help="Discover the projects this API key can reach.")


@app.command(name="list")
def list_projects(
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar="CASSIS_API_KEY",
        help="Cassis API key (sk-k6-...). Create one in Organization settings -> API keys.",
    ),
    api_url: str = typer.Option(
        DEFAULT_API_URL,
        "--api-url",
        envvar="CASSIS_API_URL",
        help="Cassis API base URL.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON response."),
) -> None:
    """List the projects available to the API key.

    Prints each project's id (what --project and CASSIS_PROJECT_ID take), name,
    published ontology version, and data-source dialect. A schema-only source
    (no connection) is marked "not executable": SQL is generated but never run.
    Exits 0 on success, 2 on usage errors, 3 on transport/API errors.
    """
    api_key = require_api_key(api_key)

    try:
        projects = get_projects(api_url=api_url, api_key=api_key)
    except (AuthError, ApiError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    if json_output:
        typer.echo(json.dumps(projects, indent=2))
        raise typer.Exit(EXIT_OK)

    if not projects:
        typer.echo("No projects visible to this API key.")
        raise typer.Exit(EXIT_OK)

    for project in projects:
        version = project.get("published_version")
        version_text = f"v{version}" if version is not None else "unpublished"
        source = project.get("data_source")
        if source:
            dialect = source.get("sql_dialect") or "?"
            source_text = dialect if source.get("is_executable") else f"{dialect}, not executable"
        else:
            source_text = "no data source"
        typer.echo(f"{project['id']}  {project['name']}  ({version_text}; {source_text})")
    raise typer.Exit(EXIT_OK)
