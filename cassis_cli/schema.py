"""`cassis schema`: the data source's schema. Pull a snapshot, plan and apply a DDL update.

The source schema is OBSERVED state (the warehouse is authoritative), so the
snapshot is a gitignored cache, never a committed file: `pull` writes
`<base-path>/.schema.json` and keeps it out of git via the ontology dir's
`.gitignore`. Agents working in a checkout grep it instead of paging through
the MCP `get_source_schema` tool; `pulled_at` records how stale it is.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from cassis_cli.api import (
    DEFAULT_API_URL,
    ApiError,
    AuthError,
    SchemaPlanConflictError,
    SchemaPlanRejectedError,
    UploadValidationError,
    get_ontology_export,
    get_schema_export,
    get_schema_plan,
    get_schema_plan_checkout,
    post_ontology_import,
    post_schema_plan,
    post_schema_plan_apply,
    post_schema_plan_preview,
    post_schema_plan_preview_warehouse,
    post_schema_plan_warehouse,
)
from cassis_cli.common import (
    DEFAULT_BASE_PATH,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
    MAX_DDL_BYTES,
    collect_files,
    collect_tree,
    poll_until,
    require_api_key,
    resolve_project_id,
    sync_ontology_tree,
)
from cassis_cli.schema_plan import plan_counts, plan_is_empty, render_plan

app = typer.Typer(help="Pull the data source's schema; plan, apply (locally) and push a schema update from DDL.")

_PLAN_TERMINAL = {"ready", "failed", "cancelled", "stale", "expired", "applied"}
_APPLY_TERMINAL = {"applied", "failed", "stale", "expired", "cancelled"}

SNAPSHOT_FILENAME = ".schema.json"
# Written by `schema apply`, read by `schema push`: which app ontology the local
# tree was rendered from, so a push can refuse to overwrite edits made in the
# app in between. Local cache, never committed.
APPLY_MARKER_FILENAME = ".schema-apply.json"
_GITIGNORE_HEADER = "# Cassis local caches (observed state — never commit)"
_GITIGNORED = (SNAPSHOT_FILENAME, APPLY_MARKER_FILENAME)


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
) -> None:
    """Download the source schema into `<base-path>/.schema.json` (gitignored).

    The snapshot is the schema as Cassis last introspected it from the
    warehouse (or parsed from an uploaded DDL) — every table with its columns
    and types, plus a `pulled_at` stamp so staleness is visible. Re-run after
    a warehouse sync to refresh. Exits 0 on success, 2 on usage errors, 3 on
    transport/API errors.
    """
    api_key = require_api_key(api_key)
    ontology_dir = path / base_path
    project_id = resolve_project_id(project_id, ontology_dir)

    try:
        result = get_schema_export(api_url=api_url, api_key=api_key, project_id=project_id)
    except (AuthError, ApiError) as exc:
        # NoSourceSchemaError lands here too: the server's message already says
        # what to do (sync or upload a DDL); the class exists so api.py doesn't
        # bury it under the misleading project-scope hint.
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    snapshot = {
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_id": project_id,
        "schema_version": result["schema_version"],
        "tables": result["tables"],
    }

    try:
        ontology_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = ontology_dir / SNAPSHOT_FILENAME
        snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        ensure_gitignored(ontology_dir)
    except OSError as exc:
        # Usage-class exit (2), like the other local-file failures in the exit
        # table — the API call succeeded, the checkout is what's broken.
        typer.secho(f"Could not write the snapshot: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc

    table_count = len(result["tables"])
    column_count = sum(len(t.get("columns") or []) for t in result["tables"])
    version = result["schema_version"].get("version")
    typer.secho(
        f"✓ Pulled source schema v{version}: {table_count} tables, {column_count} columns "
        f"-> {snapshot_path} (gitignored)",
        fg=typer.colors.GREEN,
    )


def ensure_gitignored(ontology_dir: Path) -> None:
    """Make sure the snapshot never lands in git: keep `.gitignore` covering it.

    Appends to (or creates) the ontology dir's own `.gitignore` — local to the
    export directory, so it survives repo-level `.gitignore` rewrites and needs
    no knowledge of the checkout layout.
    """
    gitignore = ontology_dir / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        existing = ""
    lines = existing.splitlines()
    missing = [name for name in _GITIGNORED if name not in lines]
    if not missing:
        return
    prefix = "" if not existing else existing.rstrip("\n") + "\n"
    header = "" if _GITIGNORE_HEADER in lines else f"{_GITIGNORE_HEADER}\n"
    gitignore.write_text(prefix + header + "".join(f"{name}\n" for name in missing), encoding="utf-8")


_PATH_OPTION = typer.Option(
    Path("."), "--path", help="Repository checkout root (the directory containing the ontology export path)."
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
_API_URL_OPTION = typer.Option(DEFAULT_API_URL, "--api-url", envvar="CASSIS_API_URL", help="Cassis API base URL.")
_BASE_PATH_OPTION = typer.Option(
    DEFAULT_BASE_PATH,
    "--base-path",
    envvar="CASSIS_BASE_PATH",
    help="Repository directory the ontology is exported under (the project's git-sync Path setting).",
)
_COMPLETE_OPTION = typer.Option(
    False,
    "--complete",
    help="The file is the project's complete source schema: schemas absent from it are treated as dropped. "
    "Without it, the upload only speaks for the schemas it contains.",
)
_POLL_OPTION = typer.Option(5.0, "--poll-interval", help="Seconds between polls.")
_TIMEOUT_OPTION = typer.Option(1800.0, "--timeout", help="Give up waiting after this many seconds.")
_JSON_OPTION = typer.Option(False, "--json", help="Print the plan record as raw JSON (the plan itself goes to stderr).")
_OUT_OPTION = typer.Option(None, "--out", help="Also write the plan record (JSON) to this file.")
_WAREHOUSE_OPTION = typer.Option(
    False,
    "--warehouse",
    help="Plan from the project's connected warehouse (introspected server-side) instead of a DDL file.",
)


def _require_one_source(
    ddl_file: "Optional[Path]", warehouse: bool, *, or_plan: "Optional[str]" = None, accepts_plan: bool = False
) -> None:
    """Exactly one of a DDL file, --warehouse (and, for apply, --plan <id>)."""
    given = sum(1 for x in (ddl_file is not None, warehouse, or_plan is not None) if x)
    if given != 1:
        options = "a DDL file, --warehouse or --plan <id>" if accepts_plan else "a DDL file or --warehouse"
        typer.secho(f"Pass exactly one of {options}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)


@app.command()
def plan(
    ddl_file: Optional[Path] = typer.Argument(
        None, help="Path to the DDL file (.sql, .ddl, .txt) containing CREATE TABLE statements. Or --warehouse."
    ),
    warehouse: bool = _WAREHOUSE_OPTION,
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    complete_source: bool = _COMPLETE_OPTION,
    poll_interval: float = _POLL_OPTION,
    timeout: float = _TIMEOUT_OPTION,
    json_output: bool = _JSON_OPTION,
    out: Optional[Path] = _OUT_OPTION,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Compute the plan in the request and keep nothing server-side: no plan to apply or resume, "
        "the project's current plan untouched. For a DDL not deployed to the warehouse yet.",
    ),
    write_checkout: bool = typer.Option(
        False,
        "--write-checkout",
        help="With --dry-run: also write the ontology files the plan would produce under <path>/<base-path>.",
    ),
) -> None:
    """Preview what a schema update would change. Nothing is applied.

    Cassis diffs the new schema (a DDL file, or with --warehouse the connected
    warehouse introspected server-side) against the stored schema and derives
    the ontology edits it implies: every change on a table that is in the
    ontology (placed in a domain), with everything a drop would take with it.
    Tables outside the ontology only move the schema. A file speaks only for
    the schemas it contains unless --complete; a warehouse plan is always
    whole-source. Exits 0 when the plan is ready (even when it is empty), 1
    when the plan failed (unparseable or truncated DDL, unreachable
    warehouse), 2 on usage errors, 3 on transport errors or a timeout.

    --dry-run is the prepare-ahead gesture: the plan is computed synchronously
    and nothing is kept server-side, so it works for a schema change that is
    still a PR (a dbt model, a migration) and leaves the project's current plan
    alone. --write-checkout then writes the resulting ontology files into the
    checkout, to commit next to the schema change; nothing is pushed.
    """
    api_key = require_api_key(api_key)
    _require_one_source(ddl_file, warehouse)
    if write_checkout and not dry_run:
        typer.secho(
            "--write-checkout needs --dry-run (use `cassis schema apply` otherwise).", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(EXIT_USAGE)
    resolved_project = resolve_project_id(project_id, path / base_path, quiet=json_output)
    assert resolved_project is not None
    if dry_run:
        preview = _preview(
            ddl_file,
            warehouse=warehouse,
            api_url=api_url,
            api_key=api_key,
            project_id=resolved_project,
            complete_source=complete_source,
            json_output=json_output,
            out=out,
        )
        if write_checkout:
            ontology_dir = path / base_path.strip().strip("/")
            written, deleted, _kept = _write_checkout(ontology_dir, preview["files"], json_output=json_output)
            typer.secho(
                f"✓ Wrote {len(written)} ontology file(s) into {ontology_dir}"
                + (f", deleted {len(deleted)} stale file(s)" if deleted else "")
                + ". The app is unchanged.",
                fg=typer.colors.GREEN,
                err=json_output,
            )
        if json_output:
            typer.echo(json.dumps(preview, indent=2))
        raise typer.Exit(EXIT_OK)
    record = _plan(
        ddl_file,
        warehouse=warehouse,
        api_url=api_url,
        api_key=api_key,
        project_id=resolved_project,
        complete_source=complete_source,
        poll_interval=poll_interval,
        timeout=timeout,
        json_output=json_output,
        out=out,
    )
    if json_output:
        typer.echo(json.dumps(record, indent=2))
    if record.get("status") != "ready":
        raise typer.Exit(EXIT_VALIDATION_FAILED)
    raise typer.Exit(EXIT_OK)


@app.command()
def apply(
    ddl_file: Optional[Path] = typer.Argument(
        None, help="DDL file to plan and apply locally. Or --warehouse, or --plan <id> for an existing plan."
    ),
    warehouse: bool = _WAREHOUSE_OPTION,
    plan_id: Optional[str] = typer.Option(None, "--plan", help="Write this ready plan instead of planning a file."),
    force: bool = typer.Option(
        False, "--force", help="Write even if the local ontology differs from the app's (local edits are overwritten)."
    ),
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    complete_source: bool = _COMPLETE_OPTION,
    poll_interval: float = _POLL_OPTION,
    timeout: float = _TIMEOUT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Plan a DDL update and write the resulting ontology into the local checkout. The app is not modified.

    GitOps: the plan is computed server-side (read-only), then the ontology
    files it would produce are written under <path>/<base-path> (renamed
    tables, dropped columns, rewritten joins and metrics) for you to review
    with `git diff`, edit, commit. Stale ontology files (dropped tables and
    joins) are deleted when git can restore them. Send the result with
    `cassis schema push <ddl>`. Exits 0 when written, 1 when the plan failed /
    is stale, 2 on usage errors, 3 on transport errors.
    """
    api_key = require_api_key(api_key)
    _require_one_source(ddl_file, warehouse, or_plan=plan_id, accepts_plan=True)
    base_path = base_path.strip().strip("/")
    ontology_dir = (path / Path(base_path)).resolve()
    resolved_project = resolve_project_id(project_id, ontology_dir, quiet=json_output)
    assert resolved_project is not None
    _require_checkout_in_sync(
        ontology_dir, api_url=api_url, api_key=api_key, project_id=resolved_project, force=force, base_path=base_path
    )

    record = _plan_or_fetch(
        ddl_file,
        plan_id,
        warehouse=warehouse,
        api_url=api_url,
        api_key=api_key,
        project_id=resolved_project,
        complete_source=complete_source,
        poll_interval=poll_interval,
        timeout=timeout,
        json_output=json_output,
    )
    if record.get("status") != "ready":
        raise typer.Exit(EXIT_VALIDATION_FAILED)
    try:
        checkout = get_schema_plan_checkout(
            api_url=api_url, api_key=api_key, project_id=resolved_project, plan_id=str(record["id"])
        )
    except SchemaPlanConflictError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except (AuthError, ApiError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    written, deleted, kept = _write_checkout(ontology_dir, checkout["files"], json_output=json_output)
    _write_apply_marker(ontology_dir, record)
    for warning in checkout.get("warnings") or []:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW, err=True)
    if json_output:
        typer.echo(json.dumps({"plan": record, "written": written, "deleted": deleted, "kept": kept}, indent=2))
        raise typer.Exit(EXIT_OK)
    typer.secho(
        f"✓ Wrote {len(written)} ontology file(s) into {ontology_dir}"
        + (f", deleted {len(deleted)} stale file(s)" if deleted else "")
        + ". The app is unchanged.",
        fg=typer.colors.GREEN,
    )
    for entry in kept:
        typer.secho(f"  kept {base_path}/{entry['path']} ({entry['reason']})", fg=typer.colors.YELLOW)
    if warehouse:
        push_hint = "--warehouse"
    elif ddl_file is not None:
        push_hint = str(ddl_file) + (" --complete" if complete_source else "")
    else:
        # A plan record does not say whether it came from a DDL or the warehouse:
        # only the caller knows, so the hint names both rather than guessing.
        push_hint = "<ddl>" + (" --complete" if record.get("complete_source") else "")
        push_hint += " (or --warehouse if this plan was introspected from the warehouse)"
    typer.echo(f"Review with git diff, then: cassis schema push {push_hint}")
    raise typer.Exit(EXIT_OK)


