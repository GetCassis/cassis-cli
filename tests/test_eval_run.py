import json

import httpx
import pytest
from cassis_cli.api import (
    ApiError,
    EvalBranchNotFoundError,
    EvalRunActiveError,
    EvalStartValidationError,
    get_eval_run,
    get_eval_run_results,
    post_eval_run_cancel,
    post_eval_run_start,
)
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
RUN_ID = "019f0000-0000-7000-8000-00000000aaaa"


@pytest.fixture
def repo(tmp_path):
    """A fake repository checkout with an ontology tree under the default base path."""
    ontology_dir = tmp_path / "cassis"
    (ontology_dir / "tables" / "public").mkdir(parents=True)
    (ontology_dir / "_project.yml").write_text("display_name: Test\n")
    (ontology_dir / "tables" / "public" / "orders.yml").write_text("schema_name: public\ntable_name: orders\n")
    return tmp_path


def _mock_api(monkeypatch, handler):
    """Route all of the eval commands' HTTP calls through an httpx.MockTransport."""
    transport = httpx.MockTransport(handler)
    for name, fn in (
        ("post_eval_run_start", post_eval_run_start),
        ("get_eval_run", get_eval_run),
        ("get_eval_run_results", get_eval_run_results),
        ("post_eval_run_cancel", post_eval_run_cancel),
    ):

        def patched(_fn=fn, **kwargs):
            kwargs["transport"] = transport
            return _fn(**kwargs)

        monkeypatch.setattr(f"cassis_cli.eval.{name}", patched)
    monkeypatch.setattr("cassis_cli.common.time.sleep", lambda _s: None)


def _run_body(status="running", total=2, label="my-branch", summary=None):
    return {
        "run_id": RUN_ID,
        "status": status,
        "total_cases": total,
        "ontology_label": label,
        "summary": summary,
        "started_at": "2026-07-21T10:00:00Z",
        "completed_at": None,
    }


def _result(status="passed", question="Total revenue?", error=None):
    return {
        "question": question,
        "status": status,
        "generated_sql": "SELECT 1",
        "duration_seconds": 5.0,
        "error": error,
    }


def _fake_monotonic():
    """Clock advancing 100s per call — forces the wait loop's deadline quickly."""
    state = {"t": 0.0}

    def monotonic():
        state["t"] += 100.0
        return state["t"]

    return monotonic


def _sequential_handler(seen):
    """start → 1 running poll with partial results → completed with full results."""
    polls = {"count": 0}

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url.endswith("/eval/runs"):
            seen["start_body"] = json.loads(request.content)
            assert request.headers["Authorization"] == "Bearer sk-k6-test"
            return httpx.Response(201, json=_run_body())
        if url.endswith(f"/eval/runs/{RUN_ID}"):
            polls["count"] += 1
            if polls["count"] <= 1:
                return httpx.Response(200, json=_run_body())
            return httpx.Response(
                200,
                json=_run_body(status="completed", summary={"total": 2, "passed": 2, "accuracy": 1.0}),
            )
        if url.endswith("/results"):
            if polls["count"] <= 1:
                return httpx.Response(200, json=[_result()])
            return httpx.Response(200, json=[_result(), _result(question="Orders by region?")])
        raise AssertionError(f"unexpected request: {request.method} {url}")

    return handler


