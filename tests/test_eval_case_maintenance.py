import json

import httpx
from cassis_cli.api import delete_eval_case, get_eval_cases
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
CASE_ID = "019f0000-0000-7000-8000-0000000000ca"


def _mock_list_api(monkeypatch, handler):
    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return get_eval_cases(**kwargs)

    monkeypatch.setattr("cassis_cli.eval.get_eval_cases", patched)


def _mock_delete_api(monkeypatch, handler):
    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return delete_eval_case(**kwargs)

    monkeypatch.setattr("cassis_cli.eval.delete_eval_case", patched)


def _list_args(*extra):
    return ["eval", "list-cases", "--project", PROJECT_ID, "--api-key", "sk-k6-test", *extra]


def _delete_args(*extra):
    return ["eval", "delete-case", CASE_ID, "--project", PROJECT_ID, "--api-key", "sk-k6-test", *extra]


class TestEvalListCases:
    def test_prints_id_and_question(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                json=[{"id": CASE_ID, "question": "Net revenue\nlast month?", "gold_sql": "SELECT 1"}],
            )

        _mock_list_api(monkeypatch, handler)

        result = runner.invoke(app, _list_args())

        assert result.exit_code == 0, result.output
        assert f"/api/ci/projects/{PROJECT_ID}/eval/cases" in seen["url"]
        assert f"{CASE_ID}  Net revenue last month?" in result.output
        assert "SELECT 1" not in result.output  # gold SQL only in --json

    def test_json_includes_gold_sql(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"id": CASE_ID, "question": "q", "gold_sql": "SELECT 1"}])

        _mock_list_api(monkeypatch, handler)

        result = runner.invoke(app, _list_args("--json"))

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == [{"id": CASE_ID, "question": "q", "gold_sql": "SELECT 1"}]

    def test_empty_suite_says_so(self, monkeypatch):
        _mock_list_api(monkeypatch, lambda request: httpx.Response(200, json=[]))

        result = runner.invoke(app, _list_args())

        assert result.exit_code == 0, result.output
        assert "No eval cases yet" in result.output

    def test_non_uuid_project_exits_two(self):
        result = runner.invoke(app, ["eval", "list-cases", "--project", "nope", "--api-key", "sk-k6-test"])
        assert result.exit_code == 2


class TestEvalDeleteCase:
    def test_deletes_and_exits_zero(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            return httpx.Response(204)

        _mock_delete_api(monkeypatch, handler)

        result = runner.invoke(app, _delete_args())

        assert result.exit_code == 0, result.output
        assert seen["method"] == "DELETE"
        assert f"/api/ci/projects/{PROJECT_ID}/eval/cases/{CASE_ID}" in seen["url"]
        assert "Deleted eval case" in result.output

    def test_unknown_case_exits_one(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Eval case not found"})

        _mock_delete_api(monkeypatch, handler)

        result = runner.invoke(app, _delete_args())

        assert result.exit_code == 1
        assert "No current eval case" in result.output

    def test_unknown_project_exits_three(self, monkeypatch):
        # A 404 whose detail is not the case-not-found message is a project
        # scoping problem, not a missing case — surfaced as an API error.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Project not found"})

        _mock_delete_api(monkeypatch, handler)

        result = runner.invoke(app, _delete_args())

        assert result.exit_code == 3
        assert "Check --project" in result.output

    def test_non_uuid_case_id_exits_two(self):
        result = runner.invoke(app, ["eval", "delete-case", "nope", "--project", PROJECT_ID, "--api-key", "sk-k6-test"])
        assert result.exit_code == 2
