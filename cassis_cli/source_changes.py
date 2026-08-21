"""`cassis source-changes` — read the Data source review queue from the terminal.

Source changes are the schema drift Cassis detected between the data source
and what the ontology tracks (tables/columns added, removed, renamed,
retyped). Reading them from a checkout lets an agent see that a breaking
`column_removed` card is pending against a table it is editing — the fix then
happens in the ontology files via a pull request. Read-only by design:
reviewing (approve applies ontology edits, dismiss mutes the table) stays in
the webapp.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import typer
from cassis_cli.api import (
    DEFAULT_API_URL,
    ApiError,
    AuthError,
    SourceChangeNotFoundError,
    get_source_change,
    get_source_changes,
)
from cassis_cli.common import (
    DEFAULT_BASE_PATH,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
    api_failure,
    one_line,
    require_api_key,
    resolve_project_id,
)

app = typer.Typer(no_args_is_help=True, help="Read the project's Data source review queue.")

STATUSES = ("pending", "approved", "rejected", "superseded")

_PATH_OPTION = typer.Option(
    Path("."),
    "--path",
    help="Repository checkout root (holds <base-path>/project.yml for the --project default).",
)
_PROJECT_OPTION = typer.Option(
    None,
    "--project",
    envvar="CASSIS_PROJECT_ID",
    help="Target Cassis project ID (UUID). Defaults to the id in <base-path>/project.yml.",
)
_API_KEY_OPTION = typer.Option(
    None,
    "--api-key",
    envvar="CASSIS_API_KEY",
    help="Cassis API key (sk-k6-...). Create one in Organization settings -> API keys.",
)
_API_URL_OPTION = typer.Option(
    DEFAULT_API_URL,
    "--api-url",
    envvar="CASSIS_API_URL",
    help="Cassis API base URL.",
)
_BASE_PATH_OPTION = typer.Option(
    DEFAULT_BASE_PATH,
    "--base-path",
    envvar="CASSIS_BASE_PATH",
    help="Repository directory the ontology is exported under (holds project.yml for the --project default).",
)


def _target(change: dict[str, Any]) -> str:
    parts = [change.get("target_schema"), change.get("target_table"), change.get("target_column")]
    return ".".join(str(p) for p in parts if p)


@app.command(name="list")
def list_source_changes(
    status: Optional[str] = typer.Option(
        None, "--status", help=f"Filter by status ({', '.join(STATUSES)}). Defaults to pending."
    ),
    limit: int = typer.Option(100, "--limit", min=1, max=500, help="Page size."),
    offset: int = typer.Option(0, "--offset", min=0, help="Page start, newest first."),
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    json_output: bool = typer.Option(False, "--json", help="Print the page as raw JSON ({items, total})."),
) -> None:
    """List the pending Data source review items, newest first.

    Prints each change's id, type, severity, status and target; the id is what
    `cassis source-changes show` takes. A `breaking` severity means a curated
    ontology object references the changed source object. Exits 0 on success,
    2 on usage errors, 3 on transport/API errors.
    """
    if status is not None and status not in STATUSES:
        typer.secho(f"--status must be one of {', '.join(STATUSES)}, got {status!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path), quiet=json_output)

    try:
        page = get_source_changes(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    except (AuthError, ApiError) as exc:
        raise api_failure(exc) from exc

    if json_output:
        typer.echo(json.dumps(page, indent=2))
        raise typer.Exit(EXIT_OK)

    items = page.get("items") or []
    total = page.get("total", len(items))
    if not items:
        typer.echo("No source changes match." if status else "No pending source changes.")
        raise typer.Exit(EXIT_OK)

    for change in items:
        typer.echo(
            f"{change.get('id')}  {change.get('change_type')}  {change.get('severity')}  "
            f"{change.get('status')}  {_target(change)}"
        )
    shown = len(items)
    if offset + shown < total:
        typer.echo(f"Showing {shown} of {total} (use --offset {offset + shown} for the next page).")
    raise typer.Exit(EXIT_OK)


@app.command()
def show(
    change_id: str = typer.Argument(..., help="Id of the change to show (from `cassis source-changes list`)."),
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    json_output: bool = typer.Option(False, "--json", help="Print the change as raw JSON."),
) -> None:
    """Show one Data source review item: its impact and the suggested edit.

    `impact` lists the curated ontology objects referencing the changed source
    object; the suggested edit describes what approving in the webapp would do
    — make the equivalent edit in the ontology files to fix headlessly. Exits
    0 on success, 1 when the change does not exist in the project, 2 on usage
    errors, 3 on transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path), quiet=json_output)

    try:
        change = get_source_change(api_url=api_url, api_key=api_key, project_id=project_id, change_id=change_id)
    except SourceChangeNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except (AuthError, ApiError) as exc:
        raise api_failure(exc) from exc

    if json_output:
        typer.echo(json.dumps(change, indent=2))
        raise typer.Exit(EXIT_OK)

    typer.echo(f"{change.get('id')}  {change.get('change_type')}  {change.get('severity')}  {change.get('status')}")
    typer.echo(f"Target: {_target(change)}")
    typer.echo(f"Raised {change.get('times_raised', 1)}x, last {change.get('last_detected_at')}")
    impact = change.get("impact") or []
    if impact:
        typer.echo("")
        typer.echo("Impact:")
        for ref in impact:
            line = f"  {ref.get('kind')}  {ref.get('confidence')}  {one_line(ref.get('object_label'))}"
            detail = ref.get("detail")
            if detail:
                line += f"  — {one_line(detail)}"
            typer.echo(line)
    edit = change.get("suggested_edit") or {}
    summary = edit.get("human_summary") if isinstance(edit, dict) else None
    if summary:
        typer.echo("")
        typer.echo(f"Suggested edit: {one_line(summary)}")
        for note in edit.get("manual_review") or []:
            typer.echo(f"  Manual review: {one_line(note)}")
    raise typer.Exit(EXIT_OK)
