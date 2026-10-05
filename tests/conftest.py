import subprocess

import pytest

_CASSIS_ENV_VARS = ("CASSIS_API_KEY", "CASSIS_API_URL", "CASSIS_APP_URL", "CASSIS_BASE_PATH", "CASSIS_PROJECT_ID")


@pytest.fixture(autouse=True)
def _scrub_cassis_env(monkeypatch):
    """Tests always see explicit flags, never the developer's shell environment."""
    for name in _CASSIS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, check=True, text=True).stdout.strip()


@pytest.fixture
def commit_all():
    """Commit everything under a directory, initialising a repository there first; return HEAD."""

    def commit(path) -> str:
        if not (path / ".git").exists():
            _git(path, "init", "-q")
            _git(path, "config", "user.email", "test@example.com")
            _git(path, "config", "user.name", "Test")
            _git(path, "config", "commit.gpgsign", "false")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "--allow-empty", "-m", "test")
        return _git(path, "rev-parse", "HEAD")

    return commit
