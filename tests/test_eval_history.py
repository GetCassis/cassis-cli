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
        assert "answer 12.0s avg · query 4.0s avg" in out
        assert "1 configuration(s) with verdicts that differ" in out
        assert "divergences: wrong_filter ×1" in out
        assert "footprint: matched 2, diverged 1" in out
        # Groups keep arrival order (newest first) and name the configuration.
        assert out.index("v3 · m · Cassis 8.23.0 · 2 runs · 1 passed") < out.index(
            "feat/pit-stops · m · Cassis 8.22.0 · 1 run · 1 passed"
        )
        assert "verdicts differ on an unchanged configuration" in out
        assert "2026-10-23 18:14" in out
        assert "wrong_filter" in out
        # A pass from another table set reads as an alternative path, not a defect.
        assert "alternative path" in out
        assert "plan 10.0s · exec 3.0s" in out
        assert "total 20.0s" in out  # the row without phase timings

    def test_a_repeat_run_counts_as_one_run_with_its_attempts(self, monkeypatch):
        # The server's shape: `runs[]` carries each run with its counts, the
        # results carry the configuration key that groups runs.
        def run(run_id, origin, label="v4"):
            return {
                "id": run_id,
                "status": "completed",
                "origin": origin,
                "ontology_label": label,
                "model": "claude-sonnet-5-5",
                "app_version": "8.30.0",
            }

        results = [
            _row(RUN_A, "passed", key="v4|s|8.30.0", label="v4", created_at="2026-11-04T10:00:00Z", attempt=1),
            _row(RUN_A, "failed", key="v4|s|8.30.0", label="v4", created_at="2026-11-04T10:01:00Z", attempt=2),
            _row(RUN_A, "passed", key="v4|s|8.30.0", label="v4", created_at="2026-11-04T10:02:00Z", attempt=3),
            _row(RUN_B, "passed", key="h|s|8.30.0", label="feat/x", created_at="2026-11-03T10:00:00Z"),
            _row(RUN_C, "passed", key="h|s|8.30.0", label="feat/x", created_at="2026-11-02T10:00:00Z"),
        ]
        for i, r in enumerate(results):
            r["id"] = f"{r['run_id']}-{i}"

        def entry(run_id, rows, passed, origin, label="v4"):
            return {
                "run_id": run_id,
                "created_at": rows[0]["created_at"],
                "result_ids": [r["id"] for r in rows],
                "passed": passed,
                "total": len(rows),
                "expected_attempts": len(rows),
                "run": run(run_id, origin, label),
            }

        history = {
            **_HISTORY,
            "results": results,
            "runs": [
                entry(RUN_A, results[:3], 2, "ci"),
                entry(RUN_B, results[3:4], 1, "cli", "feat/x"),
                entry(RUN_C, results[4:], 1, "scheduler", "feat/x"),
            ],
        }
        _mock_history_api(monkeypatch, lambda request: httpx.Response(200, json=history))

        result = runner.invoke(app, _history_args())

        assert result.exit_code == 0, result.output
        out = result.output
        assert "v4 · claude-sonnet-5-5 · Cassis 8.30.0 · 1 run · 3 attempts · 2 passed · CI" in out
        assert "attempt 3" in out
        # Runs of one configuration from different origins name none.
        assert "feat/x · claude-sonnet-5-5 · Cassis 8.30.0 · 2 runs · 2 passed\n" in out
        assert "3 runs" not in out

    def test_rows_print_their_error_note_and_deciding_rule(self, monkeypatch):
        history = {
            **_HISTORY,
            "comparison": {"numeric": "exact", "ordered": False, "columns": "automatic", "allow_empty": False},
            "results": [
                _row(
                    RUN_A,
                    "failed",
                    key="k",
                    created_at="2026-11-04T10:00:00Z",
                    error="Row count mismatch: 3 vs 4",
                    comparison_rule="row_count",
                ),
                _row(
                    RUN_B,
                    "passed",
                    key="k",
                    created_at="2026-11-03T10:00:00Z",
                    comparison_applied=False,
                    comparison_note="Not applied: SQL results were not executed",
                ),
            ],
        }
        _mock_history_api(monkeypatch, lambda request: httpx.Response(200, json=history))

        result = runner.invoke(app, _history_args())

        assert result.exit_code == 0, result.output
        assert "Comparison: exact numbers" in result.output
        assert "Row count mismatch: 3 vs 4" in result.output
        assert "Failed on row count" in result.output
        assert "Result comparison not applied: SQL results were not executed" in result.output

    def test_rows_print_one_timing_format_under_a_header_and_no_bare_dash(self, monkeypatch):
        history = {
            **_HISTORY,
            "results": [
                _row(RUN_A, "passed", key="k", created_at="2026-11-04T10:00:00Z", plan_seconds=9.4),
                _row(RUN_B, "passed", key="k", created_at="2026-11-03T10:00:00Z", duration_seconds=None),
                _row(RUN_C, "passed", key="j", created_at="2026-11-02T10:00:00Z", duration_seconds=None),
            ],
        }
        _mock_history_api(monkeypatch, lambda request: httpx.Response(200, json=history))

        result = runner.invoke(app, _history_args())

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        # The group with a timing names its one column; the bare group prints none.
        assert lines.count("    status            date              timing") == 1
        assert "  ✓ passed            2026-11-04 10:00  plan 9.4s" in lines
        assert "  ✓ passed            2026-11-03 10:00" in lines
        assert "  ✓ passed            2026-11-02 10:00" in lines
        assert " - " not in result.output and not any(line.endswith(" -") for line in lines)

    def test_a_row_from_a_change_scoped_run_says_how_much_it_covered(self, monkeypatch):
        rows = [{"case_id": CASE_ID, "selected": True}] + [{"case_id": f"c{i}", "selected": False} for i in range(14)]
        result_row = _row(RUN_A, "passed", key="k", created_at="2026-11-04T10:00:00Z")
        history = {
            **_HISTORY,
            "results": [result_row],
            "runs": [
                {
                    "run_id": RUN_A,
                    "created_at": "2026-11-04T10:00:00Z",
                    "result_ids": [result_row["id"]],
                    "passed": 1,
                    "total": 1,
                    "run": {"id": RUN_A, "selection": {"mode": "changed", "cases": rows}},
                }
            ],
        }
        _mock_history_api(monkeypatch, lambda request: httpx.Response(200, json=history))

        result = runner.invoke(app, _history_args())

        assert result.exit_code == 0, result.output
        assert "· run covered 1 of 15 cases (affected by a context change)" in result.output

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
