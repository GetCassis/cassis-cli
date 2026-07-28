"""Cross-cutting HTTP client behavior: self-identification and the upgrade nudge."""

import httpx
import pytest
from cassis_cli import __version__
from cassis_cli import api as api_module
from cassis_cli.api import post_ontology_check

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
