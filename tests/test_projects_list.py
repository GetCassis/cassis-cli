import json

import httpx
from cassis_cli.api import get_projects
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

_PROJECTS_BODY = [
    {
        "id": "019f0000-0000-7000-8000-000000000001",
        "name": "Analytics",
        "published_version": 3,
        "data_source": {"is_executable": True, "sql_dialect": "postgres"},
    },
    {
        "id": "019f0000-0000-7000-8000-000000000002",
        "name": "DDL only",
        "published_version": None,
        "data_source": {"is_executable": False, "sql_dialect": "snowflake"},
    },
    {
        "id": "019f0000-0000-7000-8000-000000000003",
        "name": "Fresh",
        "published_version": None,
        "data_source": None,
    },
]


def _mock_api(monkeypatch, handler):
    original = get_projects

    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr("cassis_cli.projects.get_projects", patched)


def _projects_handler(request):
    assert request.headers["Authorization"] == "Bearer sk-k6-test"
    assert str(request.url).endswith("/api/ci/projects")
    return httpx.Response(200, json=_PROJECTS_BODY)


class TestProjectsListCommand:
    def test_lists_projects_with_version_and_source(self, monkeypatch):
        _mock_api(monkeypatch, _projects_handler)

        result = runner.invoke(app, ["projects", "list", "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "019f0000-0000-7000-8000-000000000001  Analytics  (v3; postgres)" in result.output
        assert "DDL only  (unpublished; snowflake, not executable)" in result.output
        assert "Fresh  (unpublished; no data source)" in result.output

    def test_json_output_prints_raw_response(self, monkeypatch):
        _mock_api(monkeypatch, _projects_handler)

        result = runner.invoke(app, ["projects", "list", "--api-key", "sk-k6-test", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == _PROJECTS_BODY

    def test_empty_list_says_so(self, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=[]))

        result = runner.invoke(app, ["projects", "list", "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "No projects visible" in result.output

    def test_missing_api_key_exits_two(self):
        result = runner.invoke(app, ["projects", "list"])
        assert result.exit_code == 2
        assert "No API key" in result.output

    def test_rejected_key_exits_three(self, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(401, json={"detail": "nope"}))

        result = runner.invoke(app, ["projects", "list", "--api-key", "sk-k6-bad"])

        assert result.exit_code == 3
        assert "rejected the API key" in result.output
