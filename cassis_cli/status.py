"""`cassis status` — published version vs repository head, from the terminal.

One command answers what previously needed the webapp or the GitHub Actions
tab: which version is published, whether it matches the local checkout, and
whether anything is awaiting publication. `--watch` polls until the published
version catches up with the local head (e.g. right after merging a PR whose
CI publishes the ontology).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional, Tuple

import typer
from cassis_cli.api import DEFAULT_API_URL, ApiError, AuthError, get_project_status
from cassis_cli.common import (
    DEFAULT_BASE_PATH,
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    poll_until,
    require_api_key,
    resolve_project_id,
)


def _git(path: Path, *args: str) -> Optional[str]:
    """Run a git command in the checkout; None on failure, stdout (possibly empty) on success."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
    # Two separate handlers: the repo-wide black targets py314 and would strip
    # the parens off a tuple form, which is a SyntaxError on the CLI's
    # supported Python (>=3.10).
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _local_comparison(path: Path, head: Optional[str], published_sha: Optional[str]) -> "Tuple[str, bool]":
    """Describe the local checkout vs the published commit. Returns (text, in_sync)."""
    if head is None:
        return "not a git checkout (no local comparison)", False
    if published_sha is None:
        return f"at {head[:9]} (the published version records no git commit)", False
    if head == published_sha:
        return f"in sync with the published version ({head[:9]})", True
    if _git(path, "merge-base", "--is-ancestor", published_sha, "HEAD") is not None:
        count = _git(path, "rev-list", "--count", f"{published_sha}..HEAD") or "?"
        return f"{count} commit(s) ahead of the published version ({published_sha[:9]})", False
    if _git(path, "cat-file", "-e", f"{published_sha}^{{commit}}") is None:
        return (
            f"published commit {published_sha[:9]} not found locally (run git fetch, or the checkout is behind)",
            False,
        )
    if _git(path, "merge-base", "--is-ancestor", "HEAD", published_sha) is not None:
        count = _git(path, "rev-list", "--count", f"HEAD..{published_sha}") or "?"
        return f"{count} commit(s) behind the published version ({published_sha[:9]}), run git pull", False
    return f"diverged from the published commit {published_sha[:9]}", False


def _render(status_record: "dict[str, Any]", comparison_text: str) -> None:
    published = status_record.get("published_version")
    if published:
        label = f" {published['label']!r}" if published.get("label") else ""
        sha = published.get("git_commit_sha")
        sha_text = f", commit {sha[:9]}" if sha else ""
        typer.echo(f"Published: v{published['version']}{label} ({published.get('published_at')}{sha_text})")
    else:
        typer.echo("Published: nothing yet")
    changes = "yes" if status_record.get("has_unpublished_changes") else "no"
    typer.echo(f"Unpublished changes: {changes}")
    git_sync = status_record.get("git_sync")
    if git_sync:
        typer.echo(f"Git sync: {git_sync['provider']} {git_sync['repo']} (path {git_sync['base_path']})")
    else:
        typer.echo("Git sync: not configured")
    schema_plan = status_record.get("schema_plan")
    if isinstance(schema_plan, dict) and schema_plan.get("status") == "ready":
        changes = schema_plan.get("ontology_changes")
        changes_text = f", {changes} ontology change(s)" if changes is not None else ""
        typer.echo(f"Schema plan: ready{changes_text} (cassis schema apply --plan {schema_plan.get('id')})")
    elif isinstance(schema_plan, dict) and schema_plan.get("status") in ("planning", "applying"):
        typer.echo(f"Schema plan: {schema_plan['status']}")
    typer.echo(f"Local checkout: {comparison_text}")


def status(
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
    watch: bool = typer.Option(
        False,
        "--watch",
        help="Poll until the published version's commit matches the local git HEAD.",
    ),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="Seconds between polls with --watch."),
    timeout: float = typer.Option(600.0, "--timeout", help="Give up watching after this many seconds."),
    json_output: bool = typer.Option(False, "--json", help="Print the status as raw JSON."),
) -> None:
    """Show the project's published version vs the local checkout.

    Prints the published head (version, label, commit), whether unpublished
    changes are awaiting publication, the git-sync binding, and how the local
    git HEAD relates to the published commit. With --watch, polls until the
    published commit equals the local HEAD (a publish of your merge landing),
    then exits 0. Exits 0 on success, 2 on usage errors, 3 on transport/API
    errors or a --watch timeout.
    """
    api_key = require_api_key(api_key)
    project_id = resolve_project_id(project_id, path / base_path, quiet=json_output)
    head = _git(path, "rev-parse", "HEAD")

    if watch and head is None:
        typer.secho("--watch needs a git checkout (no local HEAD to compare against).", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)

    last_line: "dict[str, Optional[str]]" = {"text": None}

    def fetch() -> "Tuple[dict[str, Any], str, bool]":
        """The status record plus the local comparison it implies: (record, comparison_text, in_sync)."""
        record = get_project_status(api_url=api_url, api_key=api_key, project_id=project_id)
        published = record.get("published_version") or {}
        comparison_text, in_sync = _local_comparison(path, head, published.get("git_commit_sha"))
        return record, comparison_text, in_sync

    def report(snapshot: "Tuple[dict[str, Any], str, bool]") -> None:
        record, comparison_text, in_sync = snapshot
        if json_output:
            typer.echo(json.dumps({**record, "local": {"head": head, "in_sync": in_sync}}, indent=2))
        elif not watch:
            _render(record, comparison_text)
        else:
            published = record.get("published_version") or {}
            published_sha = published.get("git_commit_sha")
            version_text = f"v{published['version']}" if published else "nothing published"
            line = f"{version_text} (commit {published_sha[:9] if published_sha else 'none'}); local: {comparison_text}"
            if line != last_line["text"]:
                typer.echo(line)
                last_line["text"] = line

    def on_timeout(_snapshot: "Tuple[dict[str, Any], str, bool]") -> None:
        typer.secho(
            f"Timed out after {timeout:.0f}s: the published version still does not match the local HEAD.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    if not watch:
        try:
            report(fetch())
        except (AuthError, ApiError) as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_TRANSPORT) from exc
        raise typer.Exit(EXIT_OK)

    # Any poll failure ends the watch (max_consecutive_failures=1): unlike a
    # server-side run, nothing is lost by exiting, and the user re-runs it.
    poll_until(
        fetch,
        lambda snapshot: snapshot[2],
        poll_interval=poll_interval,
        timeout=timeout,
        on_timeout=on_timeout,
        max_consecutive_failures=1,
        on_progress=report,
    )
    typer.secho("✓ Published version matches the local HEAD.", fg=typer.colors.GREEN)
    raise typer.Exit(EXIT_OK)