class TestEvalRunCommand:
    def test_happy_path_polls_to_completion(self, repo, monkeypatch):
        seen = {}
        _mock_api(monkeypatch, _sequential_handler(seen))

        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 0, result.output
        # The local tree is sent, relative to the ontology dir.
        assert "tables/public/orders.yml" in seen["start_body"]["files"]
        assert "2 cases" in result.output
        assert "2/2 cases done" in result.output
        assert "2/2 passed" in result.output
        # Deep link to the run's webapp page (defaults to the API URL host).
        assert f"/eval?projectId={PROJECT_ID}&tab=runs&runId={RUN_ID}" in result.output

    def test_app_url_overrides_link_host(self, repo, monkeypatch):
        _mock_api(monkeypatch, _sequential_handler({}))

        result = runner.invoke(
            app,
            [
                "eval",
                "run",
                str(repo),
                "--project",
                PROJECT_ID,
                "--api-key",
                "sk-k6-test",
                "--app-url",
                "https://app.example.com/",
            ],
        )

        assert result.exit_code == 0, result.output
        assert f"https://app.example.com/eval?projectId={PROJECT_ID}&tab=runs&runId={RUN_ID}" in result.output

    def test_no_wait_json_emits_json(self, repo, monkeypatch):
        def handler(request):
            return httpx.Response(201, json=_run_body())

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app,
            ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--no-wait", "--json"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["run"]["run_id"] == RUN_ID
        assert "run_url" in payload

    def test_case_flag_sends_test_case_ids(self, repo, monkeypatch):
        case_a = "019f0000-0000-7000-8000-0000000000aa"
        case_b = "019f0000-0000-7000-8000-0000000000bb"
        seen = {}

        def handler(request):
            if request.method == "POST":
                seen["body"] = json.loads(request.content)
                return httpx.Response(201, json=_run_body(total=2))
            if str(request.url).endswith("/results"):
                return httpx.Response(200, json=[_result(), _result(question="Other?")])
            return httpx.Response(200, json=_run_body(status="completed"))

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app,
            [
                "eval",
                "run",
                str(repo),
                "--project",
                PROJECT_ID,
                "--api-key",
                "sk-k6-test",
                "--case",
                case_a,
                "--case",
                case_b,
            ],
        )

        assert result.exit_code == 0, result.output
        assert seen["body"]["test_case_ids"] == [case_a, case_b]

    def test_malformed_case_id_exits_two(self, repo, monkeypatch):
        def handler(request):  # nothing must reach the network
            raise AssertionError("no request expected")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app,
            ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--case", "not-a-uuid"],
        )

        assert result.exit_code == 2
        assert "--case must be an eval case ID" in result.output

    def test_label_with_branch_exits_two(self, repo, monkeypatch):
        def handler(request):  # nothing must reach the network
            raise AssertionError("no request expected")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app,
            [
                "eval",
                "run",
                "--project",
                PROJECT_ID,
                "--api-key",
                "sk-k6-test",
                "--branch",
                "feature-x",
                "--label",
                "MR 42",
            ],
        )

        assert result.exit_code == 2
        assert "--label cannot be used with --branch" in result.output

    def test_auth_error_while_polling_fails_fast(self, repo, monkeypatch):
        calls = {"n": 0}

        def handler(request):
            if request.method == "POST" and str(request.url).endswith("/eval/runs"):
                return httpx.Response(201, json=_run_body())
            calls["n"] += 1
            return httpx.Response(401)

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 3
        assert calls["n"] == 1  # no retry spin on a revoked key
        assert "run keeps going server-side" in result.output

    def test_timeout_exits_three_without_garbage_summary(self, repo, monkeypatch):
        def handler(request):
            if request.method == "POST" and str(request.url).endswith("/eval/runs"):
                return httpx.Response(201, json=_run_body())
            if str(request.url).endswith("/results"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=_run_body(status="running"))

        _mock_api(monkeypatch, handler)
        monkeypatch.setattr("cassis_cli.common.time.monotonic", _fake_monotonic())

        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 3
        assert "Timed out" in result.output
        assert "passed" not in result.output  # no bogus summary line after timeout

    def test_label_from_ci_env_var_on_detached_head(self, repo, monkeypatch):
        seen = {}
        _mock_api(monkeypatch, _sequential_handler(seen))
        monkeypatch.setenv("GITHUB_HEAD_REF", "feat/from-ci")

        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 0, result.output
        assert seen["start_body"]["label"] == "feat/from-ci"

    def test_failed_case_exits_one(self, repo, monkeypatch):
        def handler(request):
            url = str(request.url)
            if request.method == "POST" and url.endswith("/eval/runs"):
                return httpx.Response(201, json=_run_body(total=1))
            if url.endswith(f"/eval/runs/{RUN_ID}"):
                return httpx.Response(
                    200, json=_run_body(status="completed", total=1, summary={"total": 1, "passed": 0})
                )
            if url.endswith("/results"):
                return httpx.Response(200, json=[_result(status="failed")])
            raise AssertionError(f"unexpected request: {url}")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 1
        assert "failed" in result.output

    def test_no_wait_prints_run_id_and_exits_zero(self, repo, monkeypatch):
        def handler(request):
            assert request.method == "POST"
            return httpx.Response(201, json=_run_body())

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app,
            ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--no-wait"],
        )

        assert result.exit_code == 0
        assert RUN_ID in result.output

    def test_branch_run_sends_no_files(self, repo, monkeypatch):
        seen = {}

        def handler(request):
            url = str(request.url)
            if request.method == "POST" and url.endswith("/eval/runs"):
                seen["start_body"] = json.loads(request.content)
                return httpx.Response(201, json=_run_body(label="feature-x"))
            if url.endswith("/results"):
                return httpx.Response(200, json=[_result(), _result(question="Orders by region?")])
            return httpx.Response(
                200, json=_run_body(status="completed", label="feature-x", summary={"total": 2, "passed": 2})
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app,
            ["eval", "run", "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--branch", "feature-x"],
        )

        assert result.exit_code == 0, result.output
        assert "files" not in seen["start_body"]
        assert seen["start_body"]["branch"] == "feature-x"

    def test_invalid_tree_400_prints_findings_and_exits_one(self, repo, monkeypatch):
        def handler(request):
            return httpx.Response(
                400,
                json={
                    "detail": {
                        "message": "1 file failed validation",
                        "findings": [{"stage": "yaml", "path": "tables/bad.yml", "message": "bad yaml"}],
                    }
                },
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 1
        assert "cassis/tables/bad.yml" in result.output
        assert "bad yaml" in result.output

    def test_active_run_409_exits_three(self, repo, monkeypatch):
        def handler(request):
            return httpx.Response(409, json={"detail": "An eval run is already active"})

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 3
        assert "already active" in result.output

    def test_json_output_keeps_stdout_to_the_record(self, repo, monkeypatch):
        """`--json --wait | jq` must work: the progress lines belong to stderr."""
        _mock_api(monkeypatch, _sequential_handler({}))

        result = runner.invoke(
            app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--json"]
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)  # the whole of stdout is one JSON document
        assert payload["run"]["status"] == "completed"
        assert "cases done" in result.stderr
        assert "started" in result.stderr

    def test_json_output(self, repo, monkeypatch):
        _mock_api(monkeypatch, _sequential_handler({}))

        result = runner.invoke(
            app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--json"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.output[result.output.index("{") :])
        assert payload["run"]["status"] == "completed"
        assert len(payload["results"]) == 2

    def test_label_defaults_to_git_branch(self, repo, monkeypatch):
        seen = {}
        _mock_api(monkeypatch, _sequential_handler(seen))
        monkeypatch.setattr("cassis_cli.eval._git_branch", lambda _path: "feat/my-branch")

        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert seen["start_body"]["label"] == "feat/my-branch"

    def test_missing_api_key_exits_two(self, repo, monkeypatch):
        monkeypatch.delenv("CASSIS_API_KEY", raising=False)
        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID])
        assert result.exit_code == 2

    def test_non_uuid_project_exits_two(self, repo):
        result = runner.invoke(app, ["eval", "run", str(repo), "--project", "not-a-uuid", "--api-key", "sk-k6-test"])
        assert result.exit_code == 2


class TestEvalApiClient:
    def _call(self, handler, fn, **kwargs):
        return fn(
            api_url="https://api.example.com",
            api_key="sk-k6-test",
            project_id=PROJECT_ID,
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    def test_start_401_raises_auth_error(self):
        from cassis_cli.api import AuthError

        with pytest.raises(AuthError):
            self._call(lambda r: httpx.Response(401), post_eval_run_start, files={"a.yml": "x: 1"})

    def test_start_400_raises_validation_error_with_detail(self):
        detail = {"message": "invalid", "findings": []}
        with pytest.raises(EvalStartValidationError) as exc_info:
            self._call(
                lambda r: httpx.Response(400, json={"detail": detail}),
                post_eval_run_start,
                files={"a.yml": "x: 1"},
            )
        assert exc_info.value.detail == detail

    def test_start_409_raises_active_error(self):
        with pytest.raises(EvalRunActiveError):
            self._call(
                lambda r: httpx.Response(409, json={"detail": "active"}),
                post_eval_run_start,
                files={"a.yml": "x: 1"},
            )

    def test_get_run_unexpected_shape_raises(self):
        with pytest.raises(ApiError):
            self._call(lambda r: httpx.Response(200, json={"nope": 1}), get_eval_run, run_id=RUN_ID)

    def test_get_results_non_list_raises(self):
        with pytest.raises(ApiError):
            self._call(lambda r: httpx.Response(200, json={}), get_eval_run_results, run_id=RUN_ID)

    def test_cancel_204_is_ok(self):
        self._call(lambda r: httpx.Response(204), post_eval_run_cancel, run_id=RUN_ID)

    def test_url_join_without_double_slash(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(201, json=_run_body())

        fn = post_eval_run_start
        fn(
            api_url="https://api.example.com/",
            api_key="sk-k6-test",
            project_id=PROJECT_ID,
            files={"a.yml": "x: 1"},
            transport=httpx.MockTransport(handler),
        )
        assert seen["url"] == f"https://api.example.com/api/ci/projects/{PROJECT_ID}/eval/runs"


def _failing_handler(result_payload):
    """start → completed, with one failing case carrying `result_payload`."""

    def handler(request):
        url = str(request.url)
        if request.method == "POST" and url.endswith("/eval/runs"):
            return httpx.Response(201, json=_run_body(total=1))
        if url.endswith(f"/eval/runs/{RUN_ID}"):
            return httpx.Response(
                200,
                json=_run_body(status="completed", total=1, summary={"total": 1, "passed": 0, "accuracy": 0.0}),
            )
        if url.endswith("/results"):
            return httpx.Response(200, json=[result_payload])
        raise AssertionError(f"unexpected request: {request.method} {url}")

    return handler


def _invoke_failing(repo, monkeypatch, result_payload):
    _mock_api(monkeypatch, _failing_handler(result_payload))
    return runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])


class TestFailureEvidence:
    """A failed case has to be diagnosable from this output alone (no webapp)."""

    def test_missing_concept_prints_its_concepts(self, repo, monkeypatch):
        # This status sets no `error`, so printing only `error` showed nothing.
        payload = {
            "question": "Revenue by cohort?",
            "status": "missing_concept",
            "generated_sql": None,
            "duration_seconds": 4.0,
            "error": "Concepts missing from the ontology: cohort",
            "missing_concepts": [{"name": "cohort", "kind": "dimension"}],
        }
        result = _invoke_failing(repo, monkeypatch, payload)
        assert result.exit_code == 1, result.output
        assert "Missing concepts: cohort" in result.output

    def test_judge_rejection_prints_its_reasoning(self, repo, monkeypatch):
        # Also sets no `error` of its own on older servers.
        payload = {
            "question": "Revenue last month?",
            "status": "failed",
            "generated_sql": "SELECT sum(net) FROM orders",
            "duration_seconds": 9.0,
            "error": None,
            "judge_verdict": "not_equivalent",
            "judge_reasoning": "The generated SQL sums net revenue; the gold SQL sums gross.",
        }
        result = _invoke_failing(repo, monkeypatch, payload)
        assert result.exit_code == 1, result.output
        assert "Judge verdict: not_equivalent" in result.output
        assert "sums net revenue" in result.output

    def test_data_mismatch_prints_counts_and_both_sql(self, repo, monkeypatch):
        payload = {
            "question": "Revenue last month?",
            "status": "failed",
            "generated_sql": "SELECT sum(amount) FROM orders",
            "gold_sql": "SELECT sum(amount) FROM orders WHERE paid",
            "duration_seconds": 7.0,
            "error": "Data mismatch: 1 of 1 compared row positions differ",
            "expected_row_count": 1,
            "actual_row_count": 1,
        }
        result = _invoke_failing(repo, monkeypatch, payload)
        assert result.exit_code == 1, result.output
        assert "Rows: 1 actual vs 1 expected" in result.output
        assert "Generated SQL:" in result.output
        assert "SELECT sum(amount) FROM orders" in result.output
        assert "Gold SQL:" in result.output
        assert "WHERE paid" in result.output

    def test_row_values_are_never_printed(self, repo, monkeypatch):
        """Values ride in --json, not the CI log — see `_print_failure_evidence`."""
        payload = {
            "question": "Revenue last month?",
            "status": "failed",
            "generated_sql": "SELECT sum(amount) FROM orders",
            "duration_seconds": 7.0,
            "error": "Data mismatch: 1 of 1 compared row positions differ",
            "expected_row_count": 1,
            "actual_row_count": 1,
            "expected_output": [{"revenue": "8675309"}],
            "actual_output": [{"revenue": "1234567"}],
        }
        result = _invoke_failing(repo, monkeypatch, payload)
        assert "8675309" not in result.output
        assert "1234567" not in result.output

    def test_json_output_carries_the_row_values(self, repo, monkeypatch):
        payload = {
            "question": "Revenue last month?",
            "status": "failed",
            "generated_sql": "SELECT sum(amount) FROM orders",
            "duration_seconds": 7.0,
            "error": "Data mismatch: 1 of 1 compared row positions differ",
            "expected_output": [{"revenue": "8675309"}],
            "actual_output": [{"revenue": "1234567"}],
        }
        _mock_api(monkeypatch, _failing_handler(payload))
        result = runner.invoke(
            app,
            ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--json"],
        )
        assert result.exit_code == 1, result.output
        body = json.loads(result.stdout)
        assert body["results"][0]["expected_output"] == [{"revenue": "8675309"}]

    def test_long_error_says_it_was_truncated(self, repo, monkeypatch):
        payload = {
            "question": "Revenue last month?",
            "status": "gold_sql_error",
            "generated_sql": None,
            "duration_seconds": 1.0,
            "error": "Gold SQL execution failed: " + ("x" * 400),
        }
        result = _invoke_failing(repo, monkeypatch, payload)
        assert "truncated" in result.output

    def test_live_case_gold_sql_is_labelled(self, repo, monkeypatch):
        payload = {
            "question": "Revenue last month?",
            "status": "failed",
            "generated_sql": "SELECT 1",
            "gold_sql": "SELECT 2",
            "duration_seconds": 1.0,
            "error": "Data mismatch: 1 of 1 compared row positions differ",
            "gold_sql_from_live_case": True,
        }
        result = _invoke_failing(repo, monkeypatch, payload)
        assert "predates snapshotting" in result.output

    def test_older_server_without_the_new_fields_still_prints_the_error(self, repo, monkeypatch):
        payload = {
            "question": "Revenue last month?",
            "status": "failed",
            "generated_sql": "SELECT 1",
            "duration_seconds": 1.0,
            "error": "Data mismatch",
        }
        result = _invoke_failing(repo, monkeypatch, payload)
        assert result.exit_code == 1, result.output
        assert "Data mismatch" in result.output

    def test_passing_case_prints_no_evidence_block(self, repo, monkeypatch):
        payload = {
            "question": "Revenue last month?",
            "status": "passed",
            "generated_sql": "SELECT sum(amount) FROM orders",
            "gold_sql": "SELECT sum(amount) FROM orders",
            "duration_seconds": 3.0,
            "error": None,
        }
        _mock_api(monkeypatch, _failing_handler(payload))
        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])
        assert "Generated SQL:" not in result.output


