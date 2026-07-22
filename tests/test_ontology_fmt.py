import json

import httpx
import pytest
from cassis_cli.api import ApiError, post_ontology_fmt
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

_NON_CANONICAL = "table_name: orders\nschema_name: public\n"
_CANONICAL = "schema_name: public\ntable_name: orders\n"


@pytest.fixture
def repo(tmp_path):
    """A fake checkout whose orders.yml is valid but non-canonical (key order)."""
    ontology_dir = tmp_path / "cassis"
    (ontology_dir / "tables" / "public").mkdir(parents=True)
    (ontology_dir / "_project.yml").write_text("display_name: Test\n")
    (ontology_dir / "tables" / "public" / "orders.yml").write_text(_NON_CANONICAL)
    return tmp_path


def _mock_api(monkeypatch, handler):
    original = post_ontology_fmt

    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr("cassis_cli.ontology.post_ontology_fmt", patched)


def _fmt_handler(request):
    body = json.loads(request.content)
    assert request.headers["Authorization"] == "Bearer sk-k6-test"
    assert body["files"]["tables/public/orders.yml"] == _NON_CANONICAL
    files = dict(body["files"])
    files["tables/public/orders.yml"] = _CANONICAL
    return httpx.Response(
        200,
        json={
            "ok": True,
            "files": files,
            "changed_paths": ["tables/public/orders.yml"],
            "removed_paths": [],
            "findings": [],
        },
    )


def test_fmt_rewrites_changed_files(repo, monkeypatch):
    _mock_api(monkeypatch, _fmt_handler)

    result = runner.invoke(app, ["ontology", "fmt", str(repo), "--api-key", "sk-k6-test"])

    assert result.exit_code == 0, result.output
    assert "rewrote cassis/tables/public/orders.yml" in result.output
    assert (repo / "cassis" / "tables" / "public" / "orders.yml").read_text() == _CANONICAL


def test_fmt_check_only_exits_1_and_writes_nothing(repo, monkeypatch):
    _mock_api(monkeypatch, _fmt_handler)

    result = runner.invoke(app, ["ontology", "fmt", str(repo), "--api-key", "sk-k6-test", "--check"])

    assert result.exit_code == 1
    assert "would rewrite cassis/tables/public/orders.yml" in result.output
    assert (repo / "cassis" / "tables" / "public" / "orders.yml").read_text() == _NON_CANONICAL


def test_fmt_already_canonical_exits_0(repo, monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"ok": True, "files": body["files"], "changed_paths": [], "removed_paths": [], "findings": []},
        )

    _mock_api(monkeypatch, handler)

    result = runner.invoke(app, ["ontology", "fmt", str(repo), "--api-key", "sk-k6-test"])

    assert result.exit_code == 0
    assert "already canonical" in result.output


def test_fmt_removes_files_absent_from_canonical_tree(repo, monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        files = {p: c for p, c in body["files"].items() if p != "tables/public/orders.yml"}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "files": files,
                "changed_paths": [],
                "removed_paths": ["tables/public/orders.yml"],
                "findings": [],
            },
        )

    _mock_api(monkeypatch, handler)

    result = runner.invoke(app, ["ontology", "fmt", str(repo), "--api-key", "sk-k6-test"])

    assert result.exit_code == 0
    assert "removed cassis/tables/public/orders.yml" in result.output
    assert not (repo / "cassis" / "tables" / "public" / "orders.yml").exists()


def test_fmt_unparseable_tree_exits_1(repo, monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "ok": False,
                "files": None,
                "changed_paths": [],
                "removed_paths": [],
                "findings": [{"stage": "yaml", "path": "_project.yml", "message": "YAML parse error: bad"}],
            },
        )

    _mock_api(monkeypatch, handler)

    result = runner.invoke(app, ["ontology", "fmt", str(repo), "--api-key", "sk-k6-test"])

    assert result.exit_code == 1
    assert "Cannot format" in result.output


def test_fmt_requires_api_key(repo):
    result = runner.invoke(app, ["ontology", "fmt", str(repo)], env={"CASSIS_API_KEY": ""})
    assert result.exit_code == 2


@pytest.mark.parametrize(
    "body",
    [
        # removed_paths missing — fmt indexes it after the shape check.
        {"ok": True, "files": {}, "changed_paths": [], "findings": []},
        # ok=True but no files dict — fmt reads result["files"][p] for changed paths.
        {"ok": True, "files": None, "changed_paths": [], "removed_paths": [], "findings": []},
    ],
)
def test_post_ontology_fmt_rejects_malformed_response_shape(body):
    def handler(request):
        return httpx.Response(200, json=body)

    with pytest.raises(ApiError, match="Unexpected response shape"):
        post_ontology_fmt(
            api_url="https://api.test",
            api_key="sk-k6-test",
            files={"_project.yml": "display_name: Test\n"},
            transport=httpx.MockTransport(handler),
        )
