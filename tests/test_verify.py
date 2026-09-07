import json

import httpx
import pytest
from cassis_cli.api import (
    get_eval_run,
    get_eval_run_results,
    post_eval_run_start,
    post_ontology_check,
    post_ontology_fmt,
)
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

_PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
_RUN_ID = "019f0000-0000-7000-8000-00000000c0de"
_CANONICAL = "schema_name: public\ntable_name: orders\n"


@pytest.fixture
def repo(tmp_path):
    """A canonical checkout bound to a project, with a current AGENTS.md."""
    from cassis_cli.guide import refresh_guide

    ontology_dir = tmp_path / "cassis"
    (ontology_dir / "tables" / "public").mkdir(parents=True)
    (ontology_dir / "project.yml").write_text(f"cassis_format_version: 2\nproject_id: {_PROJECT_ID}\n")
    (ontology_dir / "tables" / "public" / "orders.yml").write_text(_CANONICAL)
    refresh_guide(ontology_dir)
    return tmp_path


def _canonical_fmt_handler(request):
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={"ok": True, "files": dict(body["files"]), "changed_paths": [], "removed_paths": [], "findings": []},
    )


def _passing_check_handler(request):
    return httpx.Response(
        200,
        json={
            "passed": True,
            "file_count": 2,
            "title": "Ontology valid",
            "summary": "All good",
            "findings": [],
            "warnings": [],
            "references_checked": True,
        },
    )


def _eval_handler(result_status):
    def handler(request):
        url = str(request.url)
        if url.endswith("/eval/runs") and request.method == "POST":
            return httpx.Response(201, json={"run_id": _RUN_ID, "status": "running", "total_cases": 1})
        if url.endswith(f"/eval/runs/{_RUN_ID}"):
            return httpx.Response(
                200, json={"run_id": _RUN_ID, "status": "completed", "total_cases": 1, "summary": None}
            )
        if url.endswith("/results"):
            return httpx.Response(200, json=[{"question": "q", "status": result_status}])
        raise AssertionError(f"unexpected URL {url}")

    return handler


def _mock_all(monkeypatch, *, fmt=None, check=None, eval_handler=None):
    """Route each module's API function through a MockTransport handler."""

    def patch(module_path, original, handler):
        def patched(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(**kwargs)

        monkeypatch.setattr(module_path, patched)

    if fmt is not None:
        patch("cassis_cli.ontology.post_ontology_fmt", post_ontology_fmt, fmt)
    if check is not None:
        patch("cassis_cli.ontology.post_ontology_check", post_ontology_check, check)
    if eval_handler is not None:
        patch("cassis_cli.eval.post_eval_run_start", post_eval_run_start, eval_handler)
        patch("cassis_cli.eval.get_eval_run", get_eval_run, eval_handler)
        patch("cassis_cli.eval.get_eval_run_results", get_eval_run_results, eval_handler)
        monkeypatch.setattr("cassis_cli.common.time.sleep", lambda seconds: None)


class TestVerifyCommand:
    def test_all_gates_pass(self, repo, monkeypatch):
        _mock_all(
            monkeypatch,
            fmt=_canonical_fmt_handler,
            check=_passing_check_handler,
            eval_handler=_eval_handler("passed"),
        )

        result = runner.invoke(app, ["verify", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "==> cassis ontology fmt --check" in result.output
        assert "==> cassis ontology check" in result.output
        assert "==> cassis eval run" in result.output
        assert "verify passed" in result.output

    def test_non_canonical_tree_fails_fast(self, repo, monkeypatch):
        def fmt_handler(request):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "files": dict(body["files"]),
                    "changed_paths": ["tables/public/orders.yml"],
                    "removed_paths": [],
                    "findings": [],
                },
            )

        called = {"check": False}

        def check_handler(request):
            called["check"] = True
            return _passing_check_handler(request)

        _mock_all(monkeypatch, fmt=fmt_handler, check=check_handler)

        result = runner.invoke(app, ["verify", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 1
        assert "run `cassis ontology fmt`" in result.output
        assert called["check"] is False  # stopped at the first gate

    def test_failing_check_stops_before_eval(self, repo, monkeypatch):
        def failing_check(request):
            return httpx.Response(
                200,
                json={
                    "passed": False,
                    "file_count": 2,
                    "title": "Ontology invalid",
                    "summary": "1 finding",
                    "findings": [{"stage": "import", "path": None, "message": "dangling ref"}],
                    "warnings": [],
                    "references_checked": False,
                },
            )

        called = {"eval": False}

        def eval_handler(request):
            called["eval"] = True
            raise AssertionError("eval must not run")

        _mock_all(monkeypatch, fmt=_canonical_fmt_handler, check=failing_check, eval_handler=eval_handler)

        result = runner.invoke(app, ["verify", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 1
        assert called["eval"] is False

    def test_failing_eval_case_fails_verify(self, repo, monkeypatch):
        _mock_all(
            monkeypatch,
            fmt=_canonical_fmt_handler,
            check=_passing_check_handler,
            eval_handler=_eval_handler("failed"),
        )

        result = runner.invoke(app, ["verify", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 1

    def test_no_eval_skips_the_suite(self, repo, monkeypatch):
        _mock_all(monkeypatch, fmt=_canonical_fmt_handler, check=_passing_check_handler)

        result = runner.invoke(app, ["verify", str(repo), "--api-key", "sk-k6-test", "--no-eval"])

        assert result.exit_code == 0
        assert "eval skipped" in result.output

    def test_missing_api_key_exits_two(self, repo):
        result = runner.invoke(app, ["verify", str(repo)])
        assert result.exit_code == 2
        assert "No API key" in result.output
