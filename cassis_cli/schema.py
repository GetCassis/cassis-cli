"""`cassis schema` — local snapshot of the data source's source schema.

The source schema is OBSERVED state (the warehouse is authoritative), so the
snapshot is a gitignored cache, never a committed file: `pull` writes
`<base-path>/.schema.json` and keeps it out of git via the ontology dir's
`.gitignore`. Agents working in a checkout grep it instead of paging through
the MCP `get_source_schema` tool; `pulled_at` records how stale it is.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from cassis_cli.api import (
    DEFAULT_API_URL,
    ApiError,
    AuthError,
    SourceChangeConflictError,
    get_schema_export,
    get_source_change_run,
    post_detect_from_ddl,
)
from cassis_cli.common import (
    DEFAULT_BASE_PATH,
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
    require_api_key,
    resolve_project_id,
)

app = typer.Typer(help="Pull or push the data source's schema.")

_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
_MAX_CONSECUTIVE_POLL_FAILURES = 5

SNAPSHOT_FILENAME = ".schema.json"
_GITIGNORE_HEADER = "# Cassis local caches (observed state — never commit)"


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
    except (OSError, UnicodeDecodeError) as _exc:  # `as` keeps black from stripping the parens (3.14-only syntax)
        existing = ""
    if SNAPSHOT_FILENAME in existing.splitlines():
        return
    prefix = "" if not existing else existing.rstrip("\n") + "\n"
    gitignore.write_text(f"{prefix}{_GITIGNORE_HEADER}\n{SNAPSHOT_FILENAME}\n", encoding="utf-8")


@app.command()
def push(
    ddl_file: Path = typer.Argument(
        ..., help="Path to the DDL file (.sql, .ddl, .txt) containing CREATE TABLE statements."
    ),
    path: Path = typer.Option(
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
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="Seconds between polls."),
    timeout: float = typer.Option(600.0, "--timeout", help="Give up waiting after this many seconds."),
    json_output: bool = typer.Option(False, "--json", help="Print the run record as raw JSON."),
) -> None:
    """Upload a DDL file to detect source-schema changes (same as the webapp's "Update from DDL").

    The DDL must contain at least one CREATE TABLE statement and represents the
    project's complete source schema. Cassis diffs it against the ontology:
    added, dropped, and changed objects appear in Ontology > Review > Data
    source for approval. Re-uploading a corrected DDL supersedes the previous
    one. Only works on DDL-only projects (no warehouse connection).

    Always waits for the detection run to finish: the server parses the DDL
    inside the run (a large file takes a while, and an unparseable one fails
    the run rather than the upload request), so exit 0 means the schema parsed
    AND was applied — there is no fire-and-forget mode. Exits 0 on success, 1
    on a failed detection run, 2 on usage errors, 3 on transport/API errors or
    a timeout.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / base_path)

    try:
        ddl_text = ddl_file.read_text(encoding="utf-8")
    except OSError as exc:
        typer.secho(f"Cannot read {ddl_file}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc
    except UnicodeDecodeError as exc:
        typer.secho(f"Cannot read {ddl_file}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc

    if not ddl_text.strip():
        typer.secho("DDL file is empty.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)

    try:
        run = post_detect_from_ddl(api_url=api_url, api_key=api_key, project_id=project_id, ddl=ddl_text)
    except SourceChangeConflictError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except (AuthError, ApiError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    run_id = run["run_id"]
    typer.echo(f"Detection run started: {run_id}")

    run = _wait_for_detection_run(
        api_url=api_url,
        api_key=api_key,
        project_id=project_id,
        run_id=run_id,
        poll_interval=poll_interval,
        timeout=timeout,
    )

    if json_output:
        typer.echo(json.dumps(run, indent=2))

    run_status = run.get("status")
    if run_status == "completed":
        summary = run.get("summary") or {}
        total = summary.get("total_changes", 0)
        typer.secho(
            f"✓ Detection completed: {total} change(s) detected." if total else "✓ Detection completed: no changes.",
            fg=typer.colors.GREEN,
        )
        raise typer.Exit(EXIT_OK)
    if run_status == "failed":
        error = run.get("error") or "unknown error"
        typer.secho(f"Detection failed: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED)
    if run_status == "cancelled":
        typer.secho("Detection run was cancelled.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED)
    typer.secho(f"Detection run ended with unexpected status: {run_status}", fg=typer.colors.RED, err=True)
    raise typer.Exit(EXIT_TRANSPORT)


def _wait_for_detection_run(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    run_id: str,
    poll_interval: float,
    timeout: float,
) -> "dict[str, Any]":
    """Poll until the detection run reaches a terminal status."""
    deadline = time.monotonic() + timeout
    consecutive_failures = 0
    while True:
        try:
            run = get_source_change_run(api_url=api_url, api_key=api_key, project_id=project_id, run_id=run_id)
            consecutive_failures = 0
        except AuthError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_TRANSPORT) from exc
        except ApiError as exc:
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_POLL_FAILURES:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(EXIT_TRANSPORT) from exc
            time.sleep(poll_interval)
            continue

        if run.get("status") in _TERMINAL_RUN_STATUSES:
            return run

        if time.monotonic() >= deadline:
            typer.secho(
                f"Timed out after {timeout:.0f}s: the detection run is still {run.get('status', '?')}.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(EXIT_TRANSPORT)
        time.sleep(poll_interval)
