import httpx
from cassis_cli.api import get_eval_cases, post_eval_case_create
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
CASE_A = "019f0000-0000-7000-8000-0000000000ca"
CASE_B = "019f0000-0000-7000-8000-0000000000cb"
CASE_C = "019f0000-0000-7000-8000-0000000000cc"
CASE_D = "019f0000-0000-7000-8000-0000000000cd"


def _mock(monkeypatch, name, fn, handler):
    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return fn(**kwargs)

    monkeypatch.setattr(f"cassis_cli.eval.{name}", patched)


def _case(case_id, question, entry_check):
    return {"id": case_id, "question": question, "gold_sql": "SELECT 1", "entry_check": entry_check}


def test_list_cases_flags_flaky_and_failing_cases(monkeypatch):
    cases = [
        _case(CASE_A, "Wins by driver?", {"state": "flaky", "passed": 2, "total": 3}),
        _case(CASE_B, "Pit stops?", {"state": "failing", "passed": 0, "total": 3}),
        _case(CASE_C, "Laps led?", {"state": "running", "run_id": CASE_A}),
        _case(CASE_D, "Podiums?", {"state": "stable", "passed": 3, "total": 3}),
    ]
    _mock(monkeypatch, "get_eval_cases", get_eval_cases, lambda request: httpx.Response(200, json=cases))

    result = runner.invoke(app, ["eval", "list-cases", "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

    assert result.exit_code == 0, result.output
    assert "⚠ flaky at creation (2/3 attempts passed)" in result.output
    assert "⚠ failing at creation (0/3 attempts passed)" in result.output
    assert "… entry check running" in result.output
    # A stable case reads like any other.
    lines = result.output.splitlines()
    podium = lines.index(f"{CASE_D}  Podiums?")
    assert podium == len(lines) - 1 or not lines[podium + 1].startswith("    ")


def test_add_case_says_the_check_is_running(monkeypatch):
    created = _case(CASE_A, "Wins by driver?", {"state": "running", "run_id": CASE_B})
    _mock(
        monkeypatch,
        "post_eval_case_create",
        post_eval_case_create,
        lambda request: httpx.Response(201, json=created),
    )

    result = runner.invoke(
        app,
        [
            "eval",
            "add-case",
            "--project",
            PROJECT_ID,
            "--api-key",
            "sk-k6-test",
            "-q",
            "Wins by driver?",
            "--gold-sql",
            "SELECT 1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Checking it: 3 attempts run on the current context" in result.output


def test_an_older_server_without_checks_prints_as_before(monkeypatch):
    cases = [{"id": CASE_A, "question": "Wins by driver?", "gold_sql": "SELECT 1"}]
    _mock(monkeypatch, "get_eval_cases", get_eval_cases, lambda request: httpx.Response(200, json=cases))

    result = runner.invoke(app, ["eval", "list-cases", "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

    assert result.output.strip() == f"{CASE_A}  Wins by driver?"
