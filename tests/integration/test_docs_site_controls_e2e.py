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
from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402

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
    with sync_playwright() as pw:
        executable = CHROME if Path(CHROME).exists() else pw.chromium.executable_path
        if not Path(executable).exists():
            pytest.skip("no Chrome or Playwright Chromium binary to drive")
        browser = pw.chromium.launch(executable_path=executable, args=["--no-sandbox", "--disable-gpu"])
        pg = browser.new_page(viewport=DESKTOP)
        yield pg
        browser.close()


ROWS_PRESENT = "() => document.querySelectorAll('.md-search-result__link').length > 0"


def _await_results(page: Page, query: str) -> None:
    """The index is fetched and indexed off the main thread, so a query typed
    before the theme has subscribed to the field is simply never searched: the
    value is set, no input is observed, and no row ever appears. Waiting on a
    clock is what makes such a test flake, so wait for the rows, and re-type
    once if the first attempt went nowhere."""
    page.wait_for_selector(".md-search-result__meta", state="attached", timeout=15000)
    for attempt in range(3):
        try:
            page.wait_for_function(ROWS_PRESENT, timeout=5000)
            return
        except PlaywrightTimeout:
            if attempt == 2:
                raise AssertionError(f"no results for {query!r} after three attempts") from None
            page.fill(".md-search__input", "")
            page.type(".md-search__input", query, delay=20)


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


SEARCH_RESULTS = """() => [...document.querySelectorAll('.md-search-result__link')]
  .map((a) => a.getAttribute('href'))"""


HEADING_TEXT = """() => {
  const h = document.querySelector('h1').cloneNode(true);
  const anchor = h.querySelector('.headerlink');
  if (anchor) anchor.remove();
  // the Chinese build marks its word boundaries with a zero-width space, and
  // a query carrying them matches the stored token exactly -- which is a
  // thing no reader can type, so the test would prove nothing
  return h.textContent.replace(/\u200b/g, '').trim();
}"""


@pytest.mark.parametrize(("path", "language"), [("", "en"), ("zh/", "zh")])
def test_search_keeps_to_the_language_the_reader_is_in(page: Page, site: str, path: str, language: str) -> None:
    """Both languages are built from one tree, and the search index is written
    once for the pair. A reader on the English site searching an English word
    gets the Chinese page for it back, which is a result they cannot read and
    a link that leaves the language they chose.

    The query is the page's own heading rather than a written-in word, so the
    Chinese half searches Chinese without this file holding a Chinese string.
    """
    _open(page, site, path + "sandbox/")
    query = page.evaluate(HEADING_TEXT)
    assert query, f"the {language} sandbox page has no heading to search for"

    _open(page, site, path)
    page.click(".md-search__input")
    page.fill(".md-search__input", query)
    _await_results(page, query)
    hrefs = page.evaluate(SEARCH_RESULTS)
    assert hrefs, f"no results for {query!r} on the {language} site"
    stray = [h for h in hrefs if ("/zh/" in h) != (language == "zh")]
    assert not stray, f"the {language} site returned {len(stray)} result(s) from the other language: {stray[:3]}"


RAIL_FIELD = """() => {
  const e = document.querySelector('.md-header .md-search');
  const r = e.getBoundingClientRect();
  return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
}"""


RESTING_INK = """() => {
  const input = document.querySelector('.md-header .md-search__input');
  const chip = getComputedStyle(document.querySelector('.md-header .md-search__form'), '::after');
  const css = getComputedStyle(input);
  return {padLeft: css.paddingLeft, fontSize: css.fontSize,
          colour: getComputedStyle(input, '::placeholder').color,
          chipWidth: chip.width, chipRight: chip.right};
}"""


STANDING_INK = """() => {
  const css = getComputedStyle(document.querySelector('.md-header .md-search'), '::after');
  return {padLeft: css.paddingLeft, fontSize: css.fontSize, colour: css.color,
          layers: (css.backgroundImage.match(/url\\(/g) || []).length,
          position: css.backgroundPosition};
}"""


