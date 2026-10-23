"""The first-run wizard can be finished in a phone-width window, driven in Chromium.

The wizard is a fixed frame with no close control, and its model step has no
Skip: a first run ends only through the footer's forward button, after a chat
model is picked from a pill in the roles list. On a desktop the frame keeps a
780px floor; below that width it has to give the floor up, or the pill and the
button sit past the window's right edge, where a fixed frame offers no scroll
to reach them. happy-dom applies no stylesheet and lays nothing out, and
ui-web/scripts/gates/narrow-fold-css.test.mjs reads the rules rather than the
boxes they produce, so this file loads the built ui-web/dist/index.html over its canned
`?stub=1&onboard=demo` transport and walks the wizard to its close, checking
at every stage that no control lies outside the window.

It needs that build: `npm run --prefix ui-web build && python
ui-web/build.py`. Without it the module skips; with one older than the sources
it fails, since a stale build would report on rules that are no longer there.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, Page, sync_playwright  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.slow(reason="Launches a real Chromium.")]

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "ui-web" / "dist"
PAGE = DIST / "index.html"
SOURCES = (REPO / "ui-web" / "src", REPO / "i18n")

MEASURE = """() => {
  const app = document.querySelector('.ob-app')
  if (!app) return null
  const vw = innerWidth
  const box = (el) => { const b = el.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.right)] }
  const controls = [...document.querySelectorAll('.ob-app button, .ob-app input, .ob-app select')]
    .filter((el) => el.getClientRects().length)
  return {
    floor: getComputedStyle(app).minWidth,
    app: box(app),
    outside: controls
      .filter((el) => { const b = el.getBoundingClientRect(); return b.left < -0.5 || b.right > vw + 0.5 })
      .map((el) => [el.className, (el.innerText || '').trim().slice(0, 24), box(el)]),
  }
}"""


# The wizard has moved on from the step it was on: to the next one, or closed.
STEPPED = """(was) => {
  const now = document.querySelector('.ob-app .ob-sitem[data-state=current]')
  return !now || now.innerText !== was
}"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    if not PAGE.is_file():
        pytest.skip("ui-web/dist/index.html not built (npm run --prefix ui-web build && python ui-web/build.py)")
    built = PAGE.stat().st_mtime
    newer = [p for root in SOURCES for p in root.rglob("*") if p.is_file() and p.stat().st_mtime > built]
    assert not newer, f"ui-web/dist/index.html is older than {newer[0].relative_to(REPO)}; rebuild it"
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(DIST)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        executable = shutil.which("google-chrome") or shutil.which("chromium")
        try:
            b = pw.chromium.launch(executable_path=executable, args=["--no-sandbox", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001 - no browser on this host is a skip, not a failure
            pytest.skip(f"Chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def wizard(browser: Browser, origin: str, request: pytest.FixtureRequest) -> Iterator[Page]:
    width, height = request.param
    context = browser.new_context(viewport={"width": width, "height": height}, locale="en-US", reduced_motion="reduce")
    page = context.new_page()
    page.goto(f"{origin}/index.html?stub=1&onboard=demo")
    page.wait_for_selector(".ob-app .keyfield input")
    yield page
    context.close()


def _inside(page: Page, stage: str) -> None:
    m = page.evaluate(MEASURE)
    assert m is not None, f"{stage}: the wizard is not open"
    assert m["floor"] == "0px", f"{stage}: the frame kept its floor ({m['floor']}), app box {m['app']}"
    assert m["outside"] == [], f"{stage}: controls past the window's edge (class, text, [left, right]): {m['outside']}"


@pytest.mark.parametrize("wizard", [(390, 844), (360, 740), (320, 568)], indirect=True, ids=lambda v: f"{v[0]}x{v[1]}")
def test_the_wizard_can_be_finished_at_phone_width(wizard: Page) -> None:
    page = wizard
    _inside(page, "model step")
    page.fill(".ob-app .keyfield input", "sk-test-0000")
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_role("button", name="Disconnect", exact=True).wait_for()
    _inside(page, "provider connected")
    page.locator(".ob-app .settings-pm").first.click()
    page.get_by_text("claude-sonnet-5", exact=True).first.click()
    primary = page.locator(".ob-app .ob-foot .ob-primary")
    page.wait_for_selector(".ob-app .ob-foot .ob-primary:not(:disabled)")
    _inside(page, "model picked")
    for _ in range(4):
        current = page.locator(".ob-sitem[data-state=current]").inner_text()
        primary.click()
        page.wait_for_function(STEPPED, arg=current)
        if page.locator(".ob-app").count() == 0:
            return
        page.wait_for_selector(".ob-app .ob-foot .ob-primary:not(:disabled)")
        _inside(page, page.locator(".ob-sitem[data-state=current]").inner_text())
    pytest.fail("the wizard did not close after its last step")


@pytest.mark.parametrize("wizard", [(1280, 800)], indirect=True, ids=["1280x800"])
def test_a_desktop_window_keeps_the_frame_floor(wizard: Page) -> None:
    assert wizard.evaluate("() => getComputedStyle(document.querySelector('.ob-app')).minWidth") == "780px"
