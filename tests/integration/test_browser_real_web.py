"""The browser against a real page, with a real Chromium.

Skipped unless playwright and its Chromium are both installed: from a source
checkout `uv sync --all-extras && uv run playwright install chromium`; on an
installed raven, `<raven's python> -m playwright install chromium`.

What this pins is the loop the agent actually walks -- navigate, read the page
into refs, click a ref, land somewhere else -- because that is the part that
cannot be proven with a stub. Nothing here asserts on page copy beyond a title
that has been stable for two decades.
"""

from __future__ import annotations

import os
import pwd

import pytest

from raven.browser import get_browser
from raven.browser.driver import Browser

pytestmark = pytest.mark.skipif(not Browser.probe()[0], reason="browser extra not installed")


@pytest.fixture
async def browser():
    b = get_browser()
    try:
        yield b
    finally:
        await b.close()


async def test_navigate_read_and_click_a_ref(browser) -> None:
    """The agent's loop: open, read refs, click one, arrive somewhere else."""
    state = await browser.goto("example.com")
    assert state["started"] is True
    assert "example.com" in state["url"]
    assert state["title"] == "Example Domain"

    snap = await browser.snapshot()
    assert snap["refs"], "a page with a link must yield at least one ref"
    link = next(r for r in snap["refs"] if r["role"] == "link")
    assert link["ref"].startswith("ref_")

    after = await browser.click(ref=link["ref"])
    assert after["url"] != state["url"]

    back = await browser.go("back")
    assert "example.com" in back["url"]


async def test_screenshot_is_a_real_jpeg(browser) -> None:
    """The panel draws this straight into an <img>, so it has to decode."""
    import base64

    await browser.goto("example.com")

    shot = await browser.screenshot(quality=40)
    raw = base64.b64decode(shot)

    assert raw[:2] == b"\xff\xd8", "JPEG SOI marker"
    assert len(raw) > 1000


async def test_screencast_pushes_frames_at_the_asked_viewport(browser) -> None:
    """The live view's whole premise: resize to the panel, frames arrive pushed.

    One deliberate scroll guarantees at least one paint after the cast starts,
    so the wait below is bounded by activity we caused, not by hoping the page
    repaints on its own.
    """
    import asyncio
    import base64

    await browser.goto("example.com")

    frames: list[tuple[str, dict]] = []
    got_one = asyncio.Event()

    def sink(jpeg: str, meta: dict) -> None:
        frames.append((jpeg, meta))
        got_one.set()

    await browser.start_stream(sink, width=640, height=480, quality=40)
    assert browser.streaming is True
    assert browser.viewport == (640, 480)
    await browser.scroll(dy=40)
    await asyncio.wait_for(got_one.wait(), timeout=10)
    await browser.stop_stream()

    assert browser.streaming is False
    raw = base64.b64decode(frames[0][0])
    assert raw[:2] == b"\xff\xd8", "screencast frames are JPEGs"


async def test_raw_pointer_and_ime_text_reach_the_page(browser) -> None:
    """down/up at coordinates is a click; insert_text lands composed text."""
    await browser.goto("example.com")
    page = browser._s.page
    await page.evaluate(
        "() => { const i = document.createElement('input'); i.id = 'probe';"
        " i.style.cssText = 'position:fixed;left:10px;top:10px;width:200px';"
        " document.body.appendChild(i); }"
    )

    await browser.mouse("down", x=60, y=20)
    await browser.mouse("up", x=60, y=20)
    await browser.insert_text("你好, raven")

    assert await page.evaluate("() => document.getElementById('probe').value") == "你好, raven"


async def test_the_profile_outlives_a_relaunch(browser) -> None:
    """Logins must survive restarts and the pop-out relaunch: cookies live in
    the persistent profile, not in the process."""
    await browser.goto("example.com")
    await browser._s.page.evaluate("() => { document.cookie = 'raven_probe=1; max-age=600'; }")
    await browser.close()

    await browser.goto("example.com")
    got = await browser._s.page.evaluate("() => document.cookie")

    assert "raven_probe=1" in got


async def test_chromium_starts_only_on_demand(browser) -> None:
    """state() is called on every panel draw and must not launch anything."""
    assert browser.started is False
    assert (await browser.state())["started"] is False

    await browser.goto("example.com")

    assert browser.started is True


@pytest.fixture(autouse=True)
def _browsers_from_the_real_home(monkeypatch):
    """The suite redirects HOME to a temp dir; playwright keeps its browsers
    under the real one. Point it there unless the caller already did."""
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", os.path.join(real_home, "Library", "Caches", "ms-playwright"))
