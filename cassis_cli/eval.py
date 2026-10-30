"""`cassis eval` subcommands."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional
from uuid import UUID

import typer
from cassis_cli.api import (
    DEFAULT_API_URL,
    ApiError,
    AuthError,
    EvalBranchNotFoundError,
    EvalCaseExistsError,
    EvalCaseGoldSqlError,
    EvalCaseNotFoundError,
    EvalRunActiveError,
    EvalStartValidationError,
    delete_eval_case,
    get_eval_case_history,
    get_eval_cases,
    get_eval_run,
    get_eval_run_comparison,
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
    echo_poll_retry,
    poll_until,
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

# Caps on the evidence printed under a failed case. Bounded so a suite of
# failures stays readable in a CI log, and always announced when they bite.
_MAX_ERROR_CHARS = 200
_MAX_REASONING_CHARS = 500
_MAX_SQL_LINES = 20
# A line cap alone bounds nothing: a gold SQL written as one long line in a YAML
# case is a single line of any size, printed twice per failing case.
_MAX_SQL_CHARS = 2000


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
        typer.secho("Context validation failed:", fg=typer.colors.RED, bold=True, err=True)
        message = detail.get("message")
        if message:
            typer.echo(message, err=True)
        for finding in detail["findings"]:
            location = f"{base_path}/{finding.get('path')}: " if finding.get("path") else ""
            typer.echo(f"  {location}{finding.get('message', '')} ({finding.get('stage', '?')})", err=True)
    else:
        typer.secho(str(detail), fg=typer.colors.RED, err=True)


def _shorten(text: str, limit: int) -> str:
    """Shorten to `limit` characters and say so. A silent cut can drop the cause."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... (truncated, {len(text)} chars)"


def _print_sql(label: str, sql: str) -> None:
    """Print SQL under a failed case, bounded on both lines and characters."""
    lines = sql.strip().splitlines()
    typer.echo(f"      {label}:")
    budget = _MAX_SQL_CHARS
    shown = 0
    for line in lines[:_MAX_SQL_LINES]:
        if budget <= 0:
            break
        if len(line) > budget:
            typer.echo(f"        {line[:budget]}... (truncated, {len(line)} chars)")
            budget = 0
        else:
            typer.echo(f"        {line}")
            budget -= len(line)
        shown += 1
    if shown < len(lines):
        typer.echo(f"        ... ({len(lines) - shown} more lines)")


def _print_case_evidence(r: dict[str, Any]) -> None:
    """Print what a case needs to be diagnosed here, without row values.

    Which field carries the reason depends on the status: a missing-concept
    failure explains itself through `missing_concepts`, a judge rejection
    through `judge_reasoning`, a data mismatch through `error` and the row
    counts. Printing only `error` — as this did — showed nothing at all for the
    first two.

    Expected and actual row values are deliberately not printed. They ride in
    `--json` and the webapp instead: `cassis verify` output is a CI job log, and
    warehouse values in a job log travel further than whoever reads it intends.

    Every field is read with `.get`, so an older server that returns none of
    them degrades to the error line alone.

    Also printed for passed cases when the run named its cases (`--case`): there
    the point is reading the SQL a context change now produces, not only the
    verdict.
    """
    error = r.get("error")
    if error:
        typer.echo(f"      {_shorten(str(error), _MAX_ERROR_CHARS)}")

    names = [str(c["name"]) for c in (r.get("missing_concepts") or []) if isinstance(c, dict) and c.get("name")]
    if names:
        typer.echo(f"      Missing concepts: {', '.join(names)}")

    if r.get("judge_verdict"):
        typer.echo(f"      Judge verdict: {r['judge_verdict']}")
    if r.get("judge_reasoning"):
        typer.echo(f"      Judge reasoning: {_shorten(str(r['judge_reasoning']), _MAX_REASONING_CHARS)}")

    expected, actual = r.get("expected_row_count"), r.get("actual_row_count")
    if expected is not None or actual is not None:
        shown_actual = "-" if actual is None else str(actual)
        shown_expected = "-" if expected is None else str(expected)
        typer.echo(f"      Rows: {shown_actual} actual vs {shown_expected} expected")

    # The agent's own words, for the statuses that carry no other explanation
    # (a conversational reply instead of a plan, for one).
    content = r.get("content")
    if content and not error and not names and not r.get("judge_reasoning"):
        typer.echo(f"      {_shorten(str(content), _MAX_ERROR_CHARS)}")

    if r.get("generated_sql"):
        _print_sql("Generated SQL", str(r["generated_sql"]))
    if r.get("gold_sql"):
        label = "Gold SQL"
        if r.get("gold_sql_from_live_case"):
            label = "Gold SQL (the case as it stands now; this result predates snapshotting)"
        _print_sql(label, str(r["gold_sql"]))


