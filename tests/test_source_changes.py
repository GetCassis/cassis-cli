import json

import httpx
from cassis_cli.api import get_source_change, get_source_changes
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
CHANGE_ID = "019f0000-0000-7000-8000-0000000000d1"

_ITEM = {
    "id": CHANGE_ID,
    "change_type": "column_removed",
    "severity": "breaking",
    "status": "pending",
    "target_schema": "public",
    "target_table": "orders",
    "target_column": "amount",
    "times_raised": 2,
    "last_detected_at": "2026-08-18T06:00:00Z",
    "has_suggested_edit": True,
    "suggested_edit_summary": "Remove orders.amount",
}

_DETAIL = {
    **_ITEM,
    "payload": {"column_type": "numeric"},
    "impact": [{"kind": "metric", "confidence": "exact", "object_label": "total_revenue", "detail": "SUM(amount)"}],
    "suggested_edit": {
        "operations": [{"op": "remove_column"}],
        "human_summary": "Remove orders.amount",
        "manual_review": ["metric total_revenue references it"],
    },
}


def _two_items():
    return [_ITEM, {**_ITEM, "id": CHANGE_ID[:-1] + "2", "target_column": "currency"}]


def _mock(monkeypatch, name, original, handler):
    def patched(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr(f"cassis_cli.source_changes.{name}", patched)


def _args(*extra):
    return [*extra, "--project", PROJECT_ID, "--api-key", "sk-k6-test"]


class TestSourceChangesList:
    def test_prints_one_line_per_change_and_pagination_hint(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"items": [_ITEM], "total": 7})

        _mock(monkeypatch, "get_source_changes", get_source_changes, handler)

        result = runner.invoke(app, _args("source-changes", "list", "--limit", "1"))

        assert result.exit_code == 0, result.output
        assert f"/api/ci/projects/{PROJECT_ID}/source-changes" in seen["url"]
        assert "limit=1" in seen["url"]
        assert f"{CHANGE_ID}  column_removed  breaking  pending  public.orders.amount" in result.output
        assert "Showing 1 of 7 (use --offset 1 for the next page)." in result.output

    def test_last_page_omits_pagination_hint(self, monkeypatch):
        # offset + shown == total: the tail of the queue, nothing to page to.
        _mock(
            monkeypatch,
            "get_source_changes",
            get_source_changes,
            lambda request: httpx.Response(200, json={"items": _two_items(), "total": 7}),
        )

        result = runner.invoke(app, _args("source-changes", "list", "--limit", "5", "--offset", "5"))

        assert result.exit_code == 0, result.output
        assert "next page" not in result.output

    def test_middle_page_hint_points_past_the_page(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_source_changes",
            get_source_changes,
            lambda request: httpx.Response(200, json={"items": _two_items(), "total": 7}),
        )

        result = runner.invoke(app, _args("source-changes", "list", "--limit", "2", "--offset", "2"))

        assert result.exit_code == 0, result.output
        assert "Showing 2 of 7 (use --offset 4 for the next page)." in result.output

    def test_empty_page_reports_no_changes(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_source_changes",
            get_source_changes,
            lambda request: httpx.Response(200, json={"items": [], "total": 0}),
        )

        result = runner.invoke(app, _args("source-changes", "list"))

        assert result.exit_code == 0, result.output
        assert "No pending source changes." in result.output

    def test_empty_filtered_page_reports_no_match(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_source_changes",
            get_source_changes,
            lambda request: httpx.Response(200, json={"items": [], "total": 0}),
        )

        result = runner.invoke(app, _args("source-changes", "list", "--status", "approved"))

        assert result.exit_code == 0, result.output
        assert "No source changes match." in result.output

    def test_json_output_prints_raw_page(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_source_changes",
            get_source_changes,
            lambda request: httpx.Response(200, json={"items": [_ITEM], "total": 1}),
        )

        result = runner.invoke(app, _args("source-changes", "list", "--json"))

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"items": [_ITEM], "total": 1}

    def test_rejects_unknown_status(self, monkeypatch):
        result = runner.invoke(app, _args("source-changes", "list", "--status", "bogus"))
        assert result.exit_code == 2


class TestSourceChangesShow:
    def test_prints_impact_and_suggested_edit(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_source_change",
            get_source_change,
            lambda request: httpx.Response(200, json=_DETAIL),
        )

        result = runner.invoke(app, _args("source-changes", "show", CHANGE_ID))

        assert result.exit_code == 0, result.output
        assert "Target: public.orders.amount" in result.output
        assert "metric  exact  total_revenue" in result.output
        assert "Suggested edit: Remove orders.amount" in result.output
        assert "Manual review: metric total_revenue references it" in result.output

    def test_prints_header_only_when_impact_and_edit_are_absent(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_source_change",
            get_source_change,
            lambda request: httpx.Response(200, json=_ITEM),
        )

        result = runner.invoke(app, _args("source-changes", "show", CHANGE_ID))

        assert result.exit_code == 0, result.output
        assert "Target: public.orders.amount" in result.output
        assert "Raised 2x, last 2026-08-18T06:00:00Z" in result.output
        assert "Impact:" not in result.output
        assert "Suggested edit:" not in result.output

    def test_missing_change_exits_1_on_the_wire_contract(self, monkeypatch):
        # The endpoint's exact 404 detail separates "no such change" (exit 1)
        # from a project-scope 404 (exit 3) — the same contract as issues.
        _mock(
            monkeypatch,
            "get_source_change",
            get_source_change,
            lambda request: httpx.Response(404, json={"detail": "Source change not found"}),
        )

        result = runner.invoke(app, _args("source-changes", "show", CHANGE_ID))

        assert result.exit_code == 1

    def test_project_scope_404_exits_3(self, monkeypatch):
        _mock(
            monkeypatch,
            "get_source_change",
            get_source_change,
            lambda request: httpx.Response(404, json={"detail": "Project not found"}),
        )

        result = runner.invoke(app, _args("source-changes", "show", CHANGE_ID))

        assert result.exit_code == 3
