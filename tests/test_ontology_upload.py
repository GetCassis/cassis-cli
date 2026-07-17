import json

import httpx
import pytest
from cassis_cli.api import ApiError, AuthError, UploadValidationError, post_ontology_import
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"


@pytest.fixture
def repo(tmp_path):
    """A fake repository checkout with an ontology tree under the default base path."""
    ontology_dir = tmp_path / "cassis"
    (ontology_dir / "tables" / "public").mkdir(parents=True)
    (ontology_dir / "_project.yml").write_text("display_name: Test\n")
    (ontology_dir / "tables" / "public" / "orders.yml").write_text("schema_name: public\ntable_name: orders\n")
    return tmp_path


def _mock_api(monkeypatch, handler):
    """Route the CLI's HTTP calls through an httpx.MockTransport."""
    original = post_ontology_import

    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr("cassis_cli.ontology.post_ontology_import", patched)


def _success_body(published_version):
    return {
        "domain_count": 0,
        "table_count": 1,
        "join_count": 0,
        "metric_count": 0,
        "published_version": published_version,
    }


class TestOntologyUploadCommand:
    def test_upload_publishes_by_default(self, repo, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            assert request.headers["Authorization"] == "Bearer sk-k6-test"
            return httpx.Response(200, json=_success_body(3))

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app, ["ontology", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 0
        assert seen["url"].endswith(f"/api/ci/projects/{PROJECT_ID}/ontology/import")
        assert seen["body"]["publish"] is True
        # Paths are relative to the ontology dir — no cassis/ontology/ prefix.
        assert "tables/public/orders.yml" in seen["body"]["files"]
        assert "published as v3" in result.output

    def test_no_publish_flag_and_label(self, repo, monkeypatch):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=_success_body(None))

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app,
            [
                "ontology",
                "upload",
                str(repo),
                "--project",
                PROJECT_ID,
                "--api-key",
                "sk-k6-test",
                "--no-publish",
                "--label",
                "release 1.2",
            ],
        )

        assert result.exit_code == 0
        assert seen["body"]["publish"] is False
        assert seen["body"]["label"] == "release 1.2"
        assert "Not published" in result.output

    def test_json_output(self, repo, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=_success_body(1)))

        result = runner.invoke(
            app, ["ontology", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--json"]
        )

        assert result.exit_code == 0
        assert json.loads(result.output)["published_version"] == 1

    def test_validation_rejection_exits_one(self, repo, monkeypatch):
        _mock_api(
            monkeypatch,
            lambda request: httpx.Response(400, json={"detail": "Could not parse ontology archive: bad enum"}),
        )

        result = runner.invoke(
            app, ["ontology", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 1
        assert "bad enum" in result.output

    def test_unknown_project_exits_three(self, repo, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Project not found"}))

        result = runner.invoke(
            app, ["ontology", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 3
        assert "not found or not accessible" in result.output

    def test_invalid_key_exits_three(self, repo, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(401, json={"detail": "Invalid or expired API key"}))

        result = runner.invoke(
            app, ["ontology", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-bad"]
        )

        assert result.exit_code == 3
        assert "invalid or expired" in result.output.lower()

    def test_non_uuid_project_exits_two_without_network(self, repo, monkeypatch):
        def handler(request):  # any request reaching the network is a test failure
            raise AssertionError("no request should be sent for a bad project id")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app, ["ontology", "upload", str(repo), "--project", "not-a-uuid", "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 2
        assert "UUID" in result.output

    def test_missing_project_exits_two(self, repo, monkeypatch):
        monkeypatch.delenv("CASSIS_PROJECT_ID", raising=False)
        result = runner.invoke(app, ["ontology", "upload", str(repo), "--api-key", "sk-k6-test"])
        assert result.exit_code == 2

    def test_missing_api_key_exits_two(self, repo, monkeypatch):
        monkeypatch.delenv("CASSIS_API_KEY", raising=False)
        result = runner.invoke(app, ["ontology", "upload", str(repo), "--project", PROJECT_ID])
        assert result.exit_code == 2
        assert "No API key" in result.output

    def test_missing_ontology_dir_exits_two(self, tmp_path):
        result = runner.invoke(
            app, ["ontology", "upload", str(tmp_path), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )
        assert result.exit_code == 2
        assert "No cassis/" in result.output


class TestPostOntologyImport:
    def test_auth_error_on_401(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        with pytest.raises(AuthError):
            post_ontology_import(
                api_url="https://example.com",
                api_key="sk-k6-x",
                project_id=PROJECT_ID,
                files={"a.yml": "a: 1\n"},
                publish=True,
                transport=transport,
            )

    def test_validation_error_on_400(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"detail": "nope"}))
        with pytest.raises(UploadValidationError, match="nope"):
            post_ontology_import(
                api_url="https://example.com",
                api_key="sk-k6-x",
                project_id=PROJECT_ID,
                files={"a.yml": "a: 1\n"},
                publish=True,
                transport=transport,
            )

    def test_unexpected_response_shape(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"something": "else"}))
        with pytest.raises(ApiError, match="Unexpected response shape"):
            post_ontology_import(
                api_url="https://example.com",
                api_key="sk-k6-x",
                project_id=PROJECT_ID,
                files={"a.yml": "a: 1\n"},
                publish=False,
                transport=transport,
            )
