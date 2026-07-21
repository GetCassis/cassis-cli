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
    get_ontology_export,
    post_ontology_check,
    post_ontology_import,
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
from cassis_cli.common import require_api_key as _require_api_key

app = typer.Typer(no_args_is_help=True, help="Ontology commands.")


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
def pull(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (the directory containing the ontology export path).",
    ),
    project_id: str = typer.Option(
        ...,
        "--project",
        envvar="CASSIS_PROJECT_ID",
        help="Source Cassis project ID (UUID, shown in the project's URL).",
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
        help="Delete local YAML files that no longer exist in the project's ontology (default: prune).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print a JSON summary of written/deleted files."),
) -> None:
    """Download the project's unpublished ontology into a repository checkout.

    Writes the ontology YAML tree under the export path (full sync: files are
    overwritten and, unless --no-prune, stale local YAML files are deleted, so
    the checkout ends up matching the project exactly). Review the changes with
    git diff before committing. Exits 0 on success, 2 on usage errors, 3 on
    transport/API errors.
    """
    api_key = _require_api_key(api_key)
    try:
        UUID(project_id)
    except ValueError:
        typer.secho(f"--project must be a project ID (UUID), got {project_id!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    base_path = base_path.strip().strip("/")
    if not base_path:
        typer.secho("--base-path must not be empty.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)

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

    if json_output:
        typer.echo(json.dumps({"written": written, "deleted": deleted}, indent=2))
    else:
        summary = f"✓ Pulled {len(written)} files into {ontology_dir}"
        if deleted:
            summary += f" ({len(deleted)} stale files deleted)"
        typer.secho(f"{summary}.", fg=typer.colors.GREEN)
    raise typer.Exit(EXIT_OK)


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
