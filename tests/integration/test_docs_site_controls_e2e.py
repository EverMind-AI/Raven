"""Every control on the built documentation site, clicked in a real browser.

The site's shell moves Material's header into a fixed left rail. Material sizes
and places its popups for the header bar it ships with -- the language menu
opens downward from `top: calc(100% - .2rem)`, the search panel lays its results
out at a fixed `34.4rem`. Relocating a control does not relocate the popup that
hangs off it, and a popup that lands outside the viewport or outside its own
panel still reports a box, still animates, and still answers `querySelector`.
Only a hit test against the rendered page catches it, so that is what this file
does: for each control, the point a reader would click must be inside the
viewport and must belong to the control.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, sync_playwright  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs-site"
CHROME = "/usr/bin/google-chrome"
DESKTOP = {"width": 1440, "height": 900}

HIT_TEST = """(sel) => {
  const e = document.querySelector(sel);
  if (!e) return 'element absent';
  const r = e.getBoundingClientRect();
  if (!r.width || !r.height) return `zero box ${r.width}x${r.height}`;
  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight)
    return `centre (${Math.round(cx)},${Math.round(cy)}) outside the ${innerWidth}x${innerHeight} viewport`;
  const top = document.elementFromPoint(cx, cy);
  if (e.contains(top) || (top && top.contains(e))) return 'ok';
  return `covered by ${top.tagName.toLowerCase()}.${(top.className || '').toString().split(' ')[0]}`;
}"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not shutil.which("uv"):
        pytest.skip("uv is required to build the documentation site")
    # site_url puts the project site under /Raven/, and the language switcher's
    # links are absolute, so the server has to mount it at that same path.
    root = tmp_path_factory.mktemp("site")
    out = root / "Raven"
    build = subprocess.run(
        ["uv", "run", "--group", "docs", "mkdocs", "build", "--strict", "-d", str(out)],
        cwd=DOCS,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.skip(f"mkdocs build unavailable: {build.stderr[-400:]}")

    port = _free_port()
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    try:
        yield f"http://127.0.0.1:{port}/Raven/"
    finally:
        httpd.shutdown()


@pytest.fixture(scope="module")
def page(site: str) -> Iterator[Page]:
    if not Path(CHROME).exists():
        pytest.skip("no chrome binary to drive")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox", "--disable-gpu"])
        pg = browser.new_page(viewport=DESKTOP)
        yield pg
        browser.close()


def _open(page: Page, site: str, path: str = "") -> None:
    page.goto(site + path, wait_until="domcontentloaded")
    page.wait_for_selector(".md-content")
    page.wait_for_timeout(400)


def test_language_menu_opens_inside_the_viewport(page: Page, site: str) -> None:
    _open(page, site)
    assert page.evaluate(HIT_TEST, ".md-select .md-header__button") == "ok"
    page.hover(".md-select")
    page.wait_for_timeout(400)
    assert page.evaluate(HIT_TEST, ".md-select__inner") == "ok"
    page.click('.md-select__link[href*="/zh/"]')
    page.wait_for_timeout(600)
    assert "/zh/" in page.url


def test_search_results_stay_inside_the_rail_panel(page: Page, site: str) -> None:
    _open(page, site)
    page.click(".md-search__input")
    page.fill(".md-search__input", "docker")
    page.wait_for_timeout(1200)
    assert page.evaluate("() => document.querySelectorAll('.md-search-result__link').length") > 0
    overflow = page.evaluate(
        """() => {
          const panel = document.querySelector('.md-search__output').getBoundingClientRect();
          const worst = [...document.querySelectorAll('.md-search__scrollwrap, .md-search-result__link')]
            .map(e => Math.round(e.getBoundingClientRect().right - panel.right))
            .reduce((a, b) => Math.max(a, b), 0);
          return worst;
        }"""
    )
    assert overflow <= 1, f"result rows run {overflow}px past the panel's right edge"
    assert page.evaluate(HIT_TEST, ".md-search-result__link") == "ok"


BRAND_LOCKUP = """() => {
  const mark = document.querySelector('.md-header__button.md-logo');
  const word = document.querySelector('.md-header__title .md-header__topic .md-ellipsis');
  if (!mark || !word) return null;
  const m = mark.getBoundingClientRect();
  const range = document.createRange();
  range.selectNodeContents(word);
  const w = range.getBoundingClientRect();
  return {
    text: word.textContent.trim(),
    mark: m.y + m.height / 2,
    word: w.y + w.height / 2,
    markHeight: m.height,
    wordHeight: w.height,
  };
}"""


def test_wordmark_shares_the_centre_line_of_the_mark(page: Page, site: str) -> None:
    """The mark and the word next to it read as one lockup only if they share a
    centre line. Both are anchored to the same row top, so the wordmark's line
    box has to be the mark's box; a taller line box drops the word by half the
    difference."""
    _open(page, site)
    lockup = page.evaluate(BRAND_LOCKUP)
    assert lockup is not None, "no brand lockup in the rail header"
    assert lockup["text"] == "Raven"
    drop = lockup["word"] - lockup["mark"]
    assert abs(drop) <= 1, (
        f"'Raven' sits {drop:+.2f}px off the mark's centre line "
        f"(mark {lockup['markHeight']:.1f}px tall, word box {lockup['wordHeight']:.1f}px)"
    )


JOIN = 6
"""Half the run an entry boundary turns through, matching the stylesheet."""

TOC_GEOMETRY = """() => {
  const list = document.querySelector('.md-sidebar--secondary .md-nav--secondary > .md-nav__list');
  if (!list) return null;
  const origin = list.getBoundingClientRect().top;
  const rows = [];
  const walk = (ul, depth) => {
    for (const li of ul.querySelectorAll(':scope > .md-nav__item')) {
      const a = li.querySelector(':scope > .md-nav__link');
      if (a) {
        const box = a.getBoundingClientRect();
        const id = (a.getAttribute('href') || '').slice(1);
        const heading = id ? document.getElementById(id) : null;
        const hb = heading && heading.getBoundingClientRect();
        rows.push({
          depth,
          top: box.top - origin,
          bottom: box.bottom - origin,
          pad: parseFloat(getComputedStyle(a).paddingLeft),
          active: a.classList.contains('md-nav__link--raven-active'),
          inView: Boolean(hb) && hb.bottom > 0 && hb.top < innerHeight,
        });
      }
      const nested = li.querySelector(':scope > .md-nav > .md-nav__list');
      if (nested) walk(nested, depth + 1);
    }
  };
  walk(list, 0);
  const rect = list.querySelector('.raven-toc-progress clipPath rect');
  return {
    rows,
    path: list.querySelector('.raven-toc-progress__base').getAttribute('d'),
    lit: list.querySelector('.raven-toc-progress__active').getAttribute('d'),
    clipTop: parseFloat(rect.getAttribute('y')),
    clipHeight: parseFloat(rect.getAttribute('height')),
  };
}"""


def _column(depth: int) -> int:
    return 1 + depth * 10


def _run(rows: list[dict], index: int) -> tuple[float, float]:
    row = rows[index]
    return (
        row["top"] if index == 0 else row["top"] + JOIN,
        row["bottom"] if index == len(rows) - 1 else row["bottom"] - JOIN,
    )


def _points(path: str) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in re.findall(r"[ML]([\d.-]+) ([\d.-]+)", path)]


