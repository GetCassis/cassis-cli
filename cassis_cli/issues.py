"""`cassis issues` — triage the issues Cassis raised on the project, from the terminal.

Issues are what the product found wrong while answering questions (an ontology
gap, missing data). Listing, reading the evidence behind an occurrence, and
resolving or dismissing them from a checkout keeps the fix loop next to the
ontology files instead of in the webapp.
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
    IssueNotFoundError,
    get_issue,
    get_issue_evidence,
    get_issues,
    post_issue_status,
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

app = typer.Typer(no_args_is_help=True, help="Triage the project's issues.")

STATUSES = ("open", "resolved", "dismissed")
IMPACTS = ("wrong_answer", "unreliable_answer", "no_answer", "inefficient")
CAUSES = ("ontology_gap", "missing_data")

# The five options every command in this subapp takes. Shared instances: typer
# reads the option metadata at decoration time, so one constant per flag keeps
# the six commands from repeating the same block.
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


def _validate_choice(value: Optional[str], allowed: "tuple[str, ...]", flag: str) -> Optional[str]:
    """Exit 2 (usage) when a filter value is not one the API knows."""
    if value is None or value in allowed:
        return value
    typer.secho(f"{flag} must be one of {', '.join(allowed)}, got {value!r}.", fg=typer.colors.RED, err=True)
    raise typer.Exit(EXIT_USAGE)


def _not_found_failure(exc: IssueNotFoundError) -> "typer.Exit":
    typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
    return typer.Exit(EXIT_VALIDATION_FAILED)


def _field(label: str, value: Any, *, blank_line: bool = False) -> None:
    """Print a labelled block, indenting a multi-line value under its label."""
    if value in (None, "", [], {}):
        return
    if blank_line:
        typer.echo("")
    text = value if isinstance(value, str) else json.dumps(value, indent=2)
    lines = text.strip().splitlines()
    if len(lines) == 1:
        typer.echo(f"{label}: {lines[0]}")
    else:
        typer.echo(f"{label}:")
        for line in lines:
            typer.echo(f"  {line}")


@app.command(name="list")
def list_issues(
    status: Optional[str] = typer.Option(None, "--status", help=f"Filter by status ({', '.join(STATUSES)})."),
    impact: Optional[str] = typer.Option(None, "--impact", help=f"Filter by impact ({', '.join(IMPACTS)})."),
    cause: Optional[str] = typer.Option(None, "--cause", help=f"Filter by cause ({', '.join(CAUSES)})."),
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    json_output: bool = typer.Option(False, "--json", help="Print the issues as raw JSON."),
) -> None:
    """List the project's issues, prioritized by impact then recurrence.

    Prints each issue's id, impact, occurrence count, status and title; the id
    is what `cassis issues show`, `resolve`, `dismiss` and `reopen` take. Exits
    0 on success, 2 on usage errors, 3 on transport/API errors.
    """
    status = _validate_choice(status, STATUSES, "--status")
    impact = _validate_choice(impact, IMPACTS, "--impact")
    cause = _validate_choice(cause, CAUSES, "--cause")
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path), quiet=json_output)

    try:
        issues = get_issues(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            status=status,
            impact=impact,
            cause=cause,
        )
    except (AuthError, ApiError) as exc:
        raise api_failure(exc) from exc

    if json_output:
        typer.echo(json.dumps(issues, indent=2))
        raise typer.Exit(EXIT_OK)

    if not issues:
        typer.echo("No issues match." if (status or impact or cause) else "No issues on this project.")
        raise typer.Exit(EXIT_OK)

    for issue in issues:
        occurrences = issue.get("occurrence_count_cache") or 0
        typer.echo(
            f"{issue.get('id')}  {issue.get('impact')}  x{occurrences}  "
            f"{issue.get('status')}  {one_line(issue.get('title'))}"
        )
    raise typer.Exit(EXIT_OK)


@app.command()
def show(
    issue_id: str = typer.Argument(..., help="Id of the issue to show (from `cassis issues list`)."),
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    json_output: bool = typer.Option(False, "--json", help="Print the issue as raw JSON."),
) -> None:
    """Show one issue: its diagnosis, suggested action, and occurrences.

    Each occurrence's id feeds `cassis issues evidence`, which prints what the
    agent saw. Exits 0 on success, 1 when the issue does not exist in the
    project, 2 on usage errors, 3 on transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path), quiet=json_output)

    try:
        issue = get_issue(api_url=api_url, api_key=api_key, project_id=project_id, issue_id=issue_id)
    except IssueNotFoundError as exc:
        raise _not_found_failure(exc) from exc
    except (AuthError, ApiError) as exc:
        raise api_failure(exc) from exc

    if json_output:
        typer.echo(json.dumps(issue, indent=2))
        raise typer.Exit(EXIT_OK)

    occurrences = issue.get("occurrences") or []
    typer.echo(f"{issue.get('id')}  {one_line(issue.get('title'))}")
    typer.echo(
        f"{issue.get('status')}  {issue.get('impact')}  {issue.get('cause')}  "
        f"{issue.get('occurrence_count_cache', len(occurrences))} occurrence(s)"
    )
    _field("Description", issue.get("description"), blank_line=True)
    _field("Suggested action", issue.get("suggested_action"), blank_line=True)
    typer.echo("")
    typer.echo("Fix proposal: available (review it in Cassis)" if issue.get("fix_proposal") else "Fix proposal: none")
    if occurrences:
        typer.echo("")
        typer.echo("Occurrences:")
        for occurrence in occurrences:
            typer.echo(f"  {occurrence.get('id')}  {one_line(occurrence.get('symptom'))}")
    raise typer.Exit(EXIT_OK)


