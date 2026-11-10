import json

import httpx
from cassis_cli.api import get_eval_health
from cassis_cli.main import app
from typer.testing import CliRunner


def test_health_uses_shared_server_evidence_and_filters_attention(monkeypatch):
    payload = {
        "attention_count": 1,
        "history_limit": 50,
        "cases": [
            {
                "case_id": "a",
                "question": "Count?",
                "needs_attention": True,
                "labels": ["noisy"],
                "health": {"total": 2, "passed": 1},
                "recommendation": "Review repeated attempts.",
            },
            {
                "case_id": "b",
                "question": "Stable?",
                "needs_attention": False,
                "labels": [],
                "health": {"total": 0},
                "recommendation": "Keep.",
            },
        ],
    }

    def handler(request):
        assert request.url.path.endswith("/eval/health")
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        "cassis_cli.eval.get_eval_health",
        lambda **kwargs: get_eval_health(**kwargs, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "health",
            "--project",
            "019f0000-0000-7000-8000-000000000000",
            "--api-key",
            "sk-k6-test",
            "--attention",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {**payload, "cases": [payload["cases"][0]]}
