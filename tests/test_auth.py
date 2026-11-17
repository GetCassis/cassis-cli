"""Credential lifecycle without a real browser, identity provider, or OS secrets."""

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cassis_cli import api, auth, credentials
from cassis_cli.common import require_api_key
from cassis_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()
SERVER = "https://cassis.example"
ACCOUNT = {"email": "person@example.com", "organization_id": "org", "role": "editor", "credential_type": "login"}


@pytest.fixture
def store(monkeypatch):
    class MemoryKeyring:
        data = {}

        def get_password(self, service, account):
            return self.data.get((service, account))

        def set_password(self, service, account, value):
            self.data[service, account] = value

        def delete_password(self, service, account):
            del self.data[service, account]

    backend = MemoryKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: backend)
    return credentials.CredentialStore(SERVER)


def session(**overrides):
    return {
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "expires_at": time.time() + 600,
        "account": ACCOUNT,
        **overrides,
    }


def mock_http(monkeypatch, handler):
    original = api._client
    monkeypatch.setattr(api, "_client", lambda **kw: original(**{**kw, "transport": httpx.MockTransport(handler)}))
    monkeypatch.setattr(auth, "_client", api._client)


def test_login_subsequent_command_account_and_logout(store, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        if path.endswith("/device"):
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "BCD-FGH-JKL",
                    "verification_uri": SERVER + "/cli-login",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 600}
            )
        if path.endswith("/account"):
            assert request.headers["authorization"] == "Bearer access-secret"
            return httpx.Response(200, json=ACCOUNT)
        if path.endswith("/projects"):
            assert request.headers["authorization"] == "Bearer access-secret"
            return httpx.Response(200, json=[])
        if path.endswith("/revoke"):
            return httpx.Response(200)
        raise AssertionError(path)

    mock_http(monkeypatch, handler)
    monkeypatch.setattr(auth.time, "sleep", lambda _: None)
    opened = []
    monkeypatch.setattr(auth.webbrowser, "open", opened.append)
    login = runner.invoke(app, ["login", "--api-url", SERVER])
    assert login.exit_code == 0, login.output
    assert opened == [SERVER + "/cli-login"]
    assert "person@example.com" in login.output
    assert "secret" not in login.output
    assert store.read()["refresh_token"] == "refresh-secret"
    commands = [
        runner.invoke(app, args + ["--api-url", SERVER])
        for args in (["projects", "list"], ["auth", "status"], ["logout"])
    ]
    assert all(result.exit_code == 0 for result in commands), [r.output for r in commands]
    assert "saved login" in commands[1].output
    assert all("secret" not in result.output for result in commands)
    assert store.read() is None
    assert opened == [SERVER + "/cli-login"]


@pytest.mark.parametrize("error", ["access_denied", "expired_token"])
def test_denial_expiry_and_remote_browser(store, monkeypatch, error):
    monkeypatch.setattr(auth.time, "sleep", lambda _: None)
    monkeypatch.setattr(auth.webbrowser, "open", lambda _: pytest.fail("remote login opened browser"))
    calls = []

    def post(server, endpoint, data):
        calls.append(endpoint)
        if endpoint == "device":
            return httpx.Response(
                200,
                json={
                    "device_code": "secret",
                    "user_code": "BCD-FGH-JKL",
                    "verification_uri": SERVER + "/cli-login",
                    "interval": 5,
                    "expires_in": 600,
                },
            )
        return httpx.Response(400, json={"error": error})

    monkeypatch.setattr(auth, "oauth_post", post)
    result = runner.invoke(app, ["login", "--no-browser", "--api-url", SERVER])
    assert result.exit_code == 3
    assert ("denied" if error == "access_denied" else "expired") in result.output
    assert store.read() is None
    assert calls[-1] == "cancel"


def test_cancel_and_slow_down(store, monkeypatch):
    intervals = []
    cancelled = []

    def sleep(interval):
        intervals.append(interval)
        if len(intervals) == 2:
            raise KeyboardInterrupt()

    def post(server, endpoint, data):
        if endpoint == "device":
            return httpx.Response(
                200,
                json={
                    "device_code": "secret",
                    "user_code": "BCD-FGH-JKL",
                    "verification_uri": SERVER + "/cli-login",
                    "interval": 5,
                    "expires_in": 600,
                },
            )
        if endpoint == "cancel":
            cancelled.append(data)
            return httpx.Response(204)
        return httpx.Response(400, json={"error": "slow_down"})

    monkeypatch.setattr(auth.time, "sleep", sleep)
    monkeypatch.setattr(auth, "oauth_post", post)
    result = runner.invoke(app, ["login", "--no-browser", "--api-url", SERVER])
    assert result.exit_code == 130
    assert intervals == [5, 10]
    assert cancelled == [{"device_code": "secret"}]
    assert store.read() is None


def test_concurrent_refresh_read_rotate_and_write_are_serialized(store, monkeypatch):
    store.write(session(expires_at=0))
    refreshes = []

    def post(server, endpoint, data):
        refreshes.append(data["refresh_token"])
        time.sleep(0.05)
        return httpx.Response(
            200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 600}
        )

    monkeypatch.setattr(credentials, "oauth_post", post)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(credentials.access_token, [SERVER] * 4))
    assert results == ["new-access"] * 4
    assert refreshes == ["refresh-secret"]
    assert store.read()["refresh_token"] == "new-refresh"


