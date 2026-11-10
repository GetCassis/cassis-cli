import json

import httpx
from cassis_cli.api import get_eval_cases, post_eval_case_replace
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
OLD_ID = "019f0000-0000-7000-8000-0000000000ca"
NEW_ID = "019f0000-0000-7000-8000-0000000000cb"


def _mock_api(monkeypatch, handler):
    def patched_replace(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return post_eval_case_replace(**kwargs)

    def patched_list(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return get_eval_cases(**kwargs)

    monkeypatch.setattr("cassis_cli.eval.post_eval_case_replace", patched_replace)
    monkeypatch.setattr("cassis_cli.eval.get_eval_cases", patched_list)


def _args(*extra):
    return ["eval", "replace-case", OLD_ID, "--project", PROJECT_ID, "--api-key", "sk-k6-test", *extra]


def _handler(status: int, replace_body: dict):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, json=[{"id": OLD_ID, "question": "Net revenue last month?", "gold_sql": "SELECT 1"}]
            )
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(status, json=replace_body)

    return handler, seen


class TestEvalReplaceCase:
    def test_replaces_and_prints_the_diff(self, monkeypatch):
        handler, seen = _handler(
            201,
            {
                "id": NEW_ID,
                "question": "Net revenue last month?",
                "gold_sql": "SELECT 2",
                "supersedes_case_id": OLD_ID,
                "entry_check": {"state": "running", "run_id": "019f0000-0000-7000-8000-0000000000ee"},
            },
        )
        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--gold-sql", "SELECT 2"))

        assert result.exit_code == 0, result.output
        assert f"/eval/cases/{OLD_ID}/replace" in seen["url"]
        # A field left out is not sent: the server keeps the current value.
        assert seen["body"] == {"gold_sql": "SELECT 2"}
        assert f"Replaced eval case {OLD_ID} by {NEW_ID}" in result.output
        assert "Question unchanged" in result.output
        assert "-SELECT 1" in result.output and "+SELECT 2" in result.output
        assert "Expectation unchanged" in result.output
        assert "Checking it" in result.output

    def test_a_new_expectation_alone_is_a_replacement(self, monkeypatch):
        handler, seen = _handler(
            201,
            {
                "id": NEW_ID,
                "question": "Net revenue last month?",
                "gold_sql": "SELECT 1",
                "expectation": {"kind": "cannot_answer"},
                "supersedes_case_id": OLD_ID,
            },
        )
        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--expect", "cannot-answer"))

        assert result.exit_code == 0, result.output
        assert seen["body"] == {"expectation": {"kind": "cannot_answer"}}
        assert "Gold SQL unchanged" in result.output
        assert "Expectation: an answer → the agent to say the question cannot be answered" in result.output

    def test_json_mode_warns_on_stderr_when_the_server_ignores_expect(self, monkeypatch):
        replacement = {"id": NEW_ID, "question": "Net revenue last month?", "gold_sql": "SELECT 1"}
        handler, _seen = _handler(201, replacement)
        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--expect", "cannot-answer", "--json"))

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == replacement
        assert "This server ignores --expect" in result.stderr

    def test_nothing_changed_exits_zero(self, monkeypatch):
        handler, _ = _handler(200, {"id": OLD_ID, "question": "Net revenue last month?", "gold_sql": "SELECT 1"})
        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--gold-sql", "SELECT 1"))

        assert result.exit_code == 0, result.output
        assert "Nothing changed" in result.output

    def test_already_replaced_exits_one(self, monkeypatch):
        handler, _ = _handler(409, {"detail": f"Test case {OLD_ID} was replaced by {NEW_ID}"})
        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("-q", "Net revenue last quarter?"))

        assert result.exit_code == 1
        assert "was replaced" in result.output

    def test_missing_case_exits_one(self, monkeypatch):
        handler, _ = _handler(404, {"detail": "Eval case not found"})
        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--gold-sql", "SELECT 2"))

        assert result.exit_code == 1
        assert "No current eval case" in result.output

    def test_nothing_to_change_is_a_usage_error(self, monkeypatch):
        handler, seen = _handler(201, {})
        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args())

        assert result.exit_code == 2
        assert "nothing to change" in result.output
        assert "url" not in seen

    def test_no_gold_sql_sends_an_empty_gold_sql(self, monkeypatch):
        handler, seen = _handler(
            201,
            {
                "id": NEW_ID,
                "question": "Net revenue last month?",
                "gold_sql": None,
                "expectation": {"kind": "cannot_answer"},
                "supersedes_case_id": OLD_ID,
            },
        )
        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--no-gold-sql", "--expect", "cannot-answer"))

        assert result.exit_code == 0, result.output
        assert seen["body"] == {"gold_sql": "", "expectation": {"kind": "cannot_answer"}}
        assert "-SELECT 1" in result.output

    def test_no_gold_sql_with_a_gold_sql_is_a_usage_error(self, monkeypatch):
        handler, seen = _handler(201, {})
        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--no-gold-sql", "--gold-sql", "SELECT 2"))

        assert result.exit_code == 2
        assert "url" not in seen

    def test_a_compare_only_replacement_sets_the_named_rules(self, monkeypatch):
        rules = {"numeric": "exact", "ordered": True, "columns": "automatic", "allow_empty": True}
        seen = {}

        def handler(request):
            if request.method == "GET":
                return httpx.Response(
                    200, json=[{"id": OLD_ID, "question": "Count?", "gold_sql": "SELECT 1", "comparison": rules}]
                )
            seen.update(json.loads(request.content))
            return httpx.Response(201, json={"id": NEW_ID, "question": "Count?", "gold_sql": "SELECT 1", **seen})

        _mock_api(monkeypatch, handler)
        result = runner.invoke(app, _args("--compare", "exact,ordered"))
        assert result.exit_code == 0, result.output
        assert seen == {"comparison": {**rules, "allow_empty": False}}
        assert "Comparison" in result.output
