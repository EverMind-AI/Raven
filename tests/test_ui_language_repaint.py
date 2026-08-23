"""Every module page of the served page repaints when the language flips.

``redrawAll()`` in the live layer (``ui/src/live/``) is the whole of the
language flip: the
catalogue is re-run over the static markup by ``applyI18n()``, and everything
drawn from JavaScript has to be redrawn by name from that one function. It is a
hand-written list, and a renderer missing from it is invisible -- the page keeps
the DOM it was built with and comes back in the previous language, which is what
the agents page, the memory page and the More rows did.

``ui/`` is one concatenated script with no module system and no JS test harness
(the prettier/eslint hooks in ``.pre-commit-config.yaml`` cover ``ui-tui/src``
and ``bridge/src`` only), so the list is checked here, against the source. The
convention it leans on is the one the markup already keeps: a module page is
``<section class="page" id="xPage">`` and its whole body is drawn into
``#xBody``, so whoever writes that element is the page's renderer.
"""

from __future__ import annotations

# The page ships as ordered parts assembled by ui/build.py; read the texts
# through its own manifests so this test cannot drift from what ships and
# never pins a part filename (renderers move between parts freely).
import importlib.util as _ilu
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_spec = _ilu.spec_from_file_location("_ui_build", _ROOT / "ui" / "build.py")
_build = _ilu.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_build)

try:
    BASE = (_ROOT / "ui" / "src" / "page.html").read_text(encoding="utf-8") + _build._concat("demo", _build._DEMO_PARTS)
    LIVE = _build._concat("live", _build._LIVE_PARTS)
except SystemExit as exc:  # build.py's mismatch signal is not a test outcome
    raise RuntimeError(f"ui/build.py rejected the part manifests: {exc}") from exc

_DEFINITION = re.compile(
    r"^\s*(?:(?:async\s+)?function\s+(?P<decl>\w+)\s*\(|(?P<assigned>\w+)\s*=\s*(?:async\s+)?function\s*\()"
)


def _redraw_all_body() -> str:
    """The body of ``redrawAll()``, up to the closing brace in column one."""
    live = LIVE
    start = live.index("function redrawAll() {")
    end = live.index("\n}\n", start)
    return live[start:end]


def _enclosing_function(source: str, needle: str) -> set[str]:
    """Names of the functions whose bodies mention ``needle``.

    Walks back from each hit to the nearest definition line rather than parsing
    the file: both spellings the page uses -- a top-level declaration and the
    ``name = function ()`` rebinds live.js swaps in over the demo -- start a
    line, so the nearest one above a hit is the function it sits in.
    """
    lines = source.splitlines()
    found: set[str] = set()
    for index, line in enumerate(lines):
        if needle not in line:
            continue
        for above in range(index, -1, -1):
            match = _DEFINITION.match(lines[above])
            if match:
                found.add(match.group("decl") or match.group("assigned"))
                break
    return found


def _module_pages() -> list[str]:
    return re.findall(r'<section class="page" id="(\w+)Page"', BASE)


def _registered_pages() -> list[str]:
    """The pages that can be opened at all, from the ``NAV_OF`` registry.

    The authority, not the markup: ``showPage()`` walks these keys, so a page
    absent from them is unreachable, and a page present in them is reachable
    however its section tag happens to be spelled.
    """
    block = re.search(r"const NAV_OF = \{(.*?)\n\};", BASE, re.S)
    if block is None:
        raise AssertionError("NAV_OF not found in the demo shell -- the page registry moved")
    pages = re.findall(r"^\s*(\w+)Page:", block.group(1), re.M)
    if not pages:
        raise AssertionError("NAV_OF names no pages -- the registry's shape changed")
    return pages


def test_the_markup_and_the_registry_name_the_same_pages() -> None:
    """Both readings agree, so neither can quietly cover fewer pages than it looks like."""
    assert sorted(_module_pages()) == sorted(_registered_pages())


def test_the_markup_keeps_the_page_body_convention() -> None:
    """Each module page draws into ``#<name>Body`` -- what the check below reads."""
    base = BASE
    missing = [name for name in _registered_pages() if f'id="{name}Body"' not in base]
    assert not missing, f"module pages without a #<name>Body element: {missing}"


@pytest.mark.parametrize("page", _registered_pages())
def test_every_module_page_is_redrawn_on_a_language_flip(page: str) -> None:
    renderers = _enclosing_function(BASE, f"#{page}Body") | _enclosing_function(LIVE, f"#{page}Body")
    assert renderers, f"no renderer writes #{page}Body"
    body = _redraw_all_body()
    called = [name for name in renderers if f"{name}()" in body]
    assert called, (
        f"redrawAll() calls none of {sorted(renderers)}, so {page}Page keeps its old "
        "language until a reload -- add the renderer to redrawAll() in the live layer"
    )


def test_the_shared_detail_drawer_does_not_survive_a_language_flip() -> None:
    """The one surface a flip cannot repaint is closed instead of left stale.

    ``#detail`` is drawn by five different openers, each from a subject only it
    holds, and it is not on any page -- so the redraws above cannot reach it.
    Left open it would sit in the old language over a page in the new one.
    """
    assert "closeDetail()" in _redraw_all_body(), (
        "redrawAll() leaves the shared #detail drawer open, so a sheet standing "
        "over a page reads in the old language after the page behind it repaints"
    )


def test_the_more_rows_are_redrawn_on_a_language_flip() -> None:
    """The rail's More group names its rows from the catalogue too."""
    renderers = _enclosing_function(BASE, "MORE_ROWS.forEach")
    assert renderers, "no renderer builds the More rows from MORE_ROWS"
    body = _redraw_all_body()
    called = [name for name in renderers if f"{name}()" in body]
    assert called, (
        f"redrawAll() calls none of {sorted(renderers)}, so a More group standing "
        "open across a flip keeps the old row names"
    )
