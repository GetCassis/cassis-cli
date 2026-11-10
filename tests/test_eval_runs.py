"""`cassis eval runs`: the project's runs, newest first, from the CI run list."""

import json

import httpx
from cassis_cli.api import get_eval_runs
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
RUN_A = "019f0000-0000-7000-8000-00000000000a"
RUN_B = "019f0000-0000-7000-8000-00000000000b"

_RUNS = {
    "runs": [
        {
            "run_id": RUN_A,
            "status": "completed",
            "total_cases": 3,
            "ontology_label": "v4",
            "ontology_version_number": 4,
            "model": "claude-sonnet-5-5",
            "origin": "ci",
            "summary": {"total": 3, "passed": 2, "accuracy": 0.67},
            "started_at": "2026-11-04T10:00:00Z",
            "completed_at": "2026-11-04T10:05:00Z",
        },
        {
            "run_id": RUN_B,
            "status": "skipped",
            "total_cases": 0,
            "ontology_label": "Unpublished context",
            "model": "claude-sonnet-5-5",
            "origin": "scheduler",
            "summary": None,
            "skipped": {"reason": "budget", "limit_cents": 5000, "remaining_cents": 0},
            "started_at": None,
            "completed_at": None,
        },
    ]
}


def _mock_api(monkeypatch, handler):
    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return get_eval_runs(**kwargs)

    monkeypatch.setattr("cassis_cli.eval.get_eval_runs", patched)


def _args(*extra):
    return ["eval", "runs", "--project", PROJECT_ID, "--api-key", "sk-k6-test", *extra]


class TestEvalRuns:
    def test_lists_runs_with_origin_model_and_skip_reason(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=_RUNS)

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--limit", "5"))

        assert result.exit_code == 0, result.output
        assert seen["url"].endswith(f"/api/ci/projects/{PROJECT_ID}/eval/runs?limit=5")
        out = result.output
        assert RUN_A in out and "v4 · CI · claude-sonnet-5-5 · 2/3 passed" in out
        assert "2026-11-04 10:00" in out
        assert "Unpublished context · scheduled · claude-sonnet-5-5" in out
        assert "Not started: The automatic-spend window's limit is spent ($0.00 of $50.00 left)." in out
        assert out.index(RUN_A) < out.index(RUN_B)

    def test_scoped_and_repeat_runs_never_read_as_a_full_green_suite(self, monkeypatch):
        cases = [{"case_id": str(i), "selected": i < 3} for i in range(15)]
        runs = {
            "runs": [
                {
                    **_RUNS["runs"][0],
                    "summary": {"total": 3, "passed": 3},
                    "selection": {"mode": "changed", "cases": cases},
                },
                {
                    **_RUNS["runs"][0],
                    "run_id": RUN_B,
                    "total_cases": 25,
                    "repeat_count": 3,
                    "summary": {"total": 75, "passed": 75},
                },
            ]
        }
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=runs))

        result = runner.invoke(app, _args())

        assert result.exit_code == 0, result.output
        assert "3/3 passed · 3 of 15 cases (affected by a context change)" in result.output
        assert "25 cases × 3 attempts · 75/75 attempts passed" in result.output
        assert " 75/75 passed" not in result.output

    def test_json_passes_the_list_through(self, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=_RUNS))

        result = runner.invoke(app, _args("--json"))

        assert result.exit_code == 0
        assert json.loads(result.output) == _RUNS

    def test_an_older_server_is_too_old(self, monkeypatch):
        # The start route shares the path: an older server answers a GET with 405.
        for response in (
            httpx.Response(404, json={"detail": "Not Found"}),
            httpx.Response(405, json={"detail": "Method Not Allowed"}),
        ):
            _mock_api(monkeypatch, lambda request, response=response: response)

            result = runner.invoke(app, _args())

            assert result.exit_code == 3
            assert "does not list eval runs yet" in result.output

    def test_no_runs_says_so(self, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json={"runs": []}))

        result = runner.invoke(app, _args())

        assert result.exit_code == 0
        assert "No eval runs yet" in result.output
