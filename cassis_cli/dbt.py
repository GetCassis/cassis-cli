"""`cassis dbt`: send the artifacts dbt writes to `target/` to Cassis.

`cassis dbt push` runs in the dbt project's CI after a merge to the production
branch, once `dbt docs generate` has written the project's artifacts. It
uploads `manifest.json` (required) and `catalog.json` (when present),
gzip-compressed, with the commit they were built from. Cassis checks the
files, matches every dbt relation to a table of the project's data source,
and keeps the upload as the data source's dbt baseline unless a newer one is
already there. It compares the upload with the previous baseline and files
what the change puts in doubt in the context as issues.

`cassis dbt check` runs on a pull request, after `dbt compile` (or `dbt docs
generate`): the same upload, compared with the baseline, never becoming it.
It prints the parts of the context the change puts in doubt; nothing is filed
and nothing the agent answers with changes.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import typer
from cassis_cli.api import (
    DEFAULT_API_URL,
    ApiError,
    AuthError,
    DbtUploadRejectedError,
    get_dbt_observation,
    post_dbt_observation,
)
from cassis_cli.common import (
    DEFAULT_BASE_PATH,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
    api_failure,
    echo_poll_retry,
    poll_until,
    require_api_key,
    resolve_project_id,
)

app = typer.Typer(help="Send your dbt project's artifacts to Cassis.")

ARTIFACTS = ("manifest.json", "catalog.json")
# Mirror the server's upload limits (backend/app/services/dbt/artifacts.py and
# endpoints/ci/dbt.py), so an oversized upload fails here, before sending.
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_PR_TITLE_CHARS = 1_000
MAX_GIT_TEXT_CHARS = 65_536

_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_TERMINAL = {"accepted", "superseded", "checked", "rejected", "failed"}
_MATCH_LABELS = (
    ("matched", "matched to a table of the data source"),
    ("not_in_source_schema", "not in the data source's schema"),
    ("outside_data_source", "in another database than the data source's"),
    ("not_in_catalog", "missing from catalog.json"),
    ("catalog_error", "missing from catalog.json, which reported errors"),
    ("no_relation", "with no relation in the warehouse (ephemeral)"),
)
_DETERMINISTIC = {"relation_removed", "column_removed"}
# What filing did with each finding's issue, as `dbt push` reports it.
_ISSUE_STATES = {"filed": "new", "recurred": "seen again", "reopened": "reopened"}

_TARGET_ARGUMENT = typer.Argument(
    Path("target"), help="dbt's target directory, holding manifest.json (and catalog.json)."
)
_COMMIT_OPTION = typer.Option(
    None, "--commit", help="SHA of the commit the artifacts were built from. Default: HEAD of the git checkout."
)
_COMMIT_MESSAGE_OPTION = typer.Option(None, "--commit-message", help="Default: read from git for the commit.")
_PR_TITLE_OPTION = typer.Option(
    None,
    "--pr-title",
    help="Default: the merge request or pull request title, on GitLab CI and GitHub Actions. After a merge: "
    "the title of the pull request that merged the commit (GitHub Actions, with GITHUB_TOKEN set), or the "
    "merge request title a GitLab merge commit carries.",
)
_PR_DESCRIPTION_OPTION = typer.Option(
    None,
    "--pr-description",
    help="Default: its description, on GitLab CI and GitHub Actions (after a merge: GitHub Actions only).",
)
_PROJECT_OPTION = typer.Option(
    None,
    "--project",
    envvar="CASSIS_PROJECT_ID",
    help="Cassis project ID (default: the project_id in <base-path>/project.yml, if the repo has one).",
)
_API_KEY_OPTION = typer.Option(None, "--api-key", envvar="CASSIS_API_KEY", help="Cassis API key.")
_API_URL_OPTION = typer.Option(DEFAULT_API_URL, "--api-url", envvar="CASSIS_API_URL", help="Cassis API base URL.")
_APP_URL_OPTION = typer.Option(
    None,
    "--app-url",
    envvar="CASSIS_APP_URL",
    help="Cassis webapp base URL, used for the links to filed issues (default: the API URL).",
)
_BASE_PATH_OPTION = typer.Option(
    DEFAULT_BASE_PATH, "--base-path", envvar="CASSIS_BASE_PATH", help="Context directory holding project.yml."
)
_POLL_INTERVAL_OPTION = typer.Option(2.0, "--poll-interval", help="Seconds between polls.")
_TIMEOUT_OPTION = typer.Option(
    600.0, "--timeout", help="Give up waiting after this many seconds (exit 3; the upload keeps processing)."
)
_JSON_OPTION = typer.Option(False, "--json", help="Print the upload record as raw JSON (the summary goes to stderr).")


@app.command()
def push(
    target_dir: Path = _TARGET_ARGUMENT,
    commit: Optional[str] = _COMMIT_OPTION,
    commit_message: Optional[str] = _COMMIT_MESSAGE_OPTION,
    pr_title: Optional[str] = _PR_TITLE_OPTION,
    pr_description: Optional[str] = _PR_DESCRIPTION_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    app_url: Optional[str] = _APP_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    poll_interval: float = _POLL_INTERVAL_OPTION,
    timeout: float = _TIMEOUT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Upload dbt's artifacts as the project's new dbt baseline.

    Run it after `dbt docs generate` on the production target, which writes
    manifest.json and catalog.json. Cassis keeps the upload as the baseline
    unless a newer one is already accepted, compares it with the previous
    baseline, and files each part of the context the change puts in doubt as
    an issue (impact "at risk"). Exits 0 when accepted or superseded by a
    newer upload, 1 when rejected (invalid files, or no relation matches the
    data source), 2 on usage errors, 3 on transport errors or a timeout (the
    upload keeps processing server-side), or when Cassis fails on its side
    while processing it.
    """
    record = _upload_and_wait(
        "push",
        target_dir=target_dir,
        commit=commit,
        commit_message=commit_message,
        pr_title=pr_title,
        pr_description=pr_description,
        project_id=project_id,
        api_key=api_key,
        api_url=api_url,
        base_path=base_path,
        poll_interval=poll_interval,
        timeout=timeout,
        json_output=json_output,
    )
    resolved_project = str(record.pop("_project_id"))
    _render_push(record, err=json_output)
    _render_filed(record, app_url=app_url or api_url, project_id=resolved_project, err=json_output)
    if json_output:
        typer.echo(json.dumps(record, indent=2))
    raise typer.Exit(_outcome_exit_code(record, ok=("accepted", "superseded")))


