"""`cassis verify` — the full local gate in one verb: fmt --check, check, eval run.

Every edit session recites the same litany by hand, and the README's CI
examples chain the same three commands as separate jobs. `verify` runs them in
order and stops at the first failure, so "is this change safe to merge?" is
one command in a checkout and one job in CI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import typer
from cassis_cli.api import DEFAULT_API_URL
from cassis_cli.common import DEFAULT_BASE_PATH, EXIT_OK, require_api_key
from cassis_cli.eval import run as eval_run
from cassis_cli.ontology import check as ontology_check
from cassis_cli.ontology import fmt as ontology_fmt


def _step(title: str, fn: "Callable[[], None]") -> int:
    """Run one gate; return its exit code (commands exit via typer.Exit)."""
    typer.secho(f"==> {title}", bold=True)
    try:
        fn()
    except typer.Exit as exc:
        return exc.exit_code
    return EXIT_OK


def verify(
    path: Path = typer.Argument(
        Path("."),
        help="Repository checkout root (the directory containing the ontology export path).",
    ),
    project_id: Optional[str] = typer.Option(
        None,
        "--project",
        envvar="CASSIS_PROJECT_ID",
        help="Target Cassis project ID (UUID). Defaults to the id in <base-path>/project.yml.",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar="CASSIS_API_KEY",
        help="Cassis API key (sk-k6-...). Create one in Organization settings -> API keys.",
    ),
    api_url: str = typer.Option(
        DEFAULT_API_URL,
        "--api-url",
        envvar="CASSIS_API_URL",
        help="Cassis API base URL.",
    ),
    base_path: str = typer.Option(
        DEFAULT_BASE_PATH,
        "--base-path",
        envvar="CASSIS_BASE_PATH",
        help="Repository directory the ontology is exported under (the project's git-sync Path setting).",
    ),
    run_eval: bool = typer.Option(
        True,
        "--eval/--no-eval",
        help="Run the project's eval suite as the last gate (default: run it).",
    ),
) -> None:
    """Run the three local gates in order: `ontology fmt --check`, `ontology check`, `eval run`.

    The same sequence the README's CI examples chain as separate jobs, stopping
    at the first failure. `fmt` runs in --check mode and writes nothing. Pass
    --no-eval to skip the eval suite (e.g. a project with no cases yet).
    Exits with the first failing gate's code: 0 all gates passed, 1 validation
    or eval failure, 2 usage errors, 3 transport/API errors, 130 interrupted.
    """
    api_key = require_api_key(api_key)

    code = _step(
        "cassis ontology fmt --check",
        lambda: ontology_fmt(path=path, api_key=api_key, api_url=api_url, base_path=base_path, check_only=True),
    )
    if code != EXIT_OK:
        typer.secho("Not canonical: run `cassis ontology fmt` and review the diff.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code)

    code = _step(
        "cassis ontology check",
        lambda: ontology_check(
            path=path,
            project_id=project_id,
            api_key=api_key,
            api_url=api_url,
            base_path=base_path,
            json_output=False,
        ),
    )
    if code != EXIT_OK:
        raise typer.Exit(code)

    if run_eval:
        # Kwargs must track eval_run's signature; a new required param breaks here.
        code = _step(
            "cassis eval run",
            lambda: eval_run(
                path=path,
                project_id=project_id,
                api_key=api_key,
                api_url=api_url,
                base_path=base_path,
                branch=None,
                case=None,
                label=None,
                wait=True,
                poll_interval=5.0,
                timeout=1800.0,
                json_output=False,
                app_url=None,
            ),
        )
        if code != EXIT_OK:
            raise typer.Exit(code)

    typer.secho("✓ verify passed" + ("" if run_eval else " (eval skipped)"), fg=typer.colors.GREEN, bold=True)
    raise typer.Exit(EXIT_OK)