SEARCH_SHAPE = """() => {
  const b = (s) => { const e = document.querySelector(s); if (!e) return null;
    const r = e.getBoundingClientRect(), c = getComputedStyle(e);
    return {w: Math.round(r.width), h: Math.round(r.height), left: Math.round(r.left),
            centre: Math.round(r.left + r.width / 2), top: Math.round(r.top),
            position: c.position, display: c.display}; };
  return {form: b('.md-header .md-search__form'), inner: b('.md-header .md-search__inner'),
          overlay: b('.md-search__overlay'), viewportCentre: Math.round(innerWidth / 2),
          viewport: innerWidth};
}"""


def test_search_is_a_slim_field_that_opens_a_centred_dialog(page: Page, site: str) -> None:
    """At rest the field is one control in the rail, the height the reference
    layout uses. Opening it should not grow a panel out of a 274px column: the
    reference lifts the whole thing into a dialog over the page, which is the
    only shape wide enough to show a result line without cutting it."""
    _open(page, site)
    resting = page.evaluate(SEARCH_SHAPE)
    resting_rail = page.evaluate(RAIL_FIELD)
    resting_ink = page.evaluate(RESTING_INK)
    assert abs(resting["form"]["h"] - 36) <= 1, (
        f"the resting search field is {resting['form']['h']}px tall, not the reference's 36px"
    )

    page.click(".md-search__input")
    page.fill(".md-search__input", "sandbox")
    _await_results(page, "sandbox")
    open_ = page.evaluate(SEARCH_SHAPE)
    inner, overlay = open_["inner"], open_["overlay"]
    assert inner["position"] == "fixed", f"the open search is {inner['position']}, not lifted off the rail"
    assert abs(inner["centre"] - open_["viewportCentre"]) <= 2, (
        f"the dialog's centre is at {inner['centre']}px, the viewport's at {open_['viewportCentre']}px"
    )
    assert inner["w"] >= 560, f"the dialog is only {inner['w']}px wide"
    assert overlay["display"] != "none" and overlay["w"] >= open_["viewport"] - 2, (
        f"no backdrop behind the dialog: display={overlay['display']} width={overlay['w']}"
    )
    assert page.evaluate(HIT_TEST, ".md-search-result__link") == "ok"

    # the field in the rail is a fixture of the shell: opening the dialog must
    # not move or resize it, or the column it sits in changes shape on a click
    opened_rail = page.evaluate(RAIL_FIELD)
    assert opened_rail == resting_rail, f"the rail's search field changed on opening: {resting_rail} -> {opened_rail}"

    # the form itself travels into the dialog, so what stays in the rail is a
    # drawn replica. It has to take its metrics from the original rather than
    # from hand-set constants, or the two drift apart on the next type change.
    standing = page.evaluate(STANDING_INK)
    assert standing["padLeft"] == resting_ink["padLeft"], (
        f"the replica's label starts at {standing['padLeft']}, the field's at {resting_ink['padLeft']}"
    )
    assert standing["fontSize"] == resting_ink["fontSize"], (
        f"the replica's label is {standing['fontSize']}, the field's is {resting_ink['fontSize']}"
    )
    assert standing["colour"] == resting_ink["colour"], (
        f"the replica's label is {standing['colour']}, the field's is {resting_ink['colour']}"
    )
    assert standing["layers"] == 2, (
        f"the replica paints {standing['layers']} mark(s); the field shows a magnifier and a key chip"
    )

    href = page.get_attribute(".md-search-result__link", "href")
    page.click(".md-search-result__link")
    page.wait_for_timeout(700)
    assert href.split("#")[0].rstrip("/").split("/")[-1] in page.url, (
        f"clicking a result did not land on it: url is {page.url}"
    )


SETTLE_FRAMES = """async (checked) => {
  const box = document.getElementById('__search');
  const inner = document.querySelector('.md-header .md-search__inner');
  box.checked = checked;
  box.dispatchEvent(new Event('change'));
  const seen = [];
  for (let i = 0; i < 12; i++) {
    await new Promise((r) => requestAnimationFrame(r));
    seen.push(Math.round(inner.getBoundingClientRect().width));
  }
  return seen;
}"""


