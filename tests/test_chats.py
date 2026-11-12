import json

import httpx
import pytest
from cassis_cli.api import get_chats
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()
PROJECT = "019f0000-0000-7000-8000-000000000000"
CHAT = "019f0000-0000-7000-8000-000000000001"
REPORT = {
    "summary": "Check the period",
    "observations": [
        {
            "kind": "problem",
            "confidence": "uncertain",
            "title": "Wrong period",
            "explanation": "The calendar differs.",
            "next_step": "Confirm the fiscal year",
            "evidence": [{"document_id": "trace:message1:0", "quote": "April"}],
        }
    ],
}
DETAIL = {
    "chat": {"id": CHAT, "title": "Revenue"},
    "messages": [
        {
            "id": "message1",
            "role": "assistant",
            "content": "Revenue is 12",
            "process_log": [{"type": "tool_call", "name": "get", "result": "April " + "x" * 1000}],
        },
        {
            "id": "message2",
            "role": "feedback",
            "content": "Wrong period",
            "rating": "down",
            "feedback_source": "slack",
        },
    ],
    "analysis": {"enabled": True, "analysis": {"id": "run1", "status": "completed", "result": REPORT}},
}


def args(*extra):
    return ["chats", *extra, "--project", PROJECT, "--api-key", "sk-k6-test"]


def mock(monkeypatch, handler):
    def read(**kwargs):
        return get_chats(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("cassis_cli.chats.get_chats", read)


def test_show_includes_conversation_feedback_and_analysis_without_full_trace(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=DETAIL)

    mock(monkeypatch, handler)
    result = runner.invoke(app, args("show", CHAT))
    assert result.exit_code == 0, result.output
    for text in (
        "Revenue is 12",
        "Wrong period",
        "Check the period",
        "Next step: Confirm the fiscal year",
        "trace:message1:0",
        "--trace",
    ):
        assert text in result.output
    assert "x" * 1000 not in result.output
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path == f"/api/ci/projects/{PROJECT}/chats/{CHAT}"


def test_analysis_only_reads_final_observation_schema(monkeypatch):
    mock(monkeypatch, lambda request: httpx.Response(200, json=DETAIL))
    result = runner.invoke(app, args("show", CHAT, "--analysis-only"))
    assert result.exit_code == 0, result.output
    assert "Next step: Confirm the fiscal year" in result.output
    assert "[trace:message1:0] April" in result.output
    assert "Revenue is 12" not in result.output


def test_trace_preview_full_entry_and_json(monkeypatch):
    mock(monkeypatch, lambda request: httpx.Response(200, json=DETAIL))
    preview = runner.invoke(app, args("show", CHAT, "--trace"))
    assert preview.exit_code == 0 and "--trace-entry trace:message1:0" in preview.output
    full = runner.invoke(app, args("show", CHAT, "--trace-entry", "trace:message1:0"))
    assert full.exit_code == 0 and json.loads(full.output)["result"].endswith("x" * 1000)
    output = runner.invoke(app, args("show", CHAT, "--json"))
    assert output.exit_code == 0 and json.loads(output.output)["messages"] == DETAIL["messages"]
    only = runner.invoke(app, args("show", CHAT, "--analysis-only", "--json"))
    assert json.loads(only.output) == DETAIL["analysis"]


def test_filters_pagination_and_json(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "items": [{"id": CHAT, "title": "Revenue", "analysis_summary": {"status": "completed", "problems": 1}}],
                "offset": 0,
                "total": 3,
            },
        )

    mock(monkeypatch, handler)
    output = runner.invoke(
        app,
        args(
            "list",
            "--review",
            "needs-attention",
            "--feedback",
            "down",
            "--since",
            "2026-01-01",
            "--limit",
            "1",
            "--json",
        ),
    )
    assert output.exit_code == 0, output.output
    data = json.loads(output.output)
    assert data["next_offset"] == 1 and "chatId=" in data["items"][0]["url"]
    assert seen[0].url.params["since"] == "2026-01-01T00:00:00+00:00"
    assert seen[0].url.params["feedback"] == "down" and seen[0].url.params["review"] == "needs-attention"


@pytest.mark.parametrize(
    "status,body,code",
    [(404, {"detail": "Chat not found"}, 1), (404, {"detail": "Not found"}, 3), (401, {}, 3), (200, {}, 3)],
)
def test_errors_are_not_empty_success(monkeypatch, status, body, code):
    mock(monkeypatch, lambda request: httpx.Response(status, json=body))
    assert runner.invoke(app, args("show", CHAT)).exit_code == code


def test_no_analysis_and_failed_retry_preserve_meaning(monkeypatch):
    mock(
        monkeypatch,
        lambda request: httpx.Response(200, json={**DETAIL, "analysis": {"enabled": True, "analysis": None}}),
    )
    output = runner.invoke(app, args("show", CHAT))
    assert output.exit_code == 0 and "No completed analysis available" in output.output
    mock(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                **DETAIL,
                "analysis": {
                    "enabled": True,
                    "analysis": {"status": "failed", "error": "Unavailable"},
                    "previous_analysis": DETAIL["analysis"]["analysis"],
                },
            },
        ),
    )
    output = runner.invoke(app, args("show", CHAT))
    assert (
        output.exit_code == 0 and "Previous completed analysis" in output.output and "Check the period" in output.output
    )


@pytest.mark.parametrize(
    "options", [("--since", "nonsense"), ("--review", "wrong"), ("--feedback", "wrong"), ("--limit", "101")]
)
def test_invalid_filters_fail_before_network(options):
    assert runner.invoke(app, args("list", *options)).exit_code == 2
