"""The assistant's card and the composer share a right edge, measured in Chromium.

`--col` is the one width the transcript column and the composer card both read,
so the reply the reader is reading and the field they type into end on the same
line. A width written into one of them as a literal keeps matching only until
the token moves -- and happy-dom lays nothing out, so no vitest case can see the
two edges part. This file renders the real stylesheet around the chain of boxes
the app builds (`.chat > .scroll > .col > lane > .turn.ai > .msg.ai`, and
`.chat > .dock > .dock-in`) and compares the boxes Chromium draws.

`--desk-reserve` is what the anchored desk palette publishes on the root while
it sits over the chat, and changes every frame while its corner grab is dragged
(`data-desk-resize` on the root turns the transitions off for that gesture).
The chat's own width is what changes when the workspace column beside it is
dragged. Both are swept.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, Page, sync_playwright  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.slow(reason="Launches a real Chromium.")]

REPO = Path(__file__).resolve().parents[2]
SHEET = REPO / "ui-web" / "src" / "styles" / "page.css"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><style>{sheet}</style></head>
<body style="margin:0">
<div class="chat" style="height:800px">
  <div class="scroll" id="scroll"><div class="col" id="stage"><div style="display:contents">
    <div class="turn me"><div class="msg me">question</div></div>
    <div class="turn ai"><div class="msg ai"><div class="answer"><div class="prose">{prose}</div></div></div></div>
  </div></div></div>
  <div class="dock"><div class="dock-in"><div class="field"><textarea></textarea></div></div></div>
</div>
</body></html>"""

EDGES = """() => {
  const box = (s) => document.querySelector(s).getBoundingClientRect()
  const ai = box('.msg.ai'), dock = box('.dock-in')
  return { aiLeft: ai.left, aiRight: ai.right, dockLeft: dock.left, dockRight: dock.right }
}"""


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
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1600, "height": 900}, reduced_motion="reduce")
    p = context.new_page()
    p.set_content(PAGE.format(sheet=SHEET.read_text(encoding="utf-8"), prose="word " * 400))
    yield p
    context.close()


def _layout(page: Page, *, chat_width: int, reserve: int) -> dict[str, float]:
    page.evaluate(
        """([w, r]) => {
          document.querySelector('.chat').style.width = w + 'px'
          const root = document.documentElement.style
          if (r > 0) root.setProperty('--desk-reserve', r + 'px')
          else root.removeProperty('--desk-reserve')
        }""",
        [chat_width, reserve],
    )
    return page.evaluate(EDGES)


@pytest.mark.parametrize("reserve", [0, 362])
@pytest.mark.parametrize("chat_width", [1600, 1336, 1100, 760])
def test_the_assistant_card_ends_where_the_composer_ends(page: Page, chat_width: int, reserve: int) -> None:
    e = _layout(page, chat_width=chat_width, reserve=reserve)
    assert e["aiRight"] == pytest.approx(e["dockRight"], abs=0.5), e
    assert e["aiLeft"] == pytest.approx(e["dockLeft"], abs=0.5), e


def test_the_edges_stay_together_while_the_desk_corner_is_dragged(page: Page) -> None:
    page.evaluate("() => { document.documentElement.dataset.deskResize = 'true' }")
    parted = []
    for reserve in range(0, 520, 40):
        e = _layout(page, chat_width=1600, reserve=reserve)
        if abs(e["aiRight"] - e["dockRight"]) > 0.5:
            parted.append((reserve, round(e["aiRight"], 1), round(e["dockRight"], 1)))
    assert parted == [], "(reserve, card right, composer right) where the edges parted"
