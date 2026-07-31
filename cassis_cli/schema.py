"""`cassis schema` — local snapshot of the data source's source schema.

The source schema is OBSERVED state (the warehouse is authoritative), so the
snapshot is a gitignored cache, never a committed file: `pull` writes
`<base-path>/.schema.json` and keeps it out of git via the ontology dir's
`.gitignore`. Agents working in a checkout grep it instead of paging through
the MCP `get_source_schema` tool; `pulled_at` records how stale it is.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from cassis_cli.api import DEFAULT_API_URL, ApiError, AuthError, get_schema_export
from cassis_cli.common import (
    DEFAULT_BASE_PATH,
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    require_api_key,
    resolve_project_id,
)

app = typer.Typer(help="Pull a local, gitignored snapshot of the data source's schema.")

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