def _print_results_table(results: list[dict[str, Any]], *, passed_evidence: bool = False) -> None:
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
        if status == _PASSED and not passed_evidence:
            continue
        _print_case_evidence(r)


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


def _footprint_note(case: dict[str, Any]) -> Optional[str]:
    """One line on why a case's gold SQL does not fully resolve against the context; None when it does.

    Read with `.get` throughout: an older server returns no footprint, and a
    project without a data source returns null.
    """
    footprint = case.get("footprint")
    if not isinstance(footprint, dict) or footprint.get("state") in (None, "complete"):
        return None
    reasons = []
    unresolved = footprint.get("unresolved") or []
    if unresolved:
        reasons.append(f"not in the context: {', '.join(str(t) for t in unresolved)}")
    ambiguous = footprint.get("ambiguous") or {}
    if isinstance(ambiguous, dict):
        for name, candidates in ambiguous.items():
            reasons.append(f"{name} matches several tables ({', '.join(str(c) for c in candidates)})")
    shadowed = footprint.get("shadowed") or []
    if shadowed:
        reasons.append(f"a CTE named {', '.join(str(t) for t in shadowed)} hides a context table")
    if footprint.get("error"):
        reasons.append(f"gold SQL does not parse: {_shorten(str(footprint['error']), 120)}")
    if not reasons:
        reasons.append(str(footprint.get("state")))
    return "; ".join(reasons)


def _entry_check_note(case: dict[str, Any]) -> Optional[str]:
    """One line on a case whose creation check flags it; None when stable or never checked."""
    check = case.get("entry_check")
    if not isinstance(check, dict):
        return None
    state = check.get("state")
    score = f"{check.get('passed')}/{check.get('total')}" if check.get("total") else ""
    if state == "flaky":
        return f"flaky at creation ({score} attempts passed): its verdict is noise until the question or gold SQL is tightened"
    if state == "failing":
        return f"failing at creation ({score} attempts passed): check the gold SQL, or the context gap it exposes"
    if state == "running":
        return "entry check running (3 attempts on the current context)"
    return None


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
        help="Repository directory the context is exported under (holds project.yml for the --project default).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the created case as raw JSON."),
) -> None:
    """Add a gold test case to the project's eval suite.

    Closes the loop after fixing a context issue: the question users were
    failing on becomes a gold case, so `cassis eval run` guards it from
    regressing. On an executable data source the gold SQL is run before the
    case is stored, so a case that cannot execute never enters the suite.
    The SQL comes from --gold-sql (inline) or --gold-sql-file (a file path;
    prefer it for multi-line SQL, which shell quoting mangles inline).
    Cassis then runs the new case three times in the background, and
    `cassis eval list-cases` flags it if the attempts fail or disagree.
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
        note = _footprint_note(case)
        if note:
            typer.secho(
                f"  ⚠ Its gold SQL does not fully resolve against the context ({note}); "
                "fix the SQL or add the tables to the context before relying on it.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        check = case.get("entry_check")
        if isinstance(check, dict) and check.get("state") == "running":
            typer.echo(
                "  Checking it: 3 attempts run on the current context in the background; "
                "`cassis eval list-cases` flags it if they fail or disagree."
            )
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
        help="Repository directory the context is exported under (holds project.yml for the --project default).",
    ),
    branch: Optional[str] = typer.Option(
        None,
        "--branch",
        help="Resolve each case's footprint against this context branch instead of the unpublished context.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print the cases as raw JSON (includes gold SQL and the footprint)."
    ),
) -> None:
    """List the project's eval cases: the suite `cassis eval run` scores.

    Prints each case's id and question (--json adds the gold SQL); the id is
    what `cassis eval delete-case` and `cassis eval history` take. A case
    whose gold SQL does not fully resolve against the context (a table the
    context lacks, an ambiguous name, SQL that does not parse) is flagged
    under its line, since such a case cannot be trusted to score a change,
    and so is a case whose three-attempt check after creation failed or
    disagreed.
    Exits 0 on success, 2 on usage errors (including a --branch the project
    does not have), 3 on transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))

    try:
        cases = get_eval_cases(api_url=api_url, api_key=api_key, project_id=project_id, branch=branch)
    except EvalBranchNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc
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
        flagged = 0
        for case in cases:
            question = str(case.get("question", "")).replace("\n", " ")
            typer.echo(f"{case.get('id')}  {question}")
            note = _footprint_note(case)
            if note:
                flagged += 1
                typer.secho(f"    ⚠ {note}", fg=typer.colors.YELLOW)
            check_note = _entry_check_note(case)
            if check_note:
                running = (case.get("entry_check") or {}).get("state") == "running"
                typer.secho(
                    f"    {'…' if running else '⚠'} {check_note}",
                    fg=None if running else typer.colors.YELLOW,
                )
        if flagged:
            typer.secho(
                f"{flagged} case(s) have a gold SQL that does not fully resolve against the context; "
                "fix the SQL or add the tables to the context, or they cannot be trusted to score a change.",
                fg=typer.colors.YELLOW,
                err=True,
            )
    raise typer.Exit(EXIT_OK)


