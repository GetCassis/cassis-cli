import json

import httpx
import pytest
from cassis_cli.api import post_ontology_test
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
    transport = httpx.MockTransport(handler)

    def patched(**kwargs):
        kwargs["transport"] = transport
        return post_ontology_test(**kwargs)

    monkeypatch.setattr("cassis_cli.ontology.post_ontology_test", patched)


def _completed_body(run_status="success"):
    return {
        "status": "completed",
        "run_status": run_status,
        "answer": "There are 42 orders.",
        "generated_sql": "SELECT COUNT(*) FROM orders",
        "results": [{"count": 42}],
        "total_rows": 1,
        "truncated": False,
        "missing_concepts": None,
        "warnings": None,
        "duration_seconds": 12.3,
        "error": None,
    }


def _args(repo, *extra):
    return ["ontology", "test", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", *extra]


class TestOntologyTest:
    def test_completed_probe_prints_outcome_and_exits_zero(self, repo, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=_completed_body())

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args(repo, "-q", "How many orders?"))

        assert result.exit_code == 0, result.output
        assert f"/api/ci/projects/{PROJECT_ID}/ontology/test" in seen["url"]
        assert seen["body"]["question"] == "How many orders?"
        assert "tables/public/orders.yml" in seen["body"]["files"]
        assert "SELECT COUNT(*) FROM orders" in result.output
        assert "There are 42 orders." in result.output

    def test_diagnostic_run_status_still_exits_zero(self, repo, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _completed_body(run_status="missing_concept")
            body["missing_concepts"] = [{"concept": "refunds"}]
            return httpx.Response(200, json=body)

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args(repo, "-q", "How many refunds?"))

        assert result.exit_code == 0, result.output
        assert "missing_concept" in result.output

    def test_multiple_questions_probe_each(self, repo, monkeypatch):
        questions = []

        def handler(request: httpx.Request) -> httpx.Response:
            questions.append(json.loads(request.content)["question"])
            return httpx.Response(200, json=_completed_body())

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args(repo, "-q", "Q one?", "-q", "Q two?"))

        assert result.exit_code == 0, result.output
        assert questions == ["Q one?", "Q two?"]

    def test_invalid_tree_prints_findings_and_exits_one(self, repo, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "detail": {
                        "message": "1 file failed",
                        "findings": [{"stage": "yaml", "path": "tables/bad.yml", "message": "bad yaml"}],
                    }
                },
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args(repo, "-q", "How many orders?"))

        assert result.exit_code == 1
        assert "cassis/tables/bad.yml" in result.output
        assert "bad yaml" in result.output

    def test_failed_probe_exits_one(self, repo, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "error", "error": "Test run timed out after 300s", "generated_sql": None}
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args(repo, "-q", "How many orders?"))

        assert result.exit_code == 1
        assert "timed out" in result.output

    def test_transport_error_exits_three(self, repo, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args(repo, "-q", "How many orders?"))

        assert result.exit_code == 3

    def test_json_output_prints_raw_outcome(self, repo, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_completed_body())

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args(repo, "-q", "How many orders?", "--json"))

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        # Always a list, even for a single question — stable shape for scripts.
        assert isinstance(parsed, list) and parsed[0]["status"] == "completed"
