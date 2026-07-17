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


class UploadValidationError(ApiError):
    """The API rejected the uploaded ontology tree as invalid (400)."""


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


def post_ontology_import(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    files: dict[str, str],
    publish: bool,
    label: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST the ontology tree to /api/ci/projects/{project_id}/ontology/import and return the response body."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/ontology/import"
    body: dict[str, Any] = {"files": files, "publish": publish}
    if label is not None:
        body["label"] = label
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code == 400:
        try:
            detail = response.json().get("detail") or response.text[:500]
        except ValueError:
            detail = response.text[:500]
        raise UploadValidationError(str(detail))
    if response.status_code in (403, 404):
        raise ApiError(
            f"Project not found or not accessible (HTTP {response.status_code}). Check --project and that "
            "the API key belongs to the project's organization and can edit it."
        )
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    try:
        result = response.json()
    except ValueError as exc:  # json.JSONDecodeError — e.g. a proxy or portal answering HTML with a 200
        raise ApiError(
            f"The Cassis API at {url} returned a non-JSON response — check the API URL and any proxy in between."
        ) from exc
    if not isinstance(result, dict) or not all(
        key in result for key in ("domain_count", "table_count", "join_count", "metric_count", "published_version")
    ):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result
