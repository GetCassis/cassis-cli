import json

import httpx
import pytest
from cassis_cli.api import get_source_change_run, post_detect_from_ddl
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

_PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
_RUN_ID = "019f0000-0000-7000-8000-00000000d001"

_DDL = "CREATE TABLE public.refunds (id INTEGER PRIMARY KEY, amount INTEGER NOT NULL);"


@pytest.fixture
def repo(tmp_path):
    ontology_dir = tmp_path / "cassis"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "project.yml").write_text(f"cassis_format_version: 2\nproject_id: {_PROJECT_ID}\n")
    return tmp_path


@pytest.fixture
def ddl_file(tmp_path):
    f = tmp_path / "schema.sql"
    f.write_text(_DDL, encoding="utf-8")
    return f


def _mock_api(monkeypatch, start_handler, poll_handler=None):
    def patch(module_path, original, handler):
        def patched(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(**kwargs)

        monkeypatch.setattr(module_path, patched)

    patch("cassis_cli.schema.post_detect_from_ddl", post_detect_from_ddl, start_handler)
    if poll_handler is not None:
        patch("cassis_cli.schema.get_source_change_run", get_source_change_run, poll_handler)
        monkeypatch.setattr("cassis_cli.schema.time.sleep", lambda seconds: None)


def _start_handler(request):
    assert request.headers["Authorization"] == "Bearer sk-k6-test"
    body = json.loads(request.content)
    assert "ddl" in body
    return httpx.Response(201, json={"run_id": _RUN_ID, "status": "running", "source_kind": "ddl", "trigger": "manual"})


def _completed_handler(summary=None):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "run_id": _RUN_ID,
                "status": "completed",
                "source_kind": "ddl",
                "trigger": "manual",
                "summary": summary or {"total_changes": 2},
            },
        )

    return handler


class TestSchemaPushCommand:
    def test_push_waits_and_reports_changes(self, repo, ddl_file, monkeypatch):
        _mock_api(monkeypatch, _start_handler, _completed_handler())

        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0, result.output
        assert "Detection run started" in result.output
        assert "2 change(s) detected" in result.output

    def test_push_no_changes(self, repo, ddl_file, monkeypatch):
        _mock_api(monkeypatch, _start_handler, _completed_handler(summary={"total_changes": 0}))

        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 0
        assert "no changes" in result.output

    def test_push_rejects_removed_no_wait_flag(self, repo, ddl_file, monkeypatch):
        # `--no-wait` was removed when DDL parsing moved into the detection run:
        # a fire-and-forget push could exit 0 on a schema that never parsed, so
        # exit 0 must mean "parsed AND applied" — which requires waiting.
        _mock_api(monkeypatch, _start_handler)

        result = runner.invoke(
            app, ["schema", "push", str(ddl_file), "--path", str(repo), "--api-key", "sk-k6-test", "--no-wait"]
        )

        assert result.exit_code != 0
        assert "No such option" in result.output

    def test_push_json_output(self, repo, ddl_file, monkeypatch):
        _mock_api(monkeypatch, _start_handler, _completed_handler())

        result = runner.invoke(
            app, ["schema", "push", str(ddl_file), "--path", str(repo), "--api-key", "sk-k6-test", "--json"]
        )

        assert result.exit_code == 0
        # Output has "Detection run started: ..." then the JSON blob then the status line.
        # Extract the JSON block between the first and last lines.
        lines = result.output.strip().split("\n")
        json_start = next(i for i, l in enumerate(lines) if l.strip().startswith("{"))
        json_end = next(i for i in range(len(lines) - 1, -1, -1) if lines[i].strip().startswith("}"))
        body = json.loads("\n".join(lines[json_start : json_end + 1]))
        assert body["run_id"] == _RUN_ID

    def test_push_failed_run_exits_one(self, repo, ddl_file, monkeypatch):
        def failed_handler(request):
            return httpx.Response(
                200,
                json={
                    "run_id": _RUN_ID,
                    "status": "failed",
                    "source_kind": "ddl",
                    "trigger": "manual",
                    "error": "parse error",
                },
            )

        _mock_api(monkeypatch, _start_handler, failed_handler)

        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 1
        assert "parse error" in result.output

    def test_connected_project_exits_one(self, repo, ddl_file, monkeypatch):
        def conflict_handler(request):
            return httpx.Response(409, json={"detail": "This project has a warehouse connection."})

        _mock_api(monkeypatch, conflict_handler)

        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 1

    def test_missing_file_exits_two(self, repo, tmp_path):
        result = runner.invoke(
            app,
            ["schema", "push", str(tmp_path / "missing.sql"), "--path", str(repo), "--api-key", "sk-k6-test"],
        )

        assert result.exit_code == 2
        assert "Cannot read" in result.output

    def test_empty_file_exits_two(self, repo, tmp_path):
        empty = tmp_path / "empty.sql"
        empty.write_text("   \n", encoding="utf-8")

        result = runner.invoke(app, ["schema", "push", str(empty), "--path", str(repo), "--api-key", "sk-k6-test"])

        assert result.exit_code == 2
        assert "empty" in result.output

    def test_missing_api_key_exits_two(self, repo, ddl_file):
        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo)])
        assert result.exit_code == 2
        assert "No API key" in result.output

    def test_timeout_exits_three(self, repo, ddl_file, monkeypatch):
        def still_running(request):
            return httpx.Response(
                200, json={"run_id": _RUN_ID, "status": "running", "source_kind": "ddl", "trigger": "manual"}
            )

        _mock_api(monkeypatch, _start_handler, still_running)

        result = runner.invoke(
            app,
            ["schema", "push", str(ddl_file), "--path", str(repo), "--api-key", "sk-k6-test", "--timeout", "0"],
        )

        assert result.exit_code == 3
        assert "Timed out" in result.output
