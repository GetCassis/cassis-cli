"""Explicit browser login and credential lifecycle commands."""

from __future__ import annotations

import os
import time
import webbrowser
from typing import Optional

import httpx
import typer
from cassis_cli.api import DEFAULT_API_URL, _client, authorization
from cassis_cli.common import EXIT_INTERRUPTED, api_failure, require_api_key
from cassis_cli.credentials import (
    CLIENT_ID,
    CredentialError,
    CredentialStore,
    SavedLogin,
    canonical_server,
    oauth_post,
    token_record,
)

app = typer.Typer(no_args_is_help=True, help="Inspect the account and server used by the CLI.")


def account_info(server: str, credential: str) -> dict:
    try:
        with _client(timeout=20) as client:
            response = client.get(
                server + "/api/cli-auth/account", headers={"Authorization": authorization(server, credential)}
            )
    except httpx.HTTPError as exc:
        raise CredentialError(
            "Could not reach Cassis to inspect the account. Check connectivity and --api-url."
        ) from exc
    if response.status_code == 401:
        raise CredentialError(
            "Credential expired, revoked, or invalid for this server. Run cassis logout then cassis login, or check your API key."
        )
    if response.status_code == 403:
        raise CredentialError("Access denied for this account.")
    if response.status_code != 200:
        raise CredentialError(f"Cannot inspect this account (HTTP {response.status_code}). Check the server version.")
    return response.json()


def login(
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url", envvar="CASSIS_API_URL", help="Cassis server origin."),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the verification URL without opening a browser (remote agents/SSH)."
    ),
) -> None:
    """Sign in through a browser on this or another device; save login in the OS credential store."""
    device_code = None
    tokens = None
    saved = False
    try:
        store = CredentialStore(api_url)
        with store.locked():
            if store.read():
                raise CredentialError(
                    "A login is already saved for this server. Inspect it with cassis auth status; to switch accounts, run cassis logout first."
                )
            # Verify write access before asking the person to authorize. A probe
            # contains no credentials and is removed before the network request.
            store.preflight()
        response = oauth_post(store.server, "device", {"client_id": CLIENT_ID})
        if response.status_code != 200:
            raise CredentialError(
                f"Cannot start login (HTTP {response.status_code}). Check the server URL/version or retry later."
            )
        device = response.json()
        device_code = device["device_code"]
        verification_uri = device["verification_uri"]
        # A local development SPA may have a different origin/port; production
        # verification still requires HTTPS, with no embedded credentials.
        from urllib.parse import urlsplit

        parsed = urlsplit(verification_uri)
        canonical_server(f"{parsed.scheme}://{parsed.netloc}")
        typer.echo(f"Server: {store.server}\nOpen {verification_uri}\nEnter code: {device['user_code']}")
        typer.echo("Approve only the code shown here. Waiting for browser authorization…")
        if os.environ.get("CASSIS_API_KEY"):
            typer.echo("CASSIS_API_KEY is set and will take precedence over this saved login.", err=True)
        if not no_browser:
            webbrowser.open(verification_uri)
        interval = int(device["interval"])
        deadline = time.monotonic() + int(device["expires_in"])
        while time.monotonic() < deadline:
            time.sleep(interval)
            response = oauth_post(
                store.server,
                "token",
                {
                    "client_id": CLIENT_ID,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                },
            )
            if response.status_code == 200:
                tokens = token_record(response)
                identity = account_info(store.server, tokens["access_token"])
                with store.locked():
                    if store.read():
                        raise CredentialError(
                            "Another login completed for this server. Run cassis auth status before switching accounts."
                        )
                    store.write({**tokens, "account": identity})
                    saved = True
                typer.echo(f"Signed in as {identity['email']} at {store.server}.")
                return
            error = response.json().get("error")
            if error == "slow_down":
                interval += 5
            elif error == "access_denied":
                raise CredentialError("Login was denied. No credentials saved.")
            elif error == "expired_token":
                raise CredentialError("Login code expired or was cancelled. Run cassis login again.")
            elif error != "authorization_pending":
                raise CredentialError("Login failed. Check the server and run cassis login again.")
        raise CredentialError("Login code expired. Run cassis login again.")
    except KeyboardInterrupt:
        typer.echo("Login cancelled.", err=True)
        raise typer.Exit(EXIT_INTERRUPTED)
    except (CredentialError, KeyError, ValueError) as exc:
        raise api_failure(
            exc
            if isinstance(exc, CredentialError)
            else CredentialError("Unexpected authentication response from Cassis.")
        )
    finally:
        if device_code and not saved:
            try:
                oauth_post(store.server, "cancel", {"device_code": device_code})
                if tokens:
                    oauth_post(store.server, "revoke", {"client_id": CLIENT_ID, "token": tokens["access_token"]})
            except CredentialError:
                typer.echo(
                    (
                        "Could not confirm revocation; the server session may remain until expiry."
                        if tokens
                        else "Could not confirm cancellation; the code expires within 10 minutes."
                    ),
                    err=True,
                )


@app.command("status")
def auth_status(
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url", envvar="CASSIS_API_URL", help="Cassis server origin."),
    api_key: Optional[str] = typer.Option(
        None, "--api-key", envvar="CASSIS_API_KEY", help="Explicit automation key; takes precedence over saved login."
    ),
) -> None:
    """Verify and show the effective account, server, role and credential source, without secrets."""
    try:
        server = canonical_server(api_url)
        credential = require_api_key(api_key, server)
        info = account_info(server, credential)
        source = "saved login" if isinstance(credential, SavedLogin) else "API key (--api-key or CASSIS_API_KEY)"
        typer.echo(
            f"Server: {server}\nAccount: {info['email']}\nOrganization: {info['organization_id']}\nRole: {info['role']}\nCredential: {source}"
        )
        if info.get("project_id"):
            typer.echo(f"Project scope: {info['project_id']}")
    except CredentialError as exc:
        raise api_failure(exc)


def logout(
    api_url: str = typer.Option(
        DEFAULT_API_URL, "--api-url", envvar="CASSIS_API_URL", help="Server whose saved login to revoke."
    ),
    local_only: bool = typer.Option(
        False, "--local-only", help="Remove local credentials without server revocation (e.g. server unreachable)."
    ),
) -> None:
    """Revoke this saved login and remove it locally. Automation keys and other sessions are unaffected."""
    try:
        store = CredentialStore(api_url)
        with store.locked():
            record = store.read()
            if not record:
                typer.echo(f"No saved login for {store.server}.")
                return
            if not local_only:
                try:
                    # The signed access token identifies the session even when
                    # expired, or when a refresh response was lost. Do not
                    # refresh here: replaying an old refresh token is unsafe.
                    response = oauth_post(
                        store.server, "revoke", {"client_id": CLIENT_ID, "token": record["access_token"]}
                    )
                    if response.status_code != 200:
                        raise CredentialError("Server did not confirm revocation.")
                except CredentialError as exc:
                    raise CredentialError(
                        "Logout could not revoke the server session. Local credentials were retained so you can retry. Use --local-only to remove them without revocation."
                    ) from exc
            store.delete()
        typer.echo(f"Logged out of {store.server}.")
        if local_only:
            typer.echo("Server session was not revoked; any copied credentials may work until expiry.", err=True)
        if os.environ.get("CASSIS_API_KEY"):
            typer.echo("CASSIS_API_KEY remains configured; commands will continue using it.", err=True)
    except CredentialError as exc:
        raise api_failure(exc)