TIMED_PARTS = """(checked) => {
  document.getElementById('__search').checked = checked;
  const search = document.querySelector('.md-header .md-search');
  const parts = [search, ...search.querySelectorAll('*')];
  const moving = [];
  for (const e of parts) {
    // the backdrop is the page behind the dialog, not the box in the corner
    if (e.classList.contains('md-search__overlay')) continue;
    // the placeholder is its own pseudo-element and carries its own
    // transition, which reading the input alone does not show
    for (const pseudo of [null, '::placeholder', '::before', '::after']) {
      const c = getComputedStyle(e, pseudo);
      const slowest = Math.max(...c.transitionDuration.split(',').map((d) => parseFloat(d) || 0));
      if (slowest > 0) {
        moving.push((e.className || e.tagName) + (pseudo || '') + ' ' + c.transitionProperty + ' ' + c.transitionDuration);
      }
    }
  }
  return moving;
}"""


def test_the_search_box_does_not_animate_between_its_two_states(page: Page, site: str) -> None:
    """The dialog and the field in the rail are two different shapes, and the
    theme transitions between them: leaving search runs that in the rail, so
    the corner of the page is left playing an animation after the reader has
    already moved on. Each state should be reached in one frame, and nothing
    the corner is drawn from should carry a duration -- the width is only the
    part that shows most, the field's own background fades the same way."""
    _open(page, site)
    for checked, state in ((True, "opening"), (False, "leaving")):
        widths = page.evaluate(SETTLE_FRAMES, checked)
        assert len(set(widths)) == 1, f"{state} search runs a width animation: {sorted(set(widths))[:6]}"
        timed = page.evaluate(TIMED_PARTS, checked)
        assert not timed, f"{state} search still animates: " + "; ".join(timed)


CHINESE_WORD = """async () => {
  const response = await fetch('search/search_index.json');
  const index = await response.json();
  for (const entry of index.docs) {
    const words = (entry.title || '').split('\u200b').filter(Boolean);
    // a word from the middle of a title: typing it can only find the page if
    // the run was indexed as words, not kept whole
    if (words.length > 2) return {word: words[1], location: entry.location};
  }
  return null;
}"""


def test_a_chinese_reader_finds_a_page_by_typing_one_word(page: Page, site: str) -> None:
    """Chinese runs no spaces. The build segments it and marks each boundary,
    and the theme's separator breaks on that mark; miss either and the whole
    run is one token, which only answers a reader who types the run entire.
    The word searched here is taken from the middle of a title, so it is a
    word the segmenter found rather than anything written into this file."""
    _open(page, site, "zh/")
    found = page.evaluate(CHINESE_WORD)
    assert found, "no Chinese title carries word boundaries: the build is not segmenting"

    page.click(".md-search__input")
    page.fill(".md-search__input", found["word"])
    _await_results(page, found["word"])
    hrefs = page.evaluate(SEARCH_RESULTS)
    assert hrefs, f"a word from the middle of {found['location']!r} finds nothing"
    assert any(found["location"] in href for href in hrefs), (
        f"the page the word came from is not among its own results: {hrefs[:3]}"
    )


DIALOG_RIGHT_EDGE = """() => {
  const form = document.querySelector('.md-header .md-search__form');
  const button = document.querySelector('.md-header .md-search__options .md-icon');
  if (!form || !button) return null;
  const frame = form.getBoundingClientRect();
  const chip = getComputedStyle(form, '::after');
  const hidden = chip.content === 'none';
  const right = hidden ? 0 : frame.right - parseFloat(chip.right);
  const box = button.getBoundingClientRect();
  const glyph = document.querySelector('.md-header .md-search__icon[for]').getBoundingClientRect();
  return {
    chip: hidden ? null : [Math.round(right - parseFloat(chip.width)), Math.round(right)],
    button: [Math.round(box.left), Math.round(box.right)],
    buttonMid: +(box.top + box.height / 2).toFixed(1),
    glyphMid: +(glyph.top + glyph.height / 2).toFixed(1),
  };
}"""


