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


@pytest.mark.parametrize("preview,route", [(True, "selection"), (False, "runs")])
def test_scope_preview_and_run_use_their_supported_routes(preview, route):
    def handler(request):
        assert request.url.path.endswith("/eval/" + route)
        body = json.loads(request.content)
        assert body["scope"] == "changed"
        assert "token_budget" not in body
        return httpx.Response(200, json={"mode": "changed", "cases": []} if preview else _run_body(total=0))

    post_eval_run_start(
        api_url="https://example.com",
        api_key="key",
        project_id=PROJECT_ID,
        changed=True,
        preview=preview,
        transport=httpx.MockTransport(handler),
    )


def test_old_server_reports_unsupported_selection_preview():
    with pytest.raises(ApiError):
        post_eval_run_start(
            api_url="https://example.com",
            api_key="key",
            project_id=PROJECT_ID,
            preview=True,
            transport=httpx.MockTransport(lambda request: httpx.Response(404, json={"detail": "Not Found"})),
        )


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


class TestVerdictExitCodes:
    """CI tells a regression (1) from a run that reached no verdict (4)."""

    def test_an_execution_error_alone_exits_four(self, repo, monkeypatch):
        result = _invoke_failing(repo, monkeypatch, {"question": "Q?", "status": "error", "error": "Warehouse down"})
        assert result.exit_code == 4, result.output
        assert "No verdict" in result.output

    def test_a_failed_answer_exits_one(self, repo, monkeypatch):
        result = _invoke_failing(repo, monkeypatch, {"question": "Q?", "status": "failed", "error": "Wrong total"})
        assert result.exit_code == 1, result.output

    @pytest.mark.parametrize(
        "verdict,code",
        [
            ({"regressions": ["c1"], "already_failing": [], "pending": []}, 1),
            ({"regressions": [], "already_failing": ["c1"], "pending": [], "base_version_number": 3}, 0),
            ({"regressions": [], "already_failing": [], "pending": ["c1"]}, 0),
        ],
    )
    def test_only_a_regression_against_the_base_fails(self, repo, monkeypatch, verdict, code):
        def handler(request):
            url = str(request.url)
            if request.method == "POST" and url.endswith("/eval/runs"):
                return httpx.Response(201, json=_run_body(total=1))
            if url.endswith(f"/eval/runs/{RUN_ID}"):
                body = _run_body(status="completed", total=1, summary={"total": 1, "passed": 0, "accuracy": 0.0})
                return httpx.Response(200, json={**body, "verdict": verdict})
            if url.endswith("/results"):
                return httpx.Response(200, json=[{"test_case_id": "c1", "question": "Q?", "status": "failed"}])
            raise AssertionError(url)

        _mock_api(monkeypatch, handler)
        result = runner.invoke(app, ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])
        assert result.exit_code == code, result.output
        if verdict.get("already_failing"):
            assert "Already failing on v3" in result.output


class TestUncoveredChanges:
    def test_a_selection_names_what_no_case_reads_and_warns_when_nothing_runs(self, capsys):
        from cassis_cli.eval import _print_selection

        _print_selection(
            {
                "mode": "changed",
                "base_version_number": 4,
                "cases": [{"case_id": "c1", "question": "Q?", "selected": False, "reason": "No changed dependency"}],
                "uncovered": {"tables": ["s.invoices"], "domains": ["billing"]},
            }
        )
        out = capsys.readouterr().out
        assert "No case covers this change" in out
        assert "Read by no case: s.invoices" in out
        assert "New domain with no case: billing" in out
        assert "--save-case" in out


