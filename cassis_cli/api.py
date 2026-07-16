"""Thin HTTP client for the Cassis API."""

from __future__ import annotations

from typing import Any, Optional

import httpx

DEFAULT_API_URL = "https://app.getcassis.com"
TIMEOUT_SECONDS = 60.0


class ApiError(Exception):
    """Transport or HTTP-level failure talking to the Cassis API."""


class AuthError(ApiError):
    """The API rejected the API key (401)."""


def post_ontology_check(
    *,
    api_url: str,
    api_key: str,
    files: dict[str, str],
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST the ontology tree to /api/ci/ontology-check and return the response body."""
    url = api_url.rstrip("/") + "/api/ci/ontology-check"
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(
                url,
                json={"files": files},
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    try:
        result = response.json()
    except ValueError as exc:  # json.JSONDecodeError — e.g. a proxy or portal answering HTML with a 200
        raise ApiError(
            f"The Cassis API at {url} returned a non-JSON response — check the API URL and any proxy in between."
        ) from exc
    if (
        not isinstance(result, dict)
        or not all(key in result for key in ("passed", "title", "summary"))
        or not isinstance(result.get("findings"), list)
    ):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result
