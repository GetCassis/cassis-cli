import json

import httpx
from cassis_cli.api import post_eval_case_create
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"


def _mock_api(monkeypatch, handler):
    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return post_eval_case_create(**kwargs)

    monkeypatch.setattr("cassis_cli.eval.post_eval_case_create", patched)


def _args(*extra):
    return [
        "eval",
        "add-case",
        "--project",
        PROJECT_ID,
        "--api-key",
        "sk-k6-test",
        "-q",
        "Net revenue last month?",
        "--gold-sql",
        "SELECT 1",
        *extra,
    ]


class TestEvalAddCase:
    def test_creates_case_and_exits_zero(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "id": "019f0000-0000-7000-8000-0000000000ca",
                    "question": "Net revenue last month?",
                    "gold_sql": "SELECT 1",
                },
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args())

        assert result.exit_code == 0, result.output
        assert f"/api/ci/projects/{PROJECT_ID}/eval/cases" in seen["url"]
        # No --expect: nothing is sent, so an older server reads the case as before.
        assert seen["body"] == {"question": "Net revenue last month?", "gold_sql": "SELECT 1"}
        assert "Added eval case" in result.output
        assert "Expects" not in result.output

    def test_pending_change_defers_the_check_and_softens_the_missing_table_warning(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "id": "019f0000-0000-7000-8000-0000000000ca",
                    "question": "Net revenue last month?",
                    "gold_sql": "SELECT 1 FROM s.invoices",
                    "entry_check": {"state": "pending_change"},
                    "footprint": {"state": "diverged", "tables": [], "unresolved": ["s.invoices"]},
                },
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--pending-change"))

        assert result.exit_code == 0, result.output
        assert seen["body"]["pending_change"] is True
        assert "Pending the change: Cassis checks it after the next publication" in result.output
        assert "expected until the change is published" in result.output
        assert "add the tables to the context" not in result.output

    def test_expect_cannot_answer_takes_no_fact(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "id": "019f0000-0000-7000-8000-0000000000ca",
                    "question": "Net revenue last month?",
                    "gold_sql": "SELECT 1",
                    "expectation": {"kind": "cannot_answer"},
                },
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--expect", "cannot-answer"))

        assert result.exit_code == 0, result.output
        assert seen["body"]["expectation"] == {"kind": "cannot_answer"}
        assert "Expects the agent to say the question cannot be answered" in result.output

    def test_an_older_server_that_ignores_expect_is_called_out(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                json={"id": "019f0000-0000-7000-8000-0000000000ca", "question": "Q?", "gold_sql": "SELECT 1"},
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--expect", "cannot-answer"))

        assert result.exit_code == 0, result.output
        assert "This server ignores --expect" in result.output

    def test_json_mode_still_warns_on_stderr_when_expect_is_ignored(self, monkeypatch):
        body = {"id": "019f0000-0000-7000-8000-0000000000ca", "question": "Q?", "gold_sql": "SELECT 1"}
        _mock_api(monkeypatch, lambda request: httpx.Response(201, json=body))

        result = runner.invoke(app, _args("--expect", "cannot-answer", "--json"))

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == body
        assert "This server ignores --expect" in result.stderr

    def test_expect_answer_is_the_default_and_never_warns(self, monkeypatch):
        body = {"id": "019f0000-0000-7000-8000-0000000000ca", "question": "Q?", "gold_sql": "SELECT 1"}
        _mock_api(monkeypatch, lambda request: httpx.Response(201, json=body))

        result = runner.invoke(app, _args("--expect", "answer"))

        assert result.exit_code == 0, result.output
        assert "ignores --expect" not in result.output

    def test_an_unknown_expectation_is_a_usage_error(self, monkeypatch):
        _mock_api(monkeypatch, lambda request: httpx.Response(500))

        result = runner.invoke(app, _args("--expect", "clarify"))

        assert result.exit_code == 2
        assert "--expect must be one of answer, cannot-answer" in result.output

    def test_duplicate_exits_one(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"detail": "An eval case with this exact question already exists"})

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args())

        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_unrunnable_gold_sql_exits_one(self, monkeypatch):
        # The server executes the gold SQL before storing it, so a case that
        # cannot run is a validation failure (exit 1), not an API error.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"detail": 'Gold SQL validation failed: relation "orders" does not exist'})

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args())

        assert result.exit_code == 1
        assert "Gold SQL validation failed" in result.output

    def test_transport_error_exits_three(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args())

        assert result.exit_code == 3

    def test_empty_question_is_usage_error(self, monkeypatch):
        result = runner.invoke(
            app,
            ["eval", "add-case", "--project", PROJECT_ID, "--api-key", "k", "-q", "  ", "--gold-sql", "SELECT 1"],
        )
        assert result.exit_code == 2

    def test_gold_sql_file_sends_file_content_verbatim(self, monkeypatch, tmp_path):
        # The whole point of the flag: multi-line SQL with characters the
        # shell would mangle inline (#, quotes, newlines) arrives untouched.
        sql = "-- monthly refunds\nSELECT SUM(amount) # cents\nFROM refunds\nWHERE note = 'it''s fine'\n"
        sql_file = tmp_path / "gold.sql"
        sql_file.write_text(sql, encoding="utf-8")
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={"id": "019f0000-0000-7000-8000-0000000000ca", "question": "Refunds?", "gold_sql": sql},
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(
            app,
            [
                "eval",
                "add-case",
                "--project",
                PROJECT_ID,
                "--api-key",
                "sk-k6-test",
                "-q",
                "Refunds?",
                "--gold-sql-file",
                str(sql_file),
            ],
        )

        assert result.exit_code == 0, result.output
        assert seen["body"]["gold_sql"] == sql

    def test_both_gold_sql_flags_is_usage_error(self, tmp_path):
        sql_file = tmp_path / "gold.sql"
        sql_file.write_text("SELECT 1\n")

        result = runner.invoke(app, _args("--gold-sql-file", str(sql_file)))

        assert result.exit_code == 2
        assert "at most one" in result.output

    def test_neither_gold_sql_flag_is_usage_error(self):
        result = runner.invoke(
            app, ["eval", "add-case", "--project", PROJECT_ID, "--api-key", "sk-k6-test", "-q", "Refunds?"]
        )
        assert result.exit_code == 2
        assert "Pass --gold-sql or --gold-sql-file" in result.output

    def test_unreadable_gold_sql_file_is_usage_error(self, tmp_path):
        result = runner.invoke(
            app,
            [
                "eval",
                "add-case",
                "--project",
                PROJECT_ID,
                "--api-key",
                "sk-k6-test",
                "-q",
                "Refunds?",
                "--gold-sql-file",
                str(tmp_path / "missing.sql"),
            ],
        )
        assert result.exit_code == 2
        assert "Cannot read" in result.output

    def test_json_output(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                json={
                    "id": "019f0000-0000-7000-8000-0000000000ca",
                    "question": "Net revenue last month?",
                    "gold_sql": "SELECT 1",
                },
            )

        _mock_api(monkeypatch, handler)

        result = runner.invoke(app, _args("--json"))

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["gold_sql"] == "SELECT 1"

    def test_compare_words_round_trip_and_reject_ignored_settings(self, monkeypatch):
        seen = {}
        acknowledged = True

        def handler(request):
            body = json.loads(request.content)
            seen.update(body)
            response = {"id": "case", **body}
            if not acknowledged:
                response.pop("comparison")
            return httpx.Response(201, json=response)

        _mock_api(monkeypatch, handler)
        flags = ("--compare", "exact, ordered,allow-empty")
        result = runner.invoke(app, _args(*flags))
        assert result.exit_code == 0, result.output
        assert seen["comparison"] == {
            "numeric": "exact",
            "ordered": True,
            "columns": "automatic",
            "allow_empty": True,
        }
        assert "exact numbers" in result.output
        acknowledged = False
        result = runner.invoke(app, _args(*flags, "--json"))
        assert result.exit_code == 3
        assert "did not acknowledge" in result.output

    def test_invalid_compare_words_do_not_write(self, monkeypatch):
        def handler(request):
            raise AssertionError("Invalid comparison must not reach the API")

        _mock_api(monkeypatch, handler)
        for flags in [
            ("--compare", "relative"),
            ("--compare", "default,exact"),
            ("--compare", ""),
        ]:
            result = runner.invoke(app, _args(*flags))
            assert result.exit_code == 2, result.output
