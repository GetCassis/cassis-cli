import json

import httpx
import pytest
from cassis_cli.api import ApiError, AuthError, get_ontology_export
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"

_EXPORT_FILES = {
    "_project.yml": "display_name: Test\n",
    "tables/public/orders.yml": "schema_name: public\ntable_name: orders\n",
}


def _mock_api(monkeypatch, handler):
    """Route the CLI's HTTP calls through an httpx.MockTransport."""
    original = get_ontology_export

    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr("cassis_cli.ontology.get_ontology_export", patched)


def _export_handler(request):
    assert request.method == "GET"
    assert request.headers["Authorization"] == "Bearer sk-k6-test"
    return httpx.Response(200, json={"files": _EXPORT_FILES})


class TestOntologyPullCommand:
    def test_pull_writes_tree(self, tmp_path, monkeypatch):
        _mock_api(monkeypatch, _export_handler)

        result = runner.invoke(
            app, ["ontology", "pull", str(tmp_path), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 0, result.output
        assert (tmp_path / "cassis" / "_project.yml").read_text() == "display_name: Test\n"
        assert (tmp_path / "cassis" / "tables" / "public" / "orders.yml").exists()
        assert "Pulled 2 files" in result.output

    def test_pull_prunes_stale_yaml(self, tmp_path, monkeypatch):
        stale = tmp_path / "cassis" / "tables" / "public" / "old_table.yml"
        stale.parent.mkdir(parents=True)
        stale.write_text("table_name: old_table\n")
        keeper = tmp_path / "cassis" / "notes.txt"  # non-YAML files are never touched
        keeper.write_text("keep me")
        _mock_api(monkeypatch, _export_handler)

        result = runner.invoke(
            app, ["ontology", "pull", str(tmp_path), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 0, result.output
        assert not stale.exists()
        assert keeper.exists()
        assert "1 stale files deleted" in result.output

    def test_no_prune_keeps_stale_yaml(self, tmp_path, monkeypatch):
        stale = tmp_path / "cassis" / "old.yml"
        stale.parent.mkdir(parents=True)
        stale.write_text("a: 1\n")
        _mock_api(monkeypatch, _export_handler)

        result = runner.invoke(
            app,
            ["ontology", "pull", str(tmp_path), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--no-prune"],
        )

        assert result.exit_code == 0
        assert stale.exists()

    def test_pull_overwrites_existing(self, tmp_path, monkeypatch):
        existing = tmp_path / "cassis" / "_project.yml"
        existing.parent.mkdir(parents=True)
        existing.write_text("display_name: Old\n")
        _mock_api(monkeypatch, _export_handler)

        result = runner.invoke(
            app, ["ontology", "pull", str(tmp_path), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 0
        assert existing.read_text() == "display_name: Test\n"

    def test_json_output(self, tmp_path, monkeypatch):
        _mock_api(monkeypatch, _export_handler)

        result = runner.invoke(
            app,
            ["ontology", "pull", str(tmp_path), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--json"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["written"] == sorted(_EXPORT_FILES)
        assert payload["deleted"] == []

    def test_traversal_path_from_server_is_refused(self, tmp_path, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={"files": {"../evil.yml": "a: 1\n"}})

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app, ["ontology", "pull", str(tmp_path), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 3
        assert not (tmp_path / "evil.yml").exists()

    def test_missing_api_key_exits_two(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CASSIS_API_KEY", raising=False)
        result = runner.invoke(app, ["ontology", "pull", str(tmp_path), "--project", PROJECT_ID])
        assert result.exit_code == 2

    def test_non_uuid_project_exits_two(self, tmp_path):
        result = runner.invoke(app, ["ontology", "pull", str(tmp_path), "--project", "nope", "--api-key", "sk-k6-test"])
        assert result.exit_code == 2

    def test_inaccessible_project_exits_three(self, tmp_path, monkeypatch):
        _mock_api(monkeypatch, lambda r: httpx.Response(404, json={"detail": "Project not found"}))
        result = runner.invoke(
            app, ["ontology", "pull", str(tmp_path), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )
        assert result.exit_code == 3


class TestGetOntologyExport:
    def _call(self, handler):
        return get_ontology_export(
            api_url="https://api.example.com",
            api_key="sk-k6-test",
            project_id=PROJECT_ID,
            transport=httpx.MockTransport(handler),
        )

    def test_returns_files(self):
        files = self._call(lambda r: httpx.Response(200, json={"files": _EXPORT_FILES}))
        assert files == _EXPORT_FILES

    def test_401_raises_auth_error(self):
        with pytest.raises(AuthError):
            self._call(lambda r: httpx.Response(401))

    def test_unexpected_shape_raises(self):
        with pytest.raises(ApiError):
            self._call(lambda r: httpx.Response(200, json={"files": "not-a-dict"}))
