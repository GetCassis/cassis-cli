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


class EvalStartValidationError(ApiError):
    """The API rejected the eval-run start request (400): invalid tree or no test cases.

    ``detail`` keeps the structured payload (``{"message", "findings"}`` for an
    invalid tree, or a plain string) for display.
    """

    def __init__(self, detail: object) -> None:
        super().__init__(str(detail))
        self.detail = detail


class EvalRunActiveError(ApiError):
    """Another eval run is already active for the project (409)."""


def _project_scope_error(response: httpx.Response) -> ApiError:
    # Surface the server's own message when it names the missing resource
    # (e.g. "Branch 'x' not found") — the generic hint covers the rest.
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    prefix = f"{detail} " if isinstance(detail, str) and detail else ""
    return ApiError(
        f"{prefix}(HTTP {response.status_code}). Check --project and that "
        "the API key belongs to the project's organization and can edit it."
    )


def _parse_json_response(response: httpx.Response, url: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:  # json.JSONDecodeError — e.g. a proxy or portal answering HTML with a 200
        raise ApiError(
            f"The Cassis API at {url} returned a non-JSON response — check the API URL and any proxy in between."
        ) from exc


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
    result = _parse_json_response(response, url)
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
        raise _project_scope_error(response)
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    result = _parse_json_response(response, url)
    if not isinstance(result, dict) or not all(
        key in result for key in ("domain_count", "table_count", "join_count", "metric_count", "published_version")
    ):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


def get_ontology_export(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, str]:
    """GET /api/ci/projects/{project_id}/ontology/export and return the files tree."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/ontology/export"
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code in (403, 404):
        raise _project_scope_error(response)
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    result = _parse_json_response(response, url)
    if not isinstance(result, dict) or not isinstance(result.get("files"), dict):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result["files"]


def post_eval_run_start(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    files: Optional[dict[str, str]] = None,
    branch: Optional[str] = None,
    label: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST to /api/ci/projects/{project_id}/eval/runs and return the run record."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/eval/runs"
    body: dict[str, Any] = {}
    if files is not None:
        body["files"] = files
    if branch is not None:
        body["branch"] = branch
    if label is not None:
        body["label"] = label
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(url, json=body, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code == 400:
        try:
            detail = response.json().get("detail") or response.text[:500]
        except ValueError:
            detail = response.text[:500]
        raise EvalStartValidationError(detail)
    if response.status_code == 409:
        raise EvalRunActiveError(
            "An eval run is already active for this project — wait for it to finish or cancel it "
            "(in the webapp's Evals page, or with the run id printed when it was started)."
        )
    if response.status_code in (403, 404):
        raise _project_scope_error(response)
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    result = _parse_json_response(response, url)
    if not isinstance(result, dict) or not all(key in result for key in ("run_id", "status", "total_cases")):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


def _get_eval_json(url: str, api_key: str, transport: Optional[httpx.BaseTransport]) -> Any:
    """GET an eval-run URL with the shared error mapping."""
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code in (403, 404):
        raise _project_scope_error(response)
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    return _parse_json_response(response, url)


def get_eval_run(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    run_id: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """GET /api/ci/projects/{project_id}/eval/runs/{run_id} and return the run record."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/eval/runs/{run_id}"
    result = _get_eval_json(url, api_key, transport)
    if not isinstance(result, dict) or not all(key in result for key in ("run_id", "status", "total_cases")):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


def get_eval_run_results(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    run_id: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> list[dict[str, Any]]:
    """GET /api/ci/projects/{project_id}/eval/runs/{run_id}/results and return the result list."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/eval/runs/{run_id}/results"
    result = _get_eval_json(url, api_key, transport)
    if not isinstance(result, list):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


def post_eval_run_cancel(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    run_id: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> None:
    """POST /api/ci/projects/{project_id}/eval/runs/{run_id}/cancel."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/eval/runs/{run_id}/cancel"
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc
    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code in (403, 404):
        raise _project_scope_error(response)
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")


def post_ontology_fmt(
    *,
    api_url: str,
    api_key: str,
    files: dict[str, str],
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST the ontology tree to /api/ci/ontology-fmt and return the response body."""
    url = api_url.rstrip("/") + "/api/ci/ontology-fmt"
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
    result = _parse_json_response(response, url)
    if (
        not isinstance(result, dict)
        or "ok" not in result
        or not isinstance(result.get("findings"), list)
        or not isinstance(result.get("changed_paths"), list)
        or not isinstance(result.get("removed_paths"), list)
        or (result["ok"] and not isinstance(result.get("files"), dict))
    ):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result