@app.command()
def check(
    target_dir: Path = _TARGET_ARGUMENT,
    commit: Optional[str] = _COMMIT_OPTION,
    commit_message: Optional[str] = _COMMIT_MESSAGE_OPTION,
    pr_title: Optional[str] = _PR_TITLE_OPTION,
    pr_description: Optional[str] = _PR_DESCRIPTION_OPTION,
    project_id: Optional[str] = _PROJECT_OPTION,
    api_key: Optional[str] = _API_KEY_OPTION,
    api_url: str = _API_URL_OPTION,
    base_path: str = _BASE_PATH_OPTION,
    poll_interval: float = _POLL_INTERVAL_OPTION,
    timeout: float = _TIMEOUT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Check a pull request's dbt change against the context.

    Run it on the pull request after `dbt compile` (or `dbt docs generate`)
    on the production target. Cassis compares the artifacts with the
    project's dbt baseline (the last `cassis dbt push`), finds the context
    tables whose dbt model changed or is built from a changed one, and lists
    the parts of the context the change puts in doubt. Findings are advisory:
    nothing is filed and the baseline doesn't move. Exits 0 with or without
    findings, 1 when the upload is rejected (invalid files, or no baseline
    yet: run `cassis dbt push` on the production branch first), 2 on usage
    errors, 3 on transport errors or a timeout (the upload keeps processing
    server-side), or when Cassis fails on its side while processing it.
    """
    record = _upload_and_wait(
        "check",
        target_dir=target_dir,
        commit=commit,
        commit_message=commit_message,
        pr_title=pr_title,
        pr_description=pr_description,
        project_id=project_id,
        api_key=api_key,
        api_url=api_url,
        base_path=base_path,
        poll_interval=poll_interval,
        timeout=timeout,
        json_output=json_output,
    )
    record.pop("_project_id")
    _render_check(record, err=json_output)
    if json_output:
        typer.echo(json.dumps(record, indent=2))
    raise typer.Exit(_outcome_exit_code(record, ok=("checked",)))


def _outcome_exit_code(record: "dict[str, Any]", *, ok: "tuple[str, ...]") -> int:
    """The upload's terminal status as an exit code: 0 when it went through, 1 when Cassis refused it
    (something to fix in the upload), 3 when Cassis failed on its side (nothing to fix here: push again)."""
    status = record.get("status")
    if status in ok:
        return EXIT_OK
    return EXIT_TRANSPORT if status == "failed" else EXIT_VALIDATION_FAILED


def _upload_and_wait(
    purpose: str,
    *,
    target_dir: Path,
    commit: Optional[str],
    commit_message: Optional[str],
    pr_title: Optional[str],
    pr_description: Optional[str],
    project_id: Optional[str],
    api_key: Optional[str],
    api_url: str,
    base_path: str,
    poll_interval: float,
    timeout: float,
    json_output: bool,
) -> "dict[str, Any]":
    """Read, gzip and upload the artifacts, then poll until Cassis has processed them.

    Returns the final record, with the resolved project id under ``_project_id``.
    Exits on usage, transport and refusal errors.
    """
    api_key = require_api_key(api_key)
    resolved_project = resolve_project_id(project_id, Path(base_path), quiet=json_output)
    assert resolved_project is not None
    files = _read_artifacts(target_dir)
    sha = _resolve_commit(commit, target_dir)
    fields = {"git_commit_sha": sha, "purpose": purpose}
    if commit_message is None:
        commit_message = _git_message(sha, target_dir)
    if pr_title is None or pr_description is None:
        ci_title, ci_description = _ci_pull_request(sha, commit_message)
        pr_title = pr_title if pr_title is not None else ci_title
        pr_description = pr_description if pr_description is not None else ci_description
    for key, value, limit in (
        ("git_commit_message", commit_message, None),
        ("pr_title", pr_title, MAX_PR_TITLE_CHARS),
        ("pr_description", pr_description, None),
    ):
        if value and value.strip():
            fields[key] = value.strip()[: limit or MAX_GIT_TEXT_CHARS]

    sizes = ", ".join(f"{name} ({len(content) / (1024 * 1024):.1f} MB gzipped)" for name, content in files.items())
    typer.echo(f"Uploading {sizes} for commit {sha[:12]}…", err=True)
    try:
        record = post_dbt_observation(
            api_url=api_url, api_key=api_key, project_id=resolved_project, files=files, fields=fields
        )
    except DbtUploadRejectedError as exc:
        typer.secho(f"Cassis did not accept the upload: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_VALIDATION_FAILED) from exc
    except (AuthError, ApiError) as exc:
        raise api_failure(exc) from exc
    if record.get("purpose", "push") != purpose:
        typer.secho(
            f"The Cassis server does not support `cassis dbt {purpose}` yet; upgrade it or use a matching CLI.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_TRANSPORT)

    observation_id = str(record["id"])
    typer.echo(f"Processing upload {observation_id}…", err=True)

    def on_timeout(last: "dict[str, Any]") -> None:
        typer.secho(
            f"Timed out after {timeout:g}s: upload {observation_id} is still {last.get('status', '?')}. "
            "It keeps processing server-side.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    try:
        record = poll_until(
            lambda: get_dbt_observation(
                api_url=api_url, api_key=api_key, project_id=resolved_project, observation_id=observation_id
            ),
            lambda current: current.get("status") in _TERMINAL,
            poll_interval=poll_interval,
            timeout=timeout,
            on_timeout=on_timeout,
            on_retry=echo_poll_retry,
        )
    except KeyboardInterrupt:
        typer.secho(f"Interrupted. Upload {observation_id} keeps processing server-side.", err=True)
        raise typer.Exit(EXIT_INTERRUPTED)
    return {**record, "_project_id": resolved_project}


def _read_artifacts(target_dir: Path) -> "dict[str, bytes]":
    """Read and gzip the artifacts present in ``target_dir``. Exits 2 without a manifest, or past the size limits."""
    if not (target_dir / "manifest.json").is_file():
        typer.secho(
            f"No manifest.json in {target_dir}. Pass dbt's target directory, after dbt has written it "
            "(`dbt docs generate`, or `dbt compile` for a check).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    files: "dict[str, bytes]" = {}
    manifest_generated_at: Optional[str] = None
    for name in ARTIFACTS:
        path = target_dir / name
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            typer.secho(f"Cannot read {path}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_USAGE) from exc
        if name == "manifest.json":
            manifest_generated_at = _generated_at(raw)
        elif name == "catalog.json" and _is_older(_generated_at(raw), manifest_generated_at):
            # A catalog left by an earlier `dbt docs generate` describes other relations.
            typer.secho(
                f"Skipping {path}: it was generated at {_generated_at(raw)}, before manifest.json "
                f"({manifest_generated_at}). Run `dbt docs generate` to upload a matching catalog.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            continue
        if len(raw) > MAX_UNCOMPRESSED_BYTES:
            typer.secho(
                f"{name} is {len(raw) / (1024 * 1024):.0f} MB; Cassis accepts up to "
                f"{MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(EXIT_USAGE)
        compressed = gzip.compress(raw, mtime=0)
        if len(compressed) > MAX_COMPRESSED_BYTES:
            typer.secho(
                f"{name} is {len(compressed) / (1024 * 1024):.0f} MB gzipped; Cassis accepts up to "
                f"{MAX_COMPRESSED_BYTES // (1024 * 1024)} MB.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(EXIT_USAGE)
        files[name] = compressed
    return files


# dbt writes `metadata` first: its timestamp sits in the first bytes of the file.
_GENERATED_AT = re.compile(rb'"generated_at"\s*:\s*"([^"]+)"')


def _generated_at(raw: bytes) -> Optional[str]:
    """The artifact's `metadata.generated_at`, read from the start of the file without parsing it all."""
    match = _GENERATED_AT.search(raw[:65536])
    return match.group(1).decode("utf-8", "replace") if match else None


def _is_older(value: Optional[str], than: Optional[str]) -> bool:
    """Whether timestamp ``value`` is before ``than``; False when either is missing or unreadable."""
    if value is None or than is None:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) < datetime.fromisoformat(
            than.replace("Z", "+00:00")
        )
    except ValueError:
        return False


