"""`cassis ontology` subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from uuid import UUID

import typer
from cassis_cli.api import (
    DEFAULT_API_URL,
    ApiError,
    AuthError,
    UploadValidationError,
    post_ontology_check,
    post_ontology_import,
)

app = typer.Typer(no_args_is_help=True, help="Ontology commands.")

# Repository directory the ontology tree is exported under. Must match the
# project's git-sync "Path" setting in Cassis (default "cassis").
DEFAULT_BASE_PATH = "cassis"

# Exit codes (documented in the README; stable contract for CI scripts).
EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_USAGE = 2
EXIT_TRANSPORT = 3

# Request ceilings of POST /api/ci/ontology-check, mirrored so oversized trees
# fail fast with a clear message before any upload. Source of truth:
# backend/app/schemas/ci.py (the server's 422 remains the backstop).
MAX_FILES = 2000
MAX_TOTAL_BYTES = 5 * 1024 * 1024


def _collect_files(ontology_dir: Path) -> dict[str, str]:
    """Read every YAML file under the ontology dir, keyed by posix relpath.

    Exits 2 (usage) on an unreadable or non-UTF-8 file — a local checkout
    problem, reported before anything is sent to the API.
    """
    files: dict[str, str] = {}
    for pattern in ("**/*.yml", "**/*.yaml"):
        for file in sorted(ontology_dir.glob(pattern)):
            if file.is_file():
                try:
                    files[file.relative_to(ontology_dir).as_posix()] = file.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError) as exc:
                    typer.secho(f"Cannot read {file}: {exc}", fg=typer.colors.RED, err=True)
                    raise typer.Exit(EXIT_USAGE) from exc
    return files


def _require_api_key(api_key: Optional[str]) -> str:
    """Exit 2 (usage) when no API key was provided."""
    if not api_key:
        typer.secho(
            "No API key. Set CASSIS_API_KEY or pass --api-key "
            "(create one in Cassis under Organization settings -> API keys).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    return api_key


def _collect_tree(path: Path, base_path: str) -> "tuple[dict[str, str], str]":
    """Resolve the ontology dir under the checkout and read its YAML tree.

    Returns ``(files, normalized_base_path)``. Exits 2 (usage) on an empty or
    missing dir, or a tree beyond the API request ceilings — all local checkout
    problems, reported before anything is sent to the API.
    """
    base_path = base_path.strip().strip("/")
    if not base_path:
        typer.secho("--base-path must not be empty.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)

    ontology_dir = path / Path(base_path)
    if not ontology_dir.is_dir():
        typer.secho(
            f"No {base_path}/ directory found under {path}. "
            "If the project exports to a custom path, pass it with --base-path (or CASSIS_BASE_PATH).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)

    files = _collect_files(ontology_dir)
    if not files:
        typer.secho(f"No YAML files found under {ontology_dir}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)

    total_bytes = sum(len(content.encode()) for content in files.values())
    if len(files) > MAX_FILES or total_bytes > MAX_TOTAL_BYTES:
        typer.secho(
            f"Ontology tree too large: {len(files)} files / {total_bytes / (1024 * 1024):.1f} MB "
            f"(limits: {MAX_FILES} files / {MAX_TOTAL_BYTES // (1024 * 1024)} MB). "
            "Check that --base-path points at the ontology directory, not a larger tree.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    return files, base_path


@app.command()
def check(
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
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON response."),
) -> None:
    """Validate the ontology files in a repository checkout.

    Runs the same checks as the Cassis GitHub PR check (YAML parsing,
    round-trip, import validation). Exits 0 when valid, 1 when validation
    fails, 2 on usage errors, 3 on transport/API errors.
    """
    api_key = _require_api_key(api_key)
    files, base_path = _collect_tree(path, base_path)

    try:
        result = post_ontology_check(api_url=api_url, api_key=api_key, files=files)
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc
    except ApiError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_TRANSPORT) from exc

    if json_output:
        typer.echo(json.dumps(result, indent=2))
    elif result["passed"]:
        typer.secho(f"✓ {result['summary']}", fg=typer.colors.GREEN)
    else:
        typer.secho(result["title"], fg=typer.colors.RED, bold=True)
        typer.echo(result["summary"])
        for finding in result["findings"]:
            location = f"{base_path}/{finding.get('path')}: " if finding.get("path") else ""
            typer.echo(f"  {location}{finding.get('message', '')} ({finding.get('stage', '?')})")

    raise typer.Exit(EXIT_OK if result["passed"] else EXIT_VALIDATION_FAILED)


@app.command()
def upload(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (the directory containing the ontology export path).",
    ),
    project_id: str = typer.Option(
        ...,
        "--project",
        envvar="CASSIS_PROJECT_ID",
        help="Target Cassis project ID (UUID, shown in the project's URL).",
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
    try:
        UUID(project_id)
    except ValueError:
        typer.secho(f"--project must be a project ID (UUID), got {project_id!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    files, base_path = _collect_tree(path, base_path)

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