def test_table_of_contents_rail_is_one_polyline_through_the_outline(page: Page, site: str) -> None:
    """The rail is a single line walked in document order.

    A run per entry at that entry's own column, and the gap between two entries
    is where the line changes column, so a depth change draws as a diagonal and
    a same-depth boundary draws as plain vertical. Drawing a line per nesting
    level instead leaves a second line running through the parent rows.
    """
    _open(page, site, "quick-start/")
    page.evaluate("document.getElementById('install-raven').scrollIntoView()")
    page.wait_for_timeout(500)
    geometry = page.evaluate(TOC_GEOMETRY)
    rows = geometry["rows"]
    assert len({row["depth"] for row in rows}) > 1, "this page needs two heading levels"

    for above, below in zip(rows, rows[1:]):
        assert above["bottom"] == pytest.approx(below["top"], abs=0.5), (
            "entries must meet, or the rail is drawn across a gap"
        )

    for row in rows:
        assert row["pad"] == pytest.approx(14 + row["depth"] * 12, abs=1), (
            "the indent follows the heading level, not whether the entry has children"
        )

    expected = [(_column(rows[0]["depth"]), _run(rows, 0)[0])]
    for index, row in enumerate(rows):
        expected.append((_column(row["depth"]), _run(rows, index)[1]))
        if index + 1 < len(rows):
            expected.append((_column(rows[index + 1]["depth"]), _run(rows, index + 1)[0]))

    drawn = _points(geometry["path"])
    assert len(drawn) == len(expected)
    for got, want in zip(drawn, expected):
        assert got[0] == pytest.approx(want[0], abs=0.01)
        assert got[1] == pytest.approx(want[1], abs=0.5)

    turns = sum(1 for a, b in zip(drawn, drawn[1:]) if a[0] != b[0])
    changes = sum(1 for a, b in zip(rows, rows[1:]) if a["depth"] != b["depth"])
    assert turns == changes, "one column change per depth change, no more"
    assert geometry["lit"] == geometry["path"], "the lit stretch is that same line, clipped"


def test_table_of_contents_lights_every_heading_on_screen(page: Page, site: str) -> None:
    """Current means the heading is in the viewport, so several can be current.

    The lit stretch then runs from the first current entry to the last without
    a break, which is what carries the colour across a diagonal when a parent
    and the children under it are on screen together.
    """
    _open(page, site, "quick-start/")
    for anchor in ("install-raven", "from-a-source-checkout", "configure-the-first-provider"):
        page.evaluate(f"document.getElementById('{anchor}').scrollIntoView()")
        page.wait_for_timeout(500)
        geometry = page.evaluate(TOC_GEOMETRY)
        rows = geometry["rows"]

        assert [row["active"] for row in rows] == [row["inView"] for row in rows], anchor

        current = [index for index, row in enumerate(rows) if row["active"]]
        assert current, anchor
        assert current == list(range(current[0], current[-1] + 1)), "current entries are contiguous"

        top = _run(rows, current[0])[0]
        bottom = _run(rows, current[-1])[1]
        assert geometry["clipTop"] == pytest.approx(top, abs=0.5), anchor
        assert geometry["clipHeight"] == pytest.approx(bottom - top, abs=0.5), anchor


@pytest.mark.parametrize(
    ("path", "selector", "control"),
    [
        ("", ".md-header__button.md-logo", "logo"),
        ("", ".md-sidebar--primary .md-nav__link[href]:not([href^='#'])", "rail link"),
        ("", ".md-header__source .md-source", "repository link"),
        ("self-hosting/", "[data-clipboard-target]", "code copy button"),
        ("self-hosting/", ".md-sidebar--secondary .md-nav__link", "table of contents"),
        ("self-hosting/", ".md-typeset .headerlink", "heading permalink"),
        ("webui/", ".md-footer__link--prev", "footer previous"),
        ("webui/", ".md-footer__link--next", "footer next"),
    ],
)
def test_control_is_reachable(page: Page, site: str, path: str, selector: str, control: str) -> None:
    _open(page, site, path)
    assert page.evaluate(HIT_TEST, selector) == "ok", control
