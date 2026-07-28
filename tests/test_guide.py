"""The bundled modeling guide: sync with the canonical doc, and the write logic."""

import re
from pathlib import Path

import pytest
from cassis_cli.guide import DOCTRINE_VERSION, GUIDE_FILENAME, canonical_guide, guide_status, refresh_guide

# cli/tests/test_guide.py -> repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_DOC = _REPO_ROOT / "docs" / "ontology-design-guide.md"
_BUNDLED = Path(__file__).resolve().parents[1] / "cassis_cli" / "ontology_design_guide.md"


@pytest.mark.skipif(not _CANONICAL_DOC.is_file(), reason="canonical doc only present in the monorepo checkout")
def test_bundled_guide_matches_canonical_doc():
    # The backend image doesn't ship docs/, so the CLI carries its own copy;
    # this is the openapi.json-style guard that keeps the two byte-identical.
    assert _BUNDLED.read_text(encoding="utf-8") == _CANONICAL_DOC.read_text(encoding="utf-8"), (
        "cli/cassis_cli/ontology_design_guide.md drifted from docs/ontology-design-guide.md — "
        "re-copy the canonical doc into the CLI package."
    )


def test_canonical_guide_has_banner_then_body():
    content = canonical_guide()
    assert content.startswith("<!--")
    assert "Do NOT edit" in content.split("-->", 1)[0]
    assert f"doctrine v{DOCTRINE_VERSION}" in content.split("-->", 1)[0]
    assert "# Cassis Ontology Design Guide" in content


def test_refresh_guide_creates_then_is_idempotent(tmp_path):
    ontology_dir = tmp_path / "cassis"

    assert guide_status(ontology_dir) == "missing"
    assert refresh_guide(ontology_dir) == "missing"
    written = (ontology_dir / GUIDE_FILENAME).read_text(encoding="utf-8")
    assert written == canonical_guide()
    assert guide_status(ontology_dir) == "current"

    # Second call is a no-op — nothing changed.
    assert refresh_guide(ontology_dir) == "current"


def test_refresh_guide_overwrites_local_edit(tmp_path):
    ontology_dir = tmp_path / "cassis"
    ontology_dir.mkdir()
    (ontology_dir / GUIDE_FILENAME).write_text("hand-edited, should be clobbered\n", encoding="utf-8")

    assert guide_status(ontology_dir) == "stale"
    assert refresh_guide(ontology_dir) == "stale"
    assert (ontology_dir / GUIDE_FILENAME).read_text(encoding="utf-8") == canonical_guide()


def test_refresh_guide_overwrites_older_doctrine(tmp_path):
    # An older stamp (or the pre-stamp banner) is stale: upgrade it.
    ontology_dir = tmp_path / "cassis"
    ontology_dir.mkdir()
    older = canonical_guide().replace(f"doctrine v{DOCTRINE_VERSION}", "doctrine v0")
    (ontology_dir / GUIDE_FILENAME).write_text(older, encoding="utf-8")

    assert guide_status(ontology_dir) == "stale"
    assert refresh_guide(ontology_dir) == "stale"
    assert (ontology_dir / GUIDE_FILENAME).read_text(encoding="utf-8") == canonical_guide()


def test_bannerless_file_mentioning_a_version_is_stale(tmp_path):
    # Only a leading banner comment stamps the doctrine. A hand-written file
    # whose prose happens to say "doctrine v9" must be repaired like any other
    # local edit — reading it as "newer" would leave it broken forever and nag
    # the user to upgrade a CLI that is already current.
    ontology_dir = tmp_path / "cassis"
    ontology_dir.mkdir()
    (ontology_dir / GUIDE_FILENAME).write_text(
        f"# Our notes\n\nWe follow doctrine v{DOCTRINE_VERSION + 9} here.\n", encoding="utf-8"
    )

    assert guide_status(ontology_dir) == "stale"
    assert refresh_guide(ontology_dir) == "stale"
    assert (ontology_dir / GUIDE_FILENAME).read_text(encoding="utf-8") == canonical_guide()


def test_refresh_guide_never_downgrades_newer_doctrine(tmp_path):
    # A newer stamp means a newer CLI or the Cassis server wrote the file;
    # this (older) CLI must leave it alone rather than downgrade the doctrine.
    ontology_dir = tmp_path / "cassis"
    ontology_dir.mkdir()
    newer = re.sub(r"doctrine v\d+", f"doctrine v{DOCTRINE_VERSION + 1}", canonical_guide())
    (ontology_dir / GUIDE_FILENAME).write_text(newer, encoding="utf-8")

    assert guide_status(ontology_dir) == "newer"
    assert refresh_guide(ontology_dir) == "newer"
    assert (ontology_dir / GUIDE_FILENAME).read_text(encoding="utf-8") == newer
