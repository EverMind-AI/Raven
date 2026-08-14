"""`raven serve` -- locating the page it serves.

Two independent pieces of the repo have to agree on one path for a released
raven to answer with the real page: the wheel build hook copies ``ui/dist`` to
``raven/ui/dist``, and ``resolve_ui_dist`` looks for it there. Nothing failed
loudly when they disagreed -- the gateway came up, the WebSocket worked, and
``/`` answered with the "No front end built here" placeholder, which is
indistinguishable from a developer who simply never ran the build.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from raven.cli import serve_commands


@pytest.fixture
def two_candidates(tmp_path: Path, monkeypatch):
    """A packaged copy and a source-tree copy, neither built yet."""
    packaged = tmp_path / "wheel" / "raven" / "ui" / "dist"
    source = tmp_path / "repo" / "ui"
    monkeypatch.setattr(serve_commands, "_PACKAGED_UI_DIST", packaged)
    monkeypatch.setattr(serve_commands, "_UI_DIR", source)
    return packaged, source / "dist"


def _build(dist: Path, marker: str) -> None:
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(marker, encoding="utf-8")


def test_no_page_built_is_none_rather_than_a_broken_path(two_candidates) -> None:
    """The gateway stays up and serves its notice; it must not be handed a
    directory that does not exist."""
    assert serve_commands.resolve_ui_dist() is None


def test_the_source_tree_serves_a_developer_who_built_it(two_candidates) -> None:
    _packaged, source = two_candidates
    _build(source, "source")

    assert serve_commands.resolve_ui_dist() == source


def test_the_packaged_copy_wins_over_a_stale_source_tree(two_candidates) -> None:
    """An installed raven runs from the wheel. A source tree that happens to sit
    beside it is somebody's checkout, not what this install shipped."""
    packaged, source = two_candidates
    _build(source, "source")
    _build(packaged, "packaged")

    assert serve_commands.resolve_ui_dist() == packaged


def test_a_directory_without_an_index_does_not_count(two_candidates) -> None:
    """A directory is not a built page.

    Not a state `ui/build.py` leaves behind, as it happens -- it writes
    index.html first and copies the assets second, and a failed marker check
    exits before `dist/` exists at all. The reachable half-built state is the
    opposite one (index.html present, assets missing or half-copied), and that
    one is accepted here on purpose: the page loads and its images do not,
    which is better than refusing to serve. The wheel gate in release.yml is
    where the missing assets are caught."""
    packaged, source = two_candidates
    (packaged / "assets").mkdir(parents=True)
    _build(source, "source")

    assert serve_commands.resolve_ui_dist() == source


def test_the_build_hook_copies_the_page_where_serve_looks_for_it() -> None:
    """The seam itself, asserted across the two files that must agree.

    Read as text rather than by importing the hook: hatchling is a build-time
    dependency and is not installed in the test environment, and the value that
    matters is the literal destination string either way.
    """
    hook = (Path(__file__).resolve().parent.parent / "hatch_build.py").read_text(encoding="utf-8")
    destinations = re.findall(r'=\s*"(raven/ui/[^"]+)"', hook)

    assert destinations == ["raven/ui/dist"], f"the hook's destination moved: {destinations}"

    packaged = serve_commands._PACKAGED_UI_DIST
    assert packaged.parts[-3:] == ("raven", "ui", "dist")
    # And it must be inside the installed package, not next to it: the hook puts
    # the page under `raven/`, so a resolver pointed one level out would find
    # nothing in a wheel while still passing every test above.
    assert packaged.parent.parent == Path(serve_commands.__file__).resolve().parent.parent
