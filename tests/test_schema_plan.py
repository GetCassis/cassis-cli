import json

import httpx
import pytest
from cassis_cli.api import (
    get_ontology_export,
    get_schema_plan,
    get_schema_plan_checkout,
    post_ontology_import,
    post_schema_plan,
    post_schema_plan_apply,
    post_schema_plan_warehouse,
)
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

_PROJECT_ID = "019f0000-0000-7000-8000-000000000000"
_PLAN_ID = "019f0000-0000-7000-8000-00000000d001"
_DDL = "CREATE TABLE public.refunds (id INTEGER PRIMARY KEY, amount INTEGER NOT NULL);"

_PROJECT_YML = f"cassis_format_version: 2\nproject_id: {_PROJECT_ID}\n"
# The app's current ontology (what /ontology/export returns) and the tree the plan renders.
_REMOTE = {
    "project.yml": _PROJECT_YML,
    "tables/public/orders.yml": "schema_name: public\ntable_name: orders\ncolumns:\n- name: id\n- name: legacy\n",
    "domains/README.md": "# Root\n",
}
_TREE = {
    "project.yml": _PROJECT_YML,
    "tables/public/orders.yml": "schema_name: public\ntable_name: orders\ncolumns:\n- name: id\n- name: amount\n",
    "domains/README.md": "# Root\n",
}
_READY_DOC = {
    "schema_diff": {
        "tables": [
            {
                "schema_name": "public",
                "table_name": "orders",
                "kind": "modified",
                "in_ontology": True,
                "columns": [{"name": "legacy", "kind": "removed", "old_type": "integer"}],
            },
            {"schema_name": "public", "table_name": "refunds", "kind": "added", "column_count": 2},
        ],
        "truncated": False,
        "summary": {},
    },
    "ontology_changes": [
        {
            "op": "drop_column",
            "schema_name": "public",
            "table_name": "orders",
            "column_name": "legacy",
            "loses_curation": True,
            "cascade": [{"kind": "metric", "object_label": "legacy_total", "confidence": "exact", "detail": ""}],
            "rewrites": [],
            "affects": [],
        },
    ],
    "warnings": ["something"],
    "summary": {"ontology_drop_column": 1, "ontology_changes": 1, "cascaded_objects": 1},
}
_EMPTY_DOC = {
    "schema_diff": {"tables": [], "truncated": False, "summary": {}},
    "ontology_changes": [],
    "warnings": [],
    "summary": {},
}


@pytest.fixture
def repo(tmp_path):
    ontology_dir = tmp_path / "cassis"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "project.yml").write_text(_PROJECT_YML)
    return tmp_path


@pytest.fixture
def ddl_file(tmp_path):
    f = tmp_path / "schema.sql"
    f.write_text(_DDL, encoding="utf-8")
    return f


def _record(status, **extra):
    base = {
        "id": _PLAN_ID,
        "status": status,
        "complete_source": True,
        "error": None,
        "document": None,
        "summary": None,
        "base_ontology_fingerprint": "fp-1",
    }
    base.update(extra)
    return base


def _write_tree(repo, tree):
    for rel, content in tree.items():
        (repo / "cassis" / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / "cassis" / rel).write_text(content)


def make_router(
    *,
    plan_status="ready",
    document=_READY_DOC,
    apply_status="applied",
    tree=_TREE,
    remote=_REMOTE,
    fingerprint="fp-1",
    error=None,
    seen=None,
):
    """One handler for the whole flow: export, plan, checkout, apply, ontology import."""
    state = {"applying": False}

    def handler(request):
        assert request.headers["Authorization"] == "Bearer sk-k6-test"
        path = request.url.path
        if seen is not None:
            seen.append((request.method, path, request.content))
        if request.method == "GET" and path.endswith("/ontology/export"):
            return httpx.Response(200, json={"files": remote})
        if request.method == "POST" and path.endswith("/schema/plans"):
            return httpx.Response(202, json=_record("planning"))
        if request.method == "POST" and path.endswith("/schema/plans/warehouse"):
            return httpx.Response(202, json=_record("planning"))
        if request.method == "GET" and path.endswith("/checkout"):
            return httpx.Response(200, json={"files": tree, "warnings": []})
        if request.method == "POST" and path.endswith("/apply"):
            state["applying"] = True
            return httpx.Response(202, json=_record("applying"))
        if request.method == "POST" and path.endswith("/ontology/import"):
            body = json.loads(request.content)
            assert set(body["files"]) >= set(tree)
            return httpx.Response(
                200,
                json={
                    "table_count": 1,
                    "domain_count": 1,
                    "join_count": 0,
                    "metric_count": 0,
                    "published_version": 2 if body.get("publish") else None,
                },
            )
        if request.method == "GET":
            status = apply_status if state["applying"] else plan_status
            return httpx.Response(
                200,
                json=_record(
                    status,
                    document=document,
                    summary=document["summary"] if document else None,
                    apply_result={"schema_version": 2, "warnings": []},
                    base_ontology_fingerprint=fingerprint,
                    error=error,
                ),
            )
        raise AssertionError(f"unexpected {request.method} {path}")

    return handler