def test_rotation_lock_is_shared_by_separate_processes(tmp_path):
    # File-backed test double for a keyring shared by subprocesses. Production
    # never has a file credential backend; only the lock file is persisted.
    saved = tmp_path / "fake-keyring.json"
    saved.write_text(json.dumps(session(expires_at=0)))
    count = tmp_path / "refresh-count"
    script = """
import json, sys, time
from pathlib import Path
import httpx
from cassis_cli import credentials as c
root = Path(sys.argv[1])
c._keyring = lambda: object()
c.user_data_dir = lambda *a, **kw: str(root)
c.CredentialStore.read = lambda self: json.loads((root / "fake-keyring.json").read_text())
c.CredentialStore.write = lambda self, data: (root / "fake-keyring.json").write_text(json.dumps(data))
def post(*args):
    with (root / "refresh-count").open("a") as f: f.write("1")
    time.sleep(.15)
    return httpx.Response(200, json={"access_token": "new", "refresh_token": "next", "expires_in": 600})
c.oauth_post = post
assert c.access_token("https://cassis.example") == "new"
"""
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(4)
    ]
    for process in processes:
        _, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr.decode()
    assert count.read_text() == "1"


@pytest.mark.parametrize("failure", ["revoked", "offline"])
def test_failed_refresh_does_not_retry_rotated_credential(store, monkeypatch, failure):
    store.write(session(expires_at=0))
    calls = []

    def post(*args):
        calls.append(args)
        if failure == "offline":
            raise credentials.CredentialError("Could not reach server")
        return httpx.Response(400, json={"error": "invalid_grant"})

    monkeypatch.setattr(credentials, "oauth_post", post)
    with pytest.raises(credentials.CredentialError, match="revoked|reach"):
        credentials.access_token(SERVER)
    with pytest.raises(credentials.CredentialError, match="previous refresh"):
        credentials.access_token(SERVER)
    assert len(calls) == 1


def test_explicit_key_precedence_even_on_rejection(store, monkeypatch):
    store.write(session())
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(401, json={"detail": "Invalid key"})

    mock_http(monkeypatch, handler)
    monkeypatch.setenv("CASSIS_API_KEY", "env-key")
    for args, expected in [([], "env-key"), (["--api-key", "flag-key"], "flag-key")]:
        result = runner.invoke(app, ["projects", "list", "--api-url", SERVER, *args])
        assert result.exit_code == 3
        assert seen[-1] == "Bearer " + expected
    assert len(seen) == 2
    assert store.read()["access_token"] == "access-secret"


def test_server_namespacing_and_no_token_on_redirect(store, monkeypatch):
    store.write(session())
    assert credentials.CredentialStore("https://CASSIS.example:443/").read()
    assert credentials.CredentialStore("https://other.example").read() is None
    with pytest.raises(credentials.CredentialError, match="different"):
        credentials.authorization("https://other.example", credentials.SavedLogin(SERVER))
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://other.example/api/cli-auth/token"})

    mock_http(monkeypatch, handler)
    with pytest.raises(credentials.CredentialError, match="unavailable"):
        credentials.oauth_post(SERVER, "token", {"refresh_token": "secret"})
    assert seen == ["cassis.example"]


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://person:secret@host",
        "https://host/path",
        "https://host?query",
        "https://host#fragment",
    ],
)
def test_saved_login_refuses_unsafe_server_urls(url):
    with pytest.raises(credentials.CredentialError):
        credentials.canonical_server(url)


def test_unreachable_logout_retains_login_unless_local_only(store, monkeypatch):
    store.write(session())
    monkeypatch.setattr(auth, "oauth_post", lambda *a: (_ for _ in ()).throw(credentials.CredentialError("offline")))
    result = runner.invoke(app, ["logout", "--api-url", SERVER])
    assert result.exit_code == 3
    assert "retained" in result.output
    assert store.read()
    result = runner.invoke(app, ["logout", "--api-url", SERVER, "--local-only"])
    assert result.exit_code == 0
    assert "not revoked" in result.output
    assert store.read() is None


def test_logout_uses_saved_access_proof_without_refreshing_expired_session(store, monkeypatch):
    store.write(session(expires_at=0, refresh_uncertain=True))

    def post(server, endpoint, data):
        assert server == SERVER
        assert endpoint == "revoke"
        assert data == {"client_id": credentials.CLIENT_ID, "token": "access-secret"}
        assert store.read() is not None
        return httpx.Response(200)

    monkeypatch.setattr(auth, "oauth_post", post)
    result = runner.invoke(app, ["logout", "--api-url", SERVER])
    assert result.exit_code == 0, result.output
    assert "secret" not in result.output
    assert store.read() is None


def test_no_store_no_browser_and_explicit_switching(store, monkeypatch):
    store.write(session())
    monkeypatch.setattr(auth, "oauth_post", lambda *a: pytest.fail("must not begin login"))
    result = runner.invoke(app, ["login", "--api-url", SERVER])
    assert result.exit_code == 3
    assert "logout first" in result.output
    monkeypatch.setattr(
        credentials,
        "_keyring",
        lambda: (_ for _ in ()).throw(credentials.CredentialError("No usable OS credential store")),
    )
    result = runner.invoke(app, ["login", "--api-url", SERVER])
    assert result.exit_code == 3
    assert "No usable OS credential store" in result.output


def test_plaintext_backends_rejected(monkeypatch):
    module = credentials

    class Plaintext:
        priority = 100

    monkeypatch.setattr(module.keyring, "get_keyring", lambda: Plaintext())
    with pytest.raises(module.CredentialError, match="never saved to plaintext"):
        module._native_keyring()
