"""`cassis eval` subcommands."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, List, Optional
from uuid import UUID

import typer
from cassis_cli.api import (
    DEFAULT_API_URL,
    ApiError,
    AuthError,
    EvalCaseExistsError,
    EvalCaseGoldSqlError,
    EvalCaseNotFoundError,
    EvalRunActiveError,
    EvalStartValidationError,
    delete_eval_case,
    get_eval_cases,
    get_eval_run,
    get_eval_run_results,
    post_eval_case_create,
    post_eval_run_cancel,
    post_eval_run_start,
)
from cassis_cli.common import (
    DEFAULT_BASE_PATH,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
    collect_tree,
    require_api_key,
    resolve_project_id,
)

app = typer.Typer(no_args_is_help=True, help="Eval commands.")


# Result statuses that count as "passed"; everything else is a failure or error.
_PASSED = "passed"
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
_STATUS_COLORS = {
    "passed": typer.colors.GREEN,
    "failed": typer.colors.RED,
    "error": typer.colors.RED,
    "gold_sql_error": typer.colors.YELLOW,
    "missing_concept": typer.colors.YELLOW,
    "plan_unexecutable": typer.colors.YELLOW,
}


def _run_page_url(app_url: str, project_id: str, run_id: str) -> str:
    """Webapp URL of the run's detail view (Evals page, runs tab)."""
    return f"{app_url.rstrip('/')}/eval?projectId={project_id}&tab=runs&runId={run_id}"


# CI checkouts are usually detached HEAD (`git rev-parse` says "HEAD"), but
# the CI systems expose the branch through env vars — checked first so PR/MR
# eval runs get their branch label, the feature's primary use case.
_CI_BRANCH_ENV_VARS = (
    "GITHUB_HEAD_REF",  # GitHub Actions, pull_request events
    "GITHUB_REF_NAME",  # GitHub Actions, branch pushes
    "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME",  # GitLab CI, merge_request pipelines
    "CI_COMMIT_REF_NAME",  # GitLab CI, branch pipelines
    "BITBUCKET_BRANCH",  # Bitbucket Pipelines
)


def _git_branch(path: Path) -> Optional[str]:
    """Return the checkout's branch name: CI env vars first, then git.

    None outside a repo or on a detached HEAD with no CI env var.
    """
    for var in _CI_BRANCH_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
    # Two separate handlers: the repo-wide black targets py314 and would strip
    # the parens off a tuple form, which is a SyntaxError on the CLI's
    # supported Python (>=3.10).
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return None
    branch = proc.stdout.strip()
    if proc.returncode != 0 or not branch or branch == "HEAD":
        return None
    return branch


def _print_validation_failure(detail: object, base_path: str) -> None:
    """Print a structured 400 from the start endpoint (invalid tree findings, or a plain message)."""
    if isinstance(detail, dict) and isinstance(detail.get("findings"), list):
        typer.secho("Ontology validation failed:", fg=typer.colors.RED, bold=True, err=True)
        message = detail.get("message")
        if message:
            typer.echo(message, err=True)
        for finding in detail["findings"]:
            location = f"{base_path}/{finding.get('path')}: " if finding.get("path") else ""
            typer.echo(f"  {location}{finding.get('message', '')} ({finding.get('stage', '?')})", err=True)
    else:
        typer.secho(str(detail), fg=typer.colors.RED, err=True)


def _print_results_table(results: list[dict[str, Any]]) -> None:
    for r in sorted(results, key=lambda r: (r.get("status") == _PASSED, r.get("question") or "")):
        status = r.get("status", "?")
        color = _STATUS_COLORS.get(status, typer.colors.WHITE)
        icon = "✓" if status == _PASSED else "✗"
        duration = f"{r['duration_seconds']:.0f}s" if r.get("duration_seconds") is not None else "-"
        question = (r.get("question") or "").replace("\n", " ")
        if len(question) > 70:
            question = question[:67] + "..."
        line = f"  {icon} {status:<17} {duration:>5}  {question}"
        typer.secho(line, fg=color)
        if r.get("error"):
            typer.echo(f"      {str(r['error'])[:200]}")