def route(monkeypatch, handler):
    for name, original in (
        ("post_schema_plan", post_schema_plan),
        ("post_schema_plan_warehouse", post_schema_plan_warehouse),
        ("get_schema_plan", get_schema_plan),
        ("post_schema_plan_apply", post_schema_plan_apply),
        ("get_schema_plan_checkout", get_schema_plan_checkout),
        ("post_ontology_import", post_ontology_import),
        ("get_ontology_export", get_ontology_export),
    ):

        def patched(original=original, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(**kwargs)

        monkeypatch.setattr(f"cassis_cli.schema.{name}", patched)
    monkeypatch.setattr("cassis_cli.common.time.sleep", lambda seconds: None)


def _args(*more):
    return ["--api-key", "sk-k6-test", *more]


class TestSchemaPlan:
    def test_plan_renders_and_exits_zero(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router())
        result = runner.invoke(app, ["schema", "plan", str(ddl_file), "--path", str(repo), *_args()])
        assert result.exit_code == 0, result.output
        assert "Schema plan started" in result.output
        assert "- drop column public.orders.legacy" in result.output and "loses description" in result.output
        assert "also removes: metric legacy_total" in result.output
        assert "+ public.refunds" in result.output
        assert "Plan: 0 to add, 0 to change, 1 to remove" in result.output
        assert "✓ Plan ready" in result.output

    def test_plan_json_keeps_stdout_to_the_record(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router())
        result = runner.invoke(app, ["schema", "plan", str(ddl_file), "--path", str(repo), *_args("--json")])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["status"] == "ready"
        assert "Ontology changes" in result.stderr

    def test_plan_failure_exits_one_with_the_server_message(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router(plan_status="failed", document=None, error="Is the file complete?"))
        result = runner.invoke(app, ["schema", "plan", str(ddl_file), "--path", str(repo), *_args()])
        assert result.exit_code == 1 and "Plan failed: Is the file complete?" in result.output

    def test_warehouse_plan_posts_no_ddl(self, repo, monkeypatch):
        seen = []
        route(monkeypatch, make_router(seen=seen))
        result = runner.invoke(app, ["schema", "plan", "--warehouse", "--path", str(repo), *_args()])
        assert result.exit_code == 0, result.output
        posts = [(p, c) for m, p, c in seen if m == "POST"]
        assert [p.rsplit("/", 1)[-1] for p, _ in posts] == ["warehouse"] and posts[0][1] == b""

    def test_plan_without_a_source_is_a_usage_error(self, repo, monkeypatch):
        route(monkeypatch, make_router())
        result = runner.invoke(app, ["schema", "plan", "--path", str(repo), *_args()])
        assert result.exit_code == 2 and "exactly one of a DDL file or --warehouse" in result.output

    def test_warehouse_and_ddl_together_is_a_usage_error(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router())
        result = runner.invoke(app, ["schema", "plan", str(ddl_file), "--warehouse", "--path", str(repo), *_args()])
        assert result.exit_code == 2

    def test_complete_flag_rides_the_request(self, repo, ddl_file, monkeypatch):
        seen = []
        route(monkeypatch, make_router(seen=seen))
        runner.invoke(app, ["schema", "plan", str(ddl_file), "--path", str(repo), *_args("--complete")])
        runner.invoke(app, ["schema", "plan", str(ddl_file), "--path", str(repo), *_args()])
        bodies = [json.loads(c) for m, p, c in seen if m == "POST" and p.endswith("/schema/plans")]
        assert [b["complete_source"] for b in bodies] == [True, False]

    def test_old_server_is_a_transport_error_with_upgrade_hint(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Not Found"}))
        result = runner.invoke(app, ["schema", "plan", str(ddl_file), "--path", str(repo), *_args()])
        assert result.exit_code == 3 and "upgrade the server" in result.output


class TestSchemaApply:
    def test_apply_writes_the_rendered_tree_locally_and_leaves_the_app_alone(self, repo, ddl_file, monkeypatch):
        seen = []
        route(monkeypatch, make_router(seen=seen))
        _write_tree(repo, _REMOTE)  # checkout in sync with the app

        result = runner.invoke(app, ["schema", "apply", str(ddl_file), "--path", str(repo), *_args()])

        assert result.exit_code == 0, result.output
        assert (repo / "cassis" / "tables" / "public" / "orders.yml").read_text() == _TREE["tables/public/orders.yml"]
        assert "Wrote 1 ontology file(s)" in result.output and "The app is unchanged" in result.output
        assert "cassis schema push" in result.output
        marker = json.loads((repo / "cassis" / ".schema-apply.json").read_text())
        assert marker["base_ontology_fingerprint"] == "fp-1" and marker["plan_id"] == _PLAN_ID
        assert ".schema-apply.json" in (repo / "cassis" / ".gitignore").read_text()
        assert not any(m == "POST" and p.endswith("/apply") for m, p, _c in seen)
        assert not any(p.endswith("/ontology/import") for _m, p, _c in seen)

    def test_apply_refuses_a_checkout_that_differs_from_the_app(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router())
        _write_tree(repo, {"tables/public/orders.yml": "edited locally\n"})

        result = runner.invoke(app, ["schema", "apply", str(ddl_file), "--path", str(repo), *_args()])

        assert result.exit_code == 1
        assert "differs from the app" in result.output and "cassis ontology pull" in result.output
        assert (repo / "cassis" / "tables" / "public" / "orders.yml").read_text() == "edited locally\n"

    def test_apply_force_overwrites_and_prunes_git_clean_files_only(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router())
        _write_tree(repo, {"tables/public/orders.yml": "edited locally\n", "tables/public/stale.yml": "old\n"})

        result = runner.invoke(app, ["schema", "apply", str(ddl_file), "--path", str(repo), *_args("--force")])

        assert result.exit_code == 0, result.output
        assert (repo / "cassis" / "tables" / "public" / "orders.yml").read_text() == _TREE["tables/public/orders.yml"]
        assert (repo / "cassis" / "tables" / "public" / "stale.yml").exists()  # untracked → kept

    def test_apply_existing_plan_by_id(self, repo, monkeypatch):
        route(monkeypatch, make_router())
        _write_tree(repo, _REMOTE)
        result = runner.invoke(app, ["schema", "apply", "--plan", _PLAN_ID, "--path", str(repo), *_args()])
        assert result.exit_code == 0, result.output
        assert (repo / "cassis" / "domains" / "README.md").exists()

    def test_apply_by_id_does_not_guess_the_plan_origin_in_the_push_hint(self, repo, monkeypatch):
        # A complete_source record is what both a warehouse plan and a `--complete` DDL plan look like.
        route(monkeypatch, make_router())
        _write_tree(repo, _REMOTE)
        result = runner.invoke(app, ["schema", "apply", "--plan", _PLAN_ID, "--path", str(repo), *_args()])
        assert result.exit_code == 0, result.output
        assert "cassis schema push <ddl> --complete (or --warehouse" in result.output
        assert "push --warehouse\n" not in result.output

    def test_apply_json_keeps_stdout_to_the_record(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router())
        _write_tree(repo, _REMOTE)
        result = runner.invoke(app, ["schema", "apply", str(ddl_file), "--path", str(repo), *_args("--json")])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["plan"]["status"] == "ready" and payload["written"] == ["tables/public/orders.yml"]
        assert "Plan ready" in result.stderr

    def test_apply_by_id_waits_through_an_apply_in_progress(self, repo, monkeypatch):
        polls = {"n": 0}
        inner = make_router(plan_status="applied")

        def handler(request):
            if request.method == "GET" and request.url.path.endswith(f"/schema/plans/{_PLAN_ID}"):
                polls["n"] += 1
                if polls["n"] == 1:
                    return httpx.Response(200, json=_record("applying"))
            return inner(request)

        route(monkeypatch, handler)
        _write_tree(repo, _REMOTE)
        result = runner.invoke(app, ["schema", "apply", "--plan", _PLAN_ID, "--path", str(repo), *_args()])
        assert result.exit_code == 1
        assert polls["n"] == 2 and "already applied" in result.output

    def test_apply_of_a_failed_plan_exits_one(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router(plan_status="failed", document=None))
        result = runner.invoke(app, ["schema", "apply", str(ddl_file), "--path", str(repo), *_args()])
        assert result.exit_code == 1

    def test_both_file_and_plan_is_a_usage_error(self, repo, ddl_file, monkeypatch):
        result = runner.invoke(
            app, ["schema", "apply", str(ddl_file), "--plan", _PLAN_ID, "--path", str(repo), *_args()]
        )
        assert result.exit_code == 2


class TestSchemaPush:
    def test_push_applies_the_schema_then_uploads_the_local_tree(self, repo, ddl_file, monkeypatch):
        seen = []
        route(monkeypatch, make_router(seen=seen))
        _write_tree(repo, _TREE)

        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo), *_args("--yes")])

        assert result.exit_code == 0, result.output
        order = [p.rsplit("/", 1)[-1] for m, p, _c in seen if m == "POST"]
        assert order.index("apply") < order.index("import")
        assert "Schema version 2 stored" in result.output and "Ontology pushed" in result.output
        assert "no local `schema apply` marker" in result.output

    def test_push_refuses_when_the_app_ontology_moved_since_apply(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router(fingerprint="fp-2"))
        _write_tree(repo, _TREE)
        (repo / "cassis" / ".schema-apply.json").write_text(
            json.dumps({"plan_id": _PLAN_ID, "base_ontology_fingerprint": "fp-1"})
        )

        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo), *_args("--yes")])

        assert result.exit_code == 1 and "changed since" in result.output

    def test_push_warehouse_applies_the_warehouse_plan(self, repo, monkeypatch):
        seen = []
        route(monkeypatch, make_router(seen=seen))
        _write_tree(repo, _TREE)

        result = runner.invoke(app, ["schema", "push", "--warehouse", "--path", str(repo), *_args("--yes")])

        assert result.exit_code == 0, result.output
        posts = [p.rsplit("/", 1)[-1] for m, p, _c in seen if m == "POST"]
        assert posts == ["warehouse", "apply", "import"]

    def test_push_json_keeps_stdout_to_the_record(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router())
        _write_tree(repo, _TREE)
        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo), *_args("--yes", "--json")])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["plan"]["status"] == "applied" and payload["upload"]["table_count"] == 1
        assert "Plan ready" in result.stderr

    def test_push_publish_flag(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router())
        _write_tree(repo, _TREE)
        result = runner.invoke(
            app, ["schema", "push", str(ddl_file), "--path", str(repo), *_args("--yes", "--publish")]
        )
        assert result.exit_code == 0, result.output
        assert "published as v2" in result.output

    def test_push_without_yes_and_without_tty_is_a_usage_error(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router())
        _write_tree(repo, _TREE)
        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo), *_args()])
        assert result.exit_code == 2 and "pass --yes" in result.output

    def test_push_of_a_stale_plan_exits_one(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router(plan_status="stale", document=None, error="Schema plan is stale"))
        _write_tree(repo, _TREE)
        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo), *_args("--yes")])
        assert result.exit_code == 1

    def test_push_of_an_empty_plan_uploads_the_ontology_only(self, repo, ddl_file, monkeypatch):
        seen = []
        route(monkeypatch, make_router(document=_EMPTY_DOC, seen=seen))
        _write_tree(repo, _TREE)
        result = runner.invoke(app, ["schema", "push", str(ddl_file), "--path", str(repo), *_args("--yes")])
        assert result.exit_code == 0, result.output
        assert not any(p.endswith("/apply") for _m, p, _c in seen)


