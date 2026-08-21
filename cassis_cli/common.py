"""Helpers shared by the `cassis` subcommands (tree collection, auth, exit codes)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional
from uuid import UUID

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
# trees fail fast with a clear message before any upload. Sized for ~10,000
# modeled tables (a 6,000-table tree is ~8,600 files / ~60 MB). Source of
# truth: backend/app/services/ontology_check.py, enforced by
# backend/app/schemas/ci.py (the server's 422 remains the backstop).
MAX_FILES = 20_000
MAX_TOTAL_BYTES = 100 * 1024 * 1024


def api_failure(exc: Exception) -> "typer.Exit":
    """Print a transport/API error and return the transport exit code."""
    typer.secho(str(exc), fg=typer.colors.RED, err=True)
    return typer.Exit(EXIT_TRANSPORT)


def one_line(value: object) -> str:
    """Collapse a possibly-multiline value into one trimmed line for list rows."""
    return str(value or "").replace("\n", " ").strip()


def is_ontology_file(rel_path: str) -> bool:
    """Whether a base-relative path is an ontology file the server reads.

    YAML (``project.yml``, tables, joins, metrics, legacy domains) plus domain
    Markdown — every domain is the ``README.md`` of its folder, root included
    (``domains/README.md``), so ``domains/**/README.md`` covers them all. Mirrors
    the server's ``ontology_fs.is_ontology_tree_file`` (kept in sync by hand —
    the CLI can't import the backend). Excludes the managed ``AGENTS.md`` and any
    stray Markdown note, so neither is uploaded nor deleted by ``pull --prune``.
    """
    if rel_path.endswith((".yml", ".yaml")):
        return True
    return rel_path.startswith("domains/") and rel_path.endswith("/README.md")


def git_file_states(directory: Path) -> Optional[tuple[set[str], set[str]]]:
    """(tracked, dirty) path sets for files under ``directory``, relative to it.

    ``tracked`` is every git-tracked file below the directory; ``dirty`` the
    subset whose working-tree content differs from the index (modified or
    missing). Returns ``None`` when the directory is not inside a git work tree
    or git is unavailable — callers must then treat every file as
    unrecoverable and refuse to delete it.
    """
    # GIT_OPTIONAL_LOCKS=0: read-only queries must not take the index lock
    # (and fail) when another git process is running.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        tracked_proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=directory,
            env=env,
            capture_output=True,
            check=True,
            text=True,
        )
        dirty_proc = subprocess.run(
            ["git", "ls-files", "-z", "--modified"],
            cwd=directory,
            env=env,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        return None
    tracked = {p for p in tracked_proc.stdout.split("\0") if p}
    dirty = {p for p in dirty_proc.stdout.split("\0") if p}
    return tracked, dirty


def is_legacy_domain_file(rel_path: str) -> bool:
    """Whether a base-relative path is a legacy (pre-Markdown) domain file.

    Used to report the one-time migration to the Markdown domain format, when
    ``pull``/``fmt`` remove a ``_project.yml``/``_domain.yml`` and write the
    ``domains/README.md`` (and sub-domain ``README.md``) that replaces it.
    """
    return rel_path == "_project.yml" or rel_path.endswith("/_domain.yml")


def collect_files(ontology_dir: Path) -> dict[str, str]:
    """Read every ontology file under the ontology dir, keyed by posix relpath.

    Ontology files are YAML and domain Markdown (see ``is_ontology_file``); the
    managed ``AGENTS.md`` and stray notes are skipped. Exits 2 (usage) on an
    unreadable or non-UTF-8 file — a local checkout problem, reported before
    anything is sent to the API.
    """
    files: dict[str, str] = {}
    for pattern in ("**/*.yml", "**/*.yaml", "**/*.md"):
        for file in sorted(ontology_dir.glob(pattern)):
            if not file.is_file():
                continue
            rel = file.relative_to(ontology_dir).as_posix()
            if not is_ontology_file(rel):
                continue
            try:
                files[rel] = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                typer.secho(f"Cannot read {file}: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(EXIT_USAGE) from exc
    return files


# project.yml is a two-line machine-written file (`cassis_format_version`, `project_id`);
# match the id line directly rather than pull in a YAML parser just for this.
_PROJECT_ID_LINE = re.compile(r"^project_id:\s*['\"]?([^'\"\s]+)['\"]?\s*$")


def read_project_id_from_dir(ontology_dir: Path) -> Optional[str]:
    """Return the ``project_id`` recorded in ``<ontology_dir>/project.yml``, or None."""
    try:
        text = (ontology_dir / "project.yml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        match = _PROJECT_ID_LINE.match(line.strip())
        if match:
            return match.group(1)
    return None


def resolve_project_id(
    project_id: Optional[str], ontology_dir: Path, *, optional: bool = False, quiet: bool = False
) -> Optional[str]:
    """Resolve the target project id, defaulting to the checkout's ``project.yml``.

    Precedence: an explicit ``--project`` / ``CASSIS_PROJECT_ID`` wins; otherwise
    the ``project_id`` recorded in ``<base-path>/project.yml`` (written by
    ``pull`` / publish) is used, and where it came from is noted on stderr so a
    stale value in a copied repo is visible (``quiet`` suppresses the note for
    machine-readable output). Exits 2 (usage) when the value isn't a UUID, or —
    unless ``optional`` — when no value is available at all; with ``optional``,
    an unbound checkout returns None (``check`` falls back to the project-less
    validation).
    """
    from_file = False
    if not project_id:
        project_id = read_project_id_from_dir(ontology_dir)
        from_file = project_id is not None
    if not project_id:
        if optional:
            return None
        typer.secho(
            f"No project. Pass --project (or set CASSIS_PROJECT_ID), or run in a checkout whose "
            f"{ontology_dir.name}/project.yml records it (written by `cassis ontology pull` or a publish).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    try:
        UUID(project_id)
    except ValueError:
        typer.secho(f"--project must be a project ID (UUID), got {project_id!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    if from_file and not quiet:
        typer.secho(f"Using project {project_id} from {ontology_dir.name}/project.yml.", fg=typer.colors.CYAN, err=True)
    return project_id


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
    """Resolve the ontology dir under the checkout and read its file tree.

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
        typer.secho(f"No ontology files found under {ontology_dir}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)

    # Count path bytes too, exactly like the server's _validate_tree_files —
    # a tree accepted here must never come back as a server-side 422.
    total_bytes = sum(len(rel.encode()) + len(content.encode()) for rel, content in files.items())
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
