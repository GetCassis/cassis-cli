import json

import httpx
import pytest
from cassis_cli import api as api_module
from cassis_cli.api import AuthError, post_ontology_check
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


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
    original = post_ontology_check

    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr("cassis_cli.ontology.post_ontology_check", patched)


def _passing_handler(request):
    body = json.loads(request.content)
    assert request.headers["Authorization"] == "Bearer sk-k6-test"
    # Paths are relative to the ontology dir — no cassis/ontology/ prefix.
    assert "_project.yml" in body["files"]
    assert "tables/public/orders.yml" in body["files"]
    return httpx.Response(
        200,
        json={
            "passed": True,
            "file_count": len(body["files"]),
            "title": "Ontology is valid",
            "summary": "YAML parsing, round-trip and import validation passed (2 files).",
            "findings": [],
        },
    )


def _audit_warning_handler(request):
    """A tree that validates but carries advisory quality findings."""
    return httpx.Response(
        200,
        json={
            "passed": True,
            "file_count": 2,
            "title": "Ontology is valid",
            "summary": "YAML parsing, round-trip and import validation passed (2 files).",
            "findings": [],
            "warnings": [
                {
                    "stage": "audit",
                    "path": None,
                    "message": "[unassigned_table] public.orders: Table 'public.orders' is not assigned to any domain",
                },
            ],
        },
    )


def _failing_handler(request):
    return httpx.Response(
        200,
        json={
            "passed": False,
            "file_count": 1,
            "title": "1 file(s) with YAML errors",
            "summary": "Some ontology files contain invalid YAML.",
            "findings": [{"stage": "yaml", "path": "tables/bad.yml", "message": "YAML parse error: x"}],
        },
    )


