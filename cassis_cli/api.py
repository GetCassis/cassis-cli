"""Thin HTTP client for the Cassis API."""

from __future__ import annotations

import sys
from typing import Any, Optional

import httpx
from cassis_cli import __version__

DEFAULT_API_URL = "https://app.getcassis.com"
TIMEOUT_SECONDS = 60.0

USER_AGENT = f"cassis-cli/{__version__}"

# Response header the CI endpoints set to the newest cassis-cli on PyPI.
LATEST_VERSION_HEADER = "x-cassis-cli-latest"

_upgrade_notice_shown = False


def _version_tuple(version: str) -> Optional[tuple[int, ...]]:
    try:
        return tuple(int(part) for part in version.strip().split("."))
    except ValueError:
        return None


def _maybe_print_upgrade_notice(response: httpx.Response) -> None:
    """Print a one-time stderr notice when the server advertises a newer CLI.

    Purely informational — never changes behavior or exit codes. Staying
    current matters beyond bugfixes: the ontology modeling guide written to
    ``cassis/AGENTS.md`` ships inside this package, so an old CLI keeps old
    doctrine in the repo.
    """
    global _upgrade_notice_shown
    if _upgrade_notice_shown:
        return
    latest = response.headers.get(LATEST_VERSION_HEADER)
    if not latest:
        return
    mine, theirs = _version_tuple(__version__), _version_tuple(latest)
    if mine is None or theirs is None:
        return
    # Zero-pad to equal length so "0.6" == "0.6.0" (same rule as the webapp's
    # Agent setup page — the comparison logic exists on both surfaces).
    width = max(len(mine), len(theirs))
    if theirs + (0,) * (width - len(theirs)) <= mine + (0,) * (width - len(mine)):
        return
    _upgrade_notice_shown = True
    print(
        f"notice: cassis-cli {latest} is available (you have {__version__}) — "
        "run `pip install -U cassis-cli`, then `cassis ontology fmt` to refresh cassis/AGENTS.md.",
        file=sys.stderr,
    )


def _client(*, timeout: float = TIMEOUT_SECONDS, transport: Optional[httpx.BaseTransport] = None) -> httpx.Client:
    """Build the HTTP client every API call goes through.

    Identifies the CLI to the server (User-Agent) and watches responses for
    the newer-version advertisement.
    """
    return httpx.Client(
        timeout=timeout,
        transport=transport,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"response": [_maybe_print_upgrade_notice]},
    )


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


def _detail_or_text(response: httpx.Response) -> Any:
    """Return the error response's ``detail`` (str or structured), falling back to its body."""
    try:
        return response.json().get("detail") or response.text[:500]
    except ValueError:
        return response.text[:500]


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
    project_id: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST the ontology tree to the check endpoint and return the response body.

    With ``project_id``, calls the project-scoped route, which additionally
    cross-checks the tree against the project's source schema and returns
    advisory ``warnings``; without it, the pure tree check.
    """
    if project_id:
        url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/ontology/check"
    else:
        url = api_url.rstrip("/") + "/api/ci/ontology-check"
    try:
        with _client(transport=transport) as client:
            response = client.post(
                url,
                json={"files": files},
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if project_id and response.status_code in (403, 404):
        raise _project_scope_error(response)
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
        with _client(transport=transport) as client:
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
        raise UploadValidationError(str(_detail_or_text(response)))
    if response.status_code == 426:  # this CLI is too old for the server's ontology format
        raise UploadValidationError(str(_detail_or_text(response)))
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
        with _client(transport=transport) as client:
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


def get_projects(
    *,
    api_url: str,
    api_key: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> list[dict[str, Any]]:
    """GET /api/ci/projects and return the project list."""
    url = api_url.rstrip("/") + "/api/ci/projects"
    try:
        with _client(transport=transport) as client:
            response = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    result = _parse_json_response(response, url)
    if not isinstance(result, list) or not all(isinstance(p, dict) and "id" in p and "name" in p for p in result):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


def get_project_status(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """GET /api/ci/projects/{project_id}/status and return the status record."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/status"
    try:
        with _client(transport=transport) as client:
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
    if not isinstance(result, dict) or "has_unpublished_changes" not in result:
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


class NoSourceSchemaError(ApiError):
    """The project's data source has no introspected schema to pull."""


