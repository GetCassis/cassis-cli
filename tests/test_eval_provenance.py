"""`cassis eval run` provenance and the spend-window warning.

The command sends the commit the files come from and, in CI, the pipeline
and the merge or pull request, so the Evals page says where a run came from;
it warns, without blocking, when the project's automatic runs are paused by
the spending protection, and stays quiet on a server without the window.
"""

import json

import httpx
import pytest
from cassis_cli.api import (
    EvalSpendUnsupportedError,
    get_eval_run,
    get_eval_run_results,
    get_eval_spend,
    post_eval_run_start,
)
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
RUN_ID = "019f0000-0000-7000-8000-00000000aaaa"
_CI_VARS = (
    "GITHUB_SHA",
    "GITHUB_RUN_ID",
    "GITHUB_REF",
    "GITHUB_REPOSITORY",
    "GITHUB_SERVER_URL",
    "CI_COMMIT_SHA",
    "CI_PIPELINE_ID",
    "CI_PIPELINE_URL",
    "CI_MERGE_REQUEST_IID",
    "CI_PROJECT_URL",
    "CI_MERGE_REQUEST_PROJECT_URL",
    "BITBUCKET_COMMIT",
    "BITBUCKET_PIPELINE_UUID",
    "BITBUCKET_PR_ID",
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout with a context tree and no CI environment, outside any git repository."""
    ontology_dir = tmp_path / "cassis"
    (ontology_dir / "tables" / "public").mkdir(parents=True)
    (ontology_dir / "_project.yml").write_text("display_name: Test\n")
    (ontology_dir / "tables" / "public" / "orders.yml").write_text("schema_name: public\ntable_name: orders\n")
    for var in _CI_VARS:
        monkeypatch.delenv(var, raising=False)
    # No git in the checkout: the commit comes from the CI variables alone.
    monkeypatch.setattr("cassis_cli.eval.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    return tmp_path


def _run_body(status="running"):
    return {
        "run_id": RUN_ID,
        "status": status,
        "total_cases": 1,
        "ontology_label": "my-branch",
        "summary": {"total": 1, "passed": 1, "accuracy": 1.0} if status == "completed" else None,
        "started_at": "2026-11-03T10:00:00Z",
        "completed_at": None,
    }


def _mock_api(monkeypatch, handler, *, spend=None):
    transport = httpx.MockTransport(handler)
    for name, fn in (
        ("post_eval_run_start", post_eval_run_start),
        ("get_eval_run", get_eval_run),
        ("get_eval_run_results", get_eval_run_results),
        ("get_eval_spend", get_eval_spend),
    ):

        def patched(_fn=fn, **kwargs):
            kwargs["transport"] = transport
            return _fn(**kwargs)

        monkeypatch.setattr(f"cassis_cli.eval.{name}", patched)
    monkeypatch.setattr("cassis_cli.common.time.sleep", lambda _s: None)


def _handler(seen, *, spend_response):
    def handler(request):
        url = str(request.url)
        if url.endswith("/eval/spend"):
            return spend_response
        if request.method == "POST" and url.endswith("/eval/runs"):
            seen["start_body"] = json.loads(request.content)
            return httpx.Response(201, json=_run_body())
        if url.endswith(f"/eval/runs/{RUN_ID}"):
            return httpx.Response(200, json=_run_body("completed"))
        if url.endswith("/results"):
            return httpx.Response(200, json=[{"question": "q", "status": "passed", "generated_sql": "SELECT 1"}])
        raise AssertionError(f"unexpected request: {request.method} {url}")

    return handler


_NO_WINDOW = httpx.Response(404, json={"detail": "Not Found"})
_ARGS = ["eval", "run", "--project", PROJECT_ID, "--api-key", "sk-k6-test"]


class TestProvenance:
    def test_outside_ci_sends_nothing_without_a_commit(self, repo, monkeypatch):
        seen = {}
        _mock_api(monkeypatch, _handler(seen, spend_response=_NO_WINDOW))

        result = runner.invoke(app, ["eval", "run", str(repo), *_ARGS[2:]])

        assert result.exit_code == 0, result.output
        assert not {"git_commit", "ci_pipeline", "ci_merge_request"} & seen["start_body"].keys()

    def test_github_actions_pull_request(self, repo, monkeypatch):
        seen = {}
        _mock_api(monkeypatch, _handler(seen, spend_response=_NO_WINDOW))
        monkeypatch.setenv("GITHUB_SHA", "abc123")
        monkeypatch.setenv("GITHUB_RUN_ID", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/data")
        monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
        monkeypatch.setenv("GITHUB_REF", "refs/pull/7/merge")

        result = runner.invoke(app, ["eval", "run", str(repo), *_ARGS[2:]])

        assert result.exit_code == 0, result.output
        body = seen["start_body"]
        assert body["git_commit"] == "abc123"
        assert body["ci_pipeline"] == "https://github.com/acme/data/actions/runs/42"
        assert body["ci_merge_request"] == "https://github.com/acme/data/pull/7"

    def test_gitlab_merge_request_pipeline(self, repo, monkeypatch):
        seen = {}
        _mock_api(monkeypatch, _handler(seen, spend_response=_NO_WINDOW))
        monkeypatch.setenv("CI_COMMIT_SHA", "def456")
        monkeypatch.setenv("CI_PIPELINE_ID", "99")
        monkeypatch.setenv("CI_PIPELINE_URL", "https://gitlab.com/acme/data/-/pipelines/99")
        monkeypatch.setenv("CI_MERGE_REQUEST_IID", "12")
        monkeypatch.setenv("CI_PROJECT_URL", "https://gitlab.com/acme/data")

        result = runner.invoke(app, ["eval", "run", str(repo), *_ARGS[2:]])

        assert result.exit_code == 0, result.output
        body = seen["start_body"]
        assert body["git_commit"] == "def456"
        assert body["ci_pipeline"] == "https://gitlab.com/acme/data/-/pipelines/99"
        assert body["ci_merge_request"] == "https://gitlab.com/acme/data/-/merge_requests/12"

    def test_branch_runs_send_no_commit(self, repo, monkeypatch):
        seen = {}
        _mock_api(monkeypatch, _handler(seen, spend_response=_NO_WINDOW))
        monkeypatch.setenv("GITHUB_SHA", "abc123")

        result = runner.invoke(app, ["eval", "run", str(repo), *_ARGS[2:], "--branch", "feat/x"])

        assert result.exit_code == 0, result.output
        assert "git_commit" not in seen["start_body"]


class TestSpendWarning:
    def test_warns_when_the_window_is_spent_and_runs_anyway(self, repo, monkeypatch):
        seen = {}
        spent = httpx.Response(
            200,
            json={
                "window_days": 7,
                "limit_cents": 10_000,
                "spent_cents": 10_000,
                "remaining_cents": 0,
                "billing": "platform",
                "automatic_runs_enabled": True,
                "custom_limit": False,
            },
        )
        _mock_api(monkeypatch, _handler(seen, spend_response=spent))

        result = runner.invoke(app, ["eval", "run", str(repo), *_ARGS[2:]])

        assert result.exit_code == 0, result.output
        assert "Automatic eval runs on this project are paused" in result.output
        assert "$100.00 limit is spent" in result.output
        assert "start_body" in seen

    def test_a_dry_run_or_a_usage_error_never_reads_the_window(self, repo, monkeypatch):
        def handler(request):
            url = str(request.url)
            assert not url.endswith("/eval/spend"), "the window is read only once a run has started"
            if url.endswith("/eval/selection"):
                return httpx.Response(200, json={"mode": "changed", "cases": []})
            return httpx.Response(404, json={"detail": "Base context version not found"})

        _mock_api(monkeypatch, handler)

        dry = runner.invoke(app, ["eval", "run", str(repo), *_ARGS[2:], "--changed", "--dry-run"])
        unknown_base = runner.invoke(
            app, ["eval", "run", str(repo), *_ARGS[2:], "--changed", "--base", "019f0000-0000-7000-8000-0000000000ff"]
        )

        assert dry.exit_code == 0, dry.output
        assert unknown_base.exit_code == 2, unknown_base.output
        assert "paused" not in dry.output + unknown_base.output

    def test_quiet_with_tokens_left_or_on_an_older_server(self, repo, monkeypatch):
        left = httpx.Response(
            200,
            json={
                "window_days": 7,
                "limit_cents": 10,
                "spent_cents": 1,
                "remaining_cents": 9,
                "billing": "byok",
                "automatic_runs_enabled": False,
                "custom_limit": True,
            },
        )
        for response in (left, _NO_WINDOW):
            _mock_api(monkeypatch, _handler({}, spend_response=response))
            result = runner.invoke(app, ["eval", "run", str(repo), *_ARGS[2:]])
            assert result.exit_code == 0, result.output
            assert "paused" not in result.output

    def test_older_server_raises_the_typed_error(self):
        transport = httpx.MockTransport(lambda request: _NO_WINDOW)
        with pytest.raises(EvalSpendUnsupportedError):
            get_eval_spend(api_url="https://x", api_key="k", project_id=PROJECT_ID, transport=transport)
