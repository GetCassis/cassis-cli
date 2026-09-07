"""`cassis issues` — triage the issues Cassis raised on the project, from the terminal.

Issues are what the product found wrong while answering questions (an ontology
gap, missing data). Listing, reading the evidence behind an occurrence, and
resolving or dismissing them from a checkout keeps the fix loop next to the
ontology files instead of in the webapp; `analyze` refreshes them from the
conversations that arrived since the last pass, without waiting for the nightly one.
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
    NothingToAnalyzeError,
    get_issue,
    get_issue_analysis_run,
    get_issue_evidence,
    get_issues,
    post_issue_analysis_cancel,
    post_issue_analysis_start,
    post_issue_status,
)
from cassis_cli.common import (
    DEFAULT_BASE_PATH,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
    api_failure,
    echo_poll_retry,
    one_line,
    poll_until,
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
    """Mark an issue resolved — the ontology change that fixes it is published.

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


_TERMINAL_ANALYSIS_STATUSES = frozenset({"completed", "failed", "cancelled"})


@app.command()
def analyze(
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Wait for the analysis to finish and print its summary (default), or just print the run id.",
    ),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="Seconds between status polls."),
    timeout: float = typer.Option(1800.0, "--timeout", help="Give up waiting after this many seconds."),
    json_output: bool = typer.Option(False, "--json", help="Print the final run record as raw JSON."),
) -> None:
    """Analyze the conversations nobody has analyzed yet, turning what went wrong into issues.

    The same pass as the webapp's "Analyze conversations" button — it runs on
    Cassis's workers, so a Ctrl-C or a lost connection never kills it — started
    on demand instead of waiting for the nightly one. When every conversation is
    already analyzed the command is a no-op and exits 0, so a job re-running it
    on a quiet project stays green. Exits 0 when the run completes, 1 when it
    fails or is cancelled, 2 on usage errors, 3 on transport errors, when a run
    is already in flight, or on --timeout (the run keeps going server-side).
    Ctrl-C cancels the run and exits 130.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path), quiet=json_output)

    try:
        run = post_issue_analysis_start(api_url=api_url, api_key=api_key, project_id=project_id)
    except NothingToAnalyzeError as exc:
        if json_output:
            typer.echo(json.dumps({"run": None, "message": str(exc)}, indent=2))
        else:
            typer.echo(str(exc))
        raise typer.Exit(EXIT_OK) from exc
    except (AuthError, ApiError) as exc:  # covers IssueAnalysisActiveError too
        raise api_failure(exc) from exc

    run_id = str(run["id"])
    total = int(run.get("total_chats") or 0)

    if not wait:
        if json_output:
            typer.echo(json.dumps({"run": run}, indent=2))
        else:
            typer.echo(f"Analysis run {run_id} started: {total} conversation(s) to analyze.")
            typer.echo("It keeps going server-side; the results land on the webapp's Issues page.")
        raise typer.Exit(EXIT_OK)

    if not json_output:
        typer.echo(f"Analysis run {run_id} started: {total} conversation(s) to analyze.")

    try:
        final_run = _wait_for_analysis(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            run_id=run_id,
            poll_interval=poll_interval,
            timeout=timeout,
            quiet=json_output,
        )
    except KeyboardInterrupt:
        # To stderr under --json: stdout is the machine's, and these lines would
        # land in front of the JSON document a caller is parsing.
        typer.echo("", err=json_output)
        typer.echo("Interrupted — cancelling the analysis...", err=json_output)
        try:
            post_issue_analysis_cancel(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
            typer.echo("Analysis cancelled. Conversations analyzed so far keep their occurrences.", err=json_output)
        except ApiError as exc:
            typer.secho(f"Could not cancel the analysis: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_INTERRUPTED)

    if json_output:
        typer.echo(json.dumps({"run": final_run}, indent=2))
    else:
        _print_analysis_outcome(final_run)

    if final_run.get("status") == "completed":
        raise typer.Exit(EXIT_OK)
    raise typer.Exit(EXIT_VALIDATION_FAILED)


def _wait_for_analysis(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    run_id: str,
    poll_interval: float,
    timeout: float,
    quiet: bool,
) -> dict[str, Any]:
    """Poll the run until terminal and return its final record.

    Exits 3 directly on timeout, on an auth failure (fail fast — retrying a
    revoked key can't succeed), or after several consecutive poll failures.
    The server-side run keeps going in all three cases.
    """
    last_line = {"text": ""}

    def fetch() -> "dict[str, Any]":
        try:
            return get_issue_analysis_run(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
        except IssueNotFoundError as exc:
            # The run is gone (purged): nothing to keep polling for.
            raise _not_found_failure(exc) from exc

    def on_progress(run: "dict[str, Any]") -> None:
        line = (
            f"{run.get('chats_analyzed', 0)}/{run.get('total_chats', 0)} conversations analyzed — "
            f"{run.get('occurrences_created', 0)} occurrence(s) found"
        )
        if not quiet and line != last_line["text"]:
            typer.echo(line)
            last_line["text"] = line

    def on_auth_error(exc: AuthError) -> None:
        typer.secho(f"{exc} The analysis keeps going server-side.", fg=typer.colors.RED, err=True)

    def on_give_up(exc: ApiError, failures: int) -> None:
        typer.secho(
            f"Polling failed {failures} times in a row ({exc}). "
            "Giving up — the analysis keeps going server-side; see the webapp's Issues page.",
            fg=typer.colors.RED,
            err=True,
        )

    def on_timeout(_run: "dict[str, Any]") -> None:
        typer.secho(
            f"Timed out after {timeout:.0f}s waiting for analysis run {run_id}. "
            "The analysis keeps going server-side — see the webapp's Issues page.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    return poll_until(
        fetch,
        lambda run: run.get("status") in _TERMINAL_ANALYSIS_STATUSES,
        poll_interval=poll_interval,
        timeout=timeout,
        on_timeout=on_timeout,
        on_progress=on_progress,
        on_auth_error=on_auth_error,
        on_retry=echo_poll_retry,
        on_give_up=on_give_up,
    )


def _print_analysis_outcome(run: dict[str, Any]) -> None:
    status = run.get("status")
    if status == "completed":
        typer.echo("")
        typer.echo(
            f"Analysis complete: {run.get('chats_analyzed', 0)} conversation(s) analyzed, "
            f"{run.get('occurrences_created', 0)} occurrence(s) found, "
            f"{run.get('issues_touched', 0)} issue(s) created or updated."
        )
        typer.echo("Review them with `cassis issues list`.")
    elif status == "cancelled":
        typer.secho("Analysis cancelled before it finished.", fg=typer.colors.YELLOW, err=True)
    else:
        typer.secho(f"Analysis failed: {run.get('error') or 'unknown error'}", fg=typer.colors.RED, err=True)