# How a flip reads to the person fixing it: which side of the evaluation moved.
_CAUSE_HINTS = {
    "judge": "the judge ruled differently on the same SQL: check the gold SQL and the question's wording",
    "generation": "the agent wrote different SQL from the same context: the context leaves room for both",
    "both": "the agent wrote different SQL and the judge also wavered",
    "unattributed": "the evidence decides neither side (a re-judge failed, or the same SQL returned different rows)",
}


def _print_comparison(comparison: dict[str, Any]) -> None:
    """A repeat run's attempts per case: the same comparison the webapp's run page shows."""
    summary = comparison.get("summary") or {}
    repeat = comparison.get("repeat_count") or 1
    cases = comparison.get("cases") or []
    typer.secho(f"Repeat comparison: {repeat} attempts per case, {summary.get('cases', len(cases))} cases", bold=True)
    causes = summary.get("causes") or {}
    flipped = f"{summary.get('flipped', 0)} flipped"
    if causes:
        flipped += " (" + ", ".join(f"{k} {n}" for k, n in causes.items()) + ")"
    typer.echo(
        f"  {summary.get('all_passed', 0)} passed every attempt · "
        f"{summary.get('all_failed', 0)} failed every attempt · {flipped}"
    )
    if repeat < 2:
        typer.echo("  Not a repeat run: start one with `cassis eval run --repeat N` to measure noise.")
        return
    shown = [c for c in cases if c.get("agreement") != "all_passed"]
    if not shown:
        return
    typer.echo("")
    for c in shown:
        agreement = c.get("agreement")
        icon = "~" if agreement == "flipped" else "✗"
        color = typer.colors.YELLOW if agreement == "flipped" else typer.colors.RED
        distinct = int(c.get("distinct_sql") or 0)
        sqls = f"{distinct} SQL" if distinct == 1 else f"{distinct} SQLs"
        cause = str(c.get("cause") or "")
        question = str(c.get("question") or "").replace("\n", " ")
        if len(question) > 60:
            question = question[:57] + "..."
        typer.secho(f"  {icon} {c.get('passed', 0)}/{c.get('total', 0)}  {cause:<13} {sqls:<8} {question}", fg=color)
        typer.echo(f"      {c.get('test_case_id')}")
    seen = [k for k in _CAUSE_HINTS if k in causes]
    if seen:
        typer.echo("")
        for k in seen:
            typer.echo(f"  {k}: {_CAUSE_HINTS[k]}")


