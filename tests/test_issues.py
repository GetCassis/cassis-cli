import json

import httpx
import pytest
from cassis_cli.api import (
    get_issue,
    get_issue_analysis_run,
    get_issue_evidence,
    get_issues,
    post_issue_analysis_cancel,
    post_issue_analysis_start,
    post_issue_status,
)
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
    "domains": ["sales/orders", "finance"],
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
        # The primary domain sits between the status and the title.
        assert f"{ISSUE_ID}  wrong_answer  x3  open  sales/orders  Refunds are not modeled" in result.output

    def test_prints_a_dash_when_no_domain_is_attached(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_issues",
            get_issues,
            lambda request: httpx.Response(200, json=[{**_ISSUE, "domains": []}]),
        )

        result = runner.invoke(app, _args("issues", "list"))

        assert result.exit_code == 0, result.output
        assert f"{ISSUE_ID}  wrong_answer  x3  open  -  Refunds are not modeled" in result.output

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

    def test_domain_filter_goes_on_the_query_string(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            # A domain path holds slashes, which httpx percent-encodes in a
            # query value, so read the parsed params, not the raw URL.
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        _mock(monkeypatch, "get_issues", get_issues, handler)

        result = runner.invoke(app, _args("issues", "list", "--domain", "sales/orders"))

        assert result.exit_code == 0, result.output
        assert seen["params"]["domain"] == "sales/orders"
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
        assert "Domains: sales/orders, finance" in result.output
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


RUN_ID = "019f0000-0000-7000-8000-0000000000a1"


def _run(status: str, *, chats_analyzed: int = 0, occurrences: int = 0, issues: int = 0, error=None) -> dict:
    return {
        "id": RUN_ID,
        "project_id": PROJECT_ID,
        "status": status,
        "trigger": "manual",
        "chats_analyzed": chats_analyzed,
        "total_chats": 3,
        "occurrences_created": occurrences,
        "issues_touched": issues,
        "error": error,
        "started_at": "2026-08-25T10:00:00Z",
        "completed_at": None,
        "created_at": "2026-08-25T10:00:00Z",
    }


class TestIssuesAnalyze:
    def _mock_start(self, monkeypatch, response: httpx.Response, seen: dict | None = None):
        def handler(request: httpx.Request) -> httpx.Response:
            if seen is not None:
                seen["start_url"] = str(request.url)
                seen["start_method"] = request.method
            return response

        _mock(monkeypatch, "post_issue_analysis_start", post_issue_analysis_start, handler)

    def _mock_polls(self, monkeypatch, runs: list[dict], seen: dict | None = None):
        polls = iter(runs)
        last = runs[-1]

        def handler(request: httpx.Request) -> httpx.Response:
            if seen is not None:
                seen.setdefault("poll_urls", []).append(str(request.url))
            return httpx.Response(200, json=next(polls, last))

        _mock(monkeypatch, "get_issue_analysis_run", get_issue_analysis_run, handler)

    def _analyze(self, *extra):
        return runner.invoke(app, _args("issues", "analyze", "--poll-interval", "0", *extra))

    def test_waits_for_completion_and_prints_the_summary(self, monkeypatch):
        seen: dict = {}
        self._mock_start(monkeypatch, httpx.Response(201, json=_run("running")), seen)
        self._mock_polls(
            monkeypatch,
            [_run("running", chats_analyzed=1), _run("completed", chats_analyzed=3, occurrences=2, issues=1)],
            seen,
        )

        result = self._analyze()

        assert result.exit_code == 0, result.output
        assert seen["start_method"] == "POST"
        assert seen["start_url"].endswith(f"/api/ci/projects/{PROJECT_ID}/issue-analysis/runs")
        assert seen["poll_urls"][0].endswith(f"/issue-analysis/runs/{RUN_ID}")
        assert f"Analysis run {RUN_ID} started: 3 conversation(s) to analyze." in result.output
        assert "1/3 conversations analyzed" in result.output
        assert "Analysis complete: 3 conversation(s) analyzed, 2 occurrence(s) found, 1 issue(s)" in result.output
        assert "cassis issues list" in result.output

    def test_no_wait_prints_the_run_id_only(self, monkeypatch):
        self._mock_start(monkeypatch, httpx.Response(201, json=_run("running")))
        polled = {"count": 0}

        def never(request: httpx.Request) -> httpx.Response:
            polled["count"] += 1
            return httpx.Response(200, json=_run("running"))

        _mock(monkeypatch, "get_issue_analysis_run", get_issue_analysis_run, never)

        result = self._analyze("--no-wait")

        assert result.exit_code == 0, result.output
        assert f"Analysis run {RUN_ID} started" in result.output and polled["count"] == 0

    def test_nothing_to_analyze_is_a_green_no_op(self, monkeypatch):
        self._mock_start(monkeypatch, httpx.Response(422, json={"detail": "No unanalyzed conversations"}))

        result = self._analyze()

        assert result.exit_code == 0, result.output
        assert "Nothing to analyze" in result.output

    def test_an_unrelated_422_is_not_mistaken_for_nothing_to_analyze(self, monkeypatch):
        self._mock_start(monkeypatch, httpx.Response(422, json={"detail": [{"loc": ["path", "project_id"]}]}))

        result = self._analyze()

        assert result.exit_code == 3, result.output  # transport/other error, like any non-analysis 4xx
        assert "Nothing to analyze" not in result.output

    def test_already_running_is_a_transport_failure(self, monkeypatch):
        self._mock_start(monkeypatch, httpx.Response(409, json={"detail": "already active"}))

        result = self._analyze()

        assert result.exit_code == 3, result.output
        assert "already running" in result.output

    def test_failed_run_exits_1_with_the_error(self, monkeypatch):
        self._mock_start(monkeypatch, httpx.Response(201, json=_run("running")))
        self._mock_polls(monkeypatch, [_run("failed", error="LLM provider unavailable")])

        result = self._analyze()

        assert result.exit_code == 1, result.output
        assert "Analysis failed: LLM provider unavailable" in result.output

    def test_timeout_exits_3_and_says_the_run_continues(self, monkeypatch):
        self._mock_start(monkeypatch, httpx.Response(201, json=_run("running")))
        self._mock_polls(monkeypatch, [_run("running")])

        result = self._analyze("--timeout", "0")

        assert result.exit_code == 3, result.output
        assert "keeps going server-side" in result.output

    def test_json_prints_the_final_run(self, monkeypatch):
        self._mock_start(monkeypatch, httpx.Response(201, json=_run("running")))
        self._mock_polls(monkeypatch, [_run("completed", chats_analyzed=3)])

        result = self._analyze("--json")

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"run": _run("completed", chats_analyzed=3)}

    def test_cancel_posts_to_the_cancel_route(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(204)

        post_issue_analysis_cancel(
            api_url="http://api",
            api_key="k",
            project_id=PROJECT_ID,
            run_id=RUN_ID,
            transport=httpx.MockTransport(handler),
        )

        assert seen["url"] == f"http://api/api/ci/projects/{PROJECT_ID}/issue-analysis/runs/{RUN_ID}/cancel"


class TestIssueAnalysisRunNotFound:
    """Both analysis-run routes exact-match the server's 404 detail (the wire
    contract) so a missing run reads as exit 1, not as a project-scope error."""

    def test_get_run_maps_the_not_found_detail(self):
        from cassis_cli.api import IssueNotFoundError, get_issue_analysis_run

        transport = httpx.MockTransport(
            lambda request: httpx.Response(404, json={"detail": "Issue-analysis run not found"})
        )
        with pytest.raises(IssueNotFoundError, match=f"No issue-analysis run {RUN_ID}"):
            get_issue_analysis_run(
                api_url="http://api", api_key="k", project_id=PROJECT_ID, run_id=RUN_ID, transport=transport
            )

    def test_cancel_maps_the_not_found_detail(self):
        from cassis_cli.api import IssueNotFoundError

        transport = httpx.MockTransport(
            lambda request: httpx.Response(404, json={"detail": "Issue-analysis run not found"})
        )
        with pytest.raises(IssueNotFoundError, match=f"No issue-analysis run {RUN_ID}"):
            post_issue_analysis_cancel(
                api_url="http://api", api_key="k", project_id=PROJECT_ID, run_id=RUN_ID, transport=transport
            )

    def test_other_404_details_stay_project_scope_errors(self):
        from cassis_cli.api import ApiError

        transport = httpx.MockTransport(lambda request: httpx.Response(404, json={"detail": "Project not found"}))
        with pytest.raises(ApiError):
            post_issue_analysis_cancel(
                api_url="http://api", api_key="k", project_id=PROJECT_ID, run_id=RUN_ID, transport=transport
            )
