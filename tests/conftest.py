import pytest

_CASSIS_ENV_VARS = ("CASSIS_API_KEY", "CASSIS_API_URL", "CASSIS_APP_URL", "CASSIS_BASE_PATH", "CASSIS_PROJECT_ID")


@pytest.fixture(autouse=True)
def _scrub_cassis_env(monkeypatch):
    """Tests always see explicit flags, never the developer's shell environment."""
    for name in _CASSIS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