def _format_when(iso: str) -> str:
    """`2026-10-23 18:14` from an ISO timestamp; the raw text when it does not parse."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _print_health(health: dict[str, Any]) -> None:
    """The one-line account of the case, the same numbers the webapp's case page shows."""
    total = int(health.get("total") or 0)
    if total == 0:
        typer.echo("No results yet — run the case with `cassis eval run --case <id>`.")
        return
    passed = int(health.get("passed") or 0)
    parts = [f"pass rate {passed / total:.0%} ({passed} of {total})"]
    verdict = health.get("verdict")
    if verdict:
        # The server's one word on the case, the same the webapp's case page shows.
        parts.insert(0, str(verdict).replace("_", " "))
    configurations = int(health.get("configurations") or 0)
    mixed = int(health.get("mixed_configurations") or 0)
    parts.append(f"{configurations} configuration(s)")
    if health.get("avg_plan_seconds") is not None and health.get("avg_execution_seconds") is not None:
        parts.append(f"plan/execution {health['avg_plan_seconds']:.1f}s / {health['avg_execution_seconds']:.1f}s avg")
    typer.echo(" · ".join(parts))
    if mixed:
        typer.secho(
            f"⚠ {mixed} configuration(s) with verdicts that differ on identical inputs: "
            "noise the case owner can act on, not a change in the context.",
            fg=typer.colors.YELLOW,
        )
    divergences = health.get("divergences") or {}
    states = health.get("dependency_states") or {}
    details = []
    if divergences:
        details.append("divergences: " + ", ".join(f"{k} ×{n}" for k, n in divergences.items()))
    if states:
        details.append("footprint: " + ", ".join(f"{k} {n}" for k, n in states.items()))
    if details:
        typer.echo(" · ".join(details))


