"""The version in pyproject.toml must have a CHANGELOG entry.

The GitHub mirror publishes each release as one commit whose message IS that
entry (scripts/sync-cli-to-github.sh), so a bump without one fails the mirror
after PyPI already has the wheel. Catch it here, on the MR, instead.
"""

import re
from pathlib import Path

CLI_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = CLI_ROOT / "CHANGELOG.md"
PYPROJECT = CLI_ROOT / "pyproject.toml"


def _version() -> str:
    match = re.search(r'^version = "(.+)"', PYPROJECT.read_text(), re.MULTILINE)
    assert match, "no version in pyproject.toml"
    return match.group(1)


def test_changelog_has_an_entry_for_the_current_version() -> None:
    version = _version()
    heading = re.compile(rf"^## {re.escape(version)}[ (]", re.MULTILINE)
    assert heading.search(CHANGELOG.read_text()), (
        f"CHANGELOG.md has no '## {version}' section. The mirror publishes it as the "
        f"release's commit message, so write the notes before bumping the version."
    )


def test_changelog_entry_is_not_empty() -> None:
    version = _version()
    text = CHANGELOG.read_text()
    start = re.search(rf"^## {re.escape(version)}[ (].*$", text, re.MULTILINE)
    assert start, f"no '## {version}' section (see the previous test)"
    rest = text[start.end() :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    body = rest[: next_heading.start()] if next_heading else rest
    assert body.strip(), f"the '## {version}' section is empty"