def _print_summary(run: dict[str, Any]) -> None:
    summary = run.get("summary") or {}
    if summary.get("error"):
        # A failed run carries its reason here (e.g. the worker was lost).
        typer.secho(f"Run error: {str(summary['error'])[:300]}", fg=typer.colors.RED)
    total = summary.get("total", run.get("total_cases"))
    passed = summary.get("passed", 0)
    accuracy = summary.get("accuracy")
    parts = [f"{passed}/{total} passed"]
    if accuracy is not None:
        parts.append(f"accuracy {accuracy:.0%}")
    timing = summary.get("timing") or {}
    if timing.get("p50") is not None:
        parts.append(f"p50 {timing['p50']:.0f}s")
    typer.echo(", ".join(parts))


@app.command(name="add-case")
def add_case(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (holds <base-path>/project.yml for the --project default).",
    ),
    project_id: Optional[str] = typer.Option(
        None,
        "--project",
        envvar="CASSIS_PROJECT_ID",
        help="Target Cassis project ID (UUID). Defaults to the id in <base-path>/project.yml.",
    ),
    question: str = typer.Option(
        ...,
        "-q",
        "--question",
        help="The natural-language question the case guards.",
    ),
    gold_sql: Optional[str] = typer.Option(
        None,
        "--gold-sql",
        help="The correct SQL for the question; executed at run time to produce the expected output.",
    ),
    gold_sql_file: Optional[Path] = typer.Option(
        None,
        "--gold-sql-file",
        help="Read the gold SQL from this file instead of --gold-sql (no shell quoting of multi-line SQL).",
    ),
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
    base_path: str = typer.Option(
        DEFAULT_BASE_PATH,
        "--base-path",
        envvar="CASSIS_BASE_PATH",
        help="Repository directory the ontology is exported under (holds project.yml for the --project default).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the created case as raw JSON."),
) -> None:
    """Add a gold test case to the project's eval suite.

    Closes the loop after fixing an ontology issue: the question users were
    failing on becomes a gold case, so `cassis eval run` guards it from
    regressing. On an executable data source the gold SQL is run before the
    case is stored, so a case that cannot execute never enters the suite.
    The SQL comes from --gold-sql (inline) or --gold-sql-file (a file path;
    prefer it for multi-line SQL, which shell quoting mangles inline).
    Exits 0 on creation, 1 on a duplicate question or gold SQL that does not
    run, 2 on usage errors, 3 on transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))
    if (gold_sql is None) == (gold_sql_file is None):
        typer.secho("Pass exactly one of --gold-sql or --gold-sql-file.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    if gold_sql_file is not None:
        try:
            gold_sql = gold_sql_file.read_text(encoding="utf-8")
        # Two separate handlers: the repo-wide black targets py314 and would
        # strip the parens off a tuple form, which is a SyntaxError on the
        # CLI's supported Python (>=3.10).
        except OSError as exc:
            typer.secho(f"Cannot read {gold_sql_file}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE) from exc
        except UnicodeDecodeError as exc:
            typer.secho(f"Cannot read {gold_sql_file}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE) from exc
    assert gold_sql is not None
    if not question.strip() or not gold_sql.strip():
        typer.secho("--question and the gold SQL must not be empty.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)

    try:
        case = post_eval_case_create(
            api_url=api_url, api_key=api_key, project_id=project_id, question=question, gold_sql=gold_sql
        )
    except (EvalCaseExistsError, EvalCaseGoldSqlError) as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    if json_output:
        typer.echo(json.dumps(case, indent=2))
    else:
        typer.secho(f"✓ Added eval case {case['id']}: {case['question']}", fg=typer.colors.GREEN)
    raise typer.Exit(EXIT_OK)


@app.command(name="list-cases")
def list_cases(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (holds <base-path>/project.yml for the --project default).",
    ),
    project_id: Optional[str] = typer.Option(
        None,
        "--project",
        envvar="CASSIS_PROJECT_ID",
        help="Target Cassis project ID (UUID). Defaults to the id in <base-path>/project.yml.",
    ),
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
    base_path: str = typer.Option(
        DEFAULT_BASE_PATH,
        "--base-path",
        envvar="CASSIS_BASE_PATH",
        help="Repository directory the ontology is exported under (holds project.yml for the --project default).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the cases as raw JSON (includes gold SQL)."),
) -> None:
    """List the project's eval cases: the suite `cassis eval run` scores.

    Prints each case's id and question (--json adds the gold SQL); the id is
    what `cassis eval delete-case` takes. Exits 0 on success, 2 on usage
    errors, 3 on transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))

    try:
        cases = get_eval_cases(api_url=api_url, api_key=api_key, project_id=project_id)
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    if json_output:
        typer.echo(json.dumps(cases, indent=2))
    else:
        if not cases:
            typer.echo("No eval cases yet — add one with `cassis eval add-case`.")
        for case in cases:
            question = str(case.get("question", "")).replace("\n", " ")
            typer.echo(f"{case.get('id')}  {question}")
    raise typer.Exit(EXIT_OK)


