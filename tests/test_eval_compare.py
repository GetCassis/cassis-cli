import json

import httpx
import pytest
from cassis_cli.api import (
    EvalCompareUnsupportedError,
    get_eval_run,
    get_eval_run_comparison,
    get_eval_run_results,
    post_eval_run_cancel,
    post_eval_run_start,
)
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
RUN_ID = "019f0000-0000-7000-8000-00000000aaaa"
CASE_A = "019f0000-0000-7000-8000-0000000000ca"
CASE_B = "019f0000-0000-7000-8000-0000000000cb"


@pytest.fixture
def repo(tmp_path):
    ontology_dir = tmp_path / "cassis"
    (ontology_dir / "tables" / "public").mkdir(parents=True)
    (ontology_dir / "_project.yml").write_text("display_name: Test\n")
    (ontology_dir / "tables" / "public" / "orders.yml").write_text("schema_name: public\ntable_name: orders\n")
    return tmp_path


def _mock_api(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    for name, fn in (
        ("post_eval_run_start", post_eval_run_start),
        ("get_eval_run", get_eval_run),
        ("get_eval_run_results", get_eval_run_results),
        ("post_eval_run_cancel", post_eval_run_cancel),
        ("get_eval_run_comparison", get_eval_run_comparison),
    ):

        def patched(_fn=fn, **kwargs):
            kwargs["transport"] = transport
            return _fn(**kwargs)

        monkeypatch.setattr(f"cassis_cli.eval.{name}", patched)
    monkeypatch.setattr("cassis_cli.common.time.sleep", lambda _s: None)


def _attempt(n, passed, variant):
    return {
        "attempt": n,
        "result_id": None,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "sql_variant": variant,
        "judge_verdict": "equivalent" if passed else "not_equivalent",
        "rejudge_verdict": None,
    }


_COMPARISON = {
    "run_id": RUN_ID,
    "status": "completed",
    "repeat_count": 3,
    "is_executable": False,
    "configuration_key": "abc|m|8.24.0",
    "summary": {"cases": 2, "all_passed": 1, "all_failed": 0, "flipped": 1, "causes": {"judge": 1}},
    "cases": [
        {
            "test_case_id": CASE_A,
            "question": "Pit stops\nper team?",
            "inputs_key": "0123456789ab",
            "passed": 2,
            "total": 3,
            "agreement": "flipped",
            "distinct_sql": 1,
            "judge_unstable": True,
            "cause": "judge",
            "attempts": [_attempt(1, True, 1), _attempt(2, False, 1), _attempt(3, True, 1)],
        },
        {
            "test_case_id": CASE_B,
            "question": "Wins by driver?",
            "inputs_key": "ba9876543210",
            "passed": 3,
            "total": 3,
            "agreement": "all_passed",
            "distinct_sql": 1,
            "judge_unstable": False,
            "cause": None,
            "attempts": [_attempt(1, True, 1), _attempt(2, True, 1), _attempt(3, True, 1)],
        },
    ],
}


def _compare_args(*extra):
    return ["eval", "compare", RUN_ID, "--project", PROJECT_ID, "--api-key", "sk-k6-test", *extra]


class TestCompareCommand:
    def test_prints_flips_with_their_cause(self, monkeypatch):
        def handler(request):
            assert str(request.url).endswith(f"/api/ci/projects/{PROJECT_ID}/eval/runs/{RUN_ID}/comparison")
            return httpx.Response(200, json=_COMPARISON)

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _compare_args())

        assert result.exit_code == 0, result.output
        assert "Repeat comparison: 3 attempts per case, 2 cases" in result.output
        assert "1 passed every attempt · 0 failed every attempt · 1 flipped (judge 1)" in result.output
        assert "~ 2/3  judge" in result.output
        assert "Pit stops per team?" in result.output
        assert CASE_A in result.output
        # Stable passes are counted, not listed.
        assert "Wins by driver?" not in result.output
        assert "judge: the judge ruled differently on the same SQL" in result.output

    def test_json_passes_the_body_through(self, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=_COMPARISON))

        result = runner.invoke(app, _compare_args("--json"))

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == _COMPARISON

    def test_an_older_server_says_it_cannot_compare(self, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Not Found"}))

        result = runner.invoke(app, _compare_args())

        assert result.exit_code == 3
        assert "does not compare runs yet" in result.output

    def test_an_unknown_run_is_a_scope_error(self, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Eval run not found"}))

        result = runner.invoke(app, _compare_args())

        assert result.exit_code == 3
        assert "Eval run not found" in result.output

    def test_bad_run_id_is_a_usage_error(self):
        result = runner.invoke(app, ["eval", "compare", "nope", "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 2

    def test_unsupported_error_is_an_api_error(self):
        from cassis_cli.api import ApiError

        assert issubclass(EvalCompareUnsupportedError, ApiError)


def _run_body(status="running", repeat_count=3, summary=None):
    body = {
        "run_id": RUN_ID,
        "status": status,
        "total_cases": 2,
        "ontology_label": "my-branch",
        "summary": summary,
        "started_at": "2026-10-26T10:00:00Z",
        "completed_at": None,
    }
    if repeat_count is not None:
        body["repeat_count"] = repeat_count
    return body


class TestRunRepeat:
    def test_repeat_sends_the_count_and_prints_the_comparison(self, repo, monkeypatch):
        seen = {}

        def handler(request):
            url = str(request.url)
            if request.method == "POST" and url.endswith("/eval/runs"):
                seen["body"] = json.loads(request.content)
                return httpx.Response(201, json=_run_body())
            if url.endswith(f"/eval/runs/{RUN_ID}"):
                return httpx.Response(
                    200, json=_run_body(status="completed", summary={"total": 6, "passed": 5, "accuracy": 0.83})
                )
            if url.endswith("/results"):
                return httpx.Response(
                    200,
                    json=[
                        {"test_case_id": CASE_A, "question": "q", "status": "passed", "attempt": 1},
                        {"test_case_id": CASE_A, "question": "q", "status": "failed", "attempt": 2},
                    ],
                )
            if url.endswith("/comparison"):
                return httpx.Response(200, json=_COMPARISON)
            raise AssertionError(f"unexpected request: {request.method} {url}")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--repeat", "3"]
        )

        assert seen["body"]["repeat_count"] == 3
        assert "3 attempts each" in result.output
        assert "Repeat comparison: 3 attempts per case" in result.output
        assert "~ 2/3  judge" in result.output
        # A flip is the noise a repeat run measures, not a failed suite.
        assert result.exit_code == 0, result.output

    def test_repeat_fails_when_a_case_failed_every_attempt(self, repo, monkeypatch):
        def handler(request):
            url = str(request.url)
            if request.method == "POST":
                return httpx.Response(201, json=_run_body())
            if url.endswith(f"/eval/runs/{RUN_ID}"):
                return httpx.Response(200, json=_run_body(status="completed", summary={"total": 4, "passed": 2}))
            if url.endswith("/results"):
                return httpx.Response(
                    200,
                    json=[
                        {"test_case_id": CASE_A, "question": "a", "status": "passed", "attempt": 1},
                        {"test_case_id": CASE_A, "question": "a", "status": "passed", "attempt": 2},
                        {"test_case_id": CASE_B, "question": "b", "status": "failed", "attempt": 1},
                        {"test_case_id": CASE_B, "question": "b", "status": "failed", "attempt": 2},
                    ],
                )
            return httpx.Response(200, json=_COMPARISON)

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--repeat", "2"]
        )

        assert result.exit_code == 1, result.output

    def test_repeat_json_carries_the_comparison(self, repo, monkeypatch):
        def handler(request):
            url = str(request.url)
            if request.method == "POST":
                return httpx.Response(201, json=_run_body())
            if url.endswith(f"/eval/runs/{RUN_ID}"):
                return httpx.Response(200, json=_run_body(status="completed", summary={"total": 6, "passed": 6}))
            if url.endswith("/results"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=_COMPARISON)

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app,
            ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--repeat", "3", "--json"],
        )

        assert json.loads(result.stdout)["comparison"]["summary"]["flipped"] == 1

    def test_an_older_server_ignoring_repeat_is_called_out(self, repo, monkeypatch):
        def handler(request):
            url = str(request.url)
            if request.method == "POST":
                return httpx.Response(201, json=_run_body(repeat_count=None))
            if url.endswith(f"/eval/runs/{RUN_ID}"):
                return httpx.Response(
                    200, json=_run_body(status="completed", repeat_count=None, summary={"total": 2, "passed": 2})
                )
            if url.endswith("/results"):
                return httpx.Response(200, json=[])
            raise AssertionError(f"no comparison is fetched for an ordinary run: {url}")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--repeat", "3"]
        )

        assert "ignored --repeat" in result.output

    def test_repeat_outside_two_to_five_is_a_usage_error(self, repo):
        for n in ("1", "6"):
            result = runner.invoke(
                app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--repeat", n]
            )

            assert result.exit_code == 2, n
