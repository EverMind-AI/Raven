"""A tasks board card's agent line stays inside the card, measured in Chromium.

A decision node's selection rides the agent line after the agent's name, its
`@instance` handle and the tool-count chip, on a card 196px wide
(`TasksPage.tsx`'s `COLUMN`). Route names have no length cap, so the line cannot
hold everything: the handle and the later routes give way first, the agent's
name next but only down to its first few letters, and the first route last, to
an ellipsis inside the line. happy-dom lays nothing out, so the gate beside the
stylesheet (`ui-web/scripts/gates/task-card-selection-css.test.mjs`) can only
read the declarations; this file renders `BoardCard`'s markup inside the
`foreignObject` `DagGraph` gives it, under the real `page.css` and
`features/tasks/styles.css`, and compares the boxes Chromium draws.

It sits in the default selection, not under `tests/integration`, because the
unit job is the one CI job that runs it: that job's runner ships a system
Chrome. Without a browser it skips with the reason, except under `CI`, where a
skip would leave the line unmeasured on every push and so fails instead.
"""

from __future__ import annotations

import html
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, Page, sync_playwright  # noqa: E402

pytestmark = pytest.mark.slow(reason="Launches a real Chromium.")

REPO = Path(__file__).resolve().parents[1]
SHEETS = (
    REPO / "ui-web" / "src" / "styles" / "page.css",
    REPO / "ui-web" / "src" / "features" / "tasks" / "styles.css",
)
CARD_W, CARD_H = 196, 78

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><style>{sheet}</style></head>
<body style="margin:0"><div class="tkfork"><div class="daggraph">
<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"><g class="nd" data-st="completed">
<foreignObject x="0" y="0" width="{w}" height="{h}"><div class="tkrun">
<span class="tkrunl1">Sort the incoming request</span>
<span class="tkrunl2">{line}</span>
<span class="tkrunrt">2.1s</span>
</div></foreignObject></g></svg></div></div>
<span id="floor" style="position:absolute;visibility:hidden;width:6ch"></span>
</body></html>"""

MEASURE = """() => {
  const line = document.querySelector('.tkrunl2'), edge = line.getBoundingClientRect().right
  const ag = line.querySelector('.tkrunag'), sel = line.querySelector('.tkrunsel')
  const floor = document.getElementById('floor'), handle = line.querySelector('.tkrunhandle')
  floor.style.font = getComputedStyle(ag).font
  const text = document.createRange()
  text.selectNodeContents(ag)
  return {
    line: line.clientWidth,
    floor: floor.getBoundingClientRect().width,
    agent: ag.getBoundingClientRect().width,
    agentFull: text.getBoundingClientRect().width,
    handle: handle ? handle.getBoundingClientRect().width : null,
    sel: sel.clientWidth,
    selFull: sel.scrollWidth,
    selTextOverflow: getComputedStyle(sel).textOverflow,
    selOverflow: getComputedStyle(sel).overflowX,
    past: [...line.children].map((c) => [c.className, c.getBoundingClientRect().right - edge]),
  }
}"""


def _line(agent: str, instance: str | None, tools: int | None, routes: list[str]) -> str:
    """The agent line as `BoardCard` writes it, the first route held apart from the rest."""
    parts = [f'<span class="tkrunag">{html.escape(agent)}</span>']
    if instance:
        parts.append(f'<span class="tkrunhandle">@{html.escape(instance)}</span>')
    if tools:
        parts.append(f'<span class="tkrunchip">{tools} tools</span>')
    whole = "\u2192 " + ", ".join(routes)
    first = "\u2192 " + routes[0] + (", " if len(routes) > 1 else "")
    parts.append(f'<span class="tkrunsel" title="{html.escape(whole)}">{html.escape(first)}</span>')
    if len(routes) > 1:
        rest = ", ".join(routes[1:])
        parts.append(f'<span class="tkrunselrest" title="{html.escape(whole)}">{html.escape(rest)}</span>')
    return "".join(parts)


def _unavailable(reason: str) -> NoReturn:
    if os.environ.get("CI"):
        pytest.fail(f"Chromium unavailable under CI, which must measure the agent line: {reason}")
    pytest.skip(f"Chromium unavailable: {reason}")


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    executable = shutil.which("google-chrome") or shutil.which("chromium")
    if executable is None:
        _unavailable("neither google-chrome nor chromium is on PATH")
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(executable_path=executable, args=["--no-sandbox", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001 - any launch failure means no browser to measure with
            _unavailable(str(exc))
        yield b
        b.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 400, "height": 200}, reduced_motion="reduce")
    yield context.new_page()
    context.close()


def _measure(page: Page, line: str) -> dict:
    sheet = "\n".join(p.read_text(encoding="utf-8") for p in SHEETS)
    page.set_content(PAGE.format(sheet=sheet, w=CARD_W, h=CARD_H, line=line))
    return page.evaluate(MEASURE)


CROWDED = [
    pytest.param("triage", None, 3, ["feature_request", "docs"], id="two-routes-and-chip"),
    pytest.param("triage", None, 3, ["needs_clarification"], id="long-route-and-chip"),
    pytest.param("triage", None, 3, ["everything_else"], id="catch-all-route-and-chip"),
    pytest.param("research_assistant", None, 3, ["needs_clarification"], id="long-agent-and-long-route"),
    pytest.param(
        "triage",
        "lane-7f3a",
        12,
        ["a_route_name_longer_than_the_whole_agent_line_can_hold", "docs"],
        id="route-longer-than-the-line",
    ),
]


@pytest.mark.parametrize(("agent", "instance", "tools", "routes"), CROWDED)
def test_a_crowded_line_keeps_the_agent_name_and_ends_the_route_in_an_ellipsis(
    page: Page, agent: str, instance: str | None, tools: int | None, routes: list[str]
) -> None:
    m = _measure(page, _line(agent, instance, tools, routes))
    assert m["line"] == CARD_W - 2 * 14 - 2, m
    assert m["agent"] >= min(m["agentFull"], m["floor"]) - 0.5, m
    assert m["agent"] <= m["agentFull"] + 0.5, m
    assert m["agent"] > 0, m
    assert m["sel"] > 0, m
    assert m["selFull"] > m["sel"], m
    assert (m["selOverflow"], m["selTextOverflow"]) == ("hidden", "ellipsis"), m
    assert [name for name, past in m["past"] if past > 0.5] == [], m


def test_a_long_agent_name_gives_way_before_a_short_route(page: Page) -> None:
    m = _measure(page, _line("research_assistant_with_a_long_name", None, 3, ["bug"]))
    assert m["sel"] == m["selFull"], m
    assert m["floor"] - 0.5 <= m["agent"] < m["agentFull"], m
    assert [name for name, past in m["past"] if past > 0.5] == [], m


def test_the_handle_gives_way_before_the_agent_name(page: Page) -> None:
    m = _measure(page, _line("research_assistant_with_a_long_name", "lane-7f3a", None, ["bug"]))
    assert m["agent"] < m["agentFull"], m
    assert m["handle"] < 0.5, m
    assert m["sel"] == m["selFull"], m


def test_a_line_with_room_leaves_everything_whole(page: Page) -> None:
    m = _measure(page, _line("triage", None, None, ["everything_else"]))
    assert m["agent"] == pytest.approx(m["agentFull"], abs=0.5), m
    assert m["sel"] == m["selFull"], m
    assert m["past"][-1][1] < -0.5, m