class TestOntologyCheckCommand:
    def test_pass_exits_zero(self, repo, monkeypatch):
        _mock_api(monkeypatch, _passing_handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "Ontology is valid" not in result.output  # success prints the summary line
        assert "passed" in result.output

    def test_audit_warnings_are_printed_without_failing(self, repo, monkeypatch):
        _mock_api(monkeypatch, _audit_warning_handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0, "advisory warnings must not change the exit code"
        assert "1 ontology quality warning(s)" in result.output
        assert "[unassigned_table] public.orders" in result.output

    def test_response_without_warnings_field_is_tolerated(self, repo, monkeypatch):
        # A server older than the warnings field omits it; the CLI must not KeyError.
        _mock_api(monkeypatch, _passing_handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "warning" not in result.output

    def test_failure_exits_one_and_prints_findings(self, repo, monkeypatch):
        _mock_api(monkeypatch, _failing_handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 1
        assert "YAML errors" in result.output
        # Findings point at real checkout paths.
        assert "cassis/tables/bad.yml" in result.output

    def test_json_output(self, repo, monkeypatch):
        _mock_api(monkeypatch, _failing_handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.output)["passed"] is False

    def test_missing_api_key_exits_two(self, repo, monkeypatch):
        monkeypatch.delenv("CASSIS_API_KEY", raising=False)
        result = runner.invoke(app, ["ontology", "check", str(repo)])
        assert result.exit_code == 2
        assert "No API key" in result.output

    def test_missing_ontology_dir_exits_two(self, tmp_path):
        result = runner.invoke(app, ["ontology", "check", str(tmp_path), "--api-key", "sk-k6-test"])
        assert result.exit_code == 2
        assert "No cassis/" in result.output

    def test_custom_base_path(self, tmp_path, monkeypatch):
        ontology_dir = tmp_path / "dbt" / "cassis"
        ontology_dir.mkdir(parents=True)
        (ontology_dir / "_project.yml").write_text("display_name: Test\n")

        seen = {}

        def handler(request):
            seen["files"] = json.loads(request.content)["files"]
            return httpx.Response(
                200, json={"passed": True, "file_count": 1, "title": "", "summary": "ok", "findings": []}
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app,
            ["ontology", "check", str(tmp_path), "--api-key", "sk-k6-test", "--base-path", "dbt/cassis"],
        )

        assert result.exit_code == 0
        # Paths stay relative to the base path — the server doesn't know it.
        assert seen["files"] == {"_project.yml": "display_name: Test\n"}

    def test_invalid_key_exits_three(self, repo, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(401, json={"detail": "Invalid or expired API key"}))

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-bad"])

        assert result.exit_code == 3
        assert "invalid or expired" in result.output.lower()

    def test_unreachable_api_exits_three(self, repo, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 3
        assert "Could not reach" in result.output

    def test_non_json_response_exits_three(self, repo, monkeypatch):
        """A proxy/portal answering HTML with a 200 is a transport error (3), not a validation failure (1)."""
        _mock_api(monkeypatch, lambda request: httpx.Response(200, text="<html>corporate proxy</html>"))

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 3
        assert "non-JSON response" in result.output

    def test_unexpected_response_shape_exits_three(self, repo, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json={"detail": "something else"}))

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 3
        assert "Unexpected response shape" in result.output

    def test_unreadable_file_exits_two(self, repo):
        """A non-UTF-8 file is a local usage error (2), reported before any upload — never exit 1."""
        (repo / "cassis" / "latin1.yml").write_bytes("clé: café\n".encode("latin-1"))

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 2
        assert "Cannot read" in result.output
        assert "latin1.yml" in result.output

    def test_oversized_tree_exits_two_without_network(self, repo, monkeypatch):
        """Trees beyond the server's request ceilings fail fast locally with a clear message."""
        monkeypatch.setattr("cassis_cli.common.MAX_FILES", 1)

        def handler(request):  # any request reaching the network is a test failure
            raise AssertionError("no request should be sent for an oversized tree")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 2
        assert "too large" in result.output


class TestPostOntologyCheck:
    def test_auth_error_on_401(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        with pytest.raises(AuthError):
            post_ontology_check(
                api_url=api_module.DEFAULT_API_URL, api_key="sk-k6-x", files={"a.yml": "a: 1\n"}, transport=transport
            )

    def test_url_joins_without_double_slash(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(
                200, json={"passed": True, "file_count": 1, "title": "", "summary": "", "findings": []}
            )

        post_ontology_check(
            api_url="https://example.com/",
            api_key="sk-k6-x",
            files={"a.yml": "a: 1\n"},
            transport=httpx.MockTransport(handler),
        )
        assert seen["url"] == "https://example.com/api/ci/ontology-check"


_PROJECT_ID = "019f0000-0000-7000-8000-000000000000"


class TestOntologyCheckProjectScoping:
    def test_project_yml_routes_to_the_scoped_endpoint(self, repo, monkeypatch):
        (repo / "cassis" / "project.yml").write_text(f"cassis_format_version: 2\nproject_id: {_PROJECT_ID}\n")
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "passed": True,
                    "file_count": 1,
                    "title": "",
                    "summary": "ok",
                    "findings": [],
                    "warnings": [],
                    "references_checked": True,
                },
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert seen["url"].endswith(f"/api/ci/projects/{_PROJECT_ID}/ontology/check")
        assert f"Using project {_PROJECT_ID}" in result.output
        assert "Schema references resolve" in result.output

    def test_unbound_checkout_falls_back_to_the_pure_route(self, repo, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(
                200, json={"passed": True, "file_count": 1, "title": "", "summary": "ok", "findings": []}
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert seen["url"].endswith("/api/ci/ontology-check")
        assert "schema reference checks skipped" in result.output

    def test_warnings_print_yellow_but_do_not_fail(self, repo, monkeypatch):
        (repo / "cassis" / "project.yml").write_text(f"cassis_format_version: 2\nproject_id: {_PROJECT_ID}\n")

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "passed": True,
                    "file_count": 1,
                    "title": "Ontology is valid",
                    "summary": "ok",
                    "findings": [],
                    "warnings": [
                        {
                            "stage": "references",
                            "path": None,
                            "message": "Table 'public.orderz' is not in the source schema (v1)",
                        }
                    ],
                    "references_checked": True,
                },
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "1 schema reference warning(s)" in result.output
        assert "public.orderz" in result.output

    def test_audit_warnings_do_not_hide_reference_verification(self, repo, monkeypatch):
        """The 'references resolve' line keys on reference warnings alone — audit findings must not mute it."""
        (repo / "cassis" / "project.yml").write_text(f"cassis_format_version: 2\nproject_id: {_PROJECT_ID}\n")

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "passed": True,
                    "file_count": 1,
                    "title": "Ontology is valid",
                    "summary": "ok",
                    "findings": [],
                    "warnings": [
                        {
                            "stage": "audit",
                            "path": None,
                            "message": "[missing_table_description] public.orders: Table has no description",
                        }
                    ],
                    "references_checked": True,
                },
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "Schema references resolve" in result.output
        assert "1 ontology quality warning(s)" in result.output
        assert "[missing_table_description] public.orders" in result.output

    def test_invalid_project_id_exits_two(self, repo, monkeypatch):
        result = runner.invoke(
            app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test", "--project", "not-a-uuid"]
        )
        assert result.exit_code == 2
        assert "must be a project ID" in result.output

    def test_skipped_reference_check_is_announced_not_silent(self, repo, monkeypatch):
        """A bound checkout whose project has no source schema must not read as verified."""
        (repo / "cassis" / "project.yml").write_text(f"cassis_format_version: 2\nproject_id: {_PROJECT_ID}\n")

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "passed": True,
                    "file_count": 1,
                    "title": "",
                    "summary": "ok",
                    "findings": [],
                    "warnings": [],
                    "references_checked": False,
                },
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, ["ontology", "check", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "Schema reference check skipped" in result.output
        assert "Schema references resolve" not in result.output
