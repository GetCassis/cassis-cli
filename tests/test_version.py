"""``__version__`` must resolve from the installed package's metadata.

It is derived via ``importlib.metadata`` so ``cli/pyproject.toml`` is the
single place a release bumps. When ``__version__`` was a hand-maintained
literal, a bump that missed it shipped a CLI self-identifying as the previous
version (the 1.3.0 release did exactly that): wrong ``cassis version``
output, a wrong ``User-Agent`` (what the server's format gate and staleness
tracking read), and a permanent "upgrade available" notice against itself.
CI installs the package fresh on every run, so this catches the metadata
failing to resolve (the ``0.0.0`` fallback leaking into a build).
"""

from importlib.metadata import version

from cassis_cli import __version__


def test_dunder_version_resolves_from_package_metadata():
    assert __version__ == version("cassis-cli")
    assert __version__ != "0.0.0"
