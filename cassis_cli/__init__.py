"""Cassis CLI — run Cassis actions from your CI pipelines."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cassis-cli")
except PackageNotFoundError:  # uninstalled source tree (no dist metadata)
    __version__ = "0.0.0"
