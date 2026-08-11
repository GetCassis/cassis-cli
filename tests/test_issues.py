import json

import httpx
from cassis_cli.api import get_issue, get_issue_evidence, get_issues, post_issue_status
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
ISSUE_ID = "019f0000-0000-7000-8000-0000000000e1"
OCCURRENCE_ID = "019f0000-0000-7000-8000-0000000000c1"

_ISSUE = {
    "id": ISSUE_ID,
    "title": "Refunds are not modeled",
    "description": "Questions about refunds\nhave no column to read.",
    "suggested_action": "Add refunded_cents to public.orders.",
    "cause": "ontology_gap",
    "impact": "wrong_answer",
    "status": "open",
    "occurrence_count_cache": 3,
    "fix_proposal": None,
}


def _mock(monkeypatch, name, original, handler):
    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr(f"cassis_cli.issues.{name}", patched)


def _args(*extra):
    return [*extra, "--project", PROJECT_ID, "--api-key", "sk-k6-test"]


class TestIssuesList:
    def test_prints_one_line_per_issue(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=[_ISSUE])

        _mock(monkeypatch, "get_issues", get_issues, handler)

        result = runner.invoke(app, _args("issues", "list"))

        assert result.exit_code == 0, result.output
        assert f"/api/ci/projects/{PROJECT_ID}/issues" in seen["url"]
        assert f"{ISSUE_ID}  wrong_answer  x3  open  Refunds are not modeled" in result.output

    def test_filters_go_on_the_query_string(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=[])

        _mock(monkeypatch, "get_issues", get_issues, handler)

        result = runner.invoke(
            app,
            _args("issues", "list", "--status", "open", "--impact", "no_answer", "--cause", "missing_data"),
        )

        assert result.exit_code == 0, result.output
        assert "status=open" in seen["url"]
        assert "impact=no_answer" in seen["url"]
        assert "cause=missing_data" in seen["url"]
        assert "No issues match." in result.output

    def test_json_output_prints_raw_response(self, monkeypatch):
        _mock(monkeypatch, "get_issues", get_issues, lambda request: httpx.Response(200, json=[_ISSUE]))

        result = runner.invoke(app, _args("issues", "list", "--json"))

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == [_ISSUE]

    def test_empty_list_says_so(self, monkeypatch):
        _mock(monkeypatch, "get_issues", get_issues, lambda request: httpx.Response(200, json=[]))

        result = runner.invoke(app, _args("issues", "list"))

        assert result.exit_code == 0, result.output
        assert "No issues on this project." in result.output

    def test_bad_status_value_exits_two(self):
        result = runner.invoke(app, _args("issues", "list", "--status", "closed"))

        assert result.exit_code == 2
        assert "--status must be one of" in result.output

    def test_rejected_key_exits_three(self, monkeypatch):
        _mock(monkeypatch, "get_issues", get_issues, lambda request: httpx.Response(401, json={"detail": "nope"}))

        result = runner.invoke(app, _args("issues", "list"))

        assert result.exit_code == 3
        assert "rejected the API key" in result.output


class TestIssuesShow:
    def test_prints_detail_and_occurrences(self, monkeypatch):
        body = dict(
            _ISSUE,
            occurrences=[{"id": OCCURRENCE_ID, "symptom": "Answer omitted\nrefunds", "question": "Refunds?"}],
        )
        _mock(monkeypatch, "get_issue", get_issue, lambda request: httpx.Response(200, json=body))

        result = runner.invoke(app, _args("issues", "show", ISSUE_ID))

        assert result.exit_code == 0, result.output
        assert "open  wrong_answer  ontology_gap  3 occurrence(s)" in result.output
        assert "Add refunded_cents to public.orders." in result.output
        assert "Fix proposal: none" in result.output
        assert f"{OCCURRENCE_ID}  Answer omitted refunds" in result.output

    def test_unknown_issue_exits_one(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_issue",
            get_issue,
            lambda request: httpx.Response(404, json={"detail": "Issue not found"}),
        )

        result = runner.invoke(app, _args("issues", "show", ISSUE_ID))

        assert result.exit_code == 1
        assert f"No issue {ISSUE_ID} in this project" in result.output

    def test_unknown_project_exits_three(self, monkeypatch):
        # A 404 whose detail is not the issue-not-found message is a project
        # scoping problem, not a missing issue — surfaced as an API error.
        _mock(
            monkeypatch,
            "get_issue",
            get_issue,
            lambda request: httpx.Response(404, json={"detail": "Project not found"}),
        )

        result = runner.invoke(app, _args("issues", "show", ISSUE_ID))

        assert result.exit_code == 3
        assert "Check --project" in result.output