def _git(args: "list[str]", cwd: Path) -> Optional[str]:
    """Run a read-only git command; None when git is missing or the directory is not a checkout."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=True, text=True)
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        return None
    return result.stdout


def _git_dir(target_dir: Path) -> Path:
    resolved = target_dir.resolve()
    return resolved.parent if resolved.parent.is_dir() else Path.cwd()


def _resolve_commit(commit: Optional[str], target_dir: Path) -> str:
    """The commit to record: ``--commit``, else the checkout's HEAD. Exits 2 when neither gives a SHA."""
    sha = commit if commit is not None else (_git(["rev-parse", "HEAD"], _git_dir(target_dir)) or "").strip()
    if not sha:
        typer.secho(
            "No commit: pass --commit <sha>, or run from the dbt project's git checkout.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    if not _COMMIT_SHA.match(sha):
        typer.secho(f"--commit must be a commit SHA, got {sha!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    return sha


def _git_message(sha: str, target_dir: Path) -> Optional[str]:
    return _git(["log", "-1", "--format=%B", sha], _git_dir(target_dir))


def _ci_pull_request(sha: str, commit_message: Optional[str]) -> "tuple[Optional[str], Optional[str]]":
    """The title and description of the merge request or pull request behind the commit, as far as the CI tells.

    On a merge request or pull request pipeline, the CI exposes them. After a
    merge, a GitHub `push` event names no pull request: the GitHub API is
    asked which one merged the commit, when GITHUB_TOKEN is set. A GitLab
    merge commit carries its merge request's title in its message. Otherwise
    nothing: the commit message is sent anyway.
    """
    gitlab_title = os.environ.get("CI_MERGE_REQUEST_TITLE")
    gitlab_description = os.environ.get("CI_MERGE_REQUEST_DESCRIPTION")
    if gitlab_title or gitlab_description:
        return gitlab_title or None, gitlab_description or None
    pull_request = _github_event().get("pull_request")
    if isinstance(pull_request, dict):
        return _text(pull_request.get("title")), _text(pull_request.get("body"))
    if os.environ.get("GITHUB_EVENT_NAME") == "push":
        merged_by = _github_pull_request_of(sha)
        if merged_by is not None:
            return _text(merged_by.get("title")), _text(merged_by.get("body"))
    return _gitlab_merge_title(commit_message), None


def _github_event() -> "dict[str, Any]":
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return event if isinstance(event, dict) else {}


# Asking GitHub which pull request merged the commit is a nicety: it never holds the upload up for long.
GITHUB_API_TIMEOUT_SECONDS = 5.0


def _github_pull_request_of(sha: str) -> "Optional[dict[str, Any]]":
    """The pull request that merged ``sha``, from the GitHub API; None on any failure, which is never fatal.

    GitHub lists every pull request that contains the commit, open ones and
    those merged before it included: only a merged one whose merge commit is
    ``sha`` counts, so no other pull request's title reaches the issues.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        return None
    api_url = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    try:
        response = httpx.get(
            f"{api_url.rstrip('/')}/repos/{repository}/commits/{sha}/pulls",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=GITHUB_API_TIMEOUT_SECONDS,
        )
        pulls = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        # InvalidURL (a malformed GITHUB_API_URL) is neither an HTTPError nor a ValueError.
        return None
    if not isinstance(pulls, list):
        return None
    return next((pull for pull in pulls if _merged_as(pull, sha)), None)


def _merged_as(pull: Any, sha: str) -> bool:
    """Whether ``pull`` is a merged pull request whose merge commit is ``sha`` (which may be abbreviated)."""
    if not isinstance(pull, dict) or not pull.get("merged_at"):
        return False
    merge_sha = pull.get("merge_commit_sha")
    return isinstance(merge_sha, str) and merge_sha.lower().startswith(sha.lower())


_GITLAB_MERGE_REQUEST_LINE = re.compile(r"^See merge request \S+![0-9]+$")


def _gitlab_merge_title(message: Optional[str]) -> Optional[str]:
    """The merge request title a GitLab merge commit carries; None when the message is not one.

    GitLab's default merge commit message reads "Merge branch 'a' into 'b'",
    then the title, then "See merge request group/project!N". A custom
    template that doesn't open with "Merge branch" is taken to lead with the
    title.
    """
    if not message:
        return None
    lines = [line.strip() for line in message.strip().splitlines()]
    if not any(_GITLAB_MERGE_REQUEST_LINE.match(line) for line in lines):
        return None
    if not lines[0].startswith("Merge branch "):
        return lines[0] or None
    return next((line for line in lines[1:] if line and not _GITLAB_MERGE_REQUEST_LINE.match(line)), None)


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _render_push(record: "dict[str, Any]", *, err: bool) -> None:
    """Print the outcome, then the reconciliation counts."""
    status = record.get("status")
    detail = record.get("detail") or ""
    project = record.get("dbt_project_name") or "dbt project"
    version = record.get("dbt_version")
    about = f"{project}" + (f", dbt {version}" if version else "")
    if status == "accepted" and record.get("is_baseline") is False:
        # A newer push was accepted after this one: possible when the record is polled late.
        typer.secho(
            f"✓ Accepted ({about}), but a newer upload has since replaced it as the dbt baseline.",
            fg=typer.colors.GREEN,
            err=err,
        )
    elif status == "accepted":
        typer.secho(f"✓ Accepted as the data source's dbt baseline ({about}).", fg=typer.colors.GREEN, err=err)
    elif status == "superseded":
        typer.secho(f"Kept, but not the baseline. {detail}", fg=typer.colors.YELLOW, err=err)
    elif status == "rejected":
        typer.secho(f"Rejected: {detail}", fg=typer.colors.RED, err=err)
    else:
        typer.secho(f"Processing {status}: {detail}", fg=typer.colors.RED, err=err)

    reconciliation = record.get("reconciliation")
    if not isinstance(reconciliation, dict):
        return
    counts = reconciliation.get("relations") or {}
    typer.echo(f"Relations: {reconciliation.get('relation_count', 0)} in the manifest", err=err)
    for key, label in _MATCH_LABELS:
        if counts.get(key):
            typer.echo(f"  {counts[key]} {label}", err=err)
    typer.echo(f"Tests: {reconciliation.get('test_count', 0)} declared", err=err)
    if reconciliation.get("catalog_error_count"):
        typer.secho(
            f"catalog.json reported {reconciliation['catalog_error_count']} error(s): the relations it could not "
            "describe are compared without their real columns. Run `dbt docs generate` after a successful build.",
            fg=typer.colors.YELLOW,
            err=err,
        )
    if not reconciliation.get("catalog"):
        typer.echo(
            "No catalog.json was uploaded: run `dbt docs generate` before pushing, so Cassis can tell "
            "relations dbt built from relations it only declares.",
            err=err,
        )


def _render_filed(record: "dict[str, Any]", *, app_url: str, project_id: str, err: bool) -> None:
    """After an accepted push: the findings filed as issues, with their links."""
    findings = record.get("findings")
    if record.get("status") != "accepted" or findings is None:
        return
    filed = [f for f in findings if f.get("issue_outcome") in _ISSUE_STATES]
    skipped = [f for f in findings if f.get("issue_outcome") == "skipped_dismissed"]
    review = record.get("review")
    _render_review_coverage(review, err=err)
    if not findings:
        typer.echo("Nothing in the context is put in doubt by this change.", err=err)
        return
    typer.echo(f"{len(filed)} finding(s) filed as issues:", err=err)
    for finding in sorted(filed, key=_by_reach):
        link = f"{app_url.rstrip('/')}/context?projectId={project_id}&tab=review&issueId={finding.get('issue_id')}"
        state = _ISSUE_STATES[finding["issue_outcome"]]
        typer.echo(
            f"  {finding.get('issue_id')}  ({state})  {_subject(finding)}: {finding.get('change')}"
            f"{_reach_text(finding, review)}",
            err=err,
        )
        typer.echo(f"    {link}", err=err)
    if skipped:
        typer.echo(f"{len(skipped)} finding(s) skipped: their issues were dismissed.", err=err)


def _render_check(record: "dict[str, Any]", *, err: bool) -> None:
    """A check's outcome: the context objects the change puts in doubt, then the deterministic findings.

    Within each section, objects print by reach (how many chat answers used their tables lately),
    the server's order otherwise: the order the Review queue will show them in. Confidence is
    printed, never sorted on.
    """
    status = record.get("status")
    if status == "rejected":
        typer.secho(f"Rejected: {record.get('detail') or ''}", fg=typer.colors.RED, err=err)
        return
    if status != "checked":
        typer.secho(f"Processing {status}: {record.get('detail') or ''}", fg=typer.colors.RED, err=err)
        return
    findings = record.get("findings") or []
    review = record.get("review")
    typer.secho(
        f"✓ Checked against the dbt baseline: {len(findings)} finding(s).",
        fg=typer.colors.GREEN,
        err=err,
    )
    _render_review_coverage(review, err=err)
    if not findings:
        typer.echo("Nothing in the context is put in doubt by this change.", err=err)
        return
    reviewed = [f for f in findings if f.get("source") not in _DETERMINISTIC]
    deterministic = [f for f in findings if f.get("source") in _DETERMINISTIC]
    if reviewed:
        typer.echo("", err=err)
        typer.echo("Context the change puts in doubt:", err=err)
        by_object: "dict[str, list[dict[str, Any]]]" = {}
        for finding in sorted(reviewed, key=_by_reach):
            by_object.setdefault(_object_label(finding), []).append(finding)
        for label, group in by_object.items():
            typer.secho(f"  {label}{_reach_text(group[0], review)}", bold=True, err=err)
            for finding in group:
                _render_finding(finding, err=err)
    if deterministic:
        typer.echo("", err=err)
        typer.echo("Removed from dbt:", err=err)
        for finding in sorted(deterministic, key=_by_reach):
            typer.secho(f"  {_object_label(finding)}{_reach_text(finding, review)}", bold=True, err=err)
            _render_finding(finding, err=err)


def _by_reach(finding: "dict[str, Any]") -> float:
    """Sort key: the widest reach first; ties keep the server's order (never the review's confidence)."""
    reach = finding.get("reach_count")
    return -float(reach) if isinstance(reach, (int, float)) else 0.0


def _reach_text(finding: "dict[str, Any]", review: Any) -> str:
    """How many chat answers used the finding's context tables lately, counted when the server processed it."""
    reach = finding.get("reach_count")
    if not isinstance(reach, int):
        return ""
    window = review.get("reach_window_days") if isinstance(review, dict) else None
    when = f"in the last {window} days" if window else "lately"
    return f" · reached by {reach} answer{'' if reach == 1 else 's'} {when}"


def _render_finding(finding: "dict[str, Any]", *, err: bool) -> None:
    confidence = finding.get("confidence")
    suffix = f" (confidence {confidence:.2f})" if isinstance(confidence, (int, float)) else ""
    typer.echo(f"    - {finding.get('change')} [{finding.get('changed_node')}]{suffix}", err=err)
    typer.echo(f"      Why it matters: {finding.get('why_it_matters')}", err=err)
    typer.echo(f"      Proposed change: {finding.get('proposed_change')}", err=err)


def _render_review_coverage(review: "Optional[dict[str, Any]]", *, err: bool) -> None:
    """What the comparison covered: changed relations, affected tables, and what the review left out."""
    if not isinstance(review, dict):
        return
    typer.echo(
        f"Compared with the baseline: {review.get('changed_relations', 0)} dbt relation(s) changed, "
        f"{review.get('affected_tables', 0)} context table(s) affected.",
        err=err,
    )
    if review.get("status") == "failed":
        typer.secho(f"Review failed: {review.get('detail') or ''}", fg=typer.colors.YELLOW, err=err)
    not_reviewed = review.get("not_reviewed_tables") or []
    if not_reviewed:
        why = "" if review.get("status") == "failed" else " (over the review's size budget)"
        typer.secho(
            f"Not reviewed{why}: {', '.join(not_reviewed)}",
            fg=typer.colors.YELLOW,
            err=err,
        )


def _object_label(finding: "dict[str, Any]") -> str:
    target = finding.get("context_object") or {}
    return f"{target.get('name')} ({target.get('kind')})"


def _subject(finding: "dict[str, Any]") -> str:
    """The context object the finding is about, as the issue's title names it: the dbt node is the origin, not the subject."""
    target = finding.get("context_object") or {}
    kind, name = target.get("kind"), target.get("name")
    if not name:
        return f"{finding.get('model')}.{finding['column']}" if finding.get("column") else str(finding.get("model"))
    return str(name) if kind in ("table", "column") else f"{kind} {name}"