@app.command()
def evidence(
    issue_id: str = typer.Argument(..., help="Id of the issue (from `cassis issues list`)."),
    occurrence_id: str = typer.Argument(..., help="Id of the occurrence (from `cassis issues show`)."),
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    json_output: bool = typer.Option(False, "--json", help="Print the evidence as raw JSON."),
) -> None:
    """Print the evidence behind one occurrence of an issue.

    The evidence joined from the occurrence's source: the generated SQL and
    its results, the agent's process log, and either the user's rating (chat)
    or the judge's verdict (eval). Exits 0 on success, 1 when the issue or
    occurrence does not exist in the project, 2 on usage errors, 3 on
    transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path), quiet=json_output)

    try:
        record = get_issue_evidence(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            issue_id=issue_id,
            occurrence_id=occurrence_id,
        )
    except IssueNotFoundError as exc:
        raise _not_found_failure(exc) from exc
    except (AuthError, ApiError) as exc:
        raise api_failure(exc) from exc

    if json_output:
        typer.echo(json.dumps(record, indent=2))
        raise typer.Exit(EXIT_OK)

    for key, value in record.items():
        _field(key.replace("_", " ").capitalize(), value)
    raise typer.Exit(EXIT_OK)


def _set_status(
    *,
    issue_id: str,
    status: str,
    path: Path,
    project_id: Optional[str],
    api_key: Optional[str],
    api_url: str,
    base_path: str,
) -> None:
    """Post a new status for the issue and confirm it on one line."""
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))

    try:
        issue = post_issue_status(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            issue_id=issue_id,
            status=status,
        )
    except IssueNotFoundError as exc:
        raise _not_found_failure(exc) from exc
    except (AuthError, ApiError) as exc:
        raise api_failure(exc) from exc

    typer.secho(f"✓ Issue {issue_id} is now {issue.get('status', status)}.", fg=typer.colors.GREEN)
    raise typer.Exit(EXIT_OK)


@app.command()
def resolve(
    issue_id: str = typer.Argument(..., help="Id of the issue to resolve (from `cassis issues list`)."),
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
) -> None:
    """Mark an issue resolved — the ontology change that fixes it has landed.

    Exits 0 on success, 1 when the issue does not exist in the project, 2 on
    usage errors, 3 on transport/API errors.
    """
    _set_status(
        issue_id=issue_id,
        status="resolved",
        path=path,
        project_id=project_id,
        api_key=api_key,
        api_url=api_url,
        base_path=base_path,
    )


@app.command()
def dismiss(
    issue_id: str = typer.Argument(..., help="Id of the issue to dismiss (from `cassis issues list`)."),
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
) -> None:
    """Dismiss an issue — not worth acting on.

    Exits 0 on success, 1 when the issue does not exist in the project, 2 on
    usage errors, 3 on transport/API errors.
    """
    _set_status(
        issue_id=issue_id,
        status="dismissed",
        path=path,
        project_id=project_id,
        api_key=api_key,
        api_url=api_url,
        base_path=base_path,
    )


@app.command()
def reopen(
    issue_id: str = typer.Argument(..., help="Id of the issue to reopen (from `cassis issues list`)."),
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
) -> None:
    """Reopen a resolved or dismissed issue.

    Exits 0 on success, 1 when the issue does not exist in the project, 2 on
    usage errors, 3 on transport/API errors.
    """
    _set_status(
        issue_id=issue_id,
        status="open",
        path=path,
        project_id=project_id,
        api_key=api_key,
        api_url=api_url,
        base_path=base_path,
    )
