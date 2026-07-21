"""Helpers shared by the `cassis` subcommands (tree collection, auth, exit codes)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

# Repository directory the ontology tree is exported under. Must match the
# project's git-sync "Path" setting in Cassis (default "cassis").
DEFAULT_BASE_PATH = "cassis"

# Exit codes (documented in the README; stable contract for CI scripts).
EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_USAGE = 2
EXIT_TRANSPORT = 3

# Request ceilings of the /api/ci file-tree endpoints, mirrored so oversized
# trees fail fast with a clear message before any upload. Source of truth:
# backend/app/schemas/ci.py (the server's 422 remains the backstop).
MAX_FILES = 2000
MAX_TOTAL_BYTES = 5 * 1024 * 1024


def collect_files(ontology_dir: Path) -> dict[str, str]:
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


def require_api_key(api_key: Optional[str]) -> str:
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


def collect_tree(path: Path, base_path: str) -> "tuple[dict[str, str], str]":
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

    files = collect_files(ontology_dir)
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
