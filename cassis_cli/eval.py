"""`cassis eval` subcommands."""

from __future__ import annotations

import difflib
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
    EvalBaseVersionNotFoundError,
    EvalBranchNotFoundError,
    EvalCaseDefinitionError,
    EvalCaseExistsError,
    EvalCaseGoldSqlError,
    EvalCaseNotFoundError,
    EvalCaseReplacedError,
    EvalRunActiveError,
    EvalRunNotFoundError,
    EvalStartValidationError,
    delete_eval_case,
    get_eval_case_history,
    get_eval_cases,
    get_eval_health,
    get_eval_run,
    get_eval_run_comparison,
    get_eval_run_results,
    get_eval_runs,
    get_eval_spend,
    post_eval_case_create,
    post_eval_case_replace,
    post_eval_run_cancel,
    post_eval_run_start,
)
from cassis_cli.common import (
    DEFAULT_BASE_PATH,
    EXIT_INTERRUPTED,
    EXIT_NO_VERDICT,
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
    api_failure,
    collect_tree,
    echo_poll_retry,
    poll_until,
    require_api_key,
    resolve_project_id,
)

app = typer.Typer(no_args_is_help=True, help="Eval commands.")


# Result statuses that count as "passed"; everything else is a failure or error.
_PASSED = "passed"
# `skipped` is an automatic run the spending protection did not start; the
# CLI never starts one, but a poll must still end on it.
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "skipped"}
# Where a run came from, as the case history names it.
_ORIGIN_LABELS = {
    "evals_page": "Evals page",
    "case_page": "case page",
    "chat": "context chat",
    "cli": "CLI",
    "ci": "CI",
    "scheduler": "scheduled",
    "publish": "publish",
    "merge": "merge",
    "github_check": "GitHub check",
}
_RUN_STATUS_COLORS = {
    "failed": typer.colors.RED,
    "skipped": typer.colors.YELLOW,
}
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


def _git_commit(path: Path) -> Optional[str]:
    """The checkout's HEAD commit: CI env vars first, then git. None outside a repo."""
    for var in ("GITHUB_SHA", "CI_COMMIT_SHA", "BITBUCKET_COMMIT"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, timeout=5)
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return None
    commit = proc.stdout.strip()
    return commit if proc.returncode == 0 and commit else None


def _ci_provenance() -> "tuple[Optional[str], Optional[str]]":
    """The CI pipeline (a URL when the CI exposes one) and the merge or pull request the run belongs to.

    Both None outside CI: the server then records the run as a plain CLI run.
    """
    env = os.environ
    pipeline: Optional[str] = None
    merge_request: Optional[str] = None
    if env.get("GITHUB_RUN_ID"):
        server, repo = env.get("GITHUB_SERVER_URL", "https://github.com"), env.get("GITHUB_REPOSITORY", "")
        pipeline = f"{server}/{repo}/actions/runs/{env['GITHUB_RUN_ID']}" if repo else env["GITHUB_RUN_ID"]
        # `refs/pull/<n>/merge` on pull_request events.
        ref = env.get("GITHUB_REF", "")
        if ref.startswith("refs/pull/"):
            merge_request = f"{server}/{repo}/pull/{ref.split('/')[2]}" if repo else ref.split("/")[2]
    elif env.get("CI_PIPELINE_ID"):
        pipeline = env.get("CI_PIPELINE_URL") or env["CI_PIPELINE_ID"]
        iid = env.get("CI_MERGE_REQUEST_IID")
        if iid:
            project_url = env.get("CI_MERGE_REQUEST_PROJECT_URL") or env.get("CI_PROJECT_URL")
            merge_request = f"{project_url}/-/merge_requests/{iid}" if project_url else iid
    elif env.get("BITBUCKET_PIPELINE_UUID"):
        pipeline = env["BITBUCKET_PIPELINE_UUID"]
        merge_request = env.get("BITBUCKET_PR_ID") or None
    return pipeline, merge_request


def _warn_if_spend_window_spent(*, api_url: str, api_key: str, project_id: str) -> None:
    """Say when the project's automatic runs are paused by the spending protection; never block the run.

    Called once a run has started, so a usage error or a dry run never
    carries it. Read with `.get` and swallowed on any API error: an older
    server has no window, and the warning is advice beside the run, not a
    gate on it.
    """
    try:
        spend = get_eval_spend(api_url=api_url, api_key=api_key, project_id=project_id)
    except ApiError:
        return
    remaining = spend.get("remaining_cents")
    if spend.get("automatic_runs_enabled") is not True:
        return
    incomplete = spend.get("unpriced_runs", 0)
    if not incomplete and (not isinstance(remaining, (int, float)) or remaining > 0):
        return
    reason = (
        "some runs have incomplete cost records"
        if incomplete
        else f"the {spend.get('window_days', '?')}-day window's ${spend.get('limit_cents', 0) / 100:.2f} limit is spent"
    )
    typer.secho(
        f"Automatic eval runs on this project are paused: {reason}. "
        "This run goes ahead and counts toward the window.",
        fg=typer.colors.YELLOW,
        err=True,
    )


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

    expectation = r.get("expectation")
    expects = _expectation_note(expectation)
    if expects:
        typer.echo(f"      Expected: {expects}")
    for line in _path_lines(r.get("path_evidence")):
        typer.echo(f"      {line}")

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
        for detail in _comparison_lines(r):
            typer.echo(f"      {detail}")
        if status == _PASSED and not passed_evidence:
            continue
        _print_case_evidence(r)


def _dollars(cents: Any) -> Optional[str]:
    return f"${cents / 100:.2f}" if isinstance(cents, (int, float)) else None


def _skip_reason(skipped: Any) -> Optional[str]:
    """Why an automatic run did not start, in words; None when the run was not skipped."""
    if not isinstance(skipped, dict):
        return None
    reason = skipped.get("reason")
    if reason == "baseline_required":
        return "Run evals manually first to establish a cost estimate."
    if reason == "cost_unknown":
        return "Cost records are incomplete; automatic runs are paused."
    if reason == "budget":
        estimate, remaining = _dollars(skipped.get("estimate_cents")), _dollars(skipped.get("remaining_cents"))
        limit = _dollars(skipped.get("limit_cents"))
        amounts = []
        if estimate:
            amounts.append(f"estimated {estimate}")
        if remaining and limit:
            amounts.append(f"{remaining} of {limit} left")
        elif limit:
            amounts.append(f"limit {limit}")
        return "The automatic-spend window's limit is spent" + (f" ({', '.join(amounts)})." if amounts else ".")
    return str(reason or "No reason recorded")