class TestFailureEvidence:
    """A failed case has to be diagnosable from this output alone (no webapp)."""

    def test_expectation_and_path_are_printed(self, repo, monkeypatch):
        payload = {
            "question": "Net revenue last month?",
            "status": "failed",
            "generated_sql": None,
            "duration_seconds": 4.0,
            "error": "Asked a clarification instead of saying the question cannot be answered: Which currency?",
            "expectation": {"kind": "cannot_answer"},
            "path_evidence": {
                "domains_read": ["sales"],
                "tables_read": ["public.orders", "public.refunds"],
                "retrieval": {"recall": 0.5, "precision": 1.0, "reason": None, "hit": ["public.orders"]},
                "failed_sql_attempts": 1,
                "query_attempts": 1,
                "clarifications": {
                    "count": 1,
                    "questions": [
                        {
                            "question": "Which currency?",
                            "options": ["EUR", "USD"],
                            "answer": "No answer is available for this question.",
                            "context_gap": False,
                            "answered": False,
                        },
                        {
                            "question": "Which refund table?",
                            "options": [],
                            "answer": None,
                            "context_gap": True,
                            "answered": None,
                        },
                    ],
                    "answered": False,
                },
                "context_gaps": [],
                "turns": 3,
            },
        }
        result = _invoke_failing(repo, monkeypatch, payload)
        assert result.exit_code == 1, result.output
        assert "Expected: the agent to say the question cannot be answered" in result.output
        assert "Path: 3 turns · read 2 tables (gold recall 50%, precision 100%) · 1 failed SQL attempt" in result.output
        assert "Asked: Which currency?\n" in result.output
        assert "Asked: Which refund table?" in result.output

    def test_an_older_server_without_a_path_prints_none(self, repo, monkeypatch):
        payload = {"question": "Q?", "status": "failed", "generated_sql": "SELECT 1", "error": "x"}
        result = _invoke_failing(repo, monkeypatch, payload)
        assert result.exit_code == 1, result.output
        assert "Path:" not in result.output and "Expected:" not in result.output

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
        """Values ride in --json, not the CI log — see `_print_case_evidence`."""
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

    def test_passing_case_prints_evidence_when_cases_are_named(self, repo, monkeypatch):
        # A `--case` run is checking that a context change fixes that case:
        # the SQL behind a pass is the point, not just the verdict.
        payload = {
            "question": "Revenue last month?",
            "status": "passed",
            "generated_sql": "SELECT sum(amount) FROM orders WHERE paid",
            "gold_sql": "SELECT sum(amount) FROM orders WHERE paid",
            "duration_seconds": 3.0,
            "error": None,
            "expected_row_count": 1,
            "actual_row_count": 1,
            "actual_output": [{"revenue": "8675309"}],
        }
        _mock_api(monkeypatch, _failing_handler(payload))
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
                "019f0000-0000-7000-8000-0000000000aa",
            ],
        )
        assert "Generated SQL:" in result.output
        assert "Gold SQL:" in result.output
        assert "Rows: 1 actual vs 1 expected" in result.output
        assert "8675309" not in result.output


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


def test_unknown_automatic_cost_explains_manual_baseline(capsys):
    from cassis_cli.eval import _print_summary

    _print_summary({"status": "skipped", "summary": {"skipped": {"reason": "baseline_required"}}})
    assert "Run evals manually first to establish a cost estimate" in capsys.readouterr().out


def test_unknown_catalog_does_not_claim_context_is_missing():
    from cassis_cli.eval import _footprint_note

    note = _footprint_note({"footprint": {"state": "partial", "unverified_catalogs": ["warehouse.s.orders"]}})
    assert "catalog identity unverified" in note
    assert "not in the context" not in note


VERSION_ID = "019f0000-0000-7000-8000-0000000000ff"
CASE_ID = "019f0000-0000-7000-8000-0000000000ca"


def _quiet_spend(monkeypatch):
    def no_window(**_kwargs):
        raise ApiError("no window")

    monkeypatch.setattr("cassis_cli.eval.get_eval_spend", no_window)