def test_the_dialogs_clear_button_sits_alone_on_its_centre_line(page: Page, site: str) -> None:
    """The field carries a chip naming the key that opens search, and the open
    dialog puts its clear button in that same corner. The chip is still true
    while the field sits in the rail and says nothing a reader in the dialog
    can act on, so it belongs to the closed state alone. Clicking still lands
    on the button either way, which is why this measures the boxes: the fault
    is what a reader sees, not what the click does."""
    _open(page, site)
    page.click(".md-search__input")
    page.fill(".md-search__input", "sandbox")
    _await_results(page, "sandbox")

    edge = page.evaluate(DIALOG_RIGHT_EDGE)
    assert edge is not None, "the open dialog has no clear button to sit beside"
    if edge["chip"] is not None:
        chip, button = edge["chip"], edge["button"]
        overlap = min(chip[1], button[1]) - max(chip[0], button[0])
        assert overlap <= 0, f"the key chip at {chip} and the clear button at {button} overlap by {overlap}px"

    # the theme seats that button at a fixed offset from the form's top, which
    # centres it only in a form of the theme's own height
    drift = round(edge["buttonMid"] - edge["glyphMid"], 1)
    assert abs(drift) <= 1, (
        f"the clear button's centre is {drift}px off the magnifier's, so it does not sit"
        " on the line the query is typed on"
    )


TOC_FOLLOW = """() => {
  const wrap = document.querySelector('.md-sidebar--secondary .md-sidebar__scrollwrap');
  const links = [...wrap.querySelectorAll('a.md-nav__link')];
  const active = links.filter((a) => a.classList.contains('md-nav__link--raven-active'));
  if (!active.length) return null;
  const w = wrap.getBoundingClientRect();
  const f = active[0].getBoundingClientRect();
  const max = wrap.scrollHeight - wrap.clientHeight;
  return {
    inView: f.top >= w.top - 1 && f.bottom <= w.bottom + 1,
    offCentre: Math.round((f.top + f.height / 2 - w.top) - wrap.clientHeight / 2),
    clamped: wrap.scrollTop <= 1 || wrap.scrollTop >= max - 1,
    label: active[0].textContent.trim().slice(0, 28),
  };
}"""


TOC_SETTLED = """async () => {
  const wrap = document.querySelector('.md-sidebar--secondary .md-sidebar__scrollwrap');
  const frame = () => new Promise((r) => requestAnimationFrame(r));
  let last = null;
  for (let i = 0; i < 30; i++) {
    await frame();
    if (wrap.scrollTop === last) return wrap.scrollTop;
    last = wrap.scrollTop;
  }
  return wrap.scrollTop;
}"""


def test_the_contents_column_follows_the_reader(page: Page, site: str) -> None:
    """The column scrolls independently of the page, so on a long outline the
    lit entry walks off its bottom edge and the reader loses their place. The
    reference layout centres the first lit entry in the column and clamps at
    both ends, which is what is checked here at points down a long page."""
    _open(page, site, "sandbox/")
    height = page.evaluate("() => document.body.scrollHeight")
    strays = []
    for step in range(1, 14):
        page.evaluate(f"() => window.scrollTo(0, {height} * {step / 14})")
        page.evaluate(TOC_SETTLED)
        seen = page.evaluate(TOC_FOLLOW)
        if not seen:
            continue
        if not seen["inView"]:
            strays.append(f"{seen['label']!r} is outside the column")
        elif not seen["clamped"] and abs(seen["offCentre"]) > 40:
            strays.append(f"{seen['label']!r} sits {seen['offCentre']}px off the column's centre")
    assert not strays, "; ".join(strays)


