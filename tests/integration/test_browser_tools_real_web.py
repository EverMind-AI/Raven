"""The ``browser_*`` tools against a real Chromium and a local form.

Skipped unless playwright and its Chromium are installed (see
test_browser_real_web.py for the install lines). The page is served from this
process so the run needs no network and asserts on copy it controls.

What is pinned is the acceptance the tools were written for: a form filled
and submitted through the tools alone, the screenshot arriving as an image
block, two owners landing in two tabs with neither able to take the other's,
a read leaving the front tab alone while an act brings the owner's tab
forward, a reader's touch reported to the model, and a popup following the
owner that opened it.
"""

from __future__ import annotations

import http.server
import os
import pwd
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from raven.agent.tools import browser as tools_mod
from raven.agent.tools.browser import (
    BrowserClickTool,
    BrowserNavigateTool,
    BrowserScreenshotTool,
    BrowserSnapshotTool,
    BrowserTabsTool,
    BrowserTypeTool,
)
from raven.browser import get_browser
from raven.browser.driver import Browser

pytestmark = pytest.mark.skipif(not Browser.probe()[0], reason="browser extra not installed")

FORM = b"""<!doctype html><html><head><title>Raven Form Test</title></head><body>
<h1>Sign up</h1>
<form method="get" action="/done">
  <label for="n">Name</label><input id="n" name="name" placeholder="Your name">
  <label for="e">Email</label><input id="e" name="email" placeholder="you@example.com">
  <label><input type="checkbox" name="agree" value="1"> I agree</label>
  <button type="submit">Create account</button>
</form>
<a href="/other" target="_blank">Open help in a new tab</a>
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/done"):
            body = f"<title>Done</title><h1>Welcome</h1><p>query: {self.path.split('?', 1)[-1]}</p>".encode()
        elif self.path.startswith("/other"):
            body = b"<title>Help</title><h1>Help page</h1>"
        else:
            body = FORM
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(autouse=True)
def _browsers_from_the_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", os.path.join(real_home, "Library", "Caches", "ms-playwright"))


@pytest.fixture
def site() -> Iterator[str]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


@pytest.fixture
async def browser() -> Iterator[Browser]:
    b = get_browser()
    try:
        yield b
    finally:
        await b.close()


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Who the tools act for; a test flips it to play a second agent."""
    who = {"id": "session:A"}
    monkeypatch.setattr(tools_mod, "current_owner", lambda: who["id"])
    return who


def _ref(text: str, needle: str) -> str:
    return next(line.split()[0] for line in text.splitlines() if line.startswith("ref_") and needle in line)


async def test_fill_and_submit_a_form_through_the_tools(browser: Browser, site: str, owner: dict[str, str]) -> None:
    nav, click, typed = BrowserNavigateTool(), BrowserClickTool(), BrowserTypeTool()

    page = await nav.execute(url=f"{site}/")
    assert "title: Raven Form Test" in page.model_text

    assert click.cast_params({"ref": "ref_1"})["site"] == "127.0.0.1"

    out = await typed.execute(text="Ada Lovelace", ref=_ref(page.model_text, "Your name"))
    assert "value='Ada Lovelace'" in out.model_text
    await typed.execute(text="ada@example.com", ref=_ref(page.model_text, "you@example.com"))
    await click.execute(ref=_ref(page.model_text, "checkbox"))
    done = await click.execute(ref=_ref(page.model_text, "Create account"))

    assert "title: Done" in done.model_text
    assert "name=Ada+Lovelace" in done.model_text
    assert "agree=1" in done.model_text


async def test_screenshot_reaches_the_model_as_an_image(browser: Browser, site: str, owner: dict[str, str]) -> None:
    await BrowserNavigateTool().execute(url=f"{site}/")

    shot = await BrowserScreenshotTool().execute()

    assert shot.blocks is not None
    image = shot.blocks[1]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "Raven Form Test" in shot.model_text


async def test_two_owners_two_tabs_and_neither_takes_the_other(
    browser: Browser, site: str, owner: dict[str, str]
) -> None:
    nav, tabs, snap = BrowserNavigateTool(), BrowserTabsTool(), BrowserSnapshotTool()

    await nav.execute(url=f"{site}/")
    owner["id"] = "run:B"
    other = await nav.execute(url=f"{site}/other")
    assert "title: Help" in other.model_text
    assert "tab: 2 of 2" in other.model_text

    listing = await tabs.execute(action="list")
    assert "(held by another agent)" in listing.model_text
    assert "(yours)" in listing.model_text
    refused = await tabs.execute(action="activate", index=0)
    assert refused.ok is False

    owner["id"] = "session:A"
    read = await snap.execute()
    assert "title: Raven Form Test" in read.model_text
    assert browser._s.page.url.endswith("/other"), "a read leaves the front tab where the last actor put it"

    acted = await nav.execute(action="reload")
    assert "title: Raven Form Test" in acted.model_text
    assert browser._s.page.url.endswith("/"), "an act brings the owner's tab to the front"