@app.command(name="delete-case")
def delete_case(
    case_id: str = typer.Argument(
        ...,
        help="Id of the eval case to delete (shown by `cassis eval list-cases`).",
    ),
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Repository checkout root (holds <base-path>/project.yml for the --project default).",
    ),
    project_id: Optional[str] = typer.Option(
        None,
        "--project",
        envvar="CASSIS_PROJECT_ID",
        help="Target Cassis project ID (UUID). Defaults to the id in <base-path>/project.yml.",
    ),
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
    base_path: str = typer.Option(
        DEFAULT_BASE_PATH,
        "--base-path",
        envvar="CASSIS_BASE_PATH",
        help="Repository directory the ontology is exported under (holds project.yml for the --project default).",
    ),
) -> None:
    """Delete an eval case from the project's suite.

    For pruning a case that is stale or wrong — e.g. its gold SQL encodes a
    definition the ontology has since changed. Exits 0 on deletion, 1 when
    the case does not exist in the project, 2 on usage errors, 3 on
    transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))
    try:
        UUID(case_id)
    except ValueError:
        typer.secho(f"CASE_ID must be a UUID, got {case_id!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)

    try:
        delete_eval_case(api_url=api_url, api_key=api_key, project_id=project_id, case_id=case_id)
    except EvalCaseNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    typer.secho(f"✓ Deleted eval case {case_id}.", fg=typer.colors.GREEN)
    raise typer.Exit(EXIT_OK)


@app.command()
def run(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (the directory containing the ontology export path).",
    ),
    project_id: Optional[str] = typer.Option(
        None,
        "--project",
        envvar="CASSIS_PROJECT_ID",
        help="Target Cassis project ID (UUID). Defaults to the id in <base-path>/project.yml.",
    ),
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
    base_path: str = typer.Option(
        DEFAULT_BASE_PATH,
        "--base-path",
        envvar="CASSIS_BASE_PATH",
        help="Repository directory the ontology is exported under (the project's git-sync Path setting).",
    ),
    branch: Optional[str] = typer.Option(
        None,
        "--branch",
        help="Run against an existing Cassis ontology branch by name instead of local files.",
    ),
    case: Optional[List[str]] = typer.Option(
        None,
        "--case",
        help="Run only this eval case id (repeatable; ids from `eval list-cases` or `add-case`).",
    ),
    label: Optional[str] = typer.Option(
        None,
        "--label",
        help="Run label shown in the webapp's Evals page (default: the local git branch name).",
    ),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Wait for the run to finish and print results (default), or just print the run id.",
    ),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="Seconds between status polls."),
    timeout: float = typer.Option(1800.0, "--timeout", help="Give up waiting after this many seconds."),
    json_output: bool = typer.Option(False, "--json", help="Print the final run and results as raw JSON."),
    app_url: Optional[str] = typer.Option(
        None,
        "--app-url",
        envvar="CASSIS_APP_URL",
        help="Cassis webapp base URL, used for the run-details link (default: the API URL).",
    ),
) -> None:
    """Run the project's eval suite against your local ontology files.

    Uploads the local ontology file tree and scores it in-memory — nothing is pushed or
    persisted in Cassis besides the eval run itself. With --branch, runs against
    an existing Cassis branch instead (no files are sent). With --case, only the
    named case(s) run — e.g. proving one fresh `add-case` in seconds instead of
    rerunning the whole suite. Exits 0 when the run completes with every case
    passed, 1 on any failed case / failed run / invalid tree, 2 on usage
    errors, 3 on transport errors or --timeout.
    """
    api_key = require_api_key(api_key)
    for case_id in case or []:
        try:
            UUID(case_id)
        except ValueError:
            typer.secho(f"--case must be an eval case ID (UUID), got {case_id!r}.", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE)
    if branch is not None and label is not None:
        typer.secho(
            "--label cannot be used with --branch: branch runs are labelled with the branch name.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)

    files: Optional[dict[str, str]] = None
    if branch is None:
        files, base_path = collect_tree(path, base_path)
        if label is None:
            label = _git_branch(path)
    project_id = resolve_project_id(project_id, path / Path(base_path))

    try:
        run_record = post_eval_run_start(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            files=files,
            branch=branch,
            label=label,
            case_ids=case,
        )
    except EvalStartValidationError as exc:
        _print_validation_failure(exc.detail, base_path)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except ApiError as exc:  # covers AuthError and EvalRunActiveError too
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    run_id = str(run_record["run_id"])
    total = run_record.get("total_cases", 0)
    ontology_label = run_record.get("ontology_label") or "unpublished ontology"
    run_url = _run_page_url(app_url or api_url, project_id, run_id)

    if not wait:
        if json_output:
            typer.echo(json.dumps({"run": run_record, "run_url": run_url}, indent=2))
        else:
            typer.echo(f"Eval run {run_id} started: {total} cases against {ontology_label!r}.")
            typer.echo(f"Follow it at: {run_url}")
        raise typer.Exit(EXIT_OK)

    # To stderr under --json so `cassis eval run --json | jq` gets only the record.
    typer.echo(f"Eval run {run_id} started: {total} cases against {ontology_label!r}.", err=json_output)

    try:
        final_run, results = _wait_for_run(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            run_id=run_id,
            total=total,
            poll_interval=poll_interval,
            timeout=timeout,
            json_output=json_output,
        )
    except KeyboardInterrupt:
        # To stderr under --json, for the same reason as `issues analyze`.
        typer.echo("", err=json_output)
        typer.echo("Interrupted — cancelling the run...", err=json_output)
        try:
            post_eval_run_cancel(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
            typer.echo("Run cancelled.", err=json_output)
        except ApiError as exc:
            typer.secho(f"Could not cancel the run: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_INTERRUPTED)

    if json_output:
        typer.echo(json.dumps({"run": final_run, "results": results, "run_url": run_url}, indent=2))
    else:
        typer.echo("")
        _print_results_table(results)
        typer.echo("")
        _print_summary(final_run)
        typer.echo(f"View details: {run_url}")

    status = final_run.get("status")
    if status == "completed" and all(r.get("status") == _PASSED for r in results):
        raise typer.Exit(EXIT_OK)
    raise typer.Exit(EXIT_VALIDATION_FAILED)


# Consecutive poll failures tolerated before giving up: covers transient
# blips (LB hiccup, brief network loss) without letting a permanently-broken
# poll (deleted run/project) spin until --timeout.
_MAX_CONSECUTIVE_POLL_FAILURES = 5


def _wait_for_run(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    run_id: str,
    total: int,
    poll_interval: float,
    timeout: float,
    json_output: bool = False,
) -> "tuple[dict[str, Any], list[dict[str, Any]]]":
    """Poll the run until terminal. Returns (run, results).

    Progress lines go to stderr under --json so stdout stays the JSON record.

    Exits 3 directly on timeout, on an auth failure (fail fast — retrying a
    revoked key can't succeed), or after several consecutive poll failures.
    The server-side run keeps going in all three cases.
    """
    deadline = time.monotonic() + timeout
    last_line = ""
    failures = 0
    while time.monotonic() < deadline:
        try:
            run_record = get_eval_run(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
            results = get_eval_run_results(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
        except AuthError as exc:
            typer.secho(f"{exc} The run keeps going server-side.", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_TRANSPORT) from exc
        except ApiError as exc:
            failures += 1
            if failures >= _MAX_CONSECUTIVE_POLL_FAILURES:
                typer.secho(
                    f"Polling failed {failures} times in a row ({exc}). "
                    "Giving up — the run keeps going server-side; see the webapp's Evals page.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(EXIT_TRANSPORT) from exc
            # A transient poll failure shouldn't kill a multi-minute run; keep waiting.
            typer.secho(f"(poll failed, retrying: {exc})", fg=typer.colors.YELLOW, err=True)
            time.sleep(poll_interval)
            continue
        failures = 0

        done = len(results)
        passed = sum(1 for r in results if r.get("status") == _PASSED)
        line = f"{done}/{total} cases done — {passed} ✓ {done - passed} ✗"
        if line != last_line:
            typer.echo(line, err=json_output)
            last_line = line

        if run_record.get("status") in _TERMINAL_RUN_STATUSES:
            return run_record, results
        time.sleep(poll_interval)

    typer.secho(
        f"Timed out after {timeout:.0f}s waiting for run {run_id}. "
        "The run keeps going server-side — see the webapp's Evals page.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(EXIT_TRANSPORT)