def _scope_note(run: Any) -> Optional[str]:
    """`3 of 15 cases (affected by a context change)` for a run that covered part of the suite; None for a full run.

    The suite size is the selection's case list, which holds every current
    case with its selected flag. Read with `.get`: a run from an older server
    or one without a data source carries no selection and reads as full.
    """
    selection = run.get("selection") if isinstance(run, dict) else None
    if not isinstance(selection, dict):
        return None
    rows = [row for row in selection.get("cases") or [] if isinstance(row, dict)]
    changed = selection.get("mode") not in (None, "full")
    selected = sum(bool(row.get("selected")) for row in rows) if rows else run.get("total_cases")
    if not changed and (not rows or selected >= len(rows)):
        return None
    why = "affected by a context change" if changed else "the requested cases"
    if not rows:
        return f"{selected} of the affected cases only" if isinstance(selected, int) else "the affected cases only"
    return f"{selected} of {_plural(len(rows), 'case')} ({why})"


def _pass_count(run: dict[str, Any], total: Any, passed: Any) -> str:
    """`3/3 passed`, or on a repeat run `25 cases × 3 attempts · 75/75 attempts passed`.

    A repeat run's summary counts attempts, so a bare quotient would read as
    that many cases.
    """
    repeat = run.get("repeat_count")
    if isinstance(repeat, int) and repeat > 1:
        cases = _plural(int(run.get("total_cases") or 0), "case")
        return f"{cases} × {repeat} attempts · {passed}/{total} attempts passed"
    return f"{passed}/{total} passed"


def _print_summary(run: dict[str, Any]) -> None:
    summary = run.get("summary") or {}
    if summary.get("error"):
        # A failed run carries its reason here (e.g. the worker was lost).
        typer.secho(f"Run error: {str(summary['error'])[:300]}", fg=typer.colors.RED)
    skipped = _skip_reason(run.get("skipped") or summary.get("skipped"))
    if skipped:
        typer.echo(f"Not started: {skipped}")
    total = summary.get("total", run.get("total_cases"))
    passed = summary.get("passed", 0)
    accuracy = summary.get("accuracy")
    parts = [_pass_count(run, total, passed)]
    if accuracy is not None:
        parts.append(f"accuracy {accuracy:.0%}")
    timing = summary.get("timing") or {}
    if timing.get("p50") is not None:
        parts.append(f"p50 {timing['p50']:.0f}s")
    scope = _scope_note(run)
    typer.echo(", ".join(parts) + (f" · {scope}" if scope else ""))
    shadow = summary.get("shadow")
    if isinstance(shadow, dict):
        typer.echo(
            f"Shadow selection: would skip {shadow.get('would_skip', 0)}; "
            f"compared {shadow.get('compared', 0)}, unknown {shadow.get('unknown', 0)}; "
            f"verdict changes {shadow.get('verdict_changes', 0)}, "
            f"SQL changes {shadow.get('sql_changes', 0)}, path changes {shadow.get('path_changes', 0)}."
        )


def _footprint_note(case: dict[str, Any]) -> Optional[str]:
    """One line on why a case's gold SQL does not fully resolve against the context; None when it does.

    Read with `.get` throughout: an older server returns no footprint, and a
    project without a data source returns null.
    """
    footprint = case.get("footprint")
    if not isinstance(footprint, dict) or footprint.get("state") in (None, "complete"):
        return None
    reasons = []
    unverified_catalogs = footprint.get("unverified_catalogs") or []
    if unverified_catalogs:
        reasons.append(f"catalog identity unverified: {', '.join(str(t) for t in unverified_catalogs)}")
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


_EXPECT_CHOICES = ("answer", "cannot-answer")


# `--compare` words, each changing one default rule.
_COMPARE_WORDS = ("exact", "ordered", "allow-empty")


def _compare_option(compare: Optional[str]) -> Optional[dict[str, Any]]:
    """`--compare` as the API's comparison body, or None when the option is absent.

    `default` is the default rules (1% numeric tolerance, any row order,
    columns paired by value, an empty result fails). Each word changes one:
    `exact` numbers, `ordered` rows, `allow-empty` results; words combine
    with commas. Exits 2 on an unknown word, like every option check here.
    """
    if compare is None:
        return None
    words = {w.strip().lower() for w in compare.split(",") if w.strip()}
    if not words or words - {"default", *_COMPARE_WORDS} or ("default" in words and len(words) > 1):
        typer.secho(
            f"--compare takes default, or any of {', '.join(_COMPARE_WORDS)} separated by commas.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    return {
        "numeric": "exact" if "exact" in words else "legacy",
        "ordered": "ordered" in words,
        "columns": "automatic",
        "allow_empty": "allow-empty" in words,
    }


