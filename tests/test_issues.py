import json

import httpx
import pytest
from cassis_cli.api import (
    get_issue,
    get_issue_evidence,
    get_issues,
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


def _squeezed(result) -> str:
    """The output with each run of spaces collapsed to one: `issues list` pads its columns."""
    return "\n".join(" ".join(line.split()) for line in result.output.splitlines())


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
        assert f"{ISSUE_ID} wrong_answer - x3 reach - open sales/orders Refunds are not modeled" in _squeezed(result)

    def test_prints_the_priority_first_and_a_dash_when_unranked(self, monkeypatch):
        ranked = {**_ISSUE, "priority": "P1"}
        unranked = {**_ISSUE, "id": OCCURRENCE_ID, "priority": None}
        _mock(monkeypatch, "get_issues", get_issues, lambda request: httpx.Response(200, json=[ranked, unranked]))

        result = runner.invoke(app, _args("issues", "list"))

        assert result.exit_code == 0, result.output
        squeezed = _squeezed(result).splitlines()
        assert squeezed[0].startswith(f"P1 {ISSUE_ID} wrong_answer"), squeezed
        assert squeezed[1].startswith(f"- {OCCURRENCE_ID} wrong_answer"), squeezed
        lines = result.output.splitlines()
        assert lines[0].index(ISSUE_ID) == lines[1].index(OCCURRENCE_ID), lines

    def test_prints_the_reach_of_a_dbt_issue(self, monkeypatch):
        dbt_issue = {**_ISSUE, "impact": "at_risk", "origin": "dbt", "reach_count": 12, "detected_by": "review"}
        _mock(monkeypatch, "get_issues", get_issues, lambda request: httpx.Response(200, json=[dbt_issue]))

        result = runner.invoke(app, _args("issues", "list"))

        assert result.exit_code == 0, result.output
        assert f"{ISSUE_ID} at_risk dbt x3 reach 12 open sales/orders Refunds are not modeled" in _squeezed(result)

    def test_the_columns_line_up_whatever_the_impact_origin_and_reach(self, monkeypatch):
        measured = {
            **_ISSUE,
            "impact": "at_risk",
            "origin": "dbt",
            "reach_count": 100000,
            "occurrence_count_cache": 12000,
            "domains": ["finance/revenue/recognition"],
        }
        unmeasured = {
            **_ISSUE,
            "id": OCCURRENCE_ID,
            "impact": "unreliable_answer",
            "origin": "conversation",
            "reach_count": None,
            "status": "dismissed",
        }
        _mock(monkeypatch, "get_issues", get_issues, lambda request: httpx.Response(200, json=[measured, unmeasured]))

        result = runner.invoke(app, _args("issues", "list"))

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert len({line.index("reach") for line in lines}) == 1, lines
        assert lines[0].index("dbt") == lines[1].index("conversation")
        assert lines[0].index("x12000") == lines[1].index("x3")
        assert lines[0].index("open") == lines[1].index("dismissed")
        assert lines[0].index("finance/revenue/recognition") == lines[1].index("sales/orders")
        # Titles start in one column too, whatever the counts and the domain's length.
        assert len({line.index("Refunds are not modeled") for line in lines}) == 1, lines

    def test_prints_a_dash_when_no_domain_is_attached(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_issues",
            get_issues,
            lambda request: httpx.Response(200, json=[{**_ISSUE, "domains": []}]),
        )

        result = runner.invoke(app, _args("issues", "list"))

        assert result.exit_code == 0, result.output
        assert f"{ISSUE_ID} wrong_answer - x3 reach - open - Refunds are not modeled" in _squeezed(result)

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
        assert "No issues match." in result.output

    def test_bad_status_value_exits_two(self):
        result = runner.invoke(app, _args("issues", "list", "--status", "closed"))

        assert result.exit_code == 2
        assert "--status must be one of" in result.output

    def test_rejected_key_exits_three(self, monkeypatch):
        _mock(monkeypatch, "get_issues", get_issues, lambda request: httpx.Response(401, json={"detail": "nope"}))

        result = runner.invoke(app, _args("issues", "list"))

        assert result.exit_code == 3
        assert "rejected the credential" in result.output


class TestIssuesShow:
    def test_prints_the_reach_and_how_a_dbt_issue_was_found(self, monkeypatch):
        body = {
            **_ISSUE,
            "impact": "at_risk",
            "origin": "dbt",
            "reach_count": 1,
            "reach_window_days": 30,
            "detected_by": "dbt_artifacts",
            "occurrences": [],
        }
        _mock(monkeypatch, "get_issue", get_issue, lambda request: httpx.Response(200, json=body))

        result = runner.invoke(app, _args("issues", "show", ISSUE_ID))

        assert result.exit_code == 0, result.output
        assert "Reach: 1 chat answer used its context tables in the last 30 days" in result.output
        assert (
            "Evidence: removed from dbt (its artifacts show the relation or column is no longer built)" in result.output
        )

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
        assert "Reach: not measured (its sources name no context table)" in result.output
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

        result = runner.invoke(
            app,
            _args(
                "issues",
                verb,
                ISSUE_ID,
                *({"resolve": ["--published"], "dismiss": ["--reason", "invalid"]}.get(verb, [])),
            ),
        )

        assert result.exit_code == 0, result.output
        assert seen["method"] == "POST"
        assert seen["url"].endswith(f"/api/ci/projects/{PROJECT_ID}/issues/{ISSUE_ID}/status")
        assert seen["body"] == {
            "status": expected_status,
            "reason": "invalid" if verb == "dismiss" else None,
            "detail": None,
            "confirm_published": verb == "resolve",
        }
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

        result = runner.invoke(app, _args("issues", "resolve", ISSUE_ID, "--published"))

        assert result.exit_code == 1
        assert f"No issue {ISSUE_ID} in this project" in result.output

    def test_non_uuid_project_exits_two(self):
        result = runner.invoke(app, ["issues", "resolve", ISSUE_ID, "--project", "nope", "--api-key", "sk-k6-test"])
        assert result.exit_code == 2

    def test_missing_api_key_exits_two(self):
        result = runner.invoke(app, ["issues", "list", "--project", PROJECT_ID])
        assert result.exit_code == 2
        assert "No API key" in result.output


@pytest.mark.parametrize("args", [("resolve",), ("dismiss",), ("dismiss", "--reason", "unknown")])
def test_status_requires_publication_or_reason(args):
    result = runner.invoke(app, _args("issues", args[0], ISSUE_ID, *args[1:]))
    assert result.exit_code == 2


def test_default_queue_and_explicit_all(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.params.get("status"))
        return httpx.Response(200, json=[])

    _mock(monkeypatch, "get_issues", get_issues, handler)
    assert runner.invoke(app, _args("issues", "list")).exit_code == 0
    assert runner.invoke(app, _args("issues", "list", "--status", "all")).exit_code == 0
    assert seen == ["open", "all"]


def test_show_prints_history_and_dismissal(monkeypatch):
    issue = dict(
        _ISSUE,
        dismiss_reason="declined",
        dismiss_detail="Later",
        status_history=[
            {
                "created_at": "2026-09-14T10:00:00Z",
                "from_status": "open",
                "to_status": "dismissed",
                "via": "manual",
                "actor_kind": "user",
                "reason": "declined",
                "detail": "Later",
            }
        ],
    )
    _mock(monkeypatch, "get_issue", get_issue, lambda request: httpx.Response(200, json=issue))
    result = runner.invoke(app, _args("issues", "show", ISSUE_ID))
    assert result.exit_code == 0
    assert "open -> dismissed" in result.output
    assert "Dismiss reason: declined" in result.output


@pytest.mark.parametrize("answer,expected_code", [("y\n", 0), ("n\n", 2)])
def test_resolve_interactive_publication_confirmation(monkeypatch, answer, expected_code):
    from types import SimpleNamespace

    from cassis_cli import issues

    monkeypatch.setattr(issues, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)))
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=dict(_ISSUE, status="resolved"))

    _mock(monkeypatch, "post_issue_status", post_issue_status, handler)
    result = runner.invoke(app, _args("issues", "resolve", ISSUE_ID), input=answer)
    assert result.exit_code == expected_code
    assert bool(seen) == (expected_code == 0)
    if seen:
        assert seen[0]["confirm_published"] is True


@pytest.mark.parametrize("restricted", [False, True])
def test_show_explains_restricted_sources(monkeypatch, restricted):
    issue = dict(_ISSUE, restricted_sources=restricted, occurrences=[])
    _mock(monkeypatch, "get_issue", get_issue, lambda request: httpx.Response(200, json=issue))
    result = runner.invoke(app, _args("issues", "show", ISSUE_ID))
    assert result.exit_code == 0
    assert ("restricted by your access" in result.output) is restricted
    structured = runner.invoke(app, _args("issues", "show", ISSUE_ID, "--json"))
    assert structured.exit_code == 0
    assert json.loads(structured.output)["restricted_sources"] is restricted
