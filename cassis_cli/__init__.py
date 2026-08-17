"""Validate, test and evaluate your ontology from your terminal, then publish it.

The same commands gate your pull requests in CI.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cassis-cli")
except PackageNotFoundError:  # uninstalled source tree (no dist metadata)
    __version__ = "0.0.0"