def _run_args(repo, *extra):
    return ["eval", "run", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", *extra]


class TestSelectionErrors:
    def test_dry_run_on_a_server_without_previews_says_it_is_too_old(self, repo, monkeypatch):
        _quiet_spend(monkeypatch)
        _mock_api(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Not Found"}))

        result = runner.invoke(app, _run_args(repo, "--changed", "--dry-run"))

        assert result.exit_code == 3
        assert "cannot preview a selection (--dry-run)" in result.output
        assert "API key" not in result.output

    def test_an_unknown_base_is_a_usage_error_naming_it(self, repo, monkeypatch):
        _quiet_spend(monkeypatch)
        _mock_api(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Base context version not found"}))

        result = runner.invoke(app, _run_args(repo, "--changed", "--base", VERSION_ID))

        assert result.exit_code == 2, result.output
        assert f"No published context version {VERSION_ID}" in result.output
        assert "API key" not in result.output

    def test_base_without_changed_is_a_usage_error(self, repo, monkeypatch):
        _mock_api(monkeypatch, lambda request: pytest.fail("no request expected"))

        result = runner.invoke(app, _run_args(repo, "--base", VERSION_ID))

        assert result.exit_code == 2
        assert "--base needs --changed: the base is what the change selection compares the files against." in (
            " ".join(result.output.split())
        )

    def test_dry_run_with_only_unknown_cases_exits_two(self, repo, monkeypatch):
        _quiet_spend(monkeypatch)
        other = "019f0000-0000-7000-8000-0000000000cb"
        preview = {"mode": "full", "cases": [{"case_id": other, "selected": False, "reason": "Outside"}]}
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=preview))

        result = runner.invoke(app, _run_args(repo, "--dry-run", "--case", CASE_ID))

        assert result.exit_code == 2
        assert f"No current eval case matches --case: {CASE_ID}" in result.output

    def test_dry_run_with_a_known_case_previews(self, repo, monkeypatch):
        _quiet_spend(monkeypatch)
        preview = {"mode": "full", "cases": [{"case_id": CASE_ID, "selected": True, "reason": "Requested"}]}
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=preview))

        result = runner.invoke(app, _run_args(repo, "--dry-run", "--case", CASE_ID))

        assert result.exit_code == 0, result.output
        assert "Selection: 1/1 cases" in result.output


class TestSelectionPreview:
    _PREVIEW = {
        "mode": "changed",
        "base_version_id": VERSION_ID,
        "base_version_number": 15,
        "base_published_at": "2026-11-04T09:30:00Z",
        "shadow": False,
        "cases": [
            {"case_id": CASE_ID, "question": "Revenue?", "selected": True, "reason": "Changed dependency: s.orders"},
            *(
                {"case_id": f"c{i}", "question": f"Q{i}?", "selected": False, "reason": "No changed dependency"}
                for i in range(3)
            ),
            {"case_id": "c9", "question": "Q9?", "selected": False, "reason": "Outside the requested cases"},
        ],
    }

    def _invoke(self, repo, monkeypatch, *extra):
        _quiet_spend(monkeypatch)
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=self._PREVIEW))
        return runner.invoke(app, _run_args(repo, "--changed", "--dry-run", *extra))

    def test_names_its_base_and_counts_skips_per_reason(self, repo, monkeypatch):
        result = self._invoke(repo, monkeypatch)

        assert result.exit_code == 0, result.output
        assert "Selection: 1/5 cases affected since v15 (published 2026-11-04)" in result.output
        assert f"  Run {CASE_ID}: Revenue? — Changed dependency: s.orders" in result.output
        assert "  3 skipped: No changed dependency" in result.output
        assert "  1 skipped: Outside the requested cases" in result.output
        assert "Skip c0" not in result.output

    def test_an_older_server_names_the_base_by_id(self, repo, monkeypatch):
        preview = {k: v for k, v in self._PREVIEW.items() if k not in ("base_version_number", "base_published_at")}
        _quiet_spend(monkeypatch)
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=preview))

        result = runner.invoke(app, _run_args(repo, "--changed", "--dry-run"))

        assert result.exit_code == 0, result.output
        assert f"Selection: 1/5 cases affected since base {VERSION_ID}" in result.output

    def test_verbose_lists_every_skipped_case(self, repo, monkeypatch):
        result = self._invoke(repo, monkeypatch, "--verbose")

        assert result.exit_code == 0, result.output
        assert "  Skip c0: Q0? — No changed dependency" in result.output
        assert "skipped:" not in result.output


