"""Pin ``__version__`` to the version pyproject ships.

The 1.3.0 release bumped only ``pyproject.toml``, so the published CLI
self-identified as cassis-cli/1.2.0: wrong ``cassis version`` output, a wrong
``User-Agent`` (what the server's format gate and staleness tracking read),
and a permanent "upgrade available" notice against itself. CI installs the
package fresh on every run, so this comparison catches a half-done bump there.
"""

from importlib.metadata import version

from cassis_cli import __version__


def test_dunder_version_matches_package_metadata():
    assert __version__ == version("cassis-cli")