def _print_history_rows(results: list[dict[str, Any]]) -> None:
    """Results grouped by the frozen configuration they ran under, newest group first."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        key = str(r.get("run_config_key") or f"run:{r.get('run_id')}")
        groups.setdefault(key, []).append(r)
    for rows in groups.values():
        first = rows[0]
        passed = sum(1 for r in rows if r.get("status") == _PASSED)
        header = [
            str(first.get("run_ontology_label") or "Unknown context"),
            str(first.get("run_model") or "?"),
            f"Cassis {first.get('run_app_version') or '?'}",
            f"{len(rows)} run(s)",
            f"{passed} passed",
        ]
        typer.echo("")
        typer.secho(" · ".join(header), bold=True)
        if 0 < passed < len(rows):
            typer.secho("  ⚠ verdicts differ on an unchanged configuration", fg=typer.colors.YELLOW)
        for r in rows:
            status = str(r.get("status", "?"))
            icon = "✓" if status == _PASSED else "✗"
            divergence = str(r.get("judge_divergence") or "-")
            state = str(r.get("dependency_state") or "-")
            if state == "diverged" and status == _PASSED:
                state = "alternative path"
            if r.get("plan_seconds") is not None and r.get("execution_seconds") is not None:
                timing = f"{r['plan_seconds']:.1f}s / {r['execution_seconds']:.1f}s"
            elif r.get("duration_seconds") is not None:
                timing = f"{r['duration_seconds']:.1f}s total"
            else:
                timing = "-"
            line = (
                f"  {icon} {status:<17} {_format_when(str(r.get('created_at') or ''))}  "
                f"{divergence:<16} {state:<17} {timing}"
            )
            typer.secho(line, fg=_STATUS_COLORS.get(status, typer.colors.WHITE))


@app.command()
def history(
    case_id: str = typer.Argument(
        ...,
        help="Id of the eval case (shown by `cassis eval list-cases`).",
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
        help="Repository directory the context is exported under (holds project.yml for the --project default).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the case's health and results as raw JSON."),
) -> None:
    """Show how an eval case has behaved across runs.

    Prints the case's health (pass rate, configurations whose repeated runs
    disagree, average plan and execution time, divergence and footprint
    counts), then every result grouped by the configuration it ran under
    (context, model, Cassis version), newest first: the same account the
    webapp's case page gives. Verdicts that differ inside one configuration
    are noise to act on; a change across configurations is the context or
    the model moving. Exits 0 on success, 1 when the case does not exist in
    the project, 2 on usage errors, 3 on transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))
    try:
        UUID(case_id)
    except ValueError:
        typer.secho(f"CASE_ID must be a UUID, got {case_id!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)

    try:
        record = get_eval_case_history(api_url=api_url, api_key=api_key, project_id=project_id, case_id=case_id)
    except EvalCaseNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    if json_output:
        typer.echo(json.dumps(record, indent=2))
        raise typer.Exit(EXIT_OK)

    question = str(record.get("question") or "").replace("\n", " ")
    typer.secho(f"Case {record.get('case_id')}: {question}", bold=True)
    _print_health(record.get("health") or {})
    results = record.get("results") or []
    if isinstance(results, list) and results:
        _print_history_rows(results)
    raise typer.Exit(EXIT_OK)


@app.command()
def compare(
    run_id: str = typer.Argument(
        ...,
        help="Id of the eval run (printed by `cassis eval run`).",
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
        help="Repository directory the context is exported under (holds project.yml for the --project default).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the comparison as raw JSON."),
) -> None:
    """Compare the attempts of a repeat run, case by case.

    For a run started with `cassis eval run --repeat N`: how many cases passed
    every attempt, failed every attempt or flipped, and for each flipping case
    how many distinct SQLs the agent wrote and what moved between attempts:
    `generation` (the agent wrote different SQL), `judge` (the judge ruled
    differently on the same SQL), `both`, or `unattributed`. The same
    comparison the webapp's run page shows. Exits 0 on success, 2 on usage
    errors, 3 on transport/API errors (including a server too old to compare).
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))
    try:
        UUID(run_id)
    except ValueError:
        typer.secho(f"RUN_ID must be a UUID, got {run_id!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    try:
        comparison = get_eval_run_comparison(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
    except ApiError as exc:  # covers AuthError and EvalCompareUnsupportedError
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    if json_output:
        typer.echo(json.dumps(comparison, indent=2))
        raise typer.Exit(EXIT_OK)
    if comparison.get("status") == "running":
        typer.echo("The run is still in progress; its attempts are compared once it completes.")
        raise typer.Exit(EXIT_OK)
    _print_comparison(comparison)
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
        help="Repository directory the context is exported under (holds project.yml for the --project default).",
    ),
) -> None:
    """Delete an eval case from the project's suite.

    For pruning a case that is stale or wrong — e.g. its gold SQL encodes a
    definition the context has since changed. Exits 0 on deletion, 1 when
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
        help="Repository checkout root (the directory containing the context export path).",
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
        help="Repository directory the context is exported under (the project's git-sync Path setting).",
    ),
    branch: Optional[str] = typer.Option(
        None,
        "--branch",
        help="Run against an existing Cassis context branch by name instead of local files.",
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
    repeat: Optional[int] = typer.Option(
        None,
        "--repeat",
        min=2,
        max=5,
        help="Run every case N times (2 to 5) as one diagnostic run, then compare the attempts per case.",
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
    """Run the project's eval suite against your local context files.

    Uploads the local context file tree and scores it in-memory: nothing is pushed or
    persisted in Cassis besides the eval run itself. With --branch, runs against
    an existing Cassis branch instead (no files are sent). With --case, only the
    named case(s) run — e.g. proving one fresh `add-case` in seconds instead of
    rerunning the whole suite; passed cases then print their SQL and evidence
    too, so you can check the case passes for the right reason. With --repeat
    N, every case runs N times under the same context and model, and the
    command prints how the attempts compare per case (`cassis eval compare`);
    a repeat run is diagnostic, so it files no issue and is not the run the
    webapp shows before publishing. Exits 0 when
    the run completes with every case passed (with --repeat: no case failed
    every attempt, since a flip is the noise being measured), 1 on any failed
    case / failed run / invalid tree, 2 on usage errors (including a --branch the project
    does not have), 3 on transport errors or --timeout.

    Failed cases (and, with --case, passed ones) print the generated SQL as
    the agent wrote it. Row values are never printed (they are in --json), but
    an agent that read your data while planning can carry a value it saw into
    a SQL literal — so treat this output as sensitive as the queries themselves.
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
            repeat_count=repeat,
        )
    except EvalStartValidationError as exc:
        _print_validation_failure(exc.detail, base_path)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except EvalBranchNotFoundError as exc:
        # Bad input, not a transport or permissions problem — so exit 2, and say
        # what --branch actually takes instead of pointing at the API key.
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        typer.echo(
            "--branch runs against a context branch that already exists in Cassis "
            "(create it in the webapp, or push it with `cassis context upload`). "
            "To label a run after your local checkout's branch, use --label instead.",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE) from exc
    except ApiError as exc:  # covers AuthError and EvalRunActiveError too
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    run_id = str(run_record["run_id"])
    total = run_record.get("total_cases", 0)
    ontology_label = run_record.get("ontology_label") or "unpublished context"
    run_url = _run_page_url(app_url or api_url, project_id, run_id)
    repeat_count = run_record.get("repeat_count")
    if repeat is not None and repeat > 1 and not repeat_count:
        # An older server drops the field: the run is an ordinary one, so say so
        # rather than print a comparison it never measured.
        typer.secho(
            "This Cassis server ignored --repeat: the run executes each case once, as an ordinary run.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    if not wait:
        if json_output:
            typer.echo(json.dumps({"run": run_record, "run_url": run_url}, indent=2))
        else:
            typer.echo(f"Eval run {run_id} started: {total} cases against {ontology_label!r}.")
            typer.echo(f"Follow it at: {run_url}")
        raise typer.Exit(EXIT_OK)

    # To stderr under --json so `cassis eval run --json | jq` gets only the record.
    started = f"Eval run {run_id} started: {total} cases against {ontology_label!r}"
    if repeat_count:
        started += f", {repeat_count} attempts each"
    typer.echo(f"{started}.", err=json_output)

    try:
        final_run, results = _wait_for_run(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            run_id=run_id,
            total=total * (repeat_count or 1),
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

    comparison: Optional[dict[str, Any]] = None
    if repeat_count and final_run.get("status") == "completed":
        try:
            comparison = get_eval_run_comparison(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
        except ApiError as exc:
            typer.secho(f"Could not compare the attempts: {exc}", fg=typer.colors.YELLOW, err=True)

    if json_output:
        record: dict[str, Any] = {"run": final_run, "results": results, "run_url": run_url}
        if comparison is not None:
            record["comparison"] = comparison
        typer.echo(json.dumps(record, indent=2))
    else:
        typer.echo("")
        if comparison is not None:
            _print_comparison(comparison)
        else:
            _print_results_table(results, passed_evidence=bool(case))
        typer.echo("")
        _print_summary(final_run)
        typer.echo(f"View details: {run_url}")

    if final_run.get("status") == "completed" and _suite_passed(results, repeated=bool(repeat_count)):
        raise typer.Exit(EXIT_OK)
    raise typer.Exit(EXIT_VALIDATION_FAILED)


def _suite_passed(results: "list[dict[str, Any]]", *, repeated: bool) -> bool:
    """Whether a completed run passes: every case passed, or on a repeat run, no case failed every attempt."""
    if not repeated:
        return all(r.get("status") == _PASSED for r in results)
    passed_once: dict[str, bool] = {}
    for r in results:
        key = str(r.get("test_case_id") or r.get("question"))
        passed_once[key] = passed_once.get(key, False) or r.get("status") == _PASSED
    return all(passed_once.values())


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
    last_line = {"text": ""}

    def fetch() -> "tuple[dict[str, Any], list[dict[str, Any]]]":
        run_record = get_eval_run(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
        results = get_eval_run_results(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
        return run_record, results

    def on_progress(snapshot: "tuple[dict[str, Any], list[dict[str, Any]]]") -> None:
        _run_record, results = snapshot
        done = len(results)
        passed = sum(1 for r in results if r.get("status") == _PASSED)
        line = f"{done}/{total} cases done — {passed} ✓ {done - passed} ✗"
        if line != last_line["text"]:
            typer.echo(line, err=json_output)
            last_line["text"] = line

    def on_auth_error(exc: AuthError) -> None:
        typer.secho(f"{exc} The run keeps going server-side.", fg=typer.colors.RED, err=True)

    def on_give_up(exc: ApiError, failures: int) -> None:
        typer.secho(
            f"Polling failed {failures} times in a row ({exc}). "
            "Giving up — the run keeps going server-side; see the webapp's Evals page.",
            fg=typer.colors.RED,
            err=True,
        )

    def on_timeout(_snapshot: "tuple[dict[str, Any], list[dict[str, Any]]]") -> None:
        typer.secho(
            f"Timed out after {timeout:g}s waiting for run {run_id}. "
            "The run keeps going server-side — see the webapp's Evals page.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    return poll_until(
        fetch,
        lambda snapshot: snapshot[0].get("status") in _TERMINAL_RUN_STATUSES,
        poll_interval=poll_interval,
        timeout=timeout,
        on_timeout=on_timeout,
        on_progress=on_progress,
        on_auth_error=on_auth_error,
        on_retry=echo_poll_retry,
        on_give_up=on_give_up,
    )