@app.command()
def push(
    ddl_file: Optional[Path] = typer.Argument(
        None, help="The DDL file the local ontology was applied against. Or --warehouse."
    ),
    warehouse: bool = _WAREHOUSE_OPTION,
    yes: bool = typer.Option(False, "--yes", "-y", help="Push without the confirmation prompt (CI)."),
    publish: bool = typer.Option(False, "--publish", help="Publish the pushed ontology as a new version."),
    label: Optional[str] = typer.Option(None, "--label", help="Label for the published version."),
    path: Path = _PATH_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    complete_source: bool = _COMPLETE_OPTION,
    poll_interval: float = _POLL_OPTION,
    timeout: float = _TIMEOUT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Push the new schema and the local ontology to the app.

    Two steps, in order: the new schema (a DDL file, or the connected
    warehouse with --warehouse) is planned and applied server-side (new
    schema version, tracked schema updated, ontology edits the plan lists),
    then the local ontology tree replaces the project's unpublished ontology
    (so hand edits made after `cassis schema apply` land too). Pass --publish
    to publish it as a new version. Exits 0 when pushed, 1 when the plan
    failed / is stale or the upload was rejected, 2 on usage errors, 3 on
    transport errors.
    """
    api_key = require_api_key(api_key)
    _require_one_source(ddl_file, warehouse)
    files, base_path = collect_tree(path, base_path)
    resolved_project = resolve_project_id(project_id, path / Path(base_path), quiet=json_output)
    assert resolved_project is not None

    record = _plan_or_fetch(
        ddl_file,
        None,
        warehouse=warehouse,
        api_url=api_url,
        api_key=api_key,
        project_id=resolved_project,
        complete_source=complete_source,
        poll_interval=poll_interval,
        timeout=timeout,
        json_output=json_output,
    )
    if record.get("status") != "ready":
        raise typer.Exit(EXIT_VALIDATION_FAILED)
    _require_marker_matches(path / Path(base_path), record)
    if not yes:
        if not (sys.stdin.isatty() and sys.stdout.isatty()) or json_output:
            typer.secho("Refusing to push without confirmation: pass --yes.", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE)
        if not typer.confirm(f"Push the schema and {len(files)} ontology file(s) to the app?", default=False):
            typer.secho("Nothing pushed.", fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(EXIT_OK)

    if not plan_is_empty(record):
        try:
            record = post_schema_plan_apply(
                api_url=api_url, api_key=api_key, project_id=resolved_project, plan_id=str(record["id"])
            )
        except SchemaPlanConflictError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
        except (AuthError, ApiError) as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_TRANSPORT) from exc
        typer.echo(f"Applying schema plan {record['id']}…", err=True)
        record = _wait_for_plan(
            api_url=api_url,
            api_key=api_key,
            project_id=resolved_project,
            plan_id=str(record["id"]),
            terminal=_APPLY_TERMINAL,
            what="apply",
            poll_interval=poll_interval,
            timeout=timeout,
        )
        if record.get("status") != "applied":
            typer.secho(
                f"Schema apply ended with status {record.get('status')}: {record.get('error') or ''}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(EXIT_VALIDATION_FAILED)
        result = record.get("apply_result") or {}
        typer.secho(f"✓ Schema version {result.get('schema_version', '?')} stored.", fg=typer.colors.GREEN, err=True)
    else:
        typer.secho("Schema is up to date; pushing the ontology only.", err=True)

    typer.echo(f"Uploading {len(files)} ontology file(s)…", err=True)
    try:
        upload = post_ontology_import(
            api_url=api_url, api_key=api_key, project_id=resolved_project, files=files, publish=publish, label=label
        )
    except UploadValidationError as exc:
        typer.secho("Ontology upload rejected:", fg=typer.colors.RED, bold=True, err=True)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except (AuthError, ApiError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    if json_output:
        typer.echo(json.dumps({"plan": record, "upload": upload}, indent=2))
        raise typer.Exit(EXIT_OK)
    counts = (
        f"{upload.get('table_count')} tables, {upload.get('domain_count')} domains, "
        f"{upload.get('join_count')} joins, {upload.get('metric_count')} metrics"
    )
    version = upload.get("published_version")
    if version is not None:
        typer.secho(f"✓ Ontology pushed and published as v{version} ({counts}).", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"✓ Ontology pushed ({counts}); it is now the project's unpublished ontology.", fg=typer.colors.GREEN
        )
    raise typer.Exit(EXIT_OK)


def _require_checkout_in_sync(
    ontology_dir: Path, *, api_url: str, api_key: str, project_id: str, force: bool, base_path: str
) -> None:
    """Refuse to overwrite a checkout that differs from the app's ontology (unless --force).

    The rendered tree is "the app's ontology + the plan"; writing it over local
    edits that never reached the app would lose them, and writing it over a
    checkout behind the app is fine but the user should know. Missing or empty
    local dir: nothing to protect.
    """
    if not ontology_dir.is_dir():
        return
    local = collect_files(ontology_dir)
    if not local:
        return
    try:
        remote = get_ontology_export(api_url=api_url, api_key=api_key, project_id=project_id)
    except (AuthError, ApiError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    differing = sorted(rel for rel in set(local) | set(remote) if local.get(rel) != remote.get(rel))
    if not differing:
        return
    if force:
        typer.secho(
            f"warning: {len(differing)} local ontology file(s) differ from the app and will be overwritten (--force).",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return
    typer.secho(
        f"The local ontology in {base_path}/ differs from the app's ({len(differing)} file(s)):",
        fg=typer.colors.RED,
        err=True,
    )
    for rel in differing[:10]:
        typer.secho(f"  {base_path}/{rel}", fg=typer.colors.RED, err=True)
    if len(differing) > 10:
        typer.secho(f"  … {len(differing) - 10} more", fg=typer.colors.RED, err=True)
    typer.secho(
        "Bring the checkout up to date first (`cassis ontology pull`), or push your local edits "
        "(`cassis ontology upload --no-publish`), then apply again. `--force` overwrites the local files.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(EXIT_VALIDATION_FAILED)


def _write_apply_marker(ontology_dir: Path, record: "dict[str, Any]") -> None:
    """Remember which app ontology the local tree was rendered from (for `schema push`)."""
    marker = {
        "plan_id": record.get("id"),
        "base_ontology_fingerprint": record.get("base_ontology_fingerprint"),
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        ontology_dir.mkdir(parents=True, exist_ok=True)
        (ontology_dir / APPLY_MARKER_FILENAME).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        ensure_gitignored(ontology_dir)
    except OSError as exc:
        typer.secho(f"Could not write {ontology_dir / APPLY_MARKER_FILENAME}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc


def _require_marker_matches(ontology_dir: Path, record: "dict[str, Any]") -> None:
    """Refuse a push when the app's ontology moved since the local tree was rendered.

    The local tree is a full replace: pushing it over edits made in the app in
    between would silently revert them. No marker (the user never ran
    `schema apply`, or cleaned caches) → warn and continue.
    """
    marker_path = ontology_dir / APPLY_MARKER_FILENAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        typer.secho(
            "note: no local `schema apply` marker found; the push replaces the app's ontology with this checkout.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return
    base = marker.get("base_ontology_fingerprint")
    current = record.get("base_ontology_fingerprint")
    if base and current and base != current:
        typer.secho(
            "The app's ontology changed since `cassis schema apply` rendered this checkout: pushing would revert "
            "those edits. Run `cassis ontology pull`, resolve the differences in git, run `cassis schema apply` "
            "again, then push.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_VALIDATION_FAILED)


def _plan_or_fetch(
    ddl_file: Optional[Path],
    plan_id: Optional[str],
    *,
    warehouse: bool = False,
    api_url: str,
    api_key: str,
    project_id: str,
    complete_source: bool,
    poll_interval: float,
    timeout: float,
    json_output: bool,
) -> "dict[str, Any]":
    """A rendered terminal plan record: planned from `ddl_file` / the warehouse, or fetched (and awaited) by id."""
    if ddl_file is not None or warehouse:
        return _plan(
            ddl_file,
            warehouse=warehouse,
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            complete_source=complete_source,
            poll_interval=poll_interval,
            timeout=timeout,
            json_output=json_output,
            out=None,
        )
    assert plan_id is not None
    record = _fetch_plan(api_url=api_url, api_key=api_key, project_id=project_id, plan_id=plan_id)
    if record.get("status") in ("planning", "applying"):
        # Still computing, or someone else is applying it: wait for the outcome
        # (APPLIED is terminal too, _explain_not_ready then says so).
        record = _wait_for_plan(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            plan_id=plan_id,
            terminal=_PLAN_TERMINAL,
            what="plan",
            poll_interval=poll_interval,
            timeout=timeout,
        )
    if record.get("status") == "ready":
        render_plan(record, err=True)
    else:
        _explain_not_ready(record)
    return record


def _write_checkout(
    ontology_dir: Path, files: "dict[str, str]", *, json_output: bool
) -> "tuple[list[str], list[str], list[dict[str, str]]]":
    """Write the rendered tree under `ontology_dir`; prune stale files git can restore. Same rules as `ontology pull`.

    Unchanged files are not rewritten (and not listed), so the `~` lines below
    are the files the plan actually touched.
    """
    written, deleted, kept = sync_ontology_tree(ontology_dir, files, skip_unchanged=True)
    if not json_output:
        for rel in written:
            typer.echo(f"  ~ {rel}")
        for rel in deleted:
            typer.echo(f"  - {rel}")
    return written, deleted, kept


def _read_ddl(ddl_file: Path) -> str:
    try:
        ddl_text = ddl_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        typer.secho(f"Cannot read {ddl_file}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc
    if not ddl_text.strip():
        typer.secho("DDL file is empty.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    size = len(ddl_text.encode("utf-8"))
    if size > MAX_DDL_BYTES:
        typer.secho(
            f"DDL file too large ({size / (1024 * 1024):.1f} MB, limit {MAX_DDL_BYTES // (1024 * 1024)} MB).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    return ddl_text


def _plan(
    ddl_file: Optional[Path],
    *,
    warehouse: bool = False,
    api_url: str,
    api_key: str,
    project_id: str,
    complete_source: bool,
    poll_interval: float,
    timeout: float,
    json_output: bool,
    out: Optional[Path],
) -> "dict[str, Any]":
    """Start a plan for `ddl_file` (or the warehouse), wait for it, render it. Return the terminal plan record."""
    try:
        if warehouse:
            record = post_schema_plan_warehouse(api_url=api_url, api_key=api_key, project_id=project_id)
        else:
            assert ddl_file is not None
            ddl_text = _read_ddl(ddl_file)
            record = post_schema_plan(
                api_url=api_url, api_key=api_key, project_id=project_id, ddl=ddl_text, complete_source=complete_source
            )
    except SchemaPlanConflictError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except (AuthError, ApiError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    plan_id = str(record["id"])
    typer.echo(f"Schema plan started: {plan_id}", err=True)
    try:
        record = _wait_for_plan(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            plan_id=plan_id,
            terminal=_PLAN_TERMINAL,
            what="plan",
            poll_interval=poll_interval,
            timeout=timeout,
        )
    except KeyboardInterrupt:
        typer.secho(
            f"Interrupted. The plan keeps computing server-side; resume with: cassis schema apply --plan {plan_id}",
            err=True,
        )
        raise typer.Exit(EXIT_INTERRUPTED)
    if out is not None:
        try:
            out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            typer.secho(f"Could not write {out}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE) from exc
    if record.get("status") == "ready":
        render_plan(record, err=json_output)
        if plan_is_empty(record):
            typer.secho("✓ Schema is up to date.", fg=typer.colors.GREEN, err=json_output)
        else:
            typer.secho(f"✓ Plan ready: {plan_id}", fg=typer.colors.GREEN, err=json_output)
    else:
        _explain_not_ready(record)
    return record


def _preview(
    ddl_file: Optional[Path],
    *,
    warehouse: bool,
    api_url: str,
    api_key: str,
    project_id: str,
    complete_source: bool,
    json_output: bool,
    out: Optional[Path],
) -> "dict[str, Any]":
    """Compute a dry-run plan for `ddl_file` (or the warehouse), render it. Return the preview record."""
    try:
        if warehouse:
            preview = post_schema_plan_preview_warehouse(api_url=api_url, api_key=api_key, project_id=project_id)
        else:
            assert ddl_file is not None
            preview = post_schema_plan_preview(
                api_url=api_url,
                api_key=api_key,
                project_id=project_id,
                ddl=_read_ddl(ddl_file),
                complete_source=complete_source,
            )
    except (SchemaPlanRejectedError, SchemaPlanConflictError) as exc:
        typer.secho(f"Plan failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except (AuthError, ApiError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    if out is not None:
        try:
            out.write_text(json.dumps(preview, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            typer.secho(f"Could not write {out}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE) from exc
    # The renderer reads a plan record; a preview is one without an id.
    record = {"status": "ready", "document": preview.get("document"), "summary": preview.get("summary")}
    render_plan(record, err=json_output)
    for warning in preview.get("warnings") or []:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW, err=True)
    if plan_is_empty(record):
        typer.secho("✓ Schema is up to date (dry run, nothing kept).", fg=typer.colors.GREEN, err=json_output)
    else:
        typer.secho("✓ Plan computed (dry run, nothing kept).", fg=typer.colors.GREEN, err=json_output)
    return preview


def _explain_not_ready(record: "dict[str, Any]") -> None:
    status = record.get("status")
    error = record.get("error") or ""
    if status == "failed":
        typer.secho(f"Plan failed: {error or 'unknown error'}", fg=typer.colors.RED, err=True)
    elif status == "applied":
        typer.secho("Schema plan already applied.", fg=typer.colors.YELLOW, err=True)
    elif status == "applying":
        typer.secho(
            "Schema plan is being applied right now (by the app or another push). Wait for it to finish; "
            "`cassis status` shows the outcome.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    elif status in ("stale", "expired"):
        typer.secho(f"{error or status.capitalize()} Plan again with the DDL file.", fg=typer.colors.RED, err=True)
    elif status == "cancelled":
        typer.secho("Plan was cancelled.", fg=typer.colors.YELLOW, err=True)
    else:
        typer.secho(f"Plan ended with unexpected status: {status}", fg=typer.colors.RED, err=True)


def _fetch_plan(*, api_url: str, api_key: str, project_id: str, plan_id: str) -> "dict[str, Any]":
    try:
        return get_schema_plan(api_url=api_url, api_key=api_key, project_id=project_id, plan_id=plan_id)
    except (AuthError, ApiError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc


def _wait_for_plan(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    plan_id: str,
    terminal: "set[str]",
    what: str,
    poll_interval: float,
    timeout: float,
) -> "dict[str, Any]":
    """Poll the plan record until its status is in `terminal`. Exits 3 on timeout, auth or repeated poll failures."""

    def fetch() -> "dict[str, Any]":
        return get_schema_plan(api_url=api_url, api_key=api_key, project_id=project_id, plan_id=plan_id)

    def on_timeout(record: "dict[str, Any]") -> None:
        resume = (
            f"resume with: cassis schema apply --plan {plan_id}"
            if what == "plan"
            else "`cassis status` shows the outcome once it finishes"
        )
        typer.secho(
            f"Timed out after {timeout:.0f}s: the {what} is still {record.get('status', '?')}. "
            f"It keeps running server-side; {resume}",
            fg=typer.colors.YELLOW,
            err=True,
        )

    return poll_until(
        fetch,
        lambda record: record.get("status") in terminal,
        poll_interval=poll_interval,
        timeout=timeout,
        on_timeout=on_timeout,
    )
