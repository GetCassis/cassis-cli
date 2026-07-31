import json

import httpx
import pytest
from cassis_cli.api import get_schema_export
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

_PROJECT_ID = "019f0000-0000-7000-8000-000000000000"

_SCHEMA_BODY = {
    "schema_version": {"version": 3, "taken_at": "2026-07-30T00:00:00+00:00"},
    "tables": [
        {
            "name": "public.orders",
            "table_type": "BASE TABLE",
            "description": None,
            "columns": [
                {"name": "id", "data_type": "integer"},
                {"name": "customer_id", "data_type": "integer"},
            ],
        }
    ],
}


@pytest.fixture
def repo(tmp_path):
    """A checkout bound to a project via project.yml."""
    ontology_dir = tmp_path / "cassis"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "project.yml").write_text(f"cassis_format_version: 2\nproject_id: {_PROJECT_ID}\n")
    return tmp_path


def _mock_api(monkeypatch, handler):
    original = get_schema_export

    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr("cassis_cli.schema.get_schema_export", patched)


def _schema_handler(request):
    assert request.headers["Authorization"] == "Bearer sk-k6-test"
    assert str(request.url).endswith(f"/api/ci/projects/{_PROJECT_ID}/schema")
    return httpx.Response(200, json=_SCHEMA_BODY)


class TestSchemaPullCommand:
    def test_writes_snapshot_and_gitignore(self, repo, monkeypatch):
        _mock_api(monkeypatch, _schema_handler)

        result = runner.invoke(app, ["schema", "pull", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "1 tables, 2 columns" in result.output
        snapshot = json.loads((repo / "cassis" / ".schema.json").read_text())
        assert snapshot["project_id"] == _PROJECT_ID
        assert snapshot["schema_version"]["version"] == 3
        assert snapshot["tables"] == _SCHEMA_BODY["tables"]
        assert "pulled_at" in snapshot
        assert ".schema.json" in (repo / "cassis" / ".gitignore").read_text().splitlines()

    def test_gitignore_append_preserves_existing_entries_and_is_idempotent(self, repo, monkeypatch):
        _mock_api(monkeypatch, _schema_handler)
        gitignore = repo / "cassis" / ".gitignore"
        gitignore.write_text("local-notes.md\n")

        runner.invoke(app, ["schema", "pull", str(repo), "--api-key", "sk-k6-test"])
        first = gitignore.read_text()
        runner.invoke(app, ["schema", "pull", str(repo), "--api-key", "sk-k6-test"])

        lines = gitignore.read_text().splitlines()
        assert gitignore.read_text() == first  # second pull adds nothing
        assert lines[0] == "local-notes.md"
        assert lines.count(".schema.json") == 1

    def test_explicit_project_overrides_binding(self, repo, monkeypatch):
        other = "019f0000-0000-7000-8000-00000000beef"
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json=_SCHEMA_BODY)

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["schema", "pull", str(repo), "--api-key", "sk-k6-test", "--project", other])

        assert result.exit_code == 0
        assert seen["url"].endswith(f"/api/ci/projects/{other}/schema")

    def test_no_project_binding_exits_two(self, tmp_path, monkeypatch):
        (tmp_path / "cassis").mkdir()
        result = runner.invoke(app, ["schema", "pull", str(tmp_path), "--api-key", "sk-k6-test"])
        assert result.exit_code == 2
        assert "No project" in result.output

    def test_no_source_schema_exits_three_with_the_server_message(self, repo, monkeypatch):
        _mock_api(
            monkeypatch,
            lambda request: httpx.Response(
                404,
                json={"detail": "No source schema yet — sync it from the warehouse or upload a DDL in Cassis first"},
            ),
        )

        result = runner.invoke(app, ["schema", "pull", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 3
        assert "No source schema yet" in result.output

    def test_missing_api_key_exits_two(self, repo):
        result = runner.invoke(app, ["schema", "pull", str(repo)])
        assert result.exit_code == 2
        assert "No API key" in result.output

    def test_unwritable_checkout_exits_two(self, tmp_path, monkeypatch):
        """A local write failure is a usage-class exit (2), not transport (3) —
        the API call succeeded; the checkout is what's broken."""
        (tmp_path / "cassis").write_text("a file where the ontology dir should be")
        _mock_api(monkeypatch, _schema_handler)

        result = runner.invoke(
            app, ["schema", "pull", str(tmp_path), "--api-key", "sk-k6-test", "--project", _PROJECT_ID]
        )

        assert result.exit_code == 2
        assert "Could not write the snapshot" in result.output
