"""A cannot-answer case needs no gold SQL; comparison rules a metadata-only source never applies are flagged."""

import json

import httpx
from cassis_cli.api import get_eval_cases, post_eval_case_create, post_eval_case_replace
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
CASE_ID = "019f0000-0000-7000-8000-0000000000ca"
NOT_APPLIED = "Result comparison rules are not applied on a metadata-only source"


def _mock(monkeypatch, handler):
    for name, function in (
        ("post_eval_case_create", post_eval_case_create),
        ("post_eval_case_replace", post_eval_case_replace),
        ("get_eval_cases", get_eval_cases),
    ):

        def patched(_function=function, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return _function(**kwargs)

        monkeypatch.setattr(f"cassis_cli.eval.{name}", patched)


def _add(*extra):
    return ["eval", "add-case", "--project", PROJECT_ID, "--api-key", "sk-k6-test", "-q", "Refund reason?", *extra]


def _case(**fields):
    return {"id": CASE_ID, "question": "Refund reason?", "gold_sql": None, **fields}


def test_add_case_without_gold_sql_when_the_case_expects_a_decline(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_case(expectation={"kind": "cannot_answer"}))

    _mock(monkeypatch, handler)

    result = runner.invoke(app, _add("--expect", "cannot-answer"))

    assert result.exit_code == 0, result.output
    assert seen["body"] == {"question": "Refund reason?", "expectation": {"kind": "cannot_answer"}}


def test_add_case_without_gold_sql_otherwise_is_a_usage_error(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(500))

    result = runner.invoke(app, _add())

    assert result.exit_code == 2
    assert "only an --expect cannot-answer case goes without one" in result.stderr


def test_replace_case_left_without_gold_sql_is_a_usage_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[_case(expectation={"kind": "cannot_answer"})])
        return httpx.Response(
            422, json={"detail": "A gold SQL is required unless the case expects the question cannot be answered"}
        )

    _mock(monkeypatch, handler)

    result = runner.invoke(
        app, ["eval", "replace-case", CASE_ID, "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--expect", "answer"]
    )

    assert result.exit_code == 2
    assert "A gold SQL is required" in result.stderr


def test_rules_on_a_metadata_only_source_print_one_warning(monkeypatch):
    ranked = {"numeric": "legacy", "ordered": True, "columns": "automatic", "allow_empty": False}
    cases = [
        _case(gold_sql="SELECT 1", comparison=ranked, comparison_applicable=False),
        {**_case(gold_sql="SELECT 2", comparison=ranked, comparison_applicable=False), "id": PROJECT_ID},
    ]
    _mock(monkeypatch, lambda request: httpx.Response(200, json=cases))

    listed = runner.invoke(app, ["eval", "list-cases", "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

    assert listed.exit_code == 0, listed.output
    assert listed.stderr.count(NOT_APPLIED) == 1

    _mock(monkeypatch, lambda request: httpx.Response(201, json=cases[0]))
    added = runner.invoke(app, _add("--gold-sql", "SELECT 1", "--compare", "ordered"))

    assert added.exit_code == 0, added.output
    assert NOT_APPLIED in added.stderr

    applicable = {**cases[0], "comparison_applicable": True}
    _mock(monkeypatch, lambda request: httpx.Response(201, json=applicable))
    connected = runner.invoke(app, _add("--gold-sql", "SELECT 1", "--compare", "ordered"))

    assert connected.exit_code == 0 and NOT_APPLIED not in connected.stderr
