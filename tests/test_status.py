import json

import httpx
import pytest
from cassis_cli.api import get_project_status
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

_PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
_PUBLISHED_SHA = "a" * 40
_LOCAL_HEAD = "b" * 40

_STATUS_BODY = {
    "published_version": {
        "version": 3,
        "label": "release",
        "published_at": "2026-08-01T00:00:00Z",
        "git_commit_sha": _PUBLISHED_SHA,
        "git_pr_number": 12,
    },
    "has_unpublished_changes": False,
    "git_sync": {"provider": "github", "repo": "acme/warehouse", "base_path": "cassis"},
}


@pytest.fixture
def repo(tmp_path):
    """A checkout bound to a project via project.yml."""
    ontology_dir = tmp_path / "cassis"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "project.yml").write_text(f"cassis_format_version: 2\nproject_id: {_PROJECT_ID}\n")
    return tmp_path


def _mock_api(monkeypatch, handler):
    original = get_project_status

    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr("cassis_cli.status.get_project_status", patched)


def _status_handler(request):
    assert request.headers["Authorization"] == "Bearer sk-k6-test"
    assert str(request.url).endswith(f"/api/ci/projects/{_PROJECT_ID}/status")
    return httpx.Response(200, json=_STATUS_BODY)


def _fake_git(monkeypatch, responses):
    """Replace the git shell-outs with a canned (args tuple -> stdout) mapping."""

    def fake(path, *args):
        return responses.get(args)

    monkeypatch.setattr("cassis_cli.status._git", fake)


class TestStatusCommand:
    def test_in_sync_checkout(self, repo, monkeypatch):
        _mock_api(monkeypatch, _status_handler)
        _fake_git(monkeypatch, {("rev-parse", "HEAD"): _PUBLISHED_SHA})

        result = runner.invoke(app, ["status", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "Published: v3 'release'" in result.output
        assert "Unpublished changes: no" in result.output
        assert "Git sync: github acme/warehouse (path cassis)" in result.output
        assert "in sync with the published version" in result.output

    def test_reports_pending_source_changes(self, repo, monkeypatch):
        body = dict(_STATUS_BODY, pending_source_changes={"total": 12, "breaking": 3})
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=body))
        _fake_git(monkeypatch, {("rev-parse", "HEAD"): _PUBLISHED_SHA})

        result = runner.invoke(app, ["status", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "Source changes pending review: 12, 3 breaking (cassis source-changes list)" in result.output

    def test_omits_pending_line_when_server_has_none(self, repo, monkeypatch):
        # Also the old-server shape: no `pending_source_changes` key at all.
        _mock_api(monkeypatch, _status_handler)
        _fake_git(monkeypatch, {("rev-parse", "HEAD"): _PUBLISHED_SHA})

        result = runner.invoke(app, ["status", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "Source changes pending review" not in result.output

    def test_checkout_ahead_of_published_version(self, repo, monkeypatch):
        _mock_api(monkeypatch, _status_handler)
        _fake_git(
            monkeypatch,
            {
                ("rev-parse", "HEAD"): _LOCAL_HEAD,
                ("merge-base", "--is-ancestor", _PUBLISHED_SHA, "HEAD"): "",
                ("rev-list", "--count", f"{_PUBLISHED_SHA}..HEAD"): "2",
            },
        )

        result = runner.invoke(app, ["status", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "2 commit(s) ahead of the published version" in result.output

    def test_checkout_behind_published_version(self, repo, monkeypatch):
        _mock_api(monkeypatch, _status_handler)
        _fake_git(
            monkeypatch,
            {
                ("rev-parse", "HEAD"): _LOCAL_HEAD,
                # HEAD is NOT an ancestor of published (ahead check fails implicitly)
                ("cat-file", "-e", f"{_PUBLISHED_SHA}^{{commit}}"): "",
                ("merge-base", "--is-ancestor", "HEAD", _PUBLISHED_SHA): "",
                ("rev-list", "--count", f"HEAD..{_PUBLISHED_SHA}"): "3",
            },
        )

        result = runner.invoke(app, ["status", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "3 commit(s) behind the published version" in result.output
        assert "run git pull" in result.output

    def test_unpublished_project(self, repo, monkeypatch):
        _mock_api(
            monkeypatch,
            lambda request: httpx.Response(
                200, json={"published_version": None, "has_unpublished_changes": True, "git_sync": None}
            ),
        )
        _fake_git(monkeypatch, {("rev-parse", "HEAD"): _LOCAL_HEAD})

        result = runner.invoke(app, ["status", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "Published: nothing yet" in result.output
        assert "Unpublished changes: yes" in result.output
        assert "Git sync: not configured" in result.output

    def test_json_output_includes_local_state(self, repo, monkeypatch):
        _mock_api(monkeypatch, _status_handler)
        _fake_git(monkeypatch, {("rev-parse", "HEAD"): _PUBLISHED_SHA})

        result = runner.invoke(app, ["status", str(repo), "--api-key", "sk-k6-test", "--json"])

        assert result.exit_code == 0
        body = json.loads(result.output)
        assert body["published_version"]["version"] == 3
        assert body["local"] == {"head": _PUBLISHED_SHA, "in_sync": True}

    def test_watch_exits_zero_once_published_matches_head(self, repo, monkeypatch):
        polls = {"n": 0}

        def handler(request):
            polls["n"] += 1
            sha = _PUBLISHED_SHA if polls["n"] >= 2 else "c" * 40
            body = dict(_STATUS_BODY, published_version=dict(_STATUS_BODY["published_version"], git_commit_sha=sha))
            return httpx.Response(200, json=body)

        _mock_api(monkeypatch, handler)
        _fake_git(monkeypatch, {("rev-parse", "HEAD"): _PUBLISHED_SHA})
        monkeypatch.setattr("cassis_cli.status.time.sleep", lambda seconds: None)

        result = runner.invoke(app, ["status", str(repo), "--api-key", "sk-k6-test", "--watch"])

        assert result.exit_code == 0
        assert polls["n"] == 2
        assert "matches the local HEAD" in result.output

    def test_watch_timeout_exits_three(self, repo, monkeypatch):
        _mock_api(monkeypatch, _status_handler)
        _fake_git(monkeypatch, {("rev-parse", "HEAD"): _LOCAL_HEAD})

        result = runner.invoke(app, ["status", str(repo), "--api-key", "sk-k6-test", "--watch", "--timeout", "0"])

        assert result.exit_code == 3
        assert "Timed out" in result.output

    def test_watch_outside_a_git_checkout_exits_two(self, repo, monkeypatch):
        _mock_api(monkeypatch, _status_handler)
        _fake_git(monkeypatch, {})

        result = runner.invoke(app, ["status", str(repo), "--api-key", "sk-k6-test", "--watch"])

        assert result.exit_code == 2
        assert "needs a git checkout" in result.output

    def test_missing_api_key_exits_two(self, repo):
        result = runner.invoke(app, ["status", str(repo)])
        assert result.exit_code == 2
        assert "No API key" in result.output