class TestMissingBranch:
    def test_names_the_branch_requirement_and_points_at_label(self, repo, monkeypatch):
        def handler(request):
            return httpx.Response(404, json={"detail": "Branch 'feat/x' not found"})

        _mock_api(monkeypatch, handler)
        result = runner.invoke(
            app,
            [
                "eval",
                "run",
                str(repo),
                "--project",
                PROJECT_ID,
                "--api-key",
                "sk-k6-test",
                "--branch",
                "feat/x",
            ],
        )
        # Usage error, not transport: the key and project are fine.
        assert result.exit_code == 2, result.output
        assert "Branch 'feat/x' not found" in result.output
        assert "already exists in Cassis" in result.output
        assert "--label" in result.output
        # The old copy sent people to check their key instead.
        assert "API key belongs to the project" not in result.output

    def test_a_genuine_scope_404_keeps_the_generic_hint(self):
        def handler(request):
            return httpx.Response(404, json={"detail": "Project not found"})

        with pytest.raises(ApiError) as exc:
            post_eval_run_start(
                api_url="https://api.example.com",
                api_key="sk-k6-test",
                project_id=PROJECT_ID,
                files={"_project.yml": "display_name: T\n"},
                branch=None,
                label=None,
                case_ids=None,
                transport=httpx.MockTransport(handler),
            )
        assert not isinstance(exc.value, EvalBranchNotFoundError)
        assert "Check --project" in str(exc.value)


class TestSqlPrintingIsBounded:
    """A line cap alone bounds nothing when the SQL is one very long line."""

    def test_a_single_enormous_line_is_truncated(self, repo, monkeypatch):
        payload = {
            "question": "Revenue last month?",
            "status": "failed",
            "generated_sql": "SELECT " + ("x" * 40000),
            "duration_seconds": 1.0,
            "error": "Data mismatch: 1 of 1 compared row positions differ",
        }
        result = _invoke_failing(repo, monkeypatch, payload)
        assert "truncated" in result.output
        # Comfortably under the raw 40k, with room for the rest of the report.
        assert len(result.output) < 10000

    def test_normal_sql_still_prints_in_full(self, repo, monkeypatch):
        sql = "SELECT sum(amount)\nFROM orders\nWHERE paid"
        payload = {
            "question": "Revenue last month?",
            "status": "failed",
            "generated_sql": sql,
            "duration_seconds": 1.0,
            "error": "Data mismatch: 1 of 1 compared row positions differ",
        }
        result = _invoke_failing(repo, monkeypatch, payload)
        assert "WHERE paid" in result.output
        assert "truncated" not in result.output