async def test_the_readers_hand_is_reported_to_the_model(browser: Browser, site: str, owner: dict[str, str]) -> None:
    nav, snap, click = BrowserNavigateTool(), BrowserSnapshotTool(), BrowserClickTool()
    page = await nav.execute(url=f"{site}/")

    await browser._s.page.focus("#n")
    await browser.insert_text("typed by a human")

    read = await snap.execute()
    assert "the user interacted with the browser" in read.model_text
    assert "value='typed by a human'" in read.model_text
    await click.execute(ref=_ref(page.model_text, "Your name"))
    assert "the user interacted with the browser" not in (await snap.execute()).model_text


async def test_a_popup_link_lands_the_owner_on_the_new_tab(browser: Browser, site: str, owner: dict[str, str]) -> None:
    page = await BrowserNavigateTool().execute(url=f"{site}/")

    out = await BrowserClickTool().execute(ref=_ref(page.model_text, "Open help"))

    assert "title: Help" in out.model_text
    assert "tab: 2 of 2" in out.model_text


async def test_a_model_fills_the_form_through_the_loop_and_the_panel_reads_the_same_page(
    browser: Browser, site: str, tmp_path: Path
) -> None:
    """The whole chain in one process, as ``raven serve`` runs it: a scripted
    model calls the default-registered tools, the permission gate asks once for
    the site and the grant covers the click that follows, the screenshot goes
    out as an image block, and the panel's ``browser.frame`` returns the page
    the model left."""
    from raven.agent.loop import AgentLoop
    from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
    from raven.contracts.permissions import ApprovalChoice, ApprovalOutcome
    from raven.permissions.turn import start_permission_turn
    from raven.rpc.methods import browser as rpc_browser
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    (Path(os.environ["HOME"]) / ".raven").mkdir(parents=True, exist_ok=True)
    (Path(os.environ["HOME"]) / ".raven" / "config.json").write_text('{"permissions": {"mode": "ask"}}')
    results: list[str] = []

    class _Model:
        step = 0

        async def chat_with_retry(self, messages, tools=None, **kwargs) -> LLMResponse:
            offered = {t["function"]["name"] for t in tools or []}
            assert {"browser_navigate", "browser_type", "browser_click", "browser_screenshot"} <= offered
            if self.step:
                results.append(messages[-1]["content"] if isinstance(messages[-1]["content"], str) else "")
            calls = {
                0: lambda: ToolCallRequest(id="c1", name="browser_navigate", arguments={"url": f"{site}/"}),
                1: lambda: ToolCallRequest(
                    id="c2",
                    name="browser_type",
                    arguments={"text": "Grace Hopper", "ref": _ref(results[-1], "Your name")},
                ),
                2: lambda: ToolCallRequest(
                    id="c3", name="browser_click", arguments={"ref": _ref(results[-1], "Create account")}
                ),
                3: lambda: ToolCallRequest(id="c4", name="browser_screenshot", arguments={}),
            }
            self.step += 1
            if self.step - 1 in calls:
                return LLMResponse(content="", tool_calls=[calls[self.step - 1]()], finish_reason="tool_calls")
            return LLMResponse(content="submitted", finish_reason="stop")

        def get_default_model(self) -> str:
            return "fake/model"

    prompts: list[dict] = []

    class _Responder:
        async def await_approval(self, **request) -> ApprovalOutcome:
            prompts.append(request)
            return ApprovalOutcome(choice=ApprovalChoice.ALLOW_SESSION)

    start_permission_turn(_Responder(), conversation_id="conv-1", turn_id="turn-1")
    agent = AgentLoop(provider=_Model(), workspace=tmp_path, model="fake/model")
    request = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
        text="sign me up",
        conversation="conv-1",
    )

    answer, _media = await agent._process_message(request, session_key="conv-1")

    assert answer == "submitted"
    assert "value='Grace Hopper'" in results[1]
    assert "title: Done" in results[2] and "name=Grace+Hopper" in results[2]
    assert len(prompts) == 1, "one grant for the site covers the type and the click"
    assert prompts[0]["command"].startswith("browser_type") and "site='127.0.0.1'" in prompts[0]["command"]
    panel = await rpc_browser.browser_frame({"quality": 40})
    assert panel["started"] and panel["jpeg"]
    assert "name=Grace+Hopper" in panel["url"]
