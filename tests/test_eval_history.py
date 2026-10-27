import json

import httpx
from cassis_cli.api import get_eval_case_history, get_eval_cases, post_eval_case_create
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
CASE_ID = "019f0000-0000-7000-8000-0000000000ca"
RUN_A = "019f0000-0000-7000-8000-00000000000a"
RUN_B = "019f0000-0000-7000-8000-00000000000b"
RUN_C = "019f0000-0000-7000-8000-00000000000c"


def _mock_history_api(monkeypatch, handler):
    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return get_eval_case_history(**kwargs)

    monkeypatch.setattr("cassis_cli.eval.get_eval_case_history", patched)


def _mock_list_api(monkeypatch, handler):
    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return get_eval_cases(**kwargs)

    monkeypatch.setattr("cassis_cli.eval.get_eval_cases", patched)


def _mock_create_api(monkeypatch, handler):
    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return post_eval_case_create(**kwargs)

    monkeypatch.setattr("cassis_cli.eval.post_eval_case_create", patched)


def _history_args(*extra):
    return ["eval", "history", CASE_ID, "--project", PROJECT_ID, "--api-key", "sk-k6-test", *extra]


def _row(run_id, status, *, key, label="v3", model="m", app_version="8.23.0", created_at, **fields):
    return {
        "id": f"{run_id}-r",
        "run_id": run_id,
        "test_case_id": CASE_ID,
        "status": status,
        "generated_sql": "SELECT 1",
        "duration_seconds": 20.0,
        "error": None,
        "created_at": created_at,
        "run_status": "completed",
        "run_app_version": app_version,
        "run_model": model,
        "run_is_executable": False,
        "run_ontology_label": label,
        "run_config_key": key,
        **fields,
    }


_HISTORY = {
    "case_id": CASE_ID,
    "question": "Pit stops\nper team?",
    "health": {
        "total": 3,
        "passed": 2,
        "verdict": "noisy",
        "configurations": 2,
        "mixed_configurations": 1,
        "avg_plan_seconds": 12.0,
        "avg_execution_seconds": 4.0,
        "divergences": {"wrong_filter": 1},
        "dependency_states": {"matched": 2, "diverged": 1},
    },
    "results": [
        _row(
            RUN_A,
            "passed",
            key="v3|m|8.23.0",
            created_at="2026-10-23T18:14:00Z",
            plan_seconds=10.0,
            execution_seconds=3.0,
            dependency_state="matched",
        ),
        _row(
            RUN_B,
            "failed",
            key="v3|m|8.23.0",
            created_at="2026-10-23T10:16:00Z",
            judge_divergence="wrong_filter",
            plan_seconds=14.0,
            execution_seconds=5.0,
            dependency_state="diverged",
        ),
        _row(
            RUN_C,
            "passed",
            key="hash|m|8.22.0",
            label="feat/pit-stops",
            app_version="8.22.0",
            created_at="2026-10-18T09:00:00Z",
            dependency_state="diverged",
        ),
    ],
}