class TestScopedSummary:
    def _summary(self, capsys, run):
        from cassis_cli.eval import _print_summary

        _print_summary(run)
        return capsys.readouterr().out

    def test_a_change_scoped_run_says_how_much_of_the_suite_it_covered(self, capsys):
        cases = [{"case_id": str(i), "selected": i < 3} for i in range(15)]
        run = {
            **_run_body(status="completed", total=3, summary={"total": 3, "passed": 3}),
            "selection": {"mode": "changed", "cases": cases},
        }

        assert "3/3 passed · 3 of 15 cases (affected by a context change)" in self._summary(capsys, run)

    def test_without_a_case_list_it_says_affected_cases_only(self, capsys):
        run = {
            **_run_body(status="completed", total=3, summary={"total": 3, "passed": 3}),
            "selection": {"mode": "changed"},
        }

        assert "3/3 passed · 3 of the affected cases only" in self._summary(capsys, run)

    def test_without_a_case_list_or_a_count_it_names_no_number(self, capsys):
        run = {
            **_run_body(status="completed", total=3, summary={"total": 3, "passed": 3}),
            "total_cases": None,
            "selection": {"mode": "changed"},
        }

        summary = self._summary(capsys, run)
        assert "None" not in summary
        assert "the affected cases only" in summary

    def test_a_full_run_prints_no_scope(self, capsys):
        cases = [{"case_id": str(i), "selected": True} for i in range(2)]
        run = {
            **_run_body(status="completed", summary={"total": 2, "passed": 2}),
            "selection": {"mode": "full", "shadow": True, "cases": cases},
        }

        assert self._summary(capsys, run).strip() == "2/2 passed"

    def test_a_repeat_run_counts_attempts_not_cases(self, capsys):
        run = {**_run_body(status="completed", total=25, summary={"total": 75, "passed": 75}), "repeat_count": 3}

        assert "25 cases × 3 attempts · 75/75 attempts passed" in self._summary(capsys, run)


class TestRunTargetLine:
    def _invoke(self, repo, monkeypatch, body, *extra):
        _quiet_spend(monkeypatch)
        _mock_api(monkeypatch, lambda request: httpx.Response(201, json=body))
        return runner.invoke(app, _run_args(repo, "--no-wait", *extra))

    def test_names_the_version_and_model(self, repo, monkeypatch):
        body = {**_run_body(total=1, label="v3"), "ontology_version_number": 3, "model": "claude-sonnet-5-5"}

        result = self._invoke(repo, monkeypatch, body)

        assert result.exit_code == 0, result.output
        assert f"Eval run {RUN_ID} started: 1 case · v3 · claude-sonnet-5-5." in result.output

    def test_a_files_run_names_its_label_and_the_local_files(self, repo, monkeypatch):
        body = {**_run_body(total=3, label="bt-s2b"), "model": "claude-sonnet-5-5"}

        result = self._invoke(repo, monkeypatch, body)

        assert result.exit_code == 0, result.output
        assert (
            f"Eval run {RUN_ID} started: 3 cases · label bt-s2b · unpublished context from local files"
            " · claude-sonnet-5-5." in result.output
        )

    def test_a_branch_run_names_the_branch(self, repo, monkeypatch):
        body = {**_run_body(total=3, label="feat/x"), "model": "claude-sonnet-5-5"}

        result = self._invoke(repo, monkeypatch, body, "--branch", "feat/x")

        assert result.exit_code == 0, result.output
        assert f"Eval run {RUN_ID} started: 3 cases · branch feat/x · claude-sonnet-5-5." in result.output
        assert "local files" not in result.output

    def test_a_null_label_is_unknown_not_guessed(self, repo, monkeypatch):
        result = self._invoke(repo, monkeypatch, _run_body(label=None))

        assert result.exit_code == 0, result.output
        assert "2 cases; context: unknown (server too old to say)." in result.output
        assert "unpublished context" not in result.output


def test_budget_skip_reports_dollars(capsys):
    from cassis_cli.eval import _print_summary

    _print_summary(
        {
            "status": "skipped",
            "skipped": {"reason": "budget", "estimate_cents": 250, "remaining_cents": 100, "limit_cents": 5000},
            "summary": {},
        }
    )
    out = capsys.readouterr().out
    assert "Not started: The automatic-spend window's limit is spent (estimated $2.50, $1.00 of $50.00 left)." in out
    assert "tokens" not in out