COLUMN_REACH = """() => {
  const R = (s) => { const e = document.querySelector(s); return e ? e.getBoundingClientRect() : null; };
  const rail = R('.md-sidebar--primary');
  const railWrap = R('.md-sidebar--primary .md-sidebar__scrollwrap');
  const toc = R('.md-sidebar--secondary .md-sidebar__scrollwrap');
  const foot = R('.md-header__option');
  if (!rail || !railWrap || !toc || !foot) return 'a column is missing';
  const wrap = document.querySelector('.md-sidebar--secondary .md-sidebar__scrollwrap');
  return {
    railShortBy: Math.round((rail.bottom - (foot.height + 32)) - railWrap.bottom),
    tocShortBy: Math.round(innerHeight - toc.bottom),
    tocScrollbar: getComputedStyle(wrap).scrollbarWidth,
    tocScrolls: wrap.scrollHeight > wrap.clientHeight,
    tocMask: getComputedStyle(wrap).maskImage || getComputedStyle(wrap).webkitMaskImage,
  };
}"""


@pytest.mark.parametrize("height", [700, 780, 900])
def test_both_columns_scroll_the_whole_way_down(page: Page, site: str, height: int) -> None:
    """Material's script writes an inline height on each column's scroll area,
    sized for the header bar and sidebars it ships. Under this shell both are
    too short, which cuts the list off partway down the screen and leaves dead
    space below it -- the reader sees the column's own end as an obstruction."""
    page.set_viewport_size({"width": 1440, "height": height})
    _open(page, site, "sandbox/")
    page.wait_for_timeout(400)
    reach = page.evaluate(COLUMN_REACH)
    assert isinstance(reach, dict), reach
    assert abs(reach["railShortBy"]) <= 2, f"the rail's list stops {reach['railShortBy']}px above the foot band"
    assert abs(reach["tocShortBy"]) <= 2, (
        f"the table of contents stops {reach['tocShortBy']}px above the foot of the screen"
    )
    assert reach["tocScrollbar"] == "none", (
        f"the table of contents paints a {reach['tocScrollbar']} scrollbar over its own entries"
    )
    # The current outline fits at 900px; smaller viewports must exercise overflow.
    if height <= 780:
        assert reach["tocScrolls"], "the contents column is not scrollable, so this page proves nothing"
    mask = reach["tocMask"]
    assert "gradient" in mask and mask.rstrip(")").rstrip().endswith("0"), (
        f"the contents column ends on a hard edge rather than fading out: mask is {mask!r}"
    )


PAGE_FOOT = r"""() => {
  window.scrollTo(0, document.body.scrollHeight);
  const R = (e) => e.getBoundingClientRect();
  const hits = (a, b) =>
    !(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top);
  const columns = [['the table of contents', '.md-sidebar--secondary'],
                   ['the navigation rail', '.md-sidebar--primary']];
  const clashes = [];
  for (const link of document.querySelectorAll('.md-footer__link')) {
    const f = R(link);
    const label = (link.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 24);
    for (const [name, sel] of columns) {
      const col = document.querySelector(sel);
      if (col && hits(f, R(col))) clashes.push(`footer link ${label} runs under ${name}`);
    }
  }
  const band = R(document.querySelector('.md-footer'));
  for (const entry of document.querySelectorAll('.md-sidebar--secondary a.md-nav__link')) {
    const r = R(entry);
    if (r.height && hits(r, band)) {
      clashes.push(`contents entry "${(entry.textContent || '').trim().slice(0, 20)}" sits in the footer`);
    }
  }
  return clashes;
}"""


@pytest.mark.parametrize("path", ["", "proactivity/", "sandbox/"])
def test_the_footer_keeps_to_the_content_column(page: Page, site: str, path: str) -> None:
    """The rail and the table of contents are taken out of flow and fixed to the
    viewport, so the document below them keeps the full width unless it is told
    otherwise. The footer is the part a reader reaches by scrolling to the end,
    which is where it and a fixed column are drawn over each other."""
    _open(page, site, path)
    page.wait_for_timeout(300)
    clashes = page.evaluate(PAGE_FOOT)
    assert not clashes, "at the end of the page: " + "; ".join(clashes)


