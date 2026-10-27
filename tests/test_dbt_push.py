"""`cassis dbt push` and `check`: read and gzip target/, send it with the git context, wait for the outcome."""

import json
from typing import Any

import httpx
import pytest
from cassis_cli.api import get_dbt_observation, post_dbt_observation
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()
PROJECT = "11111111-1111-1111-1111-111111111111"
OBSERVATION = "22222222-2222-2222-2222-222222222222"

_RECONCILIATION = {
    "relation_count": 8,
    "relations": {"matched": 5, "not_in_source_schema": 3, "no_relation": 0},
    "test_count": 20,
    "catalog": True,
    "catalog_error_count": 0,
}


def _record(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": OBSERVATION,
        "status": status,
        "dbt_project_name": "jaffle_shop",
        "dbt_version": "1.10.23",
        "reconciliation": None if status == "processing" else _RECONCILIATION,
        "detail": None,
        **extra,
    }


class Server:
    """Answers the two dbt routes; records what the upload sent."""

    def __init__(self, final: dict[str, Any], *, upload_status: int = 202, upload_body: Any = None) -> None:
        self.final = final
        self.upload_status = upload_status
        self.upload_body = upload_body
        self.uploads: list[httpx.Request] = []
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == f"/api/ci/projects/{PROJECT}/dbt/observations":
            self.uploads.append(request)
            return httpx.Response(self.upload_status, json=self.upload_body or _record("processing"))
        if request.method == "GET" and path == f"/api/ci/projects/{PROJECT}/dbt/observations/{OBSERVATION}":
            self.polls += 1
            return httpx.Response(200, json=_record("processing") if self.polls == 1 else self.final)
        raise AssertionError(f"unexpected {request.method} {path}")

    @property
    def body(self) -> bytes:
        return self.uploads[-1].read()