_DDL_COMMANDS = [["schema", "plan"], ["schema", "apply"], ["schema", "push", "--yes"]]


class TestSchemaInputs:
    @pytest.mark.parametrize("command", _DDL_COMMANDS, ids=lambda c: c[1])
    def test_missing_ddl_file_exits_two_before_any_plan(self, repo, tmp_path, monkeypatch, command):
        seen = []
        route(monkeypatch, make_router(seen=seen))
        _write_tree(repo, _REMOTE)
        missing = tmp_path / "missing.sql"

        result = runner.invoke(app, [*command, str(missing), "--path", str(repo), *_args()])

        assert result.exit_code == 2
        assert "Cannot read" in result.output and str(missing) in result.output
        assert not any(p.endswith("/schema/plans") for _m, p, _c in seen)

    @pytest.mark.parametrize("command", _DDL_COMMANDS, ids=lambda c: c[1])
    def test_empty_ddl_file_exits_two(self, repo, tmp_path, monkeypatch, command):
        seen = []
        route(monkeypatch, make_router(seen=seen))
        _write_tree(repo, _REMOTE)
        empty = tmp_path / "empty.sql"
        empty.write_text("   \n", encoding="utf-8")

        result = runner.invoke(app, [*command, str(empty), "--path", str(repo), *_args()])

        assert result.exit_code == 2 and "DDL file is empty" in result.output
        assert not any(p.endswith("/schema/plans") for _m, p, _c in seen)

    @pytest.mark.parametrize("command", _DDL_COMMANDS, ids=lambda c: c[1])
    def test_missing_api_key_exits_two(self, repo, ddl_file, monkeypatch, command):
        monkeypatch.delenv("CASSIS_API_KEY", raising=False)
        result = runner.invoke(app, [*command, str(ddl_file), "--path", str(repo)])
        assert result.exit_code == 2 and "No API key" in result.output

    def test_conflict_on_plan_start_exits_one_with_the_server_detail(self, repo, ddl_file, monkeypatch):
        detail = "A schema plan is already being applied for this project."

        def handler(request):
            if request.method == "POST" and request.url.path.endswith("/schema/plans"):
                return httpx.Response(409, json={"detail": detail})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        route(monkeypatch, handler)

        result = runner.invoke(app, ["schema", "plan", str(ddl_file), "--path", str(repo), *_args()])

        assert result.exit_code == 1 and detail in result.output


