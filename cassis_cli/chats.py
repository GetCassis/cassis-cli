"""Read conversations and saved analyses without running model calls."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from uuid import UUID

import typer
from cassis_cli.api import DEFAULT_API_URL, ApiError, ChatNotFoundError, get_chats
from cassis_cli.common import api_failure, one_line, require_api_key, resolve_project_id

app = typer.Typer(no_args_is_help=True, help="Read conversations and their saved analyses.")
_PATH = typer.Option(Path("."), "--path", help="Checkout root containing the context directory.")
_PROJECT = typer.Option(None, "--project", envvar="CASSIS_PROJECT_ID", help="Project ID; defaults to project.yml.")
_KEY = typer.Option(None, "--api-key", envvar="CASSIS_API_KEY", help="Cassis API key.")
_URL = typer.Option(DEFAULT_API_URL, "--api-url", envvar="CASSIS_API_URL", help="Cassis API base URL.")
_BASE = typer.Option("cassis", "--base-path", envvar="CASSIS_BASE_PATH", help="Context directory within the checkout.")
_APP_URL = typer.Option(None, "--app-url", help="Webapp base URL for conversation links; defaults to --api-url.")


def _url(base: str, project: str, chat: str) -> str:
    return base.rstrip("/") + "/chat?" + urlencode({"projectId": project, "chatId": chat})


def _status(summary: Optional[dict[str, Any]]) -> str:
    if not summary:
        return "Not analyzed"
    if summary.get("status") in ("running", "scheduled", "deferred"):
        return "In progress"
    if summary.get("status") == "completed" and summary.get("problems", 0):
        return "Needs attention"
    if summary.get("status") != "completed" or summary.get("conversation_changed"):
        return "Not analyzed"
    return "Analyzed"


def _read(**kwargs: Any) -> dict[str, Any]:
    try:
        return get_chats(**kwargs)
    except ChatNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except ApiError as exc:
        raise api_failure(exc) from exc


@app.command("list")
def list_chats(
    path: Path = _PATH,
    project_id: Optional[str] = _PROJECT,
    api_key: Optional[str] = _KEY,
    api_url: str = _URL,
    base_path: str = _BASE,
    app_url: Optional[str] = _APP_URL,
    limit: int = typer.Option(20, min=1, max=100),
    offset: int = typer.Option(0, min=0),
    since: Optional[str] = typer.Option(None, help="Last-message cutoff: ISO date or timestamp; dates use UTC."),
    feedback: Optional[str] = typer.Option(None, help="Filter: up, down, any or none."),
    review: Optional[str] = typer.Option(None, help="Filter: needs-attention (detected problems only)."),
    query: Optional[str] = typer.Option(None, "--search", help="Search title, author name or email."),
    json_output: bool = typer.Option(False, "--json", help="Print the page as JSON, including links and pagination."),
) -> None:
    """List readable chats by latest message. Reads saved results; never starts an analysis."""
    if feedback not in (None, "up", "down", "any", "none"):
        raise typer.BadParameter("Choose up, down, any or none.", param_hint="--feedback")
    if review not in (None, "needs-attention"):
        raise typer.BadParameter("Choose needs-attention.", param_hint="--review")
    if since:
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            since = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()
        except ValueError as exc:
            raise typer.BadParameter("Use an ISO date or timestamp.", param_hint="--since") from exc
    key = require_api_key(api_key)
    project = resolve_project_id(project_id, path / base_path, quiet=json_output)
    assert project is not None
    params = {
        k: v
        for k, v in dict(limit=limit, offset=offset, since=since, feedback=feedback, review=review, q=query).items()
        if v is not None
    }
    data = _read(api_url=api_url, api_key=key, project_id=project, params=params)
    for row in data["items"]:
        row["url"] = _url(app_url or api_url, project, row["id"])
    data["next_offset"] = (
        data["offset"] + len(data["items"])
        if data["items"] and data["offset"] + len(data["items"]) < data["total"]
        else None
    )
    if json_output:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if not data["items"]:
        typer.echo("No conversations match these filters.")
    for row in data["items"]:
        feedback_text = f"up {row.get('feedback_up', 0)} / down {row.get('feedback_down', 0)}"
        typer.echo(
            f"{row['id']}  {_status(row.get('analysis_summary'))}  {feedback_text}  {row.get('last_message_at') or row.get('created_at')}  {one_line(row.get('title') or 'Untitled')}"
        )
        typer.echo(f"  {row['url']}")
    typer.echo(f"{len(data['items'])} of {data['total']} conversations (offset {data['offset']}).")
    if data["next_offset"] is not None:
        typer.echo(f"Next page: repeat with --offset {data['next_offset']}")


def _analysis(view: dict[str, Any]) -> None:
    typer.echo("\nAnalysis")
    latest = view.get("analysis")
    if latest:
        typer.echo(f"Status: {latest['status']} | Launched: {latest.get('created_at')}")
        if latest.get("error"):
            typer.echo(f"Error: {latest['error']}")
    shown = latest if latest and latest.get("status") == "completed" else view.get("previous_analysis")
    if not shown or not shown.get("result"):
        typer.echo(
            "No completed analysis available." if view.get("enabled") else "Analysis is not enabled for this project."
        )
        return
    if shown is not latest:
        typer.echo(f"Previous completed analysis ({shown['id']}):")
    if shown.get("conversation_changed"):
        typer.echo("New messages are not covered by this analysis.")
    if shown.get("context_changed"):
        typer.echo("Context has changed since this analysis.")
    report = shown["result"]
    typer.echo(report["summary"])
    for index, obs in enumerate(report["observations"], 1):
        typer.echo(f"\n{index}. {obs['title']} [{obs['kind']} / {obs['confidence']}]")
        typer.echo(obs["explanation"])
        typer.echo(f"Next step: {obs['next_step']}")
        for citation in obs["evidence"]:
            typer.echo(f"  [{citation['document_id']}] {citation['quote']}")
    for issue in view.get("issues", []):
        typer.echo(f"Linked issue: {issue['id']} — {issue['title']} ({issue['status']})")


@app.command("show")
def show_chat(
    chat_id: UUID,
    path: Path = _PATH,
    project_id: Optional[str] = _PROJECT,
    api_key: Optional[str] = _KEY,
    api_url: str = _URL,
    base_path: str = _BASE,
    app_url: Optional[str] = _APP_URL,
    trace: bool = typer.Option(False, "--trace", help="Show bounded previews of execution trace entries."),
    trace_entry: Optional[str] = typer.Option(
        None, "--trace-entry", help="Show one full entry: trace:<message-id>:<index>."
    ),
    analysis_only: bool = typer.Option(False, "--analysis-only", help="Show only the saved analysis."),
    json_output: bool = typer.Option(False, "--json", help="Print the full conversation, traces and analysis as JSON."),
) -> None:
    """Read a conversation followed by its saved analysis. No model calls or automatic retries."""
    if analysis_only and (trace or trace_entry):
        raise typer.BadParameter("--analysis-only cannot be combined with trace options.")
    key = require_api_key(api_key)
    project = resolve_project_id(project_id, path / base_path, quiet=json_output)
    assert project is not None
    data = _read(api_url=api_url, api_key=key, project_id=project, chat_id=str(chat_id))
    data["url"] = _url(app_url or api_url, project, str(chat_id))
    if trace_entry:
        for message in data["messages"]:
            for i, entry in enumerate(message.get("process_log") or []):
                if trace_entry == f"trace:{message['id']}:{i}":
                    typer.echo(json.dumps(entry, indent=2, ensure_ascii=False))
                    return
        typer.echo("Trace entry not found in this conversation.", err=True)
        raise typer.Exit(1)
    if json_output:
        typer.echo(json.dumps(data["analysis"] if analysis_only else data, indent=2, ensure_ascii=False))
        return
    chat = data["chat"]
    typer.echo(f"{chat.get('title') or 'Untitled'} [{chat['id']}]")
    typer.echo(f"Created: {chat.get('created_at')} | Updated: {chat.get('updated_at')}")
    typer.echo(data["url"])
    if not analysis_only:
        for message in data["messages"]:
            typer.echo(f"\n{message['role']} — {message.get('created_at')} [message:{message['id']}]")
            if message.get("content"):
                typer.echo(message["content"])
            if message.get("generated_sql"):
                typer.echo(f"SQL:\n{message['generated_sql']}")
            if message.get("rating"):
                # A feedback message: its content, printed above, is the justification.
                typer.echo(f"Feedback: {message['rating']}")
            if message.get("error"):
                typer.echo(f"Error: {message['error']}")
            entries = message.get("process_log") or []
            if entries and not trace:
                typer.echo(f"Execution: {len(entries)} trace entries (use --trace to inspect).")
            if trace:
                for i, entry in enumerate(entries):
                    ref = f"trace:{message['id']}:{i}"
                    typer.echo(f"  [{ref}] {entry.get('name') or entry.get('type')}")
                    detail = json.dumps(entry, ensure_ascii=False)
                    typer.echo(
                        f"  {detail[:500]}"
                        + (f"… (use --trace-entry {ref} for full details)" if len(detail) > 500 else "")
                    )
    _analysis(data["analysis"])
