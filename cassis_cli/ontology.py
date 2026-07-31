"""`cassis ontology` subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer
from cassis_cli.api import (
    DEFAULT_API_URL,
    ApiError,
    AuthError,
    OntologyTestValidationError,
    UploadValidationError,
    get_ontology_export,
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
from cassis_cli.common import collect_files as _collect_files
from cassis_cli.common import collect_tree as _collect_tree
from cassis_cli.common import is_legacy_domain_file as _is_legacy_domain_file
from cassis_cli.common import require_api_key as _require_api_key
from cassis_cli.common import resolve_project_id as _resolve_project_id
from cassis_cli.guide import DOCTRINE_VERSION, GUIDE_FILENAME, guide_status, refresh_guide

app = typer.Typer(no_args_is_help=True, help="Ontology commands.")


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
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON response."),
) -> None:
    """Validate the ontology files in a repository checkout.

    Runs the same checks as the Cassis GitHub PR check (YAML parsing,
    round-trip, import validation). When the checkout is bound to a project
    (``project.yml``, ``--project``, or ``CASSIS_PROJECT_ID``), the tree is
    additionally cross-checked against the project's source schema; unmatched
    references print as warnings and never fail the check. Exits 0 when valid,
    1 when validation fails, 2 on usage errors, 3 on transport/API errors.
    """
    api_key = _require_api_key(api_key)
    files, base_path = _collect_tree(path, base_path)

    # Soft resolution: an unbound checkout is not an error — the check falls
    # back to the project-less route (no schema reference stage).
    project_id = _resolve_project_id(project_id, path / base_path, optional=True, quiet=json_output)
    if not project_id and not json_output:
        typer.secho(
            "No project binding — schema reference checks skipped (bind with `cassis ontology pull` or --project).",
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
    if json_output:
        typer.echo(json.dumps(result, indent=2))
    elif result["passed"]:
        typer.secho(f"✓ {result['summary']}", fg=typer.colors.GREEN)
        # Disambiguate silence: "no warnings" must never read as "references
        # verified" when the stage didn't run.
        if project_id and result.get("references_checked") and not warnings:
            typer.secho("✓ Schema references resolve against the source schema.", fg=typer.colors.GREEN)
        elif project_id and not result.get("references_checked"):
            typer.secho(
                "Schema reference check skipped — the project has no source schema yet.",
                fg=typer.colors.YELLOW,
                err=True,
            )
    else:
        typer.secho(result["title"], fg=typer.colors.RED, bold=True)
        typer.echo(result["summary"])
        for finding in result["findings"]:
            location = f"{base_path}/{finding.get('path')}: " if finding.get("path") else ""
            typer.echo(f"  {location}{finding.get('message', '')} ({finding.get('stage', '?')})")

    if warnings and not json_output:
        typer.secho(
            f"{len(warnings)} schema reference warning(s) — advisory, expected if the objects "
            "haven't been built or synced yet:",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        for warning in warnings:
            typer.secho(f"  {warning.get('message', '')}", fg=typer.colors.YELLOW)

    raise typer.Exit(EXIT_OK if result["passed"] else EXIT_VALIDATION_FAILED)


@app.command()
def pull(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (the directory containing the ontology export path).",
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
        help="Repository directory the ontology is exported under (the project's git-sync Path setting).",
    ),
    prune: bool = typer.Option(
        True,
        "--prune/--no-prune",
        help="Delete local ontology files that no longer exist in the project's ontology (default: prune).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print a JSON summary of written/deleted files."),
) -> None:
    """Download the project's unpublished ontology into a repository checkout.

    Writes the ontology tree under the export path (full sync: files are
    overwritten and, unless --no-prune, stale local ontology files are deleted,
    so the checkout ends up matching the project exactly). Review the changes with
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
    written: list[str] = []
    for rel, content in sorted(files.items()):
        dest = (ontology_dir / rel).resolve()
        # The server controls these paths; refuse anything escaping the
        # ontology dir rather than trusting it blindly.
        if not dest.is_relative_to(ontology_dir):
            typer.secho(f"Refusing to write outside {ontology_dir}: {rel!r}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_TRANSPORT)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        except OSError as exc:  # local checkout problem (permissions, dir/file collision) — usage, not validation
            typer.secho(f"Cannot write {dest}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE) from exc
        written.append(rel)

    deleted: list[str] = []
    if prune and ontology_dir.is_dir():
        local = _collect_files(ontology_dir)
        for rel in sorted(set(local) - set(files)):
            try:
                (ontology_dir / rel).unlink()
            except OSError as exc:
                typer.secho(f"Cannot delete {ontology_dir / rel}: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(EXIT_USAGE) from exc
            deleted.append(rel)

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
        typer.echo(json.dumps({"written": written, "deleted": deleted, "guide_written": guide_written}, indent=2))
    else:
        summary = f"✓ Pulled {len(written)} files into {ontology_dir}"
        if deleted:
            summary += f" ({len(deleted)} stale files deleted)"
        if guide_written:
            summary += f"; wrote {base_path}/{GUIDE_FILENAME}"
        typer.secho(f"{summary}.", fg=typer.colors.GREEN)
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
    publish: bool = typer.Option(
        True,
        "--publish/--no-publish",
        help="Publish the uploaded ontology immediately as a new version (default: publish).",
    ),
    label: Optional[str] = typer.Option(None, "--label", help="Label for the published version."),
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON response."),
) -> None:
    """Upload the ontology files in a repository checkout to a Cassis project.

    Replaces the project's unpublished ontology with the local tree (full
    replace) and, unless --no-publish is passed, publishes it immediately as a
    new version. Exits 0 on success, 1 when the tree fails validation, 2 on
    usage errors, 3 on transport/API errors.
    """
    api_key = _require_api_key(api_key)
    files, base_path = _collect_tree(path, base_path)
    project_id = _resolve_project_id(project_id, path / Path(base_path))

    try:
        result = post_ontology_import(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            files=files,
            publish=publish,
            label=label,
        )
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except UploadValidationError as exc:
        typer.secho("Ontology upload rejected:", fg=typer.colors.RED, bold=True, err=True)
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
        if version is not None:
            typer.secho(f"✓ Ontology uploaded and published as v{version} ({counts}).", fg=typer.colors.GREEN)
        else:
            typer.secho(
                f"✓ Ontology uploaded ({counts}). Not published — it is now the project's unpublished ontology.",
                fg=typer.colors.GREEN,
            )
    raise typer.Exit(EXIT_OK)


@app.command()
def fmt(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (the directory containing the ontology export path).",
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
    check_only: bool = typer.Option(
        False,
        "--check",
        help="Do not write anything; exit 1 if any file would change.",
    ),
) -> None:
    """Rewrite the ontology files in canonical form (think `black` for the ontology).

    Uses the exact serializer the validation round-trip compares against, so a
    formatted tree cannot fail that stage of `cassis ontology check` or the
    GitHub PR check. Unknown fields are dropped by canonicalization — review
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
        help="Repository checkout root (the directory containing the ontology export path).",
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
        help="Repository directory the ontology is exported under (the project's git-sync Path setting).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON outcomes."),
) -> None:
    """Run questions through the text-to-SQL agent using your local ontology files.

    The behavioral probe: checks that a change actually WORKS — e.g. that a
    new column gets picked —
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
        if outcome.get("status") != "completed":
            failed = True

    if json_output:
        # Always a list, regardless of how many questions ran — scripts
        # shouldn't have to branch on the shape.
        typer.echo(json.dumps(outcomes, indent=2))
    raise typer.Exit(EXIT_VALIDATION_FAILED if failed else EXIT_OK)


def _print_test_validation_failure(detail: object, base_path: str) -> None:
    """Print a structured 400 from the test endpoint (invalid tree findings, or a plain message)."""
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
