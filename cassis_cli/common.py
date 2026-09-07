"""Helpers shared by the `cassis` subcommands (tree collection, auth, exit codes)."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional, TypeVar
from uuid import UUID

import typer
from cassis_cli.api import ApiError, AuthError

# Repository directory the ontology tree is exported under. Must match the
# project's git-sync "Path" setting in Cassis (default "cassis").
DEFAULT_BASE_PATH = "cassis"

# Exit codes (documented in the README; stable contract for CI scripts).
EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_USAGE = 2
EXIT_TRANSPORT = 3
# Conventional "terminated by SIGINT" code: the user pressed Ctrl-C while a
# command was waiting on a server-side run (which the command cancels first).
EXIT_INTERRUPTED = 130

# Request ceilings of the /api/ci file-tree endpoints, mirrored so oversized
# trees fail fast with a clear message before any upload. Sized for ~10,000
# modeled tables (a 6,000-table tree is ~8,600 files / ~60 MB). Source of
# truth: backend/app/services/ontology_check.py, enforced by
# backend/app/schemas/ci.py (the server's 422 remains the backstop).
MAX_FILES = 20_000
MAX_TOTAL_BYTES = 100 * 1024 * 1024
# Mirrors the server's DDL upload ceiling (backend/app/endpoints/uploads.py).
MAX_DDL_BYTES = 10 * 1024 * 1024

# Consecutive poll failures tolerated before a wait gives up: covers transient
# blips (LB hiccup, brief network loss) without letting a permanently broken
# poll (deleted run/project) spin until --timeout.
MAX_CONSECUTIVE_POLL_FAILURES = 5

T = TypeVar("T")


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


def sync_ontology_tree(
    ontology_dir: Path,
    files: "dict[str, str]",
    *,
    prune: bool = True,
    skip_unchanged: bool = False,
    before_delete: "Optional[Callable[[list[str]], None]]" = None,
) -> "tuple[list[str], list[str], list[dict[str, str]]]":
    """Write a server-rendered ontology tree under ``ontology_dir``; prune what git can restore.

    The write/prune contract shared by ``ontology pull`` and ``schema apply``.
    Returns ``(written, deleted, kept)``: ``written`` lists every file written
    (with ``skip_unchanged``, only those whose content actually changed),
    ``deleted`` the stale ontology files removed, ``kept`` the stale files left
    in place as ``{"path", "reason"}`` entries. ``before_delete`` is called with
    the paths about to be deleted, when there are any, so a command can announce
    them first.

    The server controls the paths: anything escaping ``ontology_dir`` exits 3
    rather than being trusted. A file that cannot be written or deleted exits 2
    (a local checkout problem: permissions, dir/file collision). Pruning only
    ever deletes a stale file that is tracked and unmodified in git: an
    untracked or locally modified file is user work Cassis has never seen, and
    outside a git work tree nothing is deleted at all (#27).
    """
    ontology_dir = ontology_dir.resolve()
    written: list[str] = []
    for rel, content in sorted(files.items()):
        dest = (ontology_dir / rel).resolve()
        if not dest.is_relative_to(ontology_dir):
            typer.secho(f"Refusing to write outside {ontology_dir}: {rel!r}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_TRANSPORT)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if skip_unchanged and dest.exists() and dest.read_text(encoding="utf-8") == content:
                continue
            dest.write_text(content, encoding="utf-8")
        except OSError as exc:
            typer.secho(f"Cannot write {dest}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE) from exc
        written.append(rel)

    deleted: list[str] = []
    kept: list[dict[str, str]] = []
    if prune and ontology_dir.is_dir():
        stale = sorted(set(collect_files(ontology_dir)) - set(files))
        if stale:
            states = git_file_states(ontology_dir)
            to_delete: list[str] = []
            if states is None:
                kept = [{"path": rel, "reason": "not in a git repository"} for rel in stale]
            else:
                tracked, dirty = states
                for rel in stale:
                    if rel not in tracked:
                        kept.append({"path": rel, "reason": "untracked in git"})
                    elif rel in dirty:
                        kept.append({"path": rel, "reason": "locally modified"})
                    else:
                        to_delete.append(rel)
            if to_delete and before_delete is not None:
                before_delete(to_delete)
            for rel in to_delete:
                try:
                    (ontology_dir / rel).unlink()
                except OSError as exc:
                    typer.secho(f"Cannot delete {ontology_dir / rel}: {exc}", fg=typer.colors.RED, err=True)
                    raise typer.Exit(EXIT_USAGE) from exc
                deleted.append(rel)
    return written, deleted, kept


def _echo_error(exc: Exception) -> None:
    typer.secho(str(exc), fg=typer.colors.RED, err=True)


def echo_poll_retry(exc: ApiError) -> None:
    """The standard "(poll failed, retrying: ...)" notice, for ``poll_until(on_retry=...)``."""
    typer.secho(f"(poll failed, retrying: {exc})", fg=typer.colors.YELLOW, err=True)


def poll_until(
    fetch: "Callable[[], T]",
    is_terminal: "Callable[[T], bool]",
    *,
    poll_interval: float,
    timeout: float,
    on_timeout: "Callable[[T], None]",
    max_consecutive_failures: int = MAX_CONSECUTIVE_POLL_FAILURES,
    on_progress: "Optional[Callable[[T], None]]" = None,
    on_auth_error: "Optional[Callable[[AuthError], None]]" = None,
    on_retry: "Optional[Callable[[ApiError], None]]" = None,
    on_give_up: "Optional[Callable[[ApiError, int], None]]" = None,
) -> T:
    """Call ``fetch`` every ``poll_interval`` seconds until ``is_terminal`` accepts what it returned.

    The one wait loop behind every ``--wait``: the record is fetched at least
    once (so ``--timeout 0`` means "poll once"), ``on_progress`` sees every
    record fetched, and the terminal record is returned. Exits 3 (transport),
    after the matching callback has printed its message, when:

    - the deadline passes while the record is still not terminal
      (``on_timeout`` gets the last record);
    - ``fetch`` raises ``AuthError`` (fail fast: retrying a revoked key can't
      succeed; ``on_auth_error``, default: the error text);
    - ``fetch`` raises ``ApiError`` ``max_consecutive_failures`` times in a row
      (``on_give_up``, default: the error text). Each tolerated failure is
      reported through ``on_retry`` (default: silent) and the wait continues,
      so a transient blip never abandons a multi-minute run.

    Anything else ``fetch`` raises, ``typer.Exit`` and ``KeyboardInterrupt``
    included, propagates untouched: the caller decides whether to cancel the
    server-side run on Ctrl-C.
    """
    deadline = time.monotonic() + timeout
    failures = 0
    while True:
        try:
            record = fetch()
        except AuthError as exc:
            (on_auth_error or _echo_error)(exc)
            raise typer.Exit(EXIT_TRANSPORT) from exc
        except ApiError as exc:
            failures += 1
            if failures >= max_consecutive_failures:
                if on_give_up is not None:
                    on_give_up(exc, failures)
                else:
                    _echo_error(exc)
                raise typer.Exit(EXIT_TRANSPORT) from exc
            if on_retry is not None:
                on_retry(exc)
            time.sleep(poll_interval)
            continue
        failures = 0
        if on_progress is not None:
            on_progress(record)
        if is_terminal(record):
            return record
        if time.monotonic() >= deadline:
            on_timeout(record)
            raise typer.Exit(EXIT_TRANSPORT)
        time.sleep(poll_interval)


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