def get_schema_export(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """GET /api/ci/projects/{project_id}/schema and return the response body."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/schema"
    try:
        with _client(transport=transport) as client:
            response = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    # "No source schema" is the marker the server's export endpoint puts in its
    # 404 detail (backend endpoints/ci.py::export_source_schema — reworded only
    # with a paired CLI release). Without this routing, a schema-less project
    # would surface as the misleading "check --project / key access" hint below.
    if response.status_code == 404 and "No source schema" in response.text:
        raise NoSourceSchemaError(str(_detail_or_text(response)))
    if response.status_code in (400, 403, 404):
        raise _project_scope_error(response)
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    result = _parse_json_response(response, url)
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("tables"), list)
        or not isinstance(result.get("schema_version"), dict)
    ):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


class SourceChangeConflictError(ApiError):
    """A concurrent detection run is already active, or the project has a connected data source."""


def post_detect_from_ddl(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    ddl: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST /api/ci/projects/{project_id}/source-changes/detect-from-ddl and return the run record."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/source-changes/detect-from-ddl"
    try:
        with _client(transport=transport) as client:
            response = client.post(url, json={"ddl": ddl}, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code == 409:
        raise SourceChangeConflictError(str(_detail_or_text(response)))
    if response.status_code in (403, 404):
        raise _project_scope_error(response)
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    result = _parse_json_response(response, url)
    if not isinstance(result, dict) or "run_id" not in result:
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


def get_source_change_run(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    run_id: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """GET /api/ci/projects/{project_id}/source-changes/runs/{run_id} and return the run record."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/source-changes/runs/{run_id}"
    try:
        with _client(transport=transport) as client:
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
    if not isinstance(result, dict) or "run_id" not in result:
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


def post_eval_run_start(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    files: Optional[dict[str, str]] = None,
    branch: Optional[str] = None,
    label: Optional[str] = None,
    case_ids: Optional[list[str]] = None,
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
    if case_ids is not None:
        body["test_case_ids"] = case_ids
    try:
        with _client(transport=transport) as client:
            response = client.post(url, json=body, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code == 400:
        raise EvalStartValidationError(_detail_or_text(response))
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


class EvalCaseExistsError(ApiError):
    """The project already has an eval case with this exact question."""


class EvalCaseGoldSqlError(ApiError):
    """The API rejected the gold SQL (400): it does not run against the project's data source."""


def post_eval_case_create(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    question: str,
    gold_sql: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST to /api/ci/projects/{project_id}/eval/cases and return the created case."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/eval/cases"
    try:
        with _client(transport=transport) as client:
            response = client.post(
                url,
                json={"question": question, "gold_sql": gold_sql},
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code == 409:
        raise EvalCaseExistsError(str(_detail_or_text(response)))
    if response.status_code == 400:
        raise EvalCaseGoldSqlError(str(_detail_or_text(response)))
    if response.status_code in (403, 404):
        raise _project_scope_error(response)
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    result = _parse_json_response(response, url)
    if not isinstance(result, dict) or not all(key in result for key in ("id", "question", "gold_sql")):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


class EvalCaseNotFoundError(ApiError):
    """The project has no current eval case with this id."""


def get_eval_cases(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> list[dict[str, Any]]:
    """GET /api/ci/projects/{project_id}/eval/cases and return the case list."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/eval/cases"
    result = _get_eval_json(url, api_key, transport)
    if not isinstance(result, list):
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result


def delete_eval_case(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    case_id: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> None:
    """DELETE /api/ci/projects/{project_id}/eval/cases/{case_id}."""
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/eval/cases/{case_id}"
    try:
        with _client(transport=transport) as client:
            response = client.delete(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    # Exact-match wire contract with the DELETE endpoint's 404 detail (see
    # `delete_eval_case` in backend/app/endpoints/ci.py): it distinguishes a
    # missing case (exit 1) from a project-scope 404 (exit 3).
    if response.status_code == 404 and _detail_or_text(response) == "Eval case not found":
        raise EvalCaseNotFoundError(f"No current eval case {case_id} in this project (already deleted, or wrong id?).")
    if response.status_code in (403, 404):
        raise _project_scope_error(response)
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")


def _get_eval_json(url: str, api_key: str, transport: Optional[httpx.BaseTransport]) -> Any:
    """GET an eval-run URL with the shared error mapping."""
    try:
        with _client(transport=transport) as client:
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
        with _client(transport=transport) as client:
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
        with _client(transport=transport) as client:
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


# The server-side probe budget is 300s; leave headroom for transport.
ONTOLOGY_TEST_TIMEOUT_SECONDS = 330.0


class OntologyTestValidationError(ApiError):
    """The API rejected the ontology tree as invalid (400).

    ``detail`` keeps the structured payload (``{"message", "findings"}``) for
    display.
    """

    def __init__(self, detail: object) -> None:
        super().__init__(str(detail))
        self.detail = detail


def post_ontology_test(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    files: dict[str, str],
    question: str,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST to /api/ci/projects/{project_id}/ontology/test and return the probe outcome.

    Blocks for the duration of the agent run (up to ~5 minutes server-side).
    """
    url = api_url.rstrip("/") + f"/api/ci/projects/{project_id}/ontology/test"
    try:
        with _client(timeout=ONTOLOGY_TEST_TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(
                url,
                json={"files": files, "question": question},
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Cassis API at {url}: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("The Cassis API rejected the API key (invalid or expired).")
    if response.status_code == 400:
        raise OntologyTestValidationError(_detail_or_text(response))
    if response.status_code == 402:
        raise ApiError("Your organization has run out of credits. Contact your administrator to top up.")
    if response.status_code in (403, 404):
        raise _project_scope_error(response)
    if response.status_code >= 400:
        raise ApiError(f"Cassis API returned HTTP {response.status_code}: {response.text[:500]}")
    result = _parse_json_response(response, url)
    if not isinstance(result, dict) or "status" not in result:
        raise ApiError(f"Unexpected response shape from the Cassis API at {url}.")
    return result
