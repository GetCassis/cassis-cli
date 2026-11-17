"""Server-bound login credentials. Secrets live only in a native OS keyring.

Every read/refresh/write holds a process lock, shared by all CLI invocations for
this OS user and server. Refresh requests are never retried with an old token.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
import keyring
from filelock import FileLock, Timeout
from platformdirs import user_data_dir

CLIENT_ID = "cassis-cli"


class CredentialError(Exception):
    pass


class SavedLogin(str):
    """A non-secret marker; API calls resolve it just before each request."""


def canonical_server(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme not in ("https", "http")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise CredentialError("Cassis server must be an origin URL, without credentials, path, query, or fragment.")
    if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise CredentialError("Saved login requires HTTPS (HTTP is allowed only for local development).")
    try:
        port = url.port
    except ValueError as exc:
        raise CredentialError("Invalid Cassis server port.") from exc
    host = url.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if port and port != (443 if url.scheme == "https" else 80):
        host += ":" + str(port)
    return urlunsplit((url.scheme, host, "", "", ""))


def _native_keyring():
    backend = keyring.get_keyring()
    # ChainerBackend can include plaintext third-party providers. Select only
    # maintained native stores, never trust priority alone or a fallback file.
    candidates = getattr(backend, "backends", [backend])
    supported = {
        "keyring.backends.macOS",
        "keyring.backends.Windows",
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
    }
    for candidate in candidates:
        if type(candidate).__module__ in supported and candidate.priority > 0:
            return candidate
    raise CredentialError(
        "No usable OS credential store. Enable macOS Keychain, Windows Credential Locker, "
        "or an unlocked Linux Secret Service/KWallet session. For CI or headless automation, "
        "supply CASSIS_API_KEY through your secret manager. Secrets are never saved to plaintext files."
    )


_keyring = _native_keyring


class CredentialStore:
    def __init__(self, server: str):
        self.server = canonical_server(server)
        self.service = "cassis-cli:" + self.server
        try:
            self.backend = _keyring()
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                "Cannot open the OS credential store. Unlock it or configure CASSIS_API_KEY for automation."
            ) from exc

    def preflight(self) -> None:
        account = "probe-" + secrets.token_hex(8)
        try:
            self.backend.set_password(self.service, account, "credential-store-check")
            self.backend.delete_password(self.service, account)
        except Exception as exc:
            raise CredentialError("Cannot write to the OS credential store. Unlock it before logging in.") from exc

    def read(self) -> Optional[dict[str, Any]]:
        try:
            raw = self.backend.get_password(self.service, "session")
            data = json.loads(raw) if raw else None
            if data is not None and (not isinstance(data, dict) or data.get("server") != self.server):
                raise CredentialError("Saved login does not match this server. Run cassis logout, then cassis login.")
            return data
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError("Cannot read the OS credential store. Unlock it and try again.") from exc

    def write(self, data: dict[str, Any]) -> None:
        try:
            self.backend.set_password(self.service, "session", json.dumps({**data, "server": self.server}))
        except Exception as exc:
            raise CredentialError(
                "Cannot save login in the OS credential store. Unlock it and run cassis login again."
            ) from exc

    def delete(self) -> None:
        try:
            if self.backend.get_password(self.service, "session") is not None:
                self.backend.delete_password(self.service, "session")
        except Exception as exc:
            raise CredentialError(
                "Cannot remove the local login. Unlock the OS credential store and retry logout."
            ) from exc

    @contextmanager
    def locked(self) -> Iterator[None]:
        directory = Path(user_data_dir("cassis-cli", appauthor=False)) / "locks"
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest = hashlib.sha256(self.server.encode()).hexdigest()
            with FileLock(str(directory / (digest + ".lock")), timeout=35):
                yield
        except Timeout as exc:
            raise CredentialError("Another Cassis command is updating this login. Try again shortly.") from exc
        except OSError as exc:
            raise CredentialError("Cannot lock the saved login in the user data directory.") from exc


def oauth_post(server: str, endpoint: str, data: dict[str, str]) -> httpx.Response:
    # No redirects: never forward a refresh token to a different host or URL.
    from cassis_cli.api import _client

    try:
        with _client(timeout=20) as client:
            response = client.post(server + "/api/cli-auth/" + endpoint, data=data)
    except httpx.HTTPError as exc:
        raise CredentialError(
            "Could not reach the Cassis authentication server. Check connectivity and --api-url."
        ) from exc
    if response.is_redirect or response.status_code >= 500:
        raise CredentialError("The Cassis authentication server is unavailable. Try again later.")
    return response


def token_record(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
        if (
            response.status_code != 200
            or not isinstance(data["access_token"], str)
            or not isinstance(data["refresh_token"], str)
        ):
            raise ValueError()
        return {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": time.time() + int(data["expires_in"]),
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise CredentialError(
            "Unexpected login response from Cassis. Check the server version and run cassis login again."
        ) from exc


def access_token(server: str) -> str:
    store = CredentialStore(server)
    with store.locked():
        record = store.read()
        if not record:
            raise CredentialError("No saved login for this server. Run cassis login or set CASSIS_API_KEY.")
        if record.get("refresh_uncertain"):
            raise CredentialError("A previous refresh did not complete. Run cassis logout, then cassis login.")
        if record["expires_at"] > time.time() + 60:
            return record["access_token"]
        # Persist before sending: a lost response or process crash must never
        # cause a second invocation to reuse a rotated token and revoke a family.
        store.write({**record, "refresh_uncertain": True})
        response = oauth_post(
            store.server,
            "token",
            {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": record["refresh_token"]},
        )
        if response.status_code == 400 and response.json().get("error") == "invalid_grant":
            raise CredentialError("Cassis login expired or was revoked. Run cassis logout, then cassis login.")
        updated = {**record, **token_record(response), "refresh_uncertain": False}
        store.write(updated)
        return updated["access_token"]


def authorization(server: str, credential: str) -> str:
    if isinstance(credential, SavedLogin):
        if canonical_server(server) != str(credential):
            raise CredentialError("Saved login belongs to a different Cassis server.")
        return "Bearer " + access_token(server)
    return "Bearer " + credential
