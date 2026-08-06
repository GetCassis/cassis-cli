"""Cross-cutting HTTP client behavior: self-identification, upgrade nudge, timeouts."""

import httpx
import pytest
from cassis_cli import __version__
from cassis_cli import api as api_module
from cassis_cli.api import (
    ONTOLOGY_TREE_TIMEOUT_SECONDS,
    TIMEOUT_SECONDS,
    post_ontology_check,
    post_ontology_fmt,
    post_ontology_import,
)

_CHECK_BODY = {"passed": True, "file_count": 1, "title": "ok", "summary": "ok", "findings": []}


@pytest.fixture(autouse=True)
def reset_notice_flag(monkeypatch):
    monkeypatch.setattr(api_module, "_upgrade_notice_shown", False)


def _call(handler):
    return post_ontology_check(
        api_url="https://cassis.test",
        api_key="sk-k6-test",
        files={"_project.yml": "display_name: T\n"},
        transport=httpx.MockTransport(handler),
    )


def test_requests_identify_the_cli_version():
    seen = {}

    def handler(request):
        seen["user_agent"] = request.headers.get("user-agent")
        return httpx.Response(200, json=_CHECK_BODY)

    _call(handler)
    assert seen["user_agent"] == f"cassis-cli/{__version__}"


def test_upgrade_notice_printed_once_when_server_advertises_newer(capsys):
    def handler(request):
        return httpx.Response(200, json=_CHECK_BODY, headers={"X-Cassis-Cli-Latest": "99.0.0"})

    _call(handler)
    _call(handler)
    err = capsys.readouterr().err
    assert err.count("cassis-cli 99.0.0 is available") == 1
    assert "pip install -U cassis-cli" in err


@pytest.mark.parametrize("advertised", [__version__, "0.0.1", "", "not-a-version"])
def test_no_notice_when_not_strictly_newer(capsys, advertised):
    def handler(request):
        headers = {"X-Cassis-Cli-Latest": advertised} if advertised else {}
        return httpx.Response(200, json=_CHECK_BODY, headers=headers)

    _call(handler)
    assert "is available" not in capsys.readouterr().err


_FMT_BODY = {"ok": True, "findings": [], "changed_paths": [], "removed_paths": [], "files": {}}
_IMPORT_BODY = {
    "domain_count": 1,
    "table_count": 0,
    "join_count": 0,
    "metric_count": 0,
    "published_version": None,
}
_FILES = {"_project.yml": "display_name: T\n"}


def _timeout_of(call, body):
    """Return the read timeout httpx applied to the request `call` makes."""
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json=body)

    call(httpx.MockTransport(handler))
    return seen["timeout"]["read"]


@pytest.mark.parametrize(
    "call,body",
    [
        (
            lambda transport: post_ontology_check(
                api_url="https://cassis.test", api_key="sk-k6-test", files=_FILES, transport=transport
            ),
            _CHECK_BODY,
        ),
        (
            lambda transport: post_ontology_fmt(
                api_url="https://cassis.test", api_key="sk-k6-test", files=_FILES, transport=transport
            ),
            _FMT_BODY,
        ),
        (
            lambda transport: post_ontology_import(
                api_url="https://cassis.test",
                api_key="sk-k6-test",
                project_id="p1",
                files=_FILES,
                publish=False,
                transport=transport,
            ),
            _IMPORT_BODY,
        ),
    ],
    ids=["check", "fmt", "import"],
)
def test_whole_tree_calls_get_the_long_timeout(call, body):
    # Validating/serializing a few hundred ontology files is tens of seconds of
    # server-side CPU; the 60s default aborted real runs on a large ontology.
    assert _timeout_of(call, body) == ONTOLOGY_TREE_TIMEOUT_SECONDS
    assert ONTOLOGY_TREE_TIMEOUT_SECONDS > TIMEOUT_SECONDS


def test_ordinary_calls_keep_the_default_timeout():
    def call(transport):
        return api_module.get_projects(api_url="https://cassis.test", api_key="sk-k6-test", transport=transport)

    assert _timeout_of(call, []) == TIMEOUT_SECONDS
