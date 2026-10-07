import json
import subprocess

import httpx
import pytest
from cassis_cli.api import ApiError, AuthError, UploadValidationError, post_ontology_import
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"


@pytest.fixture
def repo(tmp_path, commit_all):
    """A git checkout with a committed ontology tree under the default base path."""
    ontology_dir = tmp_path / "cassis"
    (ontology_dir / "tables" / "public").mkdir(parents=True)
    (ontology_dir / "_project.yml").write_text("display_name: Test\n")
    (ontology_dir / "tables" / "public" / "orders.yml").write_text("schema_name: public\ntable_name: orders\n")
    commit_all(tmp_path)
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
            app, ["context", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
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
                "context",
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
            app, ["context", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test", "--json"]
        )

        assert result.exit_code == 0
        assert json.loads(result.output)["published_version"] == 1

    def test_validation_rejection_exits_one(self, repo, monkeypatch):
        _mock_api(
            monkeypatch,
            lambda request: httpx.Response(400, json={"detail": "Could not parse ontology archive: bad enum"}),
        )

        result = runner.invoke(
            app, ["context", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 1
        assert "bad enum" in result.output

    def test_unknown_project_exits_three(self, repo, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Project not found"}))

        result = runner.invoke(
            app, ["context", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 3
        assert "Check --project" in result.output

    def test_invalid_key_exits_three(self, repo, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(401, json={"detail": "Invalid or expired API key"}))

        result = runner.invoke(app, ["context", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-bad"])

        assert result.exit_code == 3
        assert "invalid or expired" in result.output.lower()

    def test_non_uuid_project_exits_two_without_network(self, repo, monkeypatch):
        def handler(request):  # any request reaching the network is a test failure
            raise AssertionError("no request should be sent for a bad project id")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app, ["context", "upload", str(repo), "--project", "not-a-uuid", "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 2
        assert "UUID" in result.output

    def test_missing_project_exits_two(self, repo, monkeypatch):
        monkeypatch.delenv("CASSIS_PROJECT_ID", raising=False)
        result = runner.invoke(app, ["context", "upload", str(repo), "--api-key", "sk-k6-test"])
        assert result.exit_code == 2

    def test_missing_api_key_exits_two(self, repo, monkeypatch):
        monkeypatch.delenv("CASSIS_API_KEY", raising=False)
        result = runner.invoke(app, ["context", "upload", str(repo), "--project", PROJECT_ID])
        assert result.exit_code == 2
        assert "No API key" in result.output

    def test_missing_ontology_dir_exits_two(self, tmp_path):
        result = runner.invoke(
            app, ["context", "upload", str(tmp_path), "--project", PROJECT_ID, "--api-key", "sk-k6-test"]
        )
        assert result.exit_code == 2
        assert "No cassis/" in result.output


class TestUploadRequiresCommittedTree:
    """The upload sends HEAD and refuses ontology files that differ from it."""

    @staticmethod
    def _invoke(repo):
        return runner.invoke(app, ["ontology", "upload", str(repo), "--project", PROJECT_ID, "--api-key", "sk-k6-test"])

    @staticmethod
    def _no_network(monkeypatch):
        def handler(request):
            raise AssertionError("no request expected")

        _mock_api(monkeypatch, handler)

    def test_sends_head(self, repo, commit_all, monkeypatch):
        head = commit_all(repo)
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=_success_body(1))

        _mock_api(monkeypatch, handler)

        result = self._invoke(repo)

        assert result.exit_code == 0, result.output
        assert seen["body"]["git_commit_sha"] == head

    def test_outside_a_git_checkout_exits_two(self, tmp_path, monkeypatch):
        (tmp_path / "cassis" / "tables").mkdir(parents=True)
        (tmp_path / "cassis" / "tables" / "orders.yml").write_text("schema_name: public\ntable_name: orders\n")
        self._no_network(monkeypatch)

        result = self._invoke(tmp_path)

        assert result.exit_code == 2
        assert "not a git checkout" in result.output

    @pytest.mark.parametrize("change", ["modified", "staged", "deleted", "untracked"])
    def test_uncommitted_ontology_file_exits_two(self, repo, monkeypatch, change):
        orders = repo / "cassis" / "tables" / "public" / "orders.yml"
        if change == "modified":
            orders.write_text("schema_name: public\ntable_name: orders\ndescription: edited\n")
        elif change == "staged":
            orders.write_text("schema_name: public\ntable_name: orders\ndescription: edited\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        elif change == "deleted":
            orders.unlink()
            (repo / "cassis" / "tables" / "public" / "items.yml").write_text("schema_name: public\ntable_name: x\n")
            subprocess.run(["git", "add", "cassis/tables/public/items.yml"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "items"], cwd=repo, check=True)
        else:
            (repo / "cassis" / "tables" / "public" / "items.yml").write_text("schema_name: public\ntable_name: x\n")
        self._no_network(monkeypatch)

        result = self._invoke(repo)

        assert result.exit_code == 2, result.output
        assert "Uncommitted changes under cassis/" in result.output
        assert "Commit the changes under cassis/, then upload again." in result.output

    def test_gitignored_ontology_file_exits_two(self, repo, monkeypatch):
        """Collected and uploaded, but not in HEAD: the recorded commit would not hold it."""
        (repo / ".gitignore").write_text("cassis/joins.yml\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "ignore"], cwd=repo, check=True)
        (repo / "cassis" / "joins.yml").write_text("[]\n")
        self._no_network(monkeypatch)

        result = self._invoke(repo)

        assert result.exit_code == 2, result.output
        assert "cassis/joins.yml" in result.output

    def test_staged_rename_out_of_the_ontology_exits_two(self, repo, monkeypatch):
        """`git mv x.yml x.txt` drops x.yml from the upload while HEAD still holds it."""
        subprocess.run(
            ["git", "mv", "cassis/tables/public/orders.yml", "cassis/tables/public/orders.txt"], cwd=repo, check=True
        )
        (repo / "cassis" / "tables" / "public" / "items.yml").write_text("schema_name: public\ntable_name: x\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "items"], cwd=repo, check=True)
        subprocess.run(["git", "reset", "-q", "--soft", "HEAD~1"], cwd=repo, check=True)  # rename staged again
        subprocess.run(["git", "commit", "-q", "-m", "only items", "--", "cassis/tables/public/items.yml"], cwd=repo)
        self._no_network(monkeypatch)

        result = self._invoke(repo)

        assert result.exit_code == 2, result.output
        assert "cassis/tables/public/orders.yml" in result.output

    def test_checkout_in_a_subdirectory_of_the_repository(self, tmp_path, commit_all, monkeypatch):
        sub = tmp_path / "analytics"
        (sub / "cassis" / "tables").mkdir(parents=True)
        (sub / "cassis" / "tables" / "orders.yml").write_text("schema_name: public\ntable_name: orders\n")
        head = commit_all(tmp_path)
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=_success_body(1))

        _mock_api(monkeypatch, handler)

        result = self._invoke(sub)

        assert result.exit_code == 0, result.output
        assert seen["body"]["git_commit_sha"] == head

    def test_uploaded_text_that_differs_from_head_exits_two_even_when_the_file_hashes_to_it(self, repo, monkeypatch):
        """A clean filter makes the working-tree file hash to its blob; the text the upload sends is checked."""
        subprocess.run(["git", "config", "filter.dropdesc.clean", "grep -v '^description:'"], cwd=repo, check=True)
        (repo / ".gitattributes").write_text("*.yml filter=dropdesc\n")
        orders = repo / "cassis" / "tables" / "public" / "orders.yml"
        orders.write_text("schema_name: public\ntable_name: orders\ndescription: not in the commit\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "filtered"], cwd=repo, check=True)
        self._no_network(monkeypatch)

        result = self._invoke(repo)

        assert result.exit_code == 2, result.output
        assert "cassis/tables/public/orders.yml" in result.output

    def test_file_committed_with_crlf_line_endings_uploads(self, repo, commit_all, monkeypatch):
        orders = repo / "cassis" / "tables" / "public" / "orders.yml"
        orders.write_bytes(b"schema_name: public\r\ntable_name: orders\r\n")
        head = commit_all(repo)
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=_success_body(1))

        _mock_api(monkeypatch, handler)

        result = self._invoke(repo)

        assert result.exit_code == 0, result.output
        assert seen["body"]["git_commit_sha"] == head

    def test_files_that_are_not_uploaded_do_not_block(self, repo, monkeypatch):
        (repo / ".gitignore").write_text("cassis/.schema.json\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "ignore"], cwd=repo, check=True)
        (repo / "cassis" / ".schema.json").write_text("{}\n")  # gitignored snapshot
        (repo / "cassis" / "NOTES.md").write_text("draft\n")  # not an ontology file
        (repo / "README.md").write_text("outside the base path\n")
        _mock_api(monkeypatch, lambda request: httpx.Response(200, json=_success_body(1)))

        result = self._invoke(repo)

        assert result.exit_code == 0, result.output


class TestPostOntologyImport:
    def test_auth_error_on_401(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        with pytest.raises(AuthError):
            post_ontology_import(
                api_url="https://example.com",
                api_key="sk-k6-x",
                project_id=PROJECT_ID,
                files={"a.yml": "a: 1\n"},
                git_commit_sha="a" * 40,
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
                git_commit_sha="a" * 40,
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
                git_commit_sha="a" * 40,
                publish=False,
                transport=transport,
            )

    def test_in_job_failure_error_body_raises_validation_error(self):
        # The server runs the import as a background job and streams keepalives;
        # a failure after the stream starts arrives as a 200 whose body carries
        # only an ``error`` key (never the success shape).
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b'  {"error": "import blew up"}'))
        with pytest.raises(UploadValidationError, match="import blew up"):
            post_ontology_import(
                api_url="https://example.com",
                api_key="sk-k6-x",
                project_id=PROJECT_ID,
                files={"a.yml": "a: 1\n"},
                git_commit_sha="a" * 40,
                publish=False,
                transport=transport,
            )


OTHER_PROJECT_ID = "019f1111-1111-7111-8111-111111111111"


class TestProjectIdDefault:
    """`--project` defaults to the id recorded in <base-path>/project.yml."""

    @staticmethod
    def _capture(seen):
        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json=_success_body(1))

        return handler

    def test_defaults_from_project_yml(self, repo, commit_all, monkeypatch):
        monkeypatch.delenv("CASSIS_PROJECT_ID", raising=False)
        (repo / "cassis" / "project.yml").write_text(f"cassis_format_version: '0.1'\nproject_id: {PROJECT_ID}\n")
        commit_all(repo)
        seen = {}
        _mock_api(monkeypatch, self._capture(seen))

        result = runner.invoke(app, ["context", "upload", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0, result.output
        assert seen["url"].endswith(f"/api/ci/projects/{PROJECT_ID}/ontology/import")
        assert "from cassis/project.yml" in result.output  # notes where the id came from

    def test_explicit_project_overrides_project_yml(self, repo, commit_all, monkeypatch):
        monkeypatch.delenv("CASSIS_PROJECT_ID", raising=False)
        (repo / "cassis" / "project.yml").write_text(f"cassis_format_version: '0.1'\nproject_id: {PROJECT_ID}\n")
        commit_all(repo)
        seen = {}
        _mock_api(monkeypatch, self._capture(seen))

        result = runner.invoke(
            app, ["context", "upload", str(repo), "--project", OTHER_PROJECT_ID, "--api-key", "sk-k6-test"]
        )

        assert result.exit_code == 0, result.output
        assert seen["url"].endswith(f"/api/ci/projects/{OTHER_PROJECT_ID}/ontology/import")

    def test_no_project_and_no_project_yml_is_usage_error(self, repo, monkeypatch):
        monkeypatch.delenv("CASSIS_PROJECT_ID", raising=False)
        # repo has only the legacy _project.yml (not a project.yml identity file).
        _mock_api(monkeypatch, self._capture({}))

        result = runner.invoke(app, ["context", "upload", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 2
        assert "No project" in result.output