CONTENT_BOUNDS = """() => {
  const content = document.querySelector('.md-content').getBoundingClientRect();
  const toc = document.querySelector('.md-sidebar--secondary').getBoundingClientRect();
  const cards = [...document.querySelectorAll('.md-typeset .grid.cards > ul > li')]
    .map(e => e.getBoundingClientRect());
  return {
    contentRight: content.right,
    tocLeft: toc.width ? toc.left : innerWidth,
    cardsRight: Math.max(content.right, ...cards.map(r => r.right)),
    pageOverflow: document.documentElement.scrollWidth - innerWidth,
  };
}"""


@pytest.mark.parametrize("width", [390, 768, 960, 1219, 1220, 1280, 1440, 1920])
@pytest.mark.parametrize("path", ["", "zh/", "playbooks/", "zh/playbooks/"])
def test_content_stays_clear_of_the_contents_column(page: Page, site: str, width: int, path: str) -> None:
    """Fixed sidebars leave no space in flow, including at the desktop breakpoint."""
    page.set_viewport_size({"width": width, "height": 900})
    try:
        _open(page, site, path)
        for fraction in (0, 0.5):
            page.evaluate("(fraction) => window.scrollTo(0, document.body.scrollHeight * fraction)", fraction)
            bounds = page.evaluate(CONTENT_BOUNDS)
            assert bounds["contentRight"] <= bounds["tocLeft"] + 1, (
                f"{path or '/'} at {width}px: body overlaps the contents column: {bounds}"
            )
            assert bounds["cardsRight"] <= bounds["contentRight"] + 1, (
                f"{path or '/'} at {width}px: cards overflow the body: {bounds}"
            )
            assert bounds["pageOverflow"] <= 1, f"{path or '/'} at {width}px: horizontal page overflow: {bounds}"
    finally:
        page.set_viewport_size(DESKTOP)


RAIL_FOOT = """() => {
  const sw = document.querySelector('.md-sidebar--primary .md-sidebar__scrollwrap');
  if (sw) sw.scrollTop = sw.scrollHeight;
  const opt = document.querySelector('.md-header__option');
  const src = document.querySelector('.md-header__source');
  if (!opt || !src) return 'the rail foot has no controls';
  const a = opt.getBoundingClientRect(), b = src.getBoundingClientRect();
  const band = {top: Math.min(a.top, b.top), bottom: Math.max(a.bottom, b.bottom)};
  const rail = document.querySelector('.md-sidebar--primary').getBoundingClientRect();
  const y = (band.top + band.bottom) / 2;
  const leaks = [];
  for (let x = Math.ceil(b.right) + 8; x < rail.right - 2; x += 12) {
    const el = document.elementFromPoint(x, y);
    if (!el) continue;
    if (el.closest('.md-sidebar__scrollwrap') || el.closest('.md-nav')) {
      leaks.push(`(${Math.round(x)},${Math.round(y)}) -> ${el.tagName.toLowerCase()}.${(el.className || '').toString().split(' ')[0]}`);
    }
  }
  const links = [...document.querySelectorAll('.md-sidebar--primary a.md-nav__link')];
  const tail = links[links.length - 1];
  const t = tail.getBoundingClientRect();
  const under = document.elementFromPoint(t.x + t.width / 2, t.y + t.height / 2);
  return {
    leaks,
    tail: tail.textContent.trim().slice(0, 30),
    tailClear: t.bottom <= band.top + 0.5,
    tailClickable: !!under && (tail.contains(under) || under.contains(tail)),
  };
}"""


def test_rail_foot_is_a_box_the_navigation_cannot_show_through(page: Page, site: str) -> None:
    """The language and repository buttons sit at the foot of the rail, over the
    same column the navigation tree scrolls in. Without a band of its own the
    tree scrolls right up behind them, so a heading and an icon share a row --
    and the band then has to leave the last entry somewhere to scroll to."""
    _open(page, site)
    foot = page.evaluate(RAIL_FOOT)
    assert isinstance(foot, dict), foot
    assert not foot["leaks"], "navigation shows through the rail foot at: " + "; ".join(foot["leaks"])
    assert foot["tailClear"] and foot["tailClickable"], (
        f"scrolled to the end, {foot['tail']!r} is not clear of the foot band "
        f"(clear={foot['tailClear']}, clickable={foot['tailClickable']})"
    )


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