class TestSchemaWaits:
    def test_plan_timeout_exits_three_with_the_resume_hint(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router(plan_status="planning", document=None))

        result = runner.invoke(app, ["schema", "plan", str(ddl_file), "--path", str(repo), *_args("--timeout", "0")])

        assert result.exit_code == 3
        assert "Timed out after 0s: the plan is still planning" in result.output
        assert f"resume with: cassis schema apply --plan {_PLAN_ID}" in result.output

    def test_push_apply_timeout_exits_three_and_points_at_status(self, repo, ddl_file, monkeypatch):
        route(monkeypatch, make_router(apply_status="applying"))
        _write_tree(repo, _TREE)

        result = runner.invoke(
            app, ["schema", "push", str(ddl_file), "--path", str(repo), *_args("--yes", "--timeout", "0")]
        )

        assert result.exit_code == 3
        assert "the apply is still applying" in result.output and "`cassis status` shows the outcome" in result.output

    def test_transient_poll_failures_are_retried(self, repo, ddl_file, monkeypatch):
        polls = {"n": 0}
        inner = make_router()

        def handler(request):
            if request.method == "GET" and request.url.path.endswith(f"/schema/plans/{_PLAN_ID}"):
                polls["n"] += 1
                if polls["n"] <= 2:
                    return httpx.Response(503, text="upstream hiccup")
            return inner(request)

        route(monkeypatch, handler)

        result = runner.invoke(app, ["schema", "plan", str(ddl_file), "--path", str(repo), *_args()])

        assert result.exit_code == 0, result.output
        assert polls["n"] == 3

    def test_persistent_poll_failures_give_up_with_exit_three(self, repo, ddl_file, monkeypatch):
        polls = {"n": 0}

        def handler(request):
            if request.method == "POST" and request.url.path.endswith("/schema/plans"):
                return httpx.Response(202, json=_record("planning"))
            polls["n"] += 1
            return httpx.Response(503, text="upstream down")

        route(monkeypatch, handler)

        result = runner.invoke(app, ["schema", "plan", str(ddl_file), "--path", str(repo), *_args()])

        assert result.exit_code == 3 and "HTTP 503" in result.output
        assert polls["n"] == 5  # the shared tolerance, then give up rather than spin until --timeout