def _comparison_note(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    numeric = value.get("numeric", "legacy")
    ordered = value.get("ordered", False)
    columns = value.get("columns", "automatic")
    allow_empty = value.get("allow_empty", False)
    if numeric == "legacy" and not ordered and columns == "automatic" and not allow_empty:
        return None
    numbers = f"{numeric} numbers"
    if numeric in ("absolute", "relative"):
        numbers = f"{numeric} tolerance {value.get('tolerance')}"
    return "; ".join(
        (
            numbers,
            "ordered rows" if ordered else "unordered rows",
            f"columns: {columns}",
            "empty answers allowed" if allow_empty else "empty answers rejected",
        )
    )


_DEFAULT_COMPARISON = "default rules (1% numeric tolerance, unordered rows, columns by value, empty rejected)"
# The rule that decided a result, as `comparison_rule` names it.
_RULE_WORDS = {
    "exact_numeric": "exact numbers",
    "absolute_tolerance": "absolute tolerance",
    "relative_tolerance": "relative tolerance",
    "legacy_tolerance": "legacy 1% tolerance",
    "row_order": "row order",
    "column_match": "column matching",
    "row_count": "row count",
}


def _rule_note(rule: Any, passed: bool) -> Optional[str]:
    """The deciding comparison rule in words; None on results from older servers."""
    if not rule:
        return None
    if rule == "empty_allowed":
        return "Empty result accepted"
    words = _RULE_WORDS.get(str(rule), str(rule).replace("_", " "))
    return f"Decided by: {words}" if passed else f"Failed on {words}"


def _application_note(r: dict[str, Any]) -> Optional[str]:
    """The server's words when the comparison rules did not govern the verdict; None when they did.

    `Result comparison not applied: <why>` names what was not applied; on a
    metadata-only source it adds who decided instead. Any other note (an
    unknown execution mode) prints as the server wrote it.
    """
    note = r.get("comparison_note")
    if not note:
        return None
    note = str(note)
    if r.get("comparison_applied") is not False:
        return note
    why = note[len("Not applied: ") :] if note.startswith("Not applied: ") else note
    line = f"Result comparison not applied: {why}"
    if "not executed" in why:
        line += " (metadata-only source: the judge decided)"
    return line


def _comparison_lines(r: dict[str, Any]) -> list[str]:
    """How a result's rows were compared: the rules, whether they applied, and the rule that decided.

    Every key is read with `.get`: an older server sends none of them and the
    result prints no comparison line.
    """
    lines = []
    rules = _comparison_note(r.get("comparison"))
    if rules:
        lines.append(f"Comparison: {rules}")
    elif r.get("comparison_applied") is True:
        lines.append(f"Comparison: {_DEFAULT_COMPARISON}")
    note = _application_note(r)
    if note:
        lines.append(note)
    rule = _rule_note(r.get("comparison_rule"), r.get("status") == _PASSED)
    if rule:
        lines.append(rule)
    return lines


def _check_comparison_ack(case: dict[str, Any], requested: Optional[dict[str, Any]]) -> None:
    if requested is None:
        return
    acknowledged = case.get("comparison")
    if not isinstance(acknowledged, dict) or any(acknowledged.get(k) != v for k, v in requested.items()):
        typer.secho(
            "The server did not acknowledge the requested result comparison settings. Update the server before relying on this case.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_TRANSPORT)


def _warn_if_comparison_not_applied(cases: list[dict[str, Any]]) -> None:
    """One stderr line when a case sets comparison rules the source will never apply.

    The server says so per case (`comparison_applicable`, false on a
    metadata-only source, where the judge decides); an older server sends no
    flag and nothing prints.
    """
    if any(c.get("comparison_applicable") is False and _comparison_note(c.get("comparison")) for c in cases):
        typer.secho(
            "  ⚠ Result comparison rules are not applied on a metadata-only source: the judge decides; "
            "the question text must carry anything that matters (ranked, each once, ...)",
            fg=typer.colors.YELLOW,
            err=True,
        )


def _expectation_option(expect: Optional[str]) -> Optional[dict[str, Any]]:
    """`--expect` as the API body, or None when the user did not set it.

    Exits 2 on a usage error, like every other option check in this module.
    """
    if expect is None:
        return None
    if expect not in _EXPECT_CHOICES:
        typer.secho(f"--expect must be one of {', '.join(_EXPECT_CHOICES)}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    return {"kind": expect.replace("-", "_")}


def _warn_if_expectation_ignored(case: dict[str, Any], requested: Optional[dict[str, Any]]) -> None:
    """Say on stderr when the server dropped a non-default `--expect`; stdout stays the record under --json."""
    if requested is None or requested.get("kind") == "answer":
        return
    acknowledged = case.get("expectation")
    if isinstance(acknowledged, dict) and acknowledged.get("kind") == requested.get("kind"):
        return
    typer.secho("  ⚠ This server ignores --expect: the case is scored as answerable.", fg=typer.colors.YELLOW, err=True)


def _expectation_note(expectation: Any) -> Optional[str]:
    """One phrase for a case's expectation; None for the answer default (and for older servers)."""
    if not isinstance(expectation, dict):
        return None
    if expectation.get("kind") == "cannot_answer":
        return "the agent to say the question cannot be answered"
    return None


def _path_lines(path: Any) -> list[str]:
    """What the agent did on the way to its answer, as the run page shows it; empty on older results."""
    if not isinstance(path, dict):
        return []
    parts: list[str] = []
    turns = path.get("turns")
    if turns is not None:
        parts.append(f"{turns} turn{'s' if turns != 1 else ''}")
    tables = path.get("tables_read") or []
    retrieval = path.get("retrieval") or {}
    read = f"read {len(tables)} table{'s' if len(tables) != 1 else ''}"
    if retrieval.get("recall") is not None:
        read += f" (gold recall {retrieval['recall']:.0%}, precision {retrieval['precision']:.0%})"
    parts.append(read)
    failed = path.get("failed_sql_attempts")
    if failed:
        parts.append(f"{failed} failed SQL attempt{'s' if failed != 1 else ''}")
    gaps = path.get("context_gaps") or []
    if gaps:
        parts.append(f"reported a context gap: {', '.join(str(g) for g in gaps)}")
    lines = ["Path: " + " · ".join(parts)]
    for q in (path.get("clarifications") or {}).get("questions") or []:
        if not isinstance(q, dict):
            continue
        lines.append(f"Asked: {_shorten(str(q.get('question')), _MAX_ERROR_CHARS)}")
    return lines


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
    if state == "pending_change":
        if check.get("issue_id"):
            return f"pending the change it guards: checked once issue {check['issue_id']} is resolved"
        return "pending the change it guards: checked after the next publication"
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
        help=(
            "The correct SQL for the question; executed at run time to produce the expected output. "
            "On a metadata-only source a judge compares SQL meaning instead, and every column and row "
            "the gold returns is part of the contract. Optional with --expect cannot-answer."
        ),
    ),
    gold_sql_file: Optional[Path] = typer.Option(
        None,
        "--gold-sql-file",
        help="Read the gold SQL from this file instead of --gold-sql (no shell quoting of multi-line SQL).",
    ),
    expect: Optional[str] = typer.Option(
        None,
        "--expect",
        help=(
            "What a correct response does: answer (the default; asking a clarification fails, so write "
            "the question with every detail it needs) or cannot-answer."
        ),
    ),
    compare: Optional[str] = typer.Option(
        None,
        "--compare",
        help=(
            "How results are compared on an executable source: default (1% numeric tolerance, any row "
            "order, empty result fails), or any of exact (numbers match exactly), ordered (row order "
            "matters) and allow-empty (an empty result is valid), separated by commas."
        ),
    ),
    pending_change: bool = typer.Option(
        False,
        "--pending-change",
        help=(
            "The case guards a change not published yet (an open pull request): its check waits for the "
            "next publication instead of failing on the current context."
        ),
    ),
    issue: Optional[str] = typer.Option(
        None,
        "--issue",
        help=(
            "The issue this case guards: its check waits for that issue to be resolved (the pull request "
            "mentioning it merged and published), or runs now when it already is."
        ),
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
    --expect says what a correct response does: answer (the default, so
    asking or refusing fails: a question that needs a clarification is
    written with the detail it lacks), or cannot-answer (say the context
    cannot ground the question; it needs no gold SQL, since it is scored
    on the decline alone). Cassis then runs the new case three
    times in the background, and `cassis eval list-cases` flags it if the
    attempts fail or disagree. With --pending-change (the case guards a
    change still in a pull request) those attempts wait for the next
    publication, since the current context lacks the change. Exits 0 on creation, 1 on a duplicate
    question or gold SQL that does not run, 2 on usage errors, 3 on
    transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))
    expectation = _expectation_option(expect)
    comparison = _compare_option(compare)
    declines = (expectation or {}).get("kind") == "cannot_answer"
    if gold_sql is not None and gold_sql_file is not None:
        typer.secho("Pass at most one of --gold-sql or --gold-sql-file.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    if gold_sql is None and gold_sql_file is None and not declines:
        typer.secho(
            "Pass --gold-sql or --gold-sql-file (only an --expect cannot-answer case goes without one).",
            fg=typer.colors.RED,
            err=True,
        )
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
    if gold_sql is not None and not gold_sql.strip() and declines:
        gold_sql = None
    if not question.strip() or (gold_sql is not None and not gold_sql.strip()):
        typer.secho("--question and the gold SQL must not be empty.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)

    try:
        case = post_eval_case_create(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            question=question,
            gold_sql=gold_sql,
            expectation=expectation,
            comparison=comparison,
            pending_change=pending_change,
            issue_id=issue,
        )
    except (EvalCaseExistsError, EvalCaseGoldSqlError) as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except EvalCaseDefinitionError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    _check_comparison_ack(case, comparison)
    if json_output:
        typer.echo(json.dumps(case, indent=2))
        _warn_if_expectation_ignored(case, expectation)
        _warn_if_comparison_not_applied([case])
    else:
        typer.secho(f"✓ Added eval case {case['id']}: {case['question']}", fg=typer.colors.GREEN)
        comparison_note = _comparison_note(case.get("comparison"))
        if comparison_note:
            typer.echo(f"  Comparison: {comparison_note}")
        expects = _expectation_note(case.get("expectation"))
        if expects:
            typer.echo(f"  Expects {expects}")
        _warn_if_expectation_ignored(case, expectation)
        _warn_if_comparison_not_applied([case])
        check = case.get("entry_check")
        pending = isinstance(check, dict) and check.get("state") == "pending_change"
        note = _footprint_note(case)
        if note and pending:
            typer.echo(
                f"  Its gold SQL reads what the current context lacks ({note}): expected until the change is published."
            )
        elif note:
            typer.secho(
                f"  ⚠ Its gold SQL does not fully resolve against the context ({note}); "
                "fix the SQL or add the tables to the context before relying on it.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        if pending and isinstance(check, dict) and check.get("issue_id"):
            typer.echo(f"  Pending the change: Cassis checks it once issue {check['issue_id']} is resolved.")
        elif pending:
            typer.echo("  Pending the change: Cassis checks it after the next publication.")
        elif isinstance(check, dict) and check.get("state") == "running":
            typer.echo(
                "  Checking it: 3 attempts run on the current context in the background; "
                "`cassis eval list-cases` flags it if they fail or disagree."
            )
    raise typer.Exit(EXIT_OK)


@app.command(name="replace-case")
def replace_case(
    case_id: str = typer.Argument(
        ...,
        help="Id of the eval case to replace (shown by `cassis eval list-cases`).",
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
    question: Optional[str] = typer.Option(
        None,
        "-q",
        "--question",
        help="The new question; omitted, the current one is kept.",
    ),
    gold_sql: Optional[str] = typer.Option(
        None,
        "--gold-sql",
        help="The new gold SQL; omitted (and no --gold-sql-file), the current one is kept.",
    ),
    gold_sql_file: Optional[Path] = typer.Option(
        None,
        "--gold-sql-file",
        help="Read the new gold SQL from this file instead of --gold-sql (no shell quoting of multi-line SQL).",
    ),
    no_gold_sql: bool = typer.Option(
        False,
        "--no-gold-sql",
        help="Remove the gold SQL; only a cannot-answer case may have none.",
    ),
    expect: Optional[str] = typer.Option(
        None,
        "--expect",
        help="The new expectation (answer, cannot-answer); omitted, the current one is kept.",
    ),
    compare: Optional[str] = typer.Option(
        None,
        "--compare",
        help=(
            "How results are compared on an executable source: default (1% numeric tolerance, any row "
            "order, empty result fails), or any of exact (numbers match exactly), ordered (row order "
            "matters) and allow-empty (an empty result is valid), separated by commas."
        ),
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
    json_output: bool = typer.Option(False, "--json", help="Print the replacement case as raw JSON."),
) -> None:
    """Replace an eval case's definition (question, gold SQL, expectation) with a new case.

    A definition never changes in place: the new case supersedes the old one,
    which stays read-only with its results and issue links (listed again with
    `cassis eval list-cases --include-replaced`), and the replacement gets its
    own three-attempt check. Prints what changed and the new id; the old id
    no longer runs. A definition identical to the current one creates
    nothing. Only a cannot-answer case may be left without a gold SQL.
    Exits 0 on success, 1 when the case does not exist, was already
    replaced, the question belongs to another case or the gold SQL does not
    run, 2 on usage errors (including a replacement left without a gold SQL
    that does not expect a cannot-answer), 3 on transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))
    try:
        UUID(case_id)
    except ValueError:
        typer.secho(f"CASE_ID must be a UUID, got {case_id!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    expectation = _expectation_option(expect)
    if (gold_sql is not None) + (gold_sql_file is not None) + no_gold_sql > 1:
        typer.secho("Pass at most one of --gold-sql, --gold-sql-file or --no-gold-sql.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    if gold_sql_file is not None:
        try:
            gold_sql = gold_sql_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.secho(f"Cannot read {gold_sql_file}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE) from exc
        except UnicodeDecodeError as exc:
            typer.secho(f"Cannot read {gold_sql_file}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE) from exc
    if question is None and gold_sql is None and not no_gold_sql and expectation is None and compare is None:
        typer.secho(
            "Pass --question, --gold-sql, --gold-sql-file, --no-gold-sql, --expect or --compare: nothing to change.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    if (question is not None and not question.strip()) or (gold_sql is not None and not gold_sql.strip()):
        typer.secho(
            "--question and the gold SQL must not be empty (--no-gold-sql removes the gold SQL).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    if no_gold_sql:
        # The server reads an empty gold SQL as "remove it".
        gold_sql = ""

    try:
        before = next(
            (
                c
                for c in get_eval_cases(api_url=api_url, api_key=api_key, project_id=project_id)
                if str(c.get("id")) == case_id
            ),
            None,
        )
        comparison = _compare_option(compare)
        case, created = post_eval_case_replace(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            case_id=case_id,
            question=question,
            gold_sql=gold_sql,
            expectation=expectation,
            comparison=comparison,
        )
    except (EvalCaseNotFoundError, EvalCaseReplacedError, EvalCaseGoldSqlError) as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except EvalCaseDefinitionError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    _check_comparison_ack(case, comparison)
    if json_output:
        typer.echo(json.dumps(case, indent=2))
        _warn_if_expectation_ignored(case, expectation)
        _warn_if_comparison_not_applied([case])
        raise typer.Exit(EXIT_OK)
    _warn_if_expectation_ignored(case, expectation)
    _warn_if_comparison_not_applied([case])
    if not created:
        typer.echo(f"Nothing changed: case {case_id} already has this definition.")
        raise typer.Exit(EXIT_OK)
    typer.secho(f"✓ Replaced eval case {case_id} by {case['id']}", fg=typer.colors.GREEN)
    if before is not None:
        _print_definition_diff(before, case)
    typer.echo("  The replaced case keeps its results and issue links; it no longer runs.")
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


def _print_definition_diff(before: dict[str, Any], after: dict[str, Any]) -> None:
    """What the replacement changed, field by field, as a unified diff."""
    for field, label in (("question", "Question"), ("gold_sql", "Gold SQL")):
        old, new = str(before.get(field) or ""), str(after.get(field) or "")
        if old == new:
            typer.echo(f"  {label} unchanged")
            continue
        typer.echo(f"  {label}:")
        for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1):
            if line.startswith(("---", "+++", "@@")):
                continue
            color = typer.colors.RED if line.startswith("-") else typer.colors.GREEN if line.startswith("+") else None
            typer.secho(f"    {line}", fg=color)
    if before.get("comparison") != after.get("comparison"):
        old_comparison = _comparison_note(before.get("comparison")) or "legacy comparison"
        new_comparison = _comparison_note(after.get("comparison")) or "legacy comparison"
        typer.echo(f"  Comparison: {old_comparison} → {new_comparison}")
    old_expects = _expectation_note(before.get("expectation")) or "an answer"
    new_expects = _expectation_note(after.get("expectation")) or "an answer"
    if old_expects == new_expects:
        typer.echo("  Expectation unchanged")
    else:
        typer.echo(f"  Expectation: {old_expects} → {new_expects}")


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
    include_replaced: bool = typer.Option(
        False,
        "--include-replaced",
        help="Also list the cases a replacement superseded (read-only, kept with their results).",
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
        cases = get_eval_cases(
            api_url=api_url, api_key=api_key, project_id=project_id, branch=branch, include_replaced=include_replaced
        )
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
        _warn_if_comparison_not_applied(cases)
    else:
        if not cases:
            typer.echo("No eval cases yet — add one with `cassis eval add-case`.")
        flagged = 0
        for case in cases:
            question = str(case.get("question", "")).replace("\n", " ")
            typer.echo(f"{case.get('id')}  {question}")
            comparison_note = _comparison_note(case.get("comparison"))
            if comparison_note:
                typer.echo(f"    comparison: {comparison_note}")
            expects = _expectation_note(case.get("expectation"))
            if expects:
                typer.echo(f"    expects {expects}")
            if case.get("replaced_by_case_id"):
                typer.secho(
                    f"    replaced by {case['replaced_by_case_id']}: read-only, kept with its results",
                    fg=typer.colors.BRIGHT_BLACK,
                )
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
        _warn_if_comparison_not_applied(cases)
    raise typer.Exit(EXIT_OK)


# How a flip reads to the person fixing it: which side of the evaluation moved.
_CAUSE_HINTS = {
    "judge": "the judge ruled differently on the same SQL: check the gold SQL and the question's wording",
    "generation": "the agent wrote different SQL from the same context: the context leaves room for both",
    "both": "the agent wrote different SQL and the judge also wavered",
    "unattributed": "the evidence decides neither side (a re-judge failed, or the same SQL returned different rows)",
}


def _print_comparison(comparison: dict[str, Any], run: Optional[dict[str, Any]] = None) -> None:
    """A repeat run's attempts per case: the same comparison the webapp's run page shows.

    `run` is the run record, when at hand, for the share of the suite a
    change-scoped run covered.
    """
    summary = comparison.get("summary") or {}
    repeat = comparison.get("repeat_count") or 1
    cases = comparison.get("cases") or []
    covered = _scope_note(run) or f"{summary.get('cases', len(cases))} cases"
    typer.secho(f"Repeat comparison: {repeat} attempts per case, {covered}", bold=True)
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
    # The answer time is the agent's whole turn, clarifications included; the
    # query time exists only where SQL executes, so each prints on its own.
    if health.get("avg_plan_seconds") is not None:
        parts.append(f"answer {health['avg_plan_seconds']:.1f}s avg")
    if health.get("avg_execution_seconds") is not None:
        parts.append(f"query {health['avg_execution_seconds']:.1f}s avg")
    typer.echo(" · ".join(parts))
    if "runs" in health:
        typer.echo(
            f"{health['runs']} runs; flips with unchanged context {health['flips_same_context']}, "
            f"changed context {health['flips_changed_context']}, unknown context {health['flips_unknown_context']}"
        )
        tokens, seconds = health.get("recorded_tokens"), health.get("recorded_seconds")
        typer.echo(
            f"Recorded usage: {tokens if tokens is not None else 'unknown'} tokens; "
            f"{f'{seconds:.1f}' if seconds is not None else 'unknown'} seconds. "
            "Context changes describe transitions, not their cause."
        )
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


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _runs_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One history run entry per run id, for results no server `runs[]` entry covers (older servers)."""
    entries: dict[str, dict[str, Any]] = {}
    for r in results:
        entry = entries.setdefault(
            str(r.get("run_id")), {"run_id": r.get("run_id"), "result_ids": [], "passed": 0, "total": 0}
        )
        entry["result_ids"].append(r.get("id"))
        entry["total"] += 1
        entry["passed"] += r.get("status") == _PASSED
    return list(entries.values())


def _print_history_rows(results: list[dict[str, Any]], runs: Any = None) -> None:
    """The server's runs (newest first) grouped by the frozen configuration they ran under.

    Counts come from each `runs[]` entry, so a three-attempt run reads as one
    run with three attempts, as on the webapp's case page.
    """
    by_id = {str(r.get("id")): r for r in results}
    entries = [e for e in runs if isinstance(e, dict)] if isinstance(runs, list) else []
    covered = {str(i) for e in entries for i in e.get("result_ids") or []}
    entries += _runs_from_results([r for r in results if str(r.get("id")) not in covered])
    groups: dict[str, list[tuple[dict[str, Any], list[dict[str, Any]]]]] = {}
    for entry in entries:
        rows = [by_id[str(i)] for i in entry.get("result_ids") or [] if str(i) in by_id]
        if not rows:
            continue
        key = str(rows[0].get("run_config_key") or f"run:{entry.get('run_id')}")
        groups.setdefault(key, []).append((entry, rows))
    for members in groups.values():
        first_entry, first_rows = members[0]
        run = first_entry.get("run") if isinstance(first_entry.get("run"), dict) else {}
        first = first_rows[0]
        attempts = sum(int(e.get("total") or len(rows)) for e, rows in members)
        passed = sum(int(e.get("passed") or 0) for e, _rows in members)
        header = [
            str(run.get("ontology_label") or first.get("run_ontology_label") or "Unknown context"),
            str(run.get("model") or first.get("run_model") or "?"),
            f"Cassis {run.get('app_version') or first.get('run_app_version') or '?'}",
            _plural(len(members), "run"),
        ]
        if attempts != len(members):
            header.append(_plural(attempts, "attempt"))
        header.append(f"{passed} passed")
        # Where the runs came from: one word when every run of the group
        # shares an origin, otherwise nothing (an older server sends none).
        origins = set()
        for entry, rows in members:
            entry_run = entry.get("run") if isinstance(entry.get("run"), dict) else {}
            origin = entry_run.get("origin") or rows[0].get("run_origin")
            if origin:
                origins.add(str(origin))
        if len(origins) == 1:
            origin = origins.pop()
            header.append(_ORIGIN_LABELS.get(origin, origin))
        typer.echo("")
        typer.secho(" · ".join(header), bold=True)
        if 0 < passed < attempts:
            typer.secho("  ⚠ verdicts differ on an unchanged configuration", fg=typer.colors.YELLOW)
        group_rows = [r for _entry, rows in members for r in rows]
        # A column prints only when some row of the group fills it, and the
        # header names the columns once there are more than status and date.
        columns = [(title, width) for title, width, cell in _HISTORY_COLUMNS if any(cell(r) for r in group_rows)]
        if columns:
            heading = f"    {'status':<17} {'date':<16}  " + " ".join(
                f"{title:<{width}}" if width else title for title, width in columns
            )
            typer.secho(heading.rstrip(), fg=typer.colors.BRIGHT_BLACK)
        shown = {title for title, _width in columns}
        for entry, rows in members:
            scope = _scope_note(entry.get("run"))
            for r in rows:
                _print_history_row(r, shown, scope)


def _history_state(r: dict[str, Any]) -> str:
    state = str(r.get("dependency_state") or "")
    return "alternative path" if state == "diverged" and r.get("status") == _PASSED else state


def _history_timing(r: dict[str, Any]) -> str:
    """`plan 11.4s · exec 0.3s`, the phases a result recorded; its total time on an older result; else empty."""
    phases = [
        f"{name} {r[key]:.1f}s"
        for name, key in (("plan", "plan_seconds"), ("exec", "execution_seconds"))
        if r.get(key) is not None
    ]
    if phases:
        return " · ".join(phases)
    return f"total {r['duration_seconds']:.1f}s" if r.get("duration_seconds") is not None else ""


# The optional columns of a history row: header, width (0 for the last, unpadded), cell.
_HISTORY_COLUMNS = (
    ("judge divergence", 16, lambda r: str(r.get("judge_divergence") or "")),
    ("footprint", 17, _history_state),
    ("timing", 0, _history_timing),
)


def _print_history_row(r: dict[str, Any], columns: "set[str]", scope: Optional[str] = None) -> None:
    status = str(r.get("status", "?"))
    icon = "✓" if status == _PASSED else "✗"
    cells = [f"{cell(r):<{width}}" if width else cell(r) for title, width, cell in _HISTORY_COLUMNS if title in columns]
    line = f"  {icon} {status:<17} {_format_when(str(r.get('created_at') or ''))}  " + " ".join(cells)
    line = line.rstrip()
    if r.get("attempt"):
        line += f"  attempt {r['attempt']}"
    if scope:
        line += f"  · run covered {scope}"
    typer.secho(line, fg=_STATUS_COLORS.get(status, typer.colors.WHITE))
    if r.get("error"):
        typer.echo(f"      {_shorten(str(r['error']), _MAX_ERROR_CHARS)}")
    note = _application_note(r)
    if note:
        typer.echo(f"      {note}")
    rule = _rule_note(r.get("comparison_rule"), status == _PASSED)
    if rule:
        typer.echo(f"      {rule}")


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
    if record.get("replaced_by_case_id"):
        typer.secho(
            f"  Replaced by {record['replaced_by_case_id']}: read-only, no longer in the suite.",
            fg=typer.colors.YELLOW,
        )
    if record.get("supersedes_case_id"):
        typer.echo(f"  Replaces {record['supersedes_case_id']}, whose results stay on that case.")
    for issue in record.get("issues") or []:
        typer.echo(f"  Issue {issue['id']} ({issue['status']}): {issue['title']}")
    rules = _comparison_note(record.get("comparison"))
    if rules:
        typer.echo(f"  Comparison: {rules}")
    _print_health(record.get("health") or {})
    results = record.get("results") or []
    if isinstance(results, list) and results:
        _print_history_rows(results, record.get("runs"))
    raise typer.Exit(EXIT_OK)


@app.command(name="runs")
def list_runs(
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
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="How many runs to list, newest first."),
    json_output: bool = typer.Option(False, "--json", help="Print the server's run list as raw JSON."),
) -> None:
    """List the project's eval runs, newest first.

    Each run prints its id, status, the context it scored, where it came from
    (CLI, CI, scheduled, Evals page, ...), the model and how many results
    passed (attempts on a repeat run, and the share of the suite a scoped run
    covered); a run the spending protection did not start says why. The id is
    what `cassis eval show` takes. Exits 0 on success, 2 on usage errors,
    3 on transport/API errors (including a server too old to list runs).
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))
    try:
        records = get_eval_runs(api_url=api_url, api_key=api_key, project_id=project_id, limit=limit)
    except ApiError as exc:  # covers AuthError
        raise api_failure(exc) from exc

    if json_output:
        typer.echo(json.dumps({"runs": records}, indent=2))
        raise typer.Exit(EXIT_OK)
    if not records:
        typer.echo("No eval runs yet — start one with `cassis eval run`.")
        raise typer.Exit(EXIT_OK)
    for record in records:
        status = str(record.get("status") or "?")
        parts = [str(record.get("ontology_label") or "Unknown context")]
        origin = record.get("origin")
        if origin:
            parts.append(_ORIGIN_LABELS.get(str(origin), str(origin)))
        if record.get("model"):
            parts.append(str(record["model"]))
        summary = record.get("summary") or {}
        if isinstance(summary, dict) and summary.get("total") is not None:
            parts.append(_pass_count(record, summary["total"], summary.get("passed", 0)))
        scope = _scope_note(record)
        if scope:
            parts.append(scope)
        when = _format_when(str(record.get("started_at") or "")) if record.get("started_at") else ""
        typer.secho(
            f"{record.get('run_id')}  {status:<10} {when:<16}  {' · '.join(parts)}",
            fg=_RUN_STATUS_COLORS.get(status),
        )
        skipped = _skip_reason(record.get("skipped") or (summary.get("skipped") if isinstance(summary, dict) else None))
        if skipped:
            typer.echo(f"    Not started: {skipped}")
    raise typer.Exit(EXIT_OK)


@app.command(name="show")
def show(
    run_id: str = typer.Argument(
        ...,
        help="Id of the eval run (printed by `cassis eval run`, listed by `cassis eval runs`).",
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
    json_output: bool = typer.Option(False, "--json", help="Print the run, its results and comparison as raw JSON."),
) -> None:
    """Print one eval run by id: its results, or how a repeat run's attempts compare.

    The read side of `cassis eval run --no-wait` and `cassis eval runs`. A
    single-attempt run prints its results and its verdict against the base
    run; a repeat run (`--repeat N`) prints, case by case, how many attempts
    passed and what moved between them: `generation` (the agent wrote
    different SQL), `judge` (the judge ruled differently on the same SQL),
    `both`, or `unattributed`. Exits 0 on success, 1 when the run does not
    exist in the project, 2 on usage errors, 3 on transport/API errors.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / Path(base_path))
    try:
        UUID(run_id)
    except ValueError:
        typer.secho(f"RUN_ID must be a UUID, got {run_id!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    try:
        run_record = get_eval_run(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
        repeated = bool(run_record.get("repeat_count"))
        completed = run_record.get("status") == "completed"
        comparison = (
            get_eval_run_comparison(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
            if repeated and completed
            else None
        )
        results = (
            get_eval_run_results(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
            if not repeated
            else []
        )
    except EvalRunNotFoundError as exc:
        typer.secho(
            f"No eval run {run_id} in this project (wrong id, or another project's run?).",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except ApiError as exc:  # covers AuthError and EvalCompareUnsupportedError
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    if json_output:
        record: dict[str, Any] = {"run": run_record, "results": results}
        if comparison is not None:
            record["comparison"] = comparison
        typer.echo(json.dumps(record, indent=2))
        raise typer.Exit(EXIT_OK)
    if run_record.get("status") == "running":
        typer.echo("The run is still in progress; `cassis eval show` prints it once it completes.")
        raise typer.Exit(EXIT_OK)
    if comparison is not None:
        _print_comparison(comparison, run_record)
    else:
        _print_results_table(results)
        typer.echo("")
        _print_summary(run_record)
        if isinstance(run_record.get("verdict"), dict):
            _print_verdict(run_record["verdict"], results)
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
    changed: bool = typer.Option(
        False, "--changed", help="Run only cases affected by context changes; conservative when evidence is incomplete."
    ),
    base: Optional[str] = typer.Option(
        None,
        "--base",
        help="Published context version UUID for change selection (default: branch base or published version).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview case selection without starting an eval."),
    verbose: bool = typer.Option(
        False, "--verbose", help="List every case the selection skips, instead of a count per reason."
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
    command prints how the attempts compare per case (`cassis eval show`);
    a repeat run is diagnostic, so it files no issue and is not the run the
    webapp shows before publishing. With --changed, only the cases a context
    change affects run: the selection prints each selected case with its
    reason and a count per skip reason (--verbose lists every skipped case),
    and the summary says how much of the suite the run covered. Exits 0 when
    the run completes with every case passed (with --repeat: no case failed
    every attempt, since a flip is the noise being measured), 1 on a
    regression or an invalid tree, 2 on usage errors (including a --branch or
    --base the project does not have), 3 on transport errors or --timeout, 4
    when the run reached no verdict (it failed or was cancelled on Cassis's
    side, or the cases that did not pass errored instead). A regression is a
    case whose answer fails now and passed on the base run; a case already
    failing there, or pending the change it guards, is listed and does not
    fail the command. Against a server that does not split failures, every
    failed answer exits 1.

    Failed cases (and, with --case, passed ones) print the generated SQL as
    the agent wrote it. Row values are never printed (they are in --json), but
    an agent that read your data while planning can carry a value it saw into
    a SQL literal — so treat this output as sensitive as the queries themselves.
    """
    api_key = require_api_key(api_key)
    if base is not None:
        try:
            UUID(base)
        except ValueError:
            typer.secho("--base must be a published context version UUID.", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE)
        if not changed:
            typer.secho(
                "--base needs --changed: the base is what the change selection compares the files against.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(EXIT_USAGE)
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
    # The run's provenance on the Evals page: the commit the files come from,
    # and in CI the pipeline and the merge or pull request.
    ci_pipeline, ci_merge_request = _ci_provenance()

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
            git_commit=_git_commit(path) if branch is None else None,
            ci_pipeline=ci_pipeline,
            ci_merge_request=ci_merge_request,
            **(
                {"changed": changed, "base_version_id": base, "preview": dry_run}
                if changed or base is not None or dry_run
                else {}
            ),
        )
    except EvalStartValidationError as exc:
        _print_validation_failure(exc.detail, base_path)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except EvalBaseVersionNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc
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

    if dry_run:
        # The preview lists the current cases; a requested id it lacks is not one.
        # The real run refuses when none matches, so the preview does too.
        listed = {str(row.get("case_id")) for row in run_record.get("cases") or [] if isinstance(row, dict)}
        if case and not listed & set(case):
            typer.secho(f"No current eval case matches --case: {', '.join(case)}.", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE)
        if json_output:
            typer.echo(json.dumps(run_record, indent=2))
        else:
            _print_selection(run_record, verbose=verbose)
        raise typer.Exit(EXIT_OK)
    _warn_if_spend_window_spent(api_url=api_url, api_key=api_key, project_id=project_id)
    selection = run_record.get("selection")
    if changed and (not isinstance(selection, dict) or selection.get("mode") != "changed"):
        typer.secho(
            "This server did not acknowledge --changed; the full requested suite is running.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if not json_output and isinstance(selection, dict):
        _print_selection(selection, verbose=verbose)
    run_id = str(run_record["run_id"])
    total = run_record.get("total_cases", 0)
    execution_config = run_record.get("execution_config")
    if not json_output and isinstance(execution_config, dict):
        typer.echo(f"SQL dialect: {execution_config.get('dialect', 'unknown')}")
        typer.echo(f"Query guidance: {execution_config.get('manual_query_hint') or 'none'}")
    target = _run_target(run_record, branch=branch)
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
            typer.echo(f"Eval run {run_id} started: {_plural(total, 'case')}{target}.")
            typer.echo(f"Follow it at: {run_url}")
        raise typer.Exit(EXIT_OK)

    # To stderr under --json so `cassis eval run --json | jq` gets only the record.
    started = f"Eval run {run_id} started: {_plural(total, 'case')}{target}"
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
            _print_comparison(comparison, final_run)
        else:
            _print_results_table(results, passed_evidence=bool(case))
        typer.echo("")
        _print_summary(final_run)
        typer.echo(f"View details: {run_url}")

    verdict = final_run.get("verdict") if isinstance(final_run.get("verdict"), dict) else None
    if not json_output and verdict is not None:
        _print_verdict(verdict, results)
    if final_run.get("status") == "completed" and _suite_passed(results, repeated=bool(repeat_count)):
        raise typer.Exit(EXIT_OK)
    if verdict is not None and final_run.get("status") == "completed":
        # The server split the failures: only a regression against the base
        # fails CI; a case already failing there, or still pending, does not.
        if verdict.get("regressions"):
            raise typer.Exit(EXIT_VALIDATION_FAILED)
        excused = {str(c) for c in [*(verdict.get("already_failing") or []), *(verdict.get("pending") or [])]}
        if not _errored_cases(results, excused=excused):
            raise typer.Exit(EXIT_OK)
    elif _answer_failed(results, repeated=bool(repeat_count)):
        raise typer.Exit(EXIT_VALIDATION_FAILED)
    if not json_output:
        typer.secho(
            "No verdict: the run did not complete or its cases errored; nothing here says the context regressed.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    raise typer.Exit(EXIT_NO_VERDICT)


def _run_target(run_record: dict[str, Any], *, branch: Optional[str] = None) -> str:
    """What a run scores and with which model, to follow its case count.

    ` · label bt-s2b · unpublished context from local files · <model>` for a
    files run, ` · branch <name>` for a `--branch` run, ` · v3` when the run
    pinned a published version. The server resolves the label; a null one
    means a server too old to say, so it is reported as unknown rather than
    guessed.
    """
    label = run_record.get("ontology_label")
    number = run_record.get("ontology_version_number")
    if not label:
        target = "; context: unknown (server too old to say)"
    elif number is not None:
        target = (f" · label {label}" if str(label) != f"v{number}" else "") + f" · v{number}"
    elif branch is not None:
        target = f" · branch {label}"
    else:
        target = f" · label {label} · unpublished context from local files"
    model = run_record.get("model")
    return f"{target} · {model}" if model else target


# Statuses that are a verdict on the answer, as opposed to an execution error
# (the agent, the judge or the warehouse failed) or a broken gold SQL.
_VERDICT_FAILURES = frozenset({"failed", "missing_concept", "plan_unexecutable"})


def _errored_cases(results: "list[dict[str, Any]]", *, excused: "set[str]") -> "set[str]":
    """The cases that never passed and errored instead of failing, outside `excused`."""
    by_case: dict[str, list[str]] = {}
    for r in results:
        by_case.setdefault(str(r.get("test_case_id")), []).append(str(r.get("status")))
    return {
        case_id
        for case_id, statuses in by_case.items()
        if case_id not in excused and _PASSED not in statuses and not any(s in _VERDICT_FAILURES for s in statuses)
    }


def _print_verdict(verdict: dict[str, Any], results: "list[dict[str, Any]]") -> None:
    """Say which failing cases the change did not cause: already failing on the base, or pending."""
    questions = {str(r.get("test_case_id")): " ".join(str(r.get("question") or "").split()) for r in results}
    base = f"v{verdict['base_version_number']}" if verdict.get("base_version_number") else "the base run"
    already = [str(c) for c in verdict.get("already_failing") or []]
    pending = [str(c) for c in verdict.get("pending") or []]
    if already:
        typer.echo(f"Already failing on {base}, not counted as regressions:")
        for case_id in already:
            typer.echo(f"  {case_id}  {questions.get(case_id, '')}")
    if pending:
        typer.echo("Pending the change they guard, not counted:")
        for case_id in pending:
            typer.echo(f"  {case_id}  {questions.get(case_id, '')}")
    if verdict.get("regressions"):
        typer.secho(f"Regressions against {base}: {len(verdict['regressions'])}", fg=typer.colors.RED)


def _answer_failed(results: "list[dict[str, Any]]", *, repeated: bool) -> bool:
    """Whether a case failed on its answer: on a repeat run, a case that never passed and failed at least once."""
    if not repeated:
        return any(r.get("status") in _VERDICT_FAILURES for r in results)
    by_case: dict[str, list[str]] = {}
    for r in results:
        by_case.setdefault(str(r.get("test_case_id") or r.get("question")), []).append(str(r.get("status")))
    return any(
        _PASSED not in statuses and any(s in _VERDICT_FAILURES for s in statuses) for statuses in by_case.values()
    )


def _suite_passed(results: "list[dict[str, Any]]", *, repeated: bool) -> bool:
    """Whether a completed run passes: every case passed, or on a repeat run, no case failed every attempt."""
    if not repeated:
        return all(r.get("status") == _PASSED for r in results)
    passed_once: dict[str, bool] = {}
    for r in results:
        key = str(r.get("test_case_id") or r.get("question"))
        passed_once[key] = passed_once.get(key, False) or r.get("status") == _PASSED
    return all(passed_once.values())


def _print_selection(selection: dict, *, verbose: bool = False) -> None:
    """The selected cases with their reasons, then one line per skip reason (every skipped case with `verbose`)."""
    cases = [row for row in selection.get("cases") or [] if isinstance(row, dict)]
    selected = sum(bool(row.get("selected")) for row in cases)
    mode = selection.get("mode", "unknown")
    if mode == "changed":
        base = selection.get("base_version_id")
        number = selection.get("base_version_number")
        published = str(selection.get("base_published_at") or "")[:10]
        if number is not None:
            since = f"since v{number}" + (f" (published {published})" if published else "")
        elif base:
            since = f"since base {base}"  # an older server names the base by id only
        else:
            since = "with no base version to compare against"
        typer.echo(f"Selection: {selected}/{len(cases)} cases affected {since}")
    else:
        typer.echo(f"Selection: {selected}/{len(cases)} cases; mode: {mode}")
    if selection.get("shadow"):
        typer.echo("Full suite: affected-case selection is measured in shadow.")
    skipped: dict[str, int] = {}
    for row in cases:
        if row.get("selected"):
            typer.echo(f"  Run {row.get('case_id')}: {row.get('question')} — {row.get('reason')}")
        elif verbose:
            typer.echo(f"  Skip {row.get('case_id')}: {row.get('question')} — {row.get('reason')}")
        else:
            reason = str(row.get("reason") or "No reason recorded")
            skipped[reason] = skipped.get(reason, 0) + 1
    for reason, count in sorted(skipped.items(), key=lambda item: -item[1]):
        typer.echo(f"  {count} skipped: {reason}")
    _print_uncovered(selection, selected=selected)


# How many uncovered tables the selection names before counting the rest.
_UNCOVERED_SHOWN = 10
_KEEP_A_GUARD_HINT = (
    "Keep a guard: add an eval case for the question before you open the pull request "
    '(`cassis context test -q "..." --save-case`, or `cassis eval add-case --pending-change`); '
    "Cassis checks it once the change is published."
)


def _print_uncovered(selection: dict, *, selected: int) -> None:
    """Say which changes no case reads, and that a change-scoped run selecting nothing tested nothing."""
    uncovered = selection.get("uncovered") if isinstance(selection.get("uncovered"), dict) else {}
    tables = [str(t) for t in uncovered.get("tables") or []]
    domains = [str(d) for d in uncovered.get("domains") or []]
    if selection.get("mode") == "changed" and selected == 0:
        typer.secho("  ⚠ No case covers this change: nothing is tested.", fg=typer.colors.YELLOW)
    if tables:
        more = f" and {len(tables) - _UNCOVERED_SHOWN} more" if len(tables) > _UNCOVERED_SHOWN else ""
        typer.secho(f"  Read by no case: {', '.join(tables[:_UNCOVERED_SHOWN])}{more}", fg=typer.colors.YELLOW)
    for domain in domains:
        typer.secho(f"  New domain with no case: {domain}", fg=typer.colors.YELLOW)
    if tables or domains or (selection.get("mode") == "changed" and selected == 0):
        typer.echo("  " + _KEEP_A_GUARD_HINT)


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


@app.command("health")
def health(
    path: Path = typer.Argument(Path("."), help="Repository checkout root."),
    project_id: Optional[str] = typer.Option(None, "--project", envvar="CASSIS_PROJECT_ID"),
    api_key: Optional[str] = typer.Option(None, "--api-key", envvar="CASSIS_API_KEY"),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url", envvar="CASSIS_API_URL"),
    base_path: str = typer.Option(DEFAULT_BASE_PATH, "--base-path", envvar="CASSIS_BASE_PATH"),
    attention_only: bool = typer.Option(False, "--attention", help="Only cases needing review."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read case health and recommendations without running evals or spending credits."""
    key = require_api_key(api_key)
    project = resolve_project_id(project_id, path / Path(base_path))
    try:
        result = get_eval_health(
            api_url=api_url,
            api_key=key,
            project_id=project,
        )
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    if attention_only:
        result = {**result, "cases": [case for case in result["cases"] if case["needs_attention"]]}
    if json_output:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(f"{result['attention_count']} cases need attention; latest {result['history_limit']} results per case.")
    for case in result["cases"]:
        typer.echo(f"\n{case['case_id']}  {case['question']}")
        typer.echo("  " + ", ".join(label.replace("_", " ") for label in case["labels"]))
        _print_health(case["health"])
        typer.echo("  " + case["recommendation"])