def route(monkeypatch, server: Server) -> None:
    for name, original in (
        ("post_dbt_observation", post_dbt_observation),
        ("get_dbt_observation", get_dbt_observation),
    ):

        def patched(original=original, **kwargs):
            kwargs["transport"] = httpx.MockTransport(server)
            return original(**kwargs)

        monkeypatch.setattr(f"cassis_cli.dbt.{name}", patched)
    monkeypatch.setattr("cassis_cli.common.time.sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _no_ci_env(monkeypatch):
    for name in (
        "CI_MERGE_REQUEST_TITLE",
        "CI_MERGE_REQUEST_DESCRIPTION",
        "GITHUB_EVENT_PATH",
        "GITHUB_EVENT_NAME",
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def dbt_repo(tmp_path, commit_all, monkeypatch):
    """A git checkout of a dbt project, with dbt's target/ written and HEAD committed."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps({"metadata": {"generated_at": "2026-10-08T00:00:00Z"}}))
    (target / "catalog.json").write_text("{}")
    (tmp_path / "dbt_project.yml").write_text("name: shop\n")
    sha = commit_all(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path, sha


def _push(*more: str):
    return runner.invoke(app, ["dbt", "push", "--api-key", "sk-k6-test", "--project", PROJECT, *more])


def test_an_accepted_push_sends_the_files_and_the_git_context(dbt_repo, monkeypatch):
    _, sha = dbt_repo
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    result = _push()

    assert result.exit_code == 0, result.output
    body = server.body
    assert b'name="manifest"; filename="manifest.json.gz"' in body
    assert b'name="catalog"; filename="catalog.json.gz"' in body
    assert sha.encode() in body
    assert b'name="git_commit_message"' in body and b"test" in body
    assert "Accepted as the data source's dbt baseline (jaffle_shop, dbt 1.10.23)" in result.output
    assert "5 matched to a table of the data source" in result.output
    assert "3 not in the data source's schema" in result.output
    assert "Tests: 20 declared" in result.output
    assert "No catalog.json" not in result.output


def test_a_catalog_with_errors_is_called_out(dbt_repo, monkeypatch):
    reconciliation = {**_RECONCILIATION, "catalog_error_count": 2, "relations": {"matched": 4, "catalog_error": 1}}
    route(monkeypatch, Server(_record("accepted", reconciliation=reconciliation)))

    result = _push()

    assert result.exit_code == 0, result.output
    assert "1 missing from catalog.json, which reported errors" in result.output
    assert "catalog.json reported 2 error(s)" in result.output


def test_waiting_gives_up_at_the_timeout(dbt_repo, monkeypatch):
    """`--timeout 0` polls once; an upload still processing then exits 3 with the upload id to come back to."""
    route(monkeypatch, Server(_record("processing")))

    result = _push("--timeout", "0")

    assert result.exit_code == 3
    assert f"Timed out after 0s: upload {OBSERVATION} is still processing" in result.output


def test_an_artifact_over_the_compressed_cap_is_a_usage_error(dbt_repo, monkeypatch):
    server = Server(_record("accepted"))
    route(monkeypatch, server)
    monkeypatch.setattr("cassis_cli.dbt.MAX_COMPRESSED_BYTES", 10)

    result = _push()

    assert result.exit_code == 2
    assert "MB" in result.output and server.uploads == []


@pytest.mark.parametrize(
    ("status", "exit_code", "message"),
    [
        ("superseded", 0, "Kept, but not the baseline."),
        ("rejected", 1, "Rejected: no relation matches"),
        ("failed", 3, "Processing failed"),
    ],
)
def test_the_outcome_sets_the_exit_code(dbt_repo, monkeypatch, status, exit_code, message):
    route(monkeypatch, Server(_record(status, detail="no relation matches")))

    result = _push()

    assert result.exit_code == exit_code
    assert message in result.output


def test_a_superseded_push_says_not_the_baseline_once(dbt_repo, monkeypatch):
    detail = (
        "Its manifest (generated 2026-10-01T00:00:00+00:00) is older than the accepted upload's "
        "(generated 2026-10-08T00:00:00+00:00, commit 0123456789ab); nothing changed."
    )
    route(monkeypatch, Server(_record("superseded", detail=detail)))

    result = _push()

    assert result.exit_code == 0
    assert f"Kept, but not the baseline. {detail}" in result.output
    assert result.output.lower().count("not the baseline") == 1


def test_an_accepted_push_since_replaced_says_so(dbt_repo, monkeypatch):
    route(monkeypatch, Server(_record("accepted", is_baseline=False)))

    result = _push()

    assert result.exit_code == 0
    assert "but a newer upload has since replaced it as the dbt baseline" in result.output


def test_json_keeps_stdout_to_the_record(dbt_repo, monkeypatch):
    route(monkeypatch, Server(_record("accepted")))

    result = runner.invoke(
        app, ["dbt", "push", "--api-key", "k", "--project", PROJECT, "--json"], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "accepted"


def test_a_refused_upload_exits_one_with_the_server_message(dbt_repo, monkeypatch):
    route(monkeypatch, Server({}, upload_status=413, upload_body={"detail": "manifest.json is larger than 32 MB"}))

    result = _push()

    assert result.exit_code == 1
    assert "Cassis did not accept the upload: manifest.json is larger than 32 MB" in result.output


def test_an_old_server_says_so(dbt_repo, monkeypatch):
    route(monkeypatch, Server({}, upload_status=404, upload_body={"detail": "Not Found"}))

    result = _push()

    assert result.exit_code == 3
    assert "does not accept dbt uploads yet" in result.output


def test_a_missing_upload_is_not_mistaken_for_an_old_server(dbt_repo, monkeypatch):
    route(monkeypatch, Server({}, upload_status=404, upload_body={"detail": "dbt upload not found"}))

    result = _push()

    assert result.exit_code == 3
    assert "was not found in this project" in result.output
    assert "does not accept dbt uploads yet" not in result.output


def test_a_target_without_manifest_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "target").mkdir()

    result = _push("--commit", "abc1234")

    assert result.exit_code == 2
    assert "No manifest.json in target" in result.output


def test_outside_a_checkout_the_commit_is_required(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "manifest.json").write_text("{}")

    assert _push().exit_code == 2
    assert _push("--commit", "main").exit_code == 2


def test_the_merge_request_title_comes_from_gitlab_ci(dbt_repo, monkeypatch):
    monkeypatch.setenv("CI_MERGE_REQUEST_TITLE", "Rename orders mart")
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    assert _push().exit_code == 0
    assert b"Rename orders mart" in server.body


def test_the_pull_request_comes_from_the_github_event(dbt_repo, monkeypatch, tmp_path_factory):
    event = tmp_path_factory.mktemp("gh") / "event.json"
    event.write_text(json.dumps({"pull_request": {"title": "Add payments", "body": "Why: finance asked"}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    assert _push().exit_code == 0
    assert b"Add payments" in server.body and b"Why: finance asked" in server.body


def _github_push(monkeypatch, tmp_path_factory, *, token: "str | None" = "ghs_test") -> None:
    event = tmp_path_factory.mktemp("gh") / "event.json"
    event.write_text(json.dumps({"ref": "refs/heads/main", "head_commit": {"message": "Merge pull request #7"}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/dbt")
    if token:
        monkeypatch.setenv("GITHUB_TOKEN", token)


def _field(body: bytes, name: str) -> "str | None":
    marker = f'name="{name}"\r\n\r\n'.encode()
    if marker not in body:
        return None
    return body.split(marker, 1)[1].split(b"\r\n--", 1)[0].decode()


def test_after_a_github_merge_the_pull_request_comes_from_the_api(dbt_repo, monkeypatch, tmp_path_factory):
    _, sha = dbt_repo
    _github_push(monkeypatch, tmp_path_factory)
    asked: "list[tuple[str, dict[str, str]]]" = []

    def fake_get(url, *, headers, timeout):
        asked.append((url, headers))
        assert timeout <= 10
        pulls = [
            {"title": "Still open", "body": None, "merged_at": None, "merge_commit_sha": None},
            {"title": "Merged before", "body": "older", "merged_at": "2026-10-01", "merge_commit_sha": "0" * 40},
            {
                "title": "Rename channel column",
                "body": "SOURCE_CHANNEL replaces CHANNEL",
                "merged_at": "2026-10-20",
                "merge_commit_sha": sha,
            },
        ]
        return httpx.Response(200, json=pulls)

    monkeypatch.setattr("cassis_cli.dbt.httpx.get", fake_get)
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    assert _push().exit_code == 0
    [(url, headers)] = asked
    assert url == f"https://api.github.com/repos/acme/dbt/commits/{sha}/pulls"
    assert headers["Authorization"] == "Bearer ghs_test"
    assert _field(server.body, "pr_title") == "Rename channel column"
    assert _field(server.body, "pr_description") == "SOURCE_CHANNEL replaces CHANNEL"


@pytest.mark.parametrize(
    "pulls",
    [
        # Merged, but by another commit: the PR merged before this one that also contains it.
        [{"title": "Older", "body": "x", "merged_at": "2026-10-01", "merge_commit_sha": "0" * 40}],
        # Still open: it contains the commit but merged nothing yet.
        [{"title": "Open", "body": "x", "merged_at": None, "merge_commit_sha": "MERGED_SHA"}],
        [],
    ],
)
def test_no_pull_request_merged_as_the_commit_sends_no_title(dbt_repo, monkeypatch, tmp_path_factory, pulls):
    _, sha = dbt_repo
    _github_push(monkeypatch, tmp_path_factory)
    for pull in pulls:
        if pull["merge_commit_sha"] == "MERGED_SHA":
            pull["merge_commit_sha"] = sha
    monkeypatch.setattr("cassis_cli.dbt.httpx.get", lambda url, **kwargs: httpx.Response(200, json=pulls))
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    assert _push().exit_code == 0
    assert _field(server.body, "pr_title") is None
    assert _field(server.body, "pr_description") is None


def test_a_malformed_github_api_url_never_stops_the_push(dbt_repo, monkeypatch, tmp_path_factory):
    _github_push(monkeypatch, tmp_path_factory)
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com:notaport")
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    result = _push()

    assert result.exit_code == 0, result.output
    assert _field(server.body, "pr_title") is None


def test_a_failing_github_api_never_stops_the_push(dbt_repo, monkeypatch, tmp_path_factory):
    _github_push(monkeypatch, tmp_path_factory)

    def failing_get(url, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr("cassis_cli.dbt.httpx.get", failing_get)
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    assert _push().exit_code == 0
    assert _field(server.body, "pr_title") is None


def test_without_a_github_token_the_api_is_not_asked(dbt_repo, monkeypatch, tmp_path_factory):
    _github_push(monkeypatch, tmp_path_factory, token=None)

    def unexpected_get(url, **kwargs):
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr("cassis_cli.dbt.httpx.get", unexpected_get)
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    assert _push().exit_code == 0
    assert _field(server.body, "pr_title") is None


@pytest.mark.parametrize(
    ("message", "title"),
    [
        (
            "Merge branch 'rename-channel' into 'main'\n\nRename channel column\n\n"
            "Closes #12\n\nSee merge request data/dbt!42",
            "Rename channel column",
        ),
        ("Rename channel column (!42)\n\nSee merge request data/dbt!42\n", "Rename channel column (!42)"),
        ("Rename channel column", None),
    ],
)
def test_a_gitlab_merge_commit_gives_its_merge_request_title(dbt_repo, monkeypatch, message, title):
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    assert _push("--commit-message", message).exit_code == 0
    assert _field(server.body, "pr_title") == title


def test_an_oversized_artifact_is_refused_before_sending(dbt_repo, monkeypatch):
    monkeypatch.setattr("cassis_cli.dbt.MAX_UNCOMPRESSED_BYTES", 10)
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    result = _push()

    assert result.exit_code == 2
    assert "Cassis accepts up to" in result.output
    assert server.uploads == []


_COLUMN_GONE = {
    "source": "column_removed",
    "changed_node": "model.shop.orders",
    "model": "orders",
    "column": "status",
    "context_object": {"kind": "column", "name": "analytics.orders.status"},
    "change": "Column `status` is gone from orders.",
    "why_it_matters": "Queries filtering on status will fail.",
    "proposed_change": "Remove analytics.orders.status from the context.",
    "confidence": 1.0,
    "reach_count": 3,
    "issue_id": None,
    "issue_outcome": None,
}
_REVIEWED = {
    **_COLUMN_GONE,
    "source": "review",
    "changed_node": "model.shop.stg_orders",
    "model": "stg_orders",
    "column": None,
    "context_object": {"kind": "metric", "name": "return_rate"},
    "change": "stg_orders now drops returned orders.",
    "why_it_matters": "return_rate is now always 0.",
    "proposed_change": "Redefine return_rate on a relation that keeps returns.",
    "confidence": 0.8,
    "reach_count": 25,
}
_REVIEW = {
    "status": "ok",
    "detail": None,
    "changed_relations": 2,
    "added_relations": 0,
    "affected_tables": 3,
    "reviewed_tables": ["analytics.orders", "analytics.customers"],
    "not_reviewed_tables": ["analytics.payments"],
    "reach_window_days": 30,
}


def _check(*more: str):
    return runner.invoke(app, ["dbt", "check", "--api-key", "sk-k6-test", "--project", PROJECT, *more])


def test_a_check_prints_findings_by_context_object_then_the_removals(dbt_repo, monkeypatch):
    server = Server(
        _record("checked", purpose="check", findings=[_COLUMN_GONE, _REVIEWED], review=_REVIEW),
        upload_body=_record("processing", purpose="check"),
    )
    route(monkeypatch, server)

    result = _check()

    assert result.exit_code == 0, result.output
    assert b'name="purpose"\r\n\r\ncheck' in server.body
    output = result.output
    assert "Checked against the dbt baseline: 2 finding(s)." in output
    assert "Not reviewed (over the review's size budget): analytics.payments" in output
    assert output.index("return_rate (metric)") < output.index("Removed from dbt:")
    assert output.index("Removed from dbt:") < output.index("analytics.orders.status (column)")
    assert "Proposed change: Redefine return_rate" in output
    assert "[model.shop.stg_orders] (confidence 0.80)" in output
    assert "return_rate (metric) · reached by 25 answers in the last 30 days" in output
    assert "analytics.orders.status (column) · reached by 3 answers in the last 30 days" in output


def test_a_check_prints_the_most_reached_context_object_first_and_keeps_the_server_order_on_ties(dbt_repo, monkeypatch):
    cold = {**_REVIEWED, "context_object": {"kind": "table", "name": "analytics.customers"}, "reach_count": 1}
    unsure = {**_REVIEWED, "context_object": {"kind": "join", "name": "a -> b"}, "confidence": 0.3}
    unmeasured = {**_REVIEWED, "context_object": {"kind": "table", "name": "analytics.legacy"}, "reach_count": None}
    server = Server(
        _record("checked", purpose="check", findings=[unmeasured, cold, unsure, _REVIEWED], review=_REVIEW),
        upload_body=_record("processing", purpose="check"),
    )
    route(monkeypatch, server)

    result = _check()

    assert result.exit_code == 0, result.output
    output = result.output
    # Equal reach: the less confident join stays before the metric, as the server sent them.
    assert output.index("a -> b (join)") < output.index("return_rate (metric)")
    assert output.index("return_rate (metric)") < output.index("analytics.customers (table)")
    assert output.index("analytics.customers (table)") < output.index("analytics.legacy (table)")
    assert "reached by 1 answer in the last 30 days" in output
    assert "analytics.legacy (table)\n" in output


def test_a_check_without_a_baseline_exits_1(dbt_repo, monkeypatch):
    detail = "No dbt baseline to compare with yet: run `cassis dbt push` on your production branch first."
    server = Server(
        _record("rejected", purpose="check", reconciliation=None, detail=detail),
        upload_body=_record("processing", purpose="check"),
    )
    route(monkeypatch, server)

    result = _check()

    assert result.exit_code == 1
    assert "run `cassis dbt push` on your production branch first" in result.output


def test_a_check_against_a_server_without_checks_stops(dbt_repo, monkeypatch):
    route(monkeypatch, Server(_record("accepted")))

    result = _check()

    assert result.exit_code == 3
    assert "does not support `cassis dbt check`" in result.output


def test_an_accepted_push_lists_the_issues_it_filed(dbt_repo, monkeypatch):
    issue_id = "33333333-3333-3333-3333-333333333333"
    filed = {**_COLUMN_GONE, "issue_id": issue_id, "issue_outcome": "filed"}
    server = Server(_record("accepted", findings=[filed], review={**_REVIEW, "not_reviewed_tables": []}))
    route(monkeypatch, server)

    result = _push("--app-url", "https://app.example.com")

    assert result.exit_code == 0, result.output
    assert "1 finding(s) filed as issues:" in result.output
    assert (
        f"{issue_id}  (new)  analytics.orders.status: Column `status` is gone from orders."
        " · reached by 3 answers in the last 30 days"
    ) in result.output
    assert f"https://app.example.com/context?projectId={PROJECT}&tab=review&issueId={issue_id}" in result.output


def test_a_catalog_older_than_the_manifest_is_skipped(dbt_repo, monkeypatch):
    root, _ = dbt_repo
    (root / "target" / "catalog.json").write_text(json.dumps({"metadata": {"generated_at": "2026-10-01T00:00:00Z"}}))
    server = Server(_record("accepted"))
    route(monkeypatch, server)

    result = _push()

    assert result.exit_code == 0, result.output
    assert b'name="catalog"' not in server.body
    assert "generated at 2026-10-01T00:00:00Z, before manifest.json (2026-10-08T00:00:00Z)" in result.output


def test_a_validation_error_prints_field_and_message(dbt_repo, monkeypatch):
    detail = [{"loc": ["body", "purpose"], "msg": "Input should be 'push' or 'check'", "type": "enum"}]
    route(monkeypatch, Server({}, upload_status=422, upload_body={"detail": detail}))

    result = _push()

    assert result.exit_code == 1
    assert "Cassis did not accept the upload: purpose: Input should be 'push' or 'check'" in result.output