class TestEvalHistory:
    def test_prints_health_and_grouped_rows(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=_HISTORY)

        _mock_history_api(monkeypatch, handler)

        result = runner.invoke(app, _history_args())

        assert result.exit_code == 0, result.output
        assert seen["url"].endswith(f"/api/ci/projects/{PROJECT_ID}/eval/cases/{CASE_ID}/history")
        out = result.output
        assert f"Case {CASE_ID}: Pit stops per team?" in out
        assert "noisy · pass rate 67% (2 of 3)" in out
        assert "2 configuration(s)" in out
        assert "plan/execution 12.0s / 4.0s avg" in out
        assert "1 configuration(s) with verdicts that differ" in out
        assert "divergences: wrong_filter ×1" in out
        assert "footprint: matched 2, diverged 1" in out
        # Groups keep arrival order (newest first) and name the configuration.
        assert out.index("v3 · m · Cassis 8.23.0 · 2 run(s) · 1 passed") < out.index(
            "feat/pit-stops · m · Cassis 8.22.0 · 1 run(s) · 1 passed"
        )
        assert "verdicts differ on an unchanged configuration" in out
        assert "2026-10-23 18:14" in out
        assert "wrong_filter" in out
        # A pass from another table set reads as an alternative path, not a defect.
        assert "alternative path" in out
        assert "10.0s / 3.0s" in out
        assert "20.0s total" in out  # the row without phase timings

    def test_no_results_says_so(self, monkeypatch):
        empty = {**_HISTORY, "health": {**_HISTORY["health"], "total": 0, "passed": 0}, "results": []}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=empty)

        _mock_history_api(monkeypatch, handler)

        result = runner.invoke(app, _history_args())

        assert result.exit_code == 0
        assert "No results yet" in result.output

    def test_json_passes_the_record_through(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_HISTORY)

        _mock_history_api(monkeypatch, handler)

        result = runner.invoke(app, _history_args("--json"))

        assert result.exit_code == 0
        assert json.loads(result.output) == _HISTORY

    def test_unknown_case_exits_one(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Eval case not found"})

        _mock_history_api(monkeypatch, handler)

        result = runner.invoke(app, _history_args())

        assert result.exit_code == 1
        assert "No current eval case" in result.output

    def test_unknown_project_exits_three(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Project not found"})

        _mock_history_api(monkeypatch, handler)

        result = runner.invoke(app, _history_args())

        assert result.exit_code == 3

    def test_non_uuid_case_id_exits_two(self):
        result = runner.invoke(app, ["eval", "history", "nope", "--project", PROJECT_ID, "--api-key", "sk-k6-test"])
        assert result.exit_code == 2


class TestListCasesFootprint:
    def test_flags_a_case_whose_gold_sql_does_not_resolve(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": CASE_ID,
                        "question": "Pit stops?",
                        "gold_sql": "SELECT 1",
                        "footprint": {
                            "state": "partial",
                            "tables": ["public.pit_stops"],
                            "unresolved": ["raw.pit_stops_archive"],
                            "ambiguous": {},
                            "shadowed": [],
                            "error": None,
                            "branch_id": None,
                        },
                    },
                    {
                        "id": RUN_A,
                        "question": "Races?",
                        "gold_sql": "SELECT 2",
                        "footprint": {"state": "complete", "tables": ["public.races"]},
                    },
                ],
            )

        _mock_list_api(monkeypatch, handler)

        result = runner.invoke(app, ["eval", "list-cases", "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 0, result.output
        assert f"{CASE_ID}  Pit stops?" in result.output
        assert "⚠ not in the context: raw.pit_stops_archive" in result.output
        assert "1 case(s) have a gold SQL that does not fully resolve" in result.output
        # A complete footprint is the normal state and says nothing.
        assert result.output.count("⚠") == 1

    def test_branch_is_passed_through_and_unknown_branch_exits_two(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(404, json={"detail": "Branch 'nope' not found"})

        _mock_list_api(monkeypatch, handler)

        result = runner.invoke(
            app, ["eval", "list-cases", "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--branch", "nope"]
        )

        assert result.exit_code == 2, result.output
        assert seen["url"].endswith("/eval/cases?branch=nope")
        assert "Branch 'nope' not found" in result.output

    def test_older_server_without_footprint_prints_plainly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"id": CASE_ID, "question": "q", "gold_sql": "SELECT 1"}])

        _mock_list_api(monkeypatch, handler)

        result = runner.invoke(app, ["eval", "list-cases", "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "⚠" not in result.output


class TestAddCaseFootprint:
    def test_warns_when_the_new_cases_gold_sql_does_not_resolve(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                json={
                    "id": CASE_ID,
                    "question": "q",
                    "gold_sql": "SELECT 1",
                    "footprint": {"state": "unparsed", "tables": [], "error": "Expected FROM at line 1"},
                },
            )

        _mock_create_api(monkeypatch, handler)

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
                "q",
                "--gold-sql",
                "SELECT 1",
            ],
        )

        assert result.exit_code == 0, result.output
        assert f"✓ Added eval case {CASE_ID}" in result.output
        assert "does not fully resolve" in result.output
        assert "gold SQL does not parse: Expected FROM at line 1" in result.output
