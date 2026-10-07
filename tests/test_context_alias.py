"""`cassis context` is the documented group; `cassis ontology` keeps working, hidden."""

import httpx
from cassis_cli.api import post_ontology_check
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _passing_check(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={"passed": True, "title": "ok", "summary": "ok", "findings": [], "warnings": []},
        )

    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return post_ontology_check(**kwargs)

    monkeypatch.setattr("cassis_cli.ontology.post_ontology_check", patched)


def _repo(tmp_path):
    tables = tmp_path / "cassis" / "tables" / "public"
    tables.mkdir(parents=True)
    (tables / "orders.yml").write_text("schema_name: public\ntable_name: orders\n")
    return tmp_path


def test_context_is_listed_and_ontology_is_hidden_from_top_level_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "context" in result.output
    assert "ontology" not in result.output.lower()


def test_context_check_help_reads_in_context_terms():
    result = runner.invoke(app, ["context", "check", "--help"])
    assert result.exit_code == 0
    assert "context files" in result.output
    assert "ontology" not in result.output.lower()


def test_context_and_legacy_ontology_run_the_same_check(monkeypatch, tmp_path):
    _passing_check(monkeypatch)
    repo = _repo(tmp_path)
    for group in ("context", "ontology"):
        result = runner.invoke(app, [group, "check", str(repo), "--api-key", "sk-k6-test"])
        assert result.exit_code == 0, (group, result.output)
        assert "✓ ok" in result.output
