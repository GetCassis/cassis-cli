"""`cassis context` subcommands (also registered, hidden, as `cassis ontology`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer
from cassis_cli.api import (
    DEFAULT_API_URL,
    ApiError,
    AuthError,
    EvalCaseExistsError,
    OntologyTestValidationError,
    UploadValidationError,
    get_eval_cases,
    get_ontology_export,
    post_eval_case_create,
    post_eval_case_replace,
    post_ontology_check,
    post_ontology_fmt,
    post_ontology_import,
    post_ontology_test,
)
from cassis_cli.common import (
    DEFAULT_BASE_PATH,
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
)
from cassis_cli.common import collect_tree as _collect_tree
from cassis_cli.common import is_legacy_domain_file as _is_legacy_domain_file
from cassis_cli.common import require_api_key as _require_api_key
from cassis_cli.common import require_committed_tree as _require_committed_tree
from cassis_cli.common import resolve_project_id as _resolve_project_id
from cassis_cli.common import sync_ontology_tree as _sync_ontology_tree
from cassis_cli.guide import DOCTRINE_VERSION, GUIDE_FILENAME, guide_status, refresh_guide

app = typer.Typer(
    no_args_is_help=True,
    help="Work on your context files: check, format and test them, pull from and upload to a Cassis project.",
)


def _warn_newer_guide(base_path: str) -> None:
    """Tell the user their checkout's AGENTS.md outruns this CLI's doctrine.

    A newer-stamped guide (written by a newer CLI or by the Cassis server) is
    never overwritten — the fix is upgrading the CLI, so say so and move on.
    """
    typer.secho(
        f"notice: {base_path}/{GUIDE_FILENAME} carries a newer Cassis doctrine than this CLI "
        f"(v{DOCTRINE_VERSION}) — leaving it in place; run `pip install -U cassis-cli` to update.",
        fg=typer.colors.YELLOW,
        err=True,
    )


@app.command()
def check(
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
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON response."),
) -> None:
    """Validate the context files in a repository checkout.

    Runs the same checks as the Cassis GitHub PR check (YAML parsing,
    round-trip, import validation), then prints advisory quality warnings
    (unassigned tables, broken joins/metrics, missing descriptions) for a
    tree that parsed. When the checkout is bound to a project
    (``project.yml``, ``--project``, or ``CASSIS_PROJECT_ID``), the tree is
    additionally cross-checked against the project's source schema (unmatched
    references print as warnings) and against its eval suite (cases whose
    gold SQL reads tables these files lack print as warnings). Warnings never
    fail the check or change the exit code. Exits 0 when valid, 1 when validation fails, 2 on usage
    errors, 3 on transport/API errors.
    """
    api_key = _require_api_key(api_key)
    files, base_path = _collect_tree(path, base_path)

    # Soft resolution: an unbound checkout is not an error — the check falls
    # back to the project-less route (no schema reference stage).
    project_id = _resolve_project_id(project_id, path / base_path, optional=True, quiet=json_output)
    if not project_id and not json_output:
        typer.secho(
            "No project binding: schema reference checks skipped (bind with `cassis context pull` or --project).",
            fg=typer.colors.CYAN,
            err=True,
        )

    try:
        result = post_ontology_check(api_url=api_url, api_key=api_key, files=files, project_id=project_id)
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    warnings = result.get("warnings") or []
    ref_warnings = [w for w in warnings if w.get("stage") == "references"]
    eval_warnings = [w for w in warnings if w.get("stage") == "evals"]
    # The evals stage carries two kinds: cases that do not resolve, and changes no case reads.
    coverage_warnings = [w for w in eval_warnings if str(w.get("message", "")).startswith("[eval_coverage]")]
    eval_warnings = [w for w in eval_warnings if w not in coverage_warnings]
    # Everything else is the quality audit; bucketing by "not one of the
    # stages named here" keeps a warning stage this CLI predates visible
    # instead of silently dropped.
    audit_warnings = [w for w in warnings if w.get("stage") not in ("references", "evals")]
    if json_output:
        typer.echo(json.dumps(result, indent=2))
    elif result["passed"]:
        typer.secho(f"✓ {result['summary']}", fg=typer.colors.GREEN)
        # Disambiguate silence: "no warnings" must never read as "references
        # verified" when the stage didn't run.
        if project_id and result.get("references_checked") and not ref_warnings:
            typer.secho("✓ Schema references resolve against the source schema.", fg=typer.colors.GREEN)
        elif project_id and not result.get("references_checked"):
            typer.secho(
                "Schema reference check skipped — the project has no source schema yet.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        if project_id and result.get("eval_cases_checked") and not eval_warnings:
            typer.secho("✓ Eval cases resolve against this context.", fg=typer.colors.GREEN)
    else:
        typer.secho(result["title"], fg=typer.colors.RED, bold=True)
        typer.echo(result["summary"])
        for finding in result["findings"]:
            location = f"{base_path}/{finding.get('path')}: " if finding.get("path") else ""
            typer.echo(f"  {location}{finding.get('message', '')} ({finding.get('stage', '?')})")

    if ref_warnings and not json_output:
        typer.secho(
            f"{len(ref_warnings)} schema reference warning(s) — advisory, expected if the objects "
            "haven't been built or synced yet:",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        for warning in ref_warnings:
            typer.secho(f"  {warning.get('message', '')}", fg=typer.colors.YELLOW)
    if eval_warnings and not json_output:
        typer.secho(
            f"{len(eval_warnings)} eval case(s) whose gold SQL does not fully resolve against this context "
            "— advisory; they cannot be trusted to score a change until the SQL or the context is fixed:",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        for warning in eval_warnings:
            typer.secho(f"  {warning.get('message', '')}", fg=typer.colors.YELLOW)
    if coverage_warnings and not json_output:
        typer.secho(
            f"{len(coverage_warnings)} change(s) no eval case guards — advisory:",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        for warning in coverage_warnings:
            typer.secho(f"  {warning.get('message', '')}", fg=typer.colors.YELLOW)
    if audit_warnings and not json_output:
        # Same wording as `context test` so the two commands read alike.
        typer.secho(
            f"{len(audit_warnings)} context quality warning(s), advisory, never fail the check:",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        for warning in audit_warnings:
            typer.secho(f"  {warning.get('message', '')}", fg=typer.colors.YELLOW)

    raise typer.Exit(EXIT_OK if result["passed"] else EXIT_VALIDATION_FAILED)


@app.command()
def pull(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (the directory containing the context export path).",
    ),
    project_id: Optional[str] = typer.Option(
        None,
        "--project",
        envvar="CASSIS_PROJECT_ID",
        help="Source Cassis project ID (UUID). Defaults to the id in <base-path>/project.yml.",
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
    prune: bool = typer.Option(
        True,
        "--prune/--no-prune",
        help=(
            "Delete local context files that no longer exist in the project's context "
            "(default: prune). Only files that are tracked and unmodified in git are "
            "deleted; untracked or locally modified files are always kept and reported."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Print a JSON summary of written/deleted files."),
) -> None:
    """Download the project's unpublished context into a repository checkout.

    Writes the context tree under the export path (full sync: files are
    overwritten and, unless --no-prune, stale local context files are deleted,
    so the checkout ends up matching the project exactly). Pruning never touches
    files git could not restore: untracked or locally modified files are kept
    and listed, and every deleted path is named. Review the changes with
    git diff before committing. Exits 0 on success, 2 on usage errors, 3 on
    transport/API errors.
    """
    api_key = _require_api_key(api_key)
    base_path = base_path.strip().strip("/")
    if not base_path:
        typer.secho("--base-path must not be empty.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    project_id = _resolve_project_id(project_id, path / Path(base_path))

    try:
        files = get_ontology_export(api_url=api_url, api_key=api_key, project_id=project_id)
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    ontology_dir = (path / Path(base_path)).resolve()

    def announce_deletions(to_delete: "list[str]") -> None:
        typer.echo(f"Deleting {len(to_delete)} stale context file(s):")
        for rel in to_delete:
            typer.echo(f"  {base_path}/{rel}")

    written, deleted, kept = _sync_ontology_tree(
        ontology_dir, files, prune=prune, before_delete=None if json_output else announce_deletions
    )

    # Managed modeling guide: refresh AGENTS.md so a repo-aware agent loads
    # current Cassis doctrine. Not part of the ontology tree (YAML-only), so it
    # was neither pulled above nor pruned.
    try:
        guide_state = refresh_guide(ontology_dir)
    except OSError as exc:
        typer.secho(f"Cannot write {ontology_dir / GUIDE_FILENAME}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc
    guide_written = guide_state in ("stale", "missing")
    if guide_state == "newer":
        _warn_newer_guide(base_path)

    if json_output:
        typer.echo(
            json.dumps(
                {"written": written, "deleted": deleted, "kept": kept, "guide_written": guide_written},
                indent=2,
            )
        )
    else:
        summary = f"✓ Pulled {len(written)} files into {ontology_dir}"
        if deleted:
            summary += f" ({len(deleted)} stale files deleted, listed above)"
        if guide_written:
            summary += f"; wrote {base_path}/{GUIDE_FILENAME}"
        typer.secho(f"{summary}.", fg=typer.colors.GREEN)
        if kept:
            typer.secho(
                f"Kept {len(kept)} local file(s) not in the project context "
                "(only files tracked and unmodified in git are pruned):",
                fg=typer.colors.YELLOW,
            )
            for entry in kept:
                typer.secho(f"  {base_path}/{entry['path']} ({entry['reason']})", fg=typer.colors.YELLOW)
            typer.secho(
                "  Commit them if they are intentional, or delete them manually.",
                fg=typer.colors.YELLOW,
            )
        migrated = sum(1 for rel in deleted if _is_legacy_domain_file(rel))
        if migrated:
            typer.secho(
                f"  Migrated {migrated} domain(s) to Markdown README.md files; "
                "the old _domain.yml / _project.yml were removed. Review the diff before committing.",
                fg=typer.colors.YELLOW,
            )
    raise typer.Exit(EXIT_OK)


@app.command()
def upload(
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
    publish: bool = typer.Option(
        True,
        "--publish/--no-publish",
        help="Publish the uploaded context immediately as a new version (default: publish).",
    ),
    label: Optional[str] = typer.Option(None, "--label", help="Label for the published version."),
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON response."),
) -> None:
    """Upload the context files in a repository checkout to a Cassis project.

    Replaces the project's unpublished context with the local tree (full
    replace) and, unless --no-publish is passed, publishes it immediately as a
    new version. The checkout must be a git repository whose context files
    match HEAD: the published version records that commit. Exits 0 on success,
    1 when the tree fails validation, 2 on usage errors (including uncommitted
    changes under the base path), 3 on transport/API errors.
    """
    api_key = _require_api_key(api_key)
    files, base_path = _collect_tree(path, base_path)
    head = _require_committed_tree(path, base_path, files)
    project_id = _resolve_project_id(project_id, path / Path(base_path))

    try:
        result = post_ontology_import(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            files=files,
            publish=publish,
            git_commit_sha=head,
            label=label,
        )
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except UploadValidationError as exc:
        typer.secho("Context upload rejected:", fg=typer.colors.RED, bold=True, err=True)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    if json_output:
        typer.echo(json.dumps(result, indent=2))
    else:
        counts = (
            f"{result['table_count']} tables, {result['domain_count']} domains, "
            f"{result['join_count']} joins, {result['metric_count']} metrics"
        )
        version = result.get("published_version")
        if version is not None and result.get("unchanged"):
            typer.secho(
                f"✓ Context uploaded ({counts}). No change: v{version} stays published, "
                f"and now records commit {head[:9]}.",
                fg=typer.colors.GREEN,
            )
        elif version is not None:
            typer.secho(f"✓ Context uploaded and published as v{version} ({counts}).", fg=typer.colors.GREEN)
        else:
            typer.secho(
                f"✓ Context uploaded ({counts}). Not published: it is now the project's unpublished context.",
                fg=typer.colors.GREEN,
            )
    raise typer.Exit(EXIT_OK)


@app.command()
def fmt(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (the directory containing the context export path).",
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
    check_only: bool = typer.Option(
        False,
        "--check",
        help="Do not write anything; exit 1 if any file would change.",
    ),
) -> None:
    """Rewrite the context files in canonical form (think `black` for your context).

    Uses the exact serializer the validation round-trip compares against, so a
    formatted tree cannot fail that stage of `cassis context check` or the
    GitHub PR check. Unknown fields are dropped by canonicalization, so review
    the diff before committing; duplicate YAML keys are rejected (the
    formatter cannot know which value was intended). Exits 0 on success
    (1 with --check when changes are needed), 1 when the tree cannot be
    parsed, 2 on usage errors, 3 on transport/API errors.
    """
    api_key = _require_api_key(api_key)
    files, base_path = _collect_tree(path, base_path)
    ontology_dir = path / Path(base_path)

    try:
        result = post_ontology_fmt(api_url=api_url, api_key=api_key, files=files)
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    if not result["ok"]:
        typer.secho("Cannot format: the tree does not parse.", fg=typer.colors.RED, bold=True, err=True)
        for finding in result["findings"]:
            location = f"{base_path}/{finding.get('path')}: " if finding.get("path") else ""
            typer.echo(f"  {location}{finding.get('message', '')}", err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED)

    changed = result["changed_paths"]
    removed = result["removed_paths"]
    # The managed AGENTS.md guide is canonicalized alongside the ontology tree
    # (it isn't in the tree, so the server round-trip above never sees it).
    # A guide stamped with a NEWER doctrine than this CLI carries is left
    # alone and does not fail --check: the repo is fine, the CLI is old.
    guide_state = guide_status(ontology_dir)
    guide_stale = guide_state in ("stale", "missing")
    if guide_state == "newer":
        _warn_newer_guide(base_path)

    if not changed and not removed and not guide_stale:
        typer.secho(f"✓ {len(files)} file(s) already canonical.", fg=typer.colors.GREEN)
        raise typer.Exit(EXIT_OK)

    if check_only:
        for p in changed:
            typer.echo(f"would rewrite {base_path}/{p}")
        for p in removed:
            typer.echo(f"would remove {base_path}/{p}")
        if guide_stale:
            typer.echo(f"would rewrite {base_path}/{GUIDE_FILENAME}")
        raise typer.Exit(EXIT_VALIDATION_FAILED)

    for p in changed:
        target = ontology_dir / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result["files"][p], encoding="utf-8")
        typer.echo(f"rewrote {base_path}/{p}")
    for p in removed:
        (ontology_dir / p).unlink(missing_ok=True)
        typer.echo(f"removed {base_path}/{p}")
    if guide_stale:
        try:
            refresh_guide(ontology_dir)
        except OSError as exc:
            typer.secho(f"Cannot write {ontology_dir / GUIDE_FILENAME}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE) from exc
        typer.echo(f"rewrote {base_path}/{GUIDE_FILENAME}")

    if changed or removed:
        typer.secho(
            f"Formatted {len(changed)} file(s)"
            + (f", removed {len(removed)}" if removed else "")
            + ". Review the diff: fields Cassis does not recognize are dropped.",
            fg=typer.colors.YELLOW,
        )
        migrated = sum(1 for p in removed if _is_legacy_domain_file(p))
        if migrated:
            typer.secho(
                f"Migrated {migrated} domain(s) to Markdown README.md files; "
                "the old _domain.yml / _project.yml were removed.",
                fg=typer.colors.YELLOW,
            )
    else:
        # Only the guide was refreshed — the "rewrote ..." line above already said so.
        typer.secho(f"✓ {len(files)} file(s) already canonical.", fg=typer.colors.GREEN)
    raise typer.Exit(EXIT_OK)


@app.command()
def test(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (the directory containing the context export path).",
    ),
    question: List[str] = typer.Option(
        ...,
        "--question",
        "-q",
        help="Natural-language question to probe (repeat for several).",
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
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON outcomes."),
    save_case: bool = typer.Option(
        False,
        "--save-case",
        help=(
            "Add each successful probe to the project's eval suite as a case: its question and the SQL it "
            "produced, as `cassis eval add-case --pending-change` would (checked after your change is "
            "published). A probe that failed, asked a clarification or wrote no SQL is not saved."
        ),
    ),
    issue: Optional[str] = typer.Option(
        None,
        "--issue",
        help="With --save-case: the issue the saved cases guard; their check waits for its resolution.",
    ),
) -> None:
    """Run questions through the text-to-SQL agent using your local context files.

    The behavioral probe: checks that a change actually WORKS (e.g. that a
    new column gets picked),
    where `cassis eval run` only checks for regressions on existing gold
    cases. Each question is one full agent run (expect ~30-90s each); nothing
    is persisted server-side. The outcome is informational, not a gate: read
    the SQL and answer, don't wire the exit code into CI verdicts. Exits 0
    when every probe completed (whatever its outcome), 1 when the tree is
    invalid or a probe failed, 2 on usage errors, 3 on transport errors.
    """
    api_key = _require_api_key(api_key)
    files, base_path = _collect_tree(path, base_path)
    project_id = _resolve_project_id(project_id, path / Path(base_path))

    outcomes: "list[dict]" = []
    failed = False
    for q in question:
        try:
            outcome = post_ontology_test(
                api_url=api_url, api_key=api_key, project_id=project_id, files=files, question=q
            )
        except OntologyTestValidationError as exc:
            _print_test_validation_failure(exc.detail, base_path)
            raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
        except AuthError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_TRANSPORT) from exc
        except ApiError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_TRANSPORT) from exc
        outcomes.append(outcome)
        if not json_output:
            _print_test_outcome(q, outcome)
            if not save_case and _probe_succeeded(outcome):
                typer.echo("  Keep it as an eval case with --save-case, so `cassis eval run` guards it.")
        if outcome.get("status") != "completed":
            failed = True
        if save_case:
            saved, save_failed = _save_probe_as_case(
                api_url=api_url,
                api_key=api_key,
                project_id=project_id,
                question=q,
                outcome=outcome,
                quiet=json_output,
                issue_id=issue,
            )
            outcome["saved_case"] = saved
            failed = failed or save_failed

    if json_output:
        # Always a list, regardless of how many questions ran — scripts
        # shouldn't have to branch on the shape.
        typer.echo(json.dumps(outcomes, indent=2))
    raise typer.Exit(EXIT_VALIDATION_FAILED if failed else EXIT_OK)


def _probe_succeeded(outcome: "dict") -> bool:
    """A probe that wrote SQL without asking anything: the kind --save-case keeps."""
    return (
        outcome.get("status") == "completed"
        and outcome.get("run_status") == "success"
        and bool(outcome.get("generated_sql"))
        and not outcome.get("clarifications")
    )


def _save_probe_as_case(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    question: str,
    outcome: "dict",
    quiet: bool,
    issue_id: Optional[str] = None,
) -> "tuple[Optional[dict], bool]":
    """Keep a successful probe as an eval case: its question and the SQL it produced.

    Return the created case and whether saving failed. A probe that failed,
    asked a clarification or wrote no SQL has no gold to keep: it is skipped
    with a line saying so, and that is not a failure.
    """
    sql = outcome.get("generated_sql")
    if outcome.get("status") != "completed" or outcome.get("run_status") != "success" or not sql:
        if not quiet:
            typer.secho("  Not saved as an eval case: the probe produced no successful SQL.", fg=typer.colors.YELLOW)
        return None, False
    asked = [c for c in outcome.get("clarifications") or [] if isinstance(c, dict) and c.get("question")]
    if asked:
        # Its SQL rests on the agent's own guess, and an answer case fails when the agent asks.
        if not quiet:
            typer.secho(
                f"  Not saved as an eval case: the agent asked \"{asked[0]['question']}\". "
                "Write the question with that detail and probe it again.",
                fg=typer.colors.YELLOW,
            )
        return None, False
    try:
        # The probe ran on local files: the change it proves is not published yet.
        case = post_eval_case_create(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            question=question,
            gold_sql=sql,
            pending_change=True,
            issue_id=issue_id,
        )
    except EvalCaseExistsError:
        # Probing again is the normal loop: a pending case takes the new SQL,
        # a case already checked keeps guarding the question.
        return _resave_probe(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            question=question,
            sql=sql,
            quiet=quiet,
            issue_id=issue_id,
        )
    except (AuthError, ApiError) as exc:
        typer.secho(f"  Could not save the eval case: {exc}", fg=typer.colors.RED, err=True)
        return None, True
    if not quiet:
        typer.secho(
            f"  Saved as eval case {case.get('id')}; Cassis checks it after your change is published.",
            fg=typer.colors.GREEN,
        )
        typer.echo("  Its gold SQL is the SQL above: read it, a wrong gold makes a wrong guard.")
    return case, False


def _resave_probe(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    question: str,
    sql: str,
    quiet: bool,
    issue_id: Optional[str],
) -> "tuple[Optional[dict], bool]":
    """Handle a probe whose question the suite already asks: refresh a pending case, keep a checked one."""
    try:
        existing = next(
            (
                c
                for c in get_eval_cases(api_url=api_url, api_key=api_key, project_id=project_id)
                if " ".join(str(c.get("question") or "").split()) == " ".join(question.split())
            ),
            None,
        )
        if existing is None:
            typer.secho(
                "  Could not save the eval case: the suite already asks this question.", fg=typer.colors.RED, err=True
            )
            return None, True
        check = existing.get("entry_check") or {}
        if not (isinstance(check, dict) and check.get("state") == "pending_change"):
            if not quiet:
                typer.secho(f"  Already guarded by eval case {existing.get('id')}.", fg=typer.colors.GREEN)
            return existing, False
        case, created = post_eval_case_replace(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            case_id=str(existing.get("id")),
            question=None,
            gold_sql=sql,
            pending_change=True,
            issue_id=issue_id or check.get("issue_id"),
        )
    except (AuthError, ApiError) as exc:
        typer.secho(f"  Could not save the eval case: {exc}", fg=typer.colors.RED, err=True)
        return None, True
    if not quiet:
        if created:
            typer.secho(
                f"  Updated pending eval case {existing.get('id')} with this SQL (now {case.get('id')}).",
                fg=typer.colors.GREEN,
            )
            typer.echo("  Its gold SQL is the SQL above: read it, a wrong gold makes a wrong guard.")
        else:
            typer.secho(f"  Already saved as pending eval case {existing.get('id')}.", fg=typer.colors.GREEN)
    return case, False


def _print_test_validation_failure(detail: object, base_path: str) -> None:
    """Print a structured 400 from the test endpoint (invalid tree findings, or a plain message)."""
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


_TEST_RESULT_ROWS_SHOWN = 10


def _print_test_outcome(question: str, outcome: "dict") -> None:
    typer.secho(f"▶ {question}", bold=True)
    if outcome.get("status") != "completed":
        typer.secho(f"  probe failed: {outcome.get('error', 'unknown error')}", fg=typer.colors.RED)
        if outcome.get("generated_sql"):
            typer.echo(f"  SQL before failure:\n{_indent(outcome['generated_sql'])}")
        return
    run_status = outcome.get("run_status", "?")
    color = typer.colors.GREEN if run_status == "success" else typer.colors.YELLOW
    duration = f" ({outcome['duration_seconds']:.0f}s)" if outcome.get("duration_seconds") is not None else ""
    typer.secho(f"  {run_status}{duration}", fg=color)
    if outcome.get("generated_sql"):
        typer.echo(_indent(outcome["generated_sql"]))
    if outcome.get("answer"):
        typer.echo(f"  Answer: {outcome['answer']}")
    for asked in outcome.get("clarifications") or []:
        # Nobody answers a probe: what the agent wanted to know is part of the outcome.
        if isinstance(asked, dict) and asked.get("question"):
            typer.secho(f"  Asked on the way: {asked['question']}", fg=typer.colors.YELLOW)
    results = outcome.get("results")
    # None means the SQL was never executed (schema-only source); an empty
    # list means the query ran and returned nothing — show the distinction.
    if results is not None:
        total = outcome.get("total_rows", len(results))
        typer.echo(f"  Results ({total} row{'s' if total != 1 else ''}):")
        for row in results[:_TEST_RESULT_ROWS_SHOWN]:
            typer.echo(f"    {row}")
        if len(results) > _TEST_RESULT_ROWS_SHOWN or outcome.get("truncated"):
            typer.echo("    ...")
    for concept in outcome.get("missing_concepts") or []:
        typer.secho(f"  missing concept: {concept}", fg=typer.colors.YELLOW)
    for warning in outcome.get("warnings") or []:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())