class TestIssuesEvidence:
    def test_dumps_the_evidence_fields(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "source_type": "chat_message",
                    "generated_sql": "SELECT SUM(amount) FROM public.orders",
                    "sql_results": [{"sum": 42}],
                    "rating": "thumbs_down",
                },
            )

        _mock(monkeypatch, "get_issue_evidence", get_issue_evidence, handler)

        result = runner.invoke(app, _args("issues", "evidence", ISSUE_ID, OCCURRENCE_ID))

        assert result.exit_code == 0, result.output
        assert seen["url"].endswith(
            f"/api/ci/projects/{PROJECT_ID}/issues/{ISSUE_ID}/occurrences/{OCCURRENCE_ID}/evidence"
        )
        assert "Source type: chat_message" in result.output
        assert "Generated sql: SELECT SUM(amount) FROM public.orders" in result.output
        assert "Rating: thumbs_down" in result.output
        assert '"sum": 42' in result.output  # structured values are dumped as JSON

    def test_unknown_occurrence_exits_one(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_issue_evidence",
            get_issue_evidence,
            lambda request: httpx.Response(404, json={"detail": "Issue not found"}),
        )

        result = runner.invoke(app, _args("issues", "evidence", ISSUE_ID, OCCURRENCE_ID))

        assert result.exit_code == 1
        assert "occurrence not found" in result.output


class TestIssuesStatusVerbs:
    def _run(self, monkeypatch, verb, expected_status):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=dict(_ISSUE, status=expected_status))

        _mock(monkeypatch, "post_issue_status", post_issue_status, handler)

        result = runner.invoke(app, _args("issues", verb, ISSUE_ID))

        assert result.exit_code == 0, result.output
        assert seen["method"] == "POST"
        assert seen["url"].endswith(f"/api/ci/projects/{PROJECT_ID}/issues/{ISSUE_ID}/status")
        assert seen["body"] == {"status": expected_status}
        assert f"Issue {ISSUE_ID} is now {expected_status}." in result.output

    def test_resolve(self, monkeypatch):
        self._run(monkeypatch, "resolve", "resolved")

    def test_dismiss(self, monkeypatch):
        self._run(monkeypatch, "dismiss", "dismissed")

    def test_reopen(self, monkeypatch):
        self._run(monkeypatch, "reopen", "open")

    def test_unknown_issue_exits_one(self, monkeypatch):
        _mock(
            monkeypatch,
            "post_issue_status",
            post_issue_status,
            lambda request: httpx.Response(404, json={"detail": "Issue not found"}),
        )

        result = runner.invoke(app, _args("issues", "resolve", ISSUE_ID))

        assert result.exit_code == 1
        assert f"No issue {ISSUE_ID} in this project" in result.output

    def test_non_uuid_project_exits_two(self):
        result = runner.invoke(app, ["issues", "resolve", ISSUE_ID, "--project", "nope", "--api-key", "sk-k6-test"])
        assert result.exit_code == 2

    def test_missing_api_key_exits_two(self):
        result = runner.invoke(app, ["issues", "list", "--project", PROJECT_ID])
        assert result.exit_code == 2
        assert "No API key" in result.output
