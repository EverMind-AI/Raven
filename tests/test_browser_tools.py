"""The ``browser_*`` tools and the driver's tab ownership, without Chromium.

What is pinned here is the contract between the model and the shared page:
every acting call reads the page back, the owner of a call is the run or the
conversation, two owners get two tabs, a reader's touch is reported to the
model once, and the permission gate is handed the site a call lands on. A real
Chromium run belongs in tests/integration/test_browser_tools_real_web.py.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from raven.agent.tools import browser as tools_mod
from raven.agent.tools.browser import (
    BROWSER_TOOL_NAMES,
    BrowserClickTool,
    BrowserNavigateTool,
    BrowserScreenshotTool,
    BrowserSnapshotTool,
    BrowserTabsTool,
    BrowserTypeTool,
    browser_tools,
    current_owner,
)
from raven.agent.tools.registry import admit_tool
from raven.browser import driver as driver_module
from raven.browser.driver import MAX_TABS, Browser, _Owner, get_browser


@pytest.fixture(autouse=True)
def fresh_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver_module, "_BROWSER", None)
    monkeypatch.setattr(tools_mod._BrowserTool, "_acted", {})
    monkeypatch.setattr(tools_mod, "_headful_attempted", False)


class _FakePage:
    def __init__(self, url: str = "about:blank", title: str = "") -> None:
        self.url = url
        self._title = title
        self._closed = False
        self._raven_loading = False
        self.front = 0

    def is_closed(self) -> bool:
        return self._closed

    async def title(self) -> str:
        return self._title

    async def close(self) -> None:
        self._closed = True

    async def bring_to_front(self) -> None:
        self.front += 1

    async def evaluate(self, js: str, *args: Any) -> Any:
        return {"url": self.url, "title": self._title, "text": f"text of {self._title}", "refs": []}


class _FakeContext:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages

    async def new_page(self) -> _FakePage:
        page = _FakePage("about:blank", f"tab{len(self.pages)}")
        self.pages.append(page)
        return page


def _running(
    b: Browser, pages: list[_FakePage], active: int = 0, monkeypatch: pytest.MonkeyPatch | None = None
) -> None:
    b._s.context = _FakeContext(pages)
    b._s.page = pages[active]

    async def ensure() -> Any:
        return b._s.page

    async def restream() -> None:
        pass

    async def reach(page: Any = None) -> tuple[bool, bool]:
        return False, False

    b._ensure = ensure  # type: ignore[method-assign]
    b._restream = restream  # type: ignore[method-assign]
    b._history_reach = reach  # type: ignore[method-assign]
    b._wire = lambda page: None  # type: ignore[method-assign]


# ── admission and defaults ─────────────────────────────────────────────


def test_every_browser_tool_is_admissible_and_declares_configured() -> None:
    for tool in browser_tools():
        spec = admit_tool(tool)
        assert spec.configured is not None, tool.name


def test_reading_and_moving_verbs_are_allowed_by_default_and_acting_verbs_ask() -> None:
    from raven.contracts.permissions import Tier
    from raven.permissions.rules import default_tier

    assert {n for n in BROWSER_TOOL_NAMES if default_tier(n) is Tier.ALLOW} == {
        "browser_navigate",
        "browser_snapshot",
        "browser_screenshot",
        "browser_scroll",
        "browser_tabs",
    }
    assert {n for n in BROWSER_TOOL_NAMES if default_tier(n) is Tier.ASK} == {
        "browser_click",
        "browser_type",
        "browser_press",
    }


def test_a_site_grant_covers_click_and_type_alike() -> None:
    """One approval per site, not one per verb and not one per ref."""
    from raven.permissions.builtin import action_digest, session_keys

    click = session_keys("browser_click", {"ref": "ref_1", "site": "example.com"})
    typed = session_keys("browser_type", {"text": "x", "ref": "ref_9", "site": "EXAMPLE.com"})
    other = session_keys("browser_click", {"ref": "ref_1", "site": "other.com"})

    assert click == typed
    assert click != other
    assert session_keys("browser_click", {"ref": "ref_1"}) == (action_digest("browser_click", {"ref": "ref_1"}),)


def test_acting_tools_write_the_site_into_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate only sees parameters, so the site rides in them; a site the
    model wrote itself is replaced, never trusted."""
    b = get_browser()
    p = _FakePage("https://shop.example.com/cart")
    _running(b, [p])
    monkeypatch.setattr(tools_mod, "current_owner", lambda: "session:x")

    out = BrowserClickTool().cast_params({"ref": "ref_2", "site": "attacker.test"})

    assert out == {"ref": "ref_2", "site": "shop.example.com"}
    assert BrowserClickTool().cast_params({"ref": "ref_2"})["site"] == "shop.example.com"


def test_no_page_means_no_site() -> None:
    assert "site" not in BrowserTypeTool().cast_params({"text": "hi"})


# ── owner identity ────────────────────────────────────────────────────


def test_owner_is_the_run_when_one_is_collecting_else_the_conversation() -> None:
    from raven.agent.subagent import activity
    from raven.token_wise import usage_context

    with usage_context.bind("conv-1"):
        assert current_owner() == "session:conv-1"
        with activity.collecting() as run:
            assert current_owner() == f"run:{id(run):x}"
    assert current_owner() == "session:default"


# ── tab ownership in the driver ───────────────────────────────────────


async def test_first_owner_takes_the_active_tab_and_the_second_gets_a_new_one() -> None:
    b = get_browser()
    p0 = _FakePage("https://a.test", "A")
    _running(b, [p0])

    a_page = await b._page_for("session:a")
    b_page = await b._page_for("run:b")

    assert a_page is p0
    assert b_page is not p0
    assert b._s.owners["session:a"].page is p0
    assert b._s.owners["run:b"].page is b_page
    assert b._s.page is b_page, "an owner's act brings its tab to the front"


async def test_a_read_does_not_move_the_front_tab_but_an_act_does() -> None:
    b = get_browser()
    p0, p1 = _FakePage("https://a.test", "A"), _FakePage("https://b.test", "B")
    _running(b, [p0, p1], active=1)
    b._s.owners["session:a"] = _Owner(p0, time.monotonic())

    await b._page_for("session:a", act=False)
    assert b._s.page is p1

    await b._page_for("session:a")
    assert b._s.page is p0
    assert p0.front == 1


async def test_an_idle_owner_loses_its_tab_to_the_next_one(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    p0 = _FakePage("https://a.test", "A")
    _running(b, [p0])
    b._s.owners["run:old"] = _Owner(p0, time.monotonic() - driver_module.OWNER_IDLE_S - 1)

    page = await b._page_for("session:new")

    assert page is p0
    assert "run:old" not in b._s.owners


async def test_an_owner_whose_tab_was_closed_is_rebound_on_its_next_call() -> None:
    b = get_browser()
    p0, p1 = _FakePage("https://a.test", "A"), _FakePage("https://b.test", "B")
    _running(b, [p0, p1], active=1)
    b._s.owners["session:a"] = _Owner(p0, time.monotonic())
    p0._closed = True
    b._s.context.pages.remove(p0)

    page = await b._page_for("session:a")

    assert page is p1


async def test_owner_cannot_take_or_close_another_owners_tab() -> None:
    b = get_browser()
    p0, p1 = _FakePage("https://a.test", "A"), _FakePage("https://b.test", "B")
    _running(b, [p0, p1])
    b._s.owners["run:other"] = _Owner(p0, time.monotonic())
    b._s.owners["session:me"] = _Owner(p1, time.monotonic())

    taken = await b.tab_activate(0, owner="session:me")
    closed = await b.tab_close(0, owner="session:me")
    tabs = await b.tabs(owner="session:me")

    assert "another agent" in taken["error"]
    assert "another agent" in closed["error"]
    assert p0._closed is False
    assert [(t["yours"], t["held"]) for t in tabs] == [(False, True), (True, False)]


async def test_the_reader_may_look_at_any_tab() -> None:
    """No owner is the reader's hand: switching in the panel is never refused,
    and it is stamped as a touch for the model to hear about."""
    b = get_browser()
    p0, p1 = _FakePage("https://a.test", "A"), _FakePage("https://b.test", "B")
    _running(b, [p0, p1], active=1)
    b._s.owners["run:other"] = _Owner(p0, time.monotonic())
    before = time.monotonic()

    out = await b.tab_activate(0)

    assert "error" not in out
    assert b._s.page is p0
    assert b.touched_since(before)


async def test_tab_limit_refuses_a_new_owner_rather_than_sharing() -> None:
    from raven.browser import BrowserBusyError

    b = get_browser()
    pages = [_FakePage(f"https://{i}.test") for i in range(MAX_TABS)]
    _running(b, pages)
    for i, p in enumerate(pages):
        b._s.owners[f"run:{i}"] = _Owner(p, time.monotonic())

    with pytest.raises(BrowserBusyError):
        await b._page_for("run:late")


async def test_a_popup_moves_its_opener_owner_to_the_new_tab() -> None:
    b = get_browser()
    p0 = _FakePage("https://a.test", "A")
    _running(b, [p0])
    b._s.owners["session:a"] = _Owner(p0, time.monotonic())
    popup = _FakePage("https://a.test/help", "Help")

    async def opener() -> Any:
        return p0

    async def wait_for_load_state(*a: Any, **k: Any) -> None:
        pass

    popup.opener = opener  # type: ignore[attr-defined]
    popup.wait_for_load_state = wait_for_load_state  # type: ignore[attr-defined]
    b._s.context.pages.append(popup)

    b._on_new_page(popup)
    await b._settle()

    assert b._s.owners["session:a"].page is popup
    assert b._s.page is popup


# ── the tools' readback ───────────────────────────────────────────────


def _stub_actions(b: Browser, calls: list[tuple[str, dict[str, Any]]], state: dict[str, Any]) -> None:
    async def rec(name: str, **kw: Any) -> dict[str, Any]:
        calls.append((name, kw))
        return dict(state)

    b.goto = lambda url, **kw: rec("goto", url=url, **kw)  # type: ignore[method-assign]
    b.go = lambda direction, **kw: rec("go", direction=direction, **kw)  # type: ignore[method-assign]
    b.click = lambda **kw: rec("click", **kw)  # type: ignore[method-assign]
    b.type_text = lambda text, **kw: rec("type_text", text=text, **kw)  # type: ignore[method-assign]

    async def snapshot(**kw: Any) -> dict[str, Any]:
        calls.append(("snapshot", kw))
        return {
            "url": state["url"],
            "title": state["title"],
            "text": "Sign up\nName Email",
            "refs": [
                {"ref": "ref_1", "role": "textbox", "name": "Your name", "x": 1, "y": 2, "value": "Ada"},
                {"ref": "ref_2", "role": "button", "name": "Create account", "x": 3, "y": 4, "disabled": True},
            ],
            "console": [{"type": "error", "text": "boom"}, {"type": "log", "text": "noise"}],
            "tab": 0,
        }

    b.snapshot = snapshot  # type: ignore[method-assign]


async def test_navigate_reads_the_page_back_for_its_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    monkeypatch.setattr(tools_mod, "current_owner", lambda: "session:c1")
    calls: list[tuple[str, dict[str, Any]]] = []
    _stub_actions(b, calls, {"url": "https://a.test/", "title": "A", "started": True, "tab": 0, "tab_count": 1})

    out = await BrowserNavigateTool().execute(url="a.test")

    assert calls[0] == ("goto", {"url": "a.test", "owner": "session:c1"})
    assert calls[1] == ("snapshot", {"owner": "session:c1"})
    assert out.ok
    assert "url: https://a.test/" in out.model_text
    assert "tab: 1 of 1" in out.model_text
    assert "ref_1 [textbox] 'Your name' value='Ada'" in out.model_text
    assert "ref_2 [button] 'Create account' (disabled)" in out.model_text
    assert "Sign up" in out.model_text
    assert "[error] boom" in out.model_text and "noise" not in out.model_text


async def test_navigate_needs_a_url_or_an_action() -> None:
    assert BrowserNavigateTool().validate_params({}) == ["url or action is required"]
    assert BrowserNavigateTool().validate_params({"action": "back"}) == []


async def test_a_driver_error_is_an_error_result_without_a_page_description(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    monkeypatch.setattr(tools_mod, "current_owner", lambda: "session:c1")
    calls: list[tuple[str, dict[str, Any]]] = []
    _stub_actions(b, calls, {"url": "https://a.test/", "title": "A", "started": True, "error": "no tab 7"})

    out = await BrowserClickTool().execute(ref="ref_1")

    assert out.ok is False
    assert out.model_text.startswith("Error: no tab 7")
    assert ("snapshot", {"owner": "session:c1"}) not in calls


async def test_the_readers_touch_is_reported_until_the_owner_acts_again(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    monkeypatch.setattr(tools_mod, "current_owner", lambda: "session:c1")
    calls: list[tuple[str, dict[str, Any]]] = []
    _stub_actions(b, calls, {"url": "https://a.test/", "title": "A", "started": True})
    monkeypatch.setattr(type(b), "started", property(lambda self: True))
    note = "the user interacted with the browser"

    first = await BrowserClickTool().execute(ref="ref_1")
    assert note not in first.model_text

    b._s.touched = time.monotonic()
    read = await BrowserSnapshotTool().execute()
    again = await BrowserSnapshotTool().execute()
    acted = await BrowserTypeTool().execute(text="x", ref="ref_1")
    after = await BrowserSnapshotTool().execute()

    assert note in read.model_text
    assert note in again.model_text, "a read does not consume the note; only the owner's own act does"
    assert note in acted.model_text, "the act that follows the touch still says so"
    assert note not in after.model_text


async def test_snapshot_and_screenshot_do_not_start_a_browser() -> None:
    assert "No page is open" in (await BrowserSnapshotTool().execute()).model_text
    assert "No page is open" in (await BrowserScreenshotTool().execute()).model_text
    assert get_browser().started is False


async def test_screenshot_hands_the_model_an_image_block_and_a_self_sufficient_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    import io

    from PIL import Image

    b = get_browser()
    monkeypatch.setattr(tools_mod, "current_owner", lambda: "session:c1")
    monkeypatch.setattr(type(b), "started", property(lambda self: True))
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "white").save(buf, format="JPEG")

    async def screenshot(**kw: Any) -> str:
        assert kw["owner"] == "session:c1"
        return base64.b64encode(buf.getvalue()).decode()

    async def snapshot(**kw: Any) -> dict[str, Any]:
        return {"url": "https://a.test/", "title": "A", "refs": [], "text": ""}

    b.screenshot = screenshot  # type: ignore[method-assign]
    b.snapshot = snapshot  # type: ignore[method-assign]

    out = await BrowserScreenshotTool().execute()

    assert out.blocks is not None
    assert out.blocks[0]["type"] == "text"
    assert out.blocks[1]["type"] == "image_url"
    assert out.blocks[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "https://a.test/" in out.model_text and "64x48" in out.model_text


async def test_tabs_list_marks_the_owners_tab_and_the_held_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    monkeypatch.setattr(tools_mod, "current_owner", lambda: "session:me")
    p0, p1 = _FakePage("https://a.test", "A"), _FakePage("https://b.test", "B")
    _running(b, [p0, p1])
    monkeypatch.setattr(type(b), "started", property(lambda self: True))
    b._s.owners["run:other"] = _Owner(p0, time.monotonic())
    b._s.owners["session:me"] = _Owner(p1, time.monotonic())

    out = await BrowserTabsTool().execute(action="list")

    assert out.model_text.splitlines() == [
        "0: A - https://a.test (held by another agent)",
        "1: B - https://b.test (yours)",
    ]


async def test_tabs_activate_needs_an_index() -> None:
    assert BrowserTabsTool().validate_params({"action": "activate"}) == ["index is required for activate and close"]


async def test_an_unavailable_browser_is_a_non_retryable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.browser import BrowserUnavailableError

    b = get_browser()
    monkeypatch.setattr(tools_mod, "current_owner", lambda: "session:c1")

    async def goto(url: str, **kw: Any) -> dict[str, Any]:
        raise BrowserUnavailableError("playwright is not installed: do X")

    b.goto = goto  # type: ignore[method-assign]

    out = await BrowserNavigateTool().execute(url="a.test")

    assert out.ok is False
    assert out.retryable is False
    assert "do X" in out.model_text


# ── the pop-out on first agent use ────────────────────────────────────


def _patch_switch(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    from types import SimpleNamespace

    import raven.config as config_mod
    from raven.config.schema import BrowserToolConfig

    cfg = SimpleNamespace(tools=SimpleNamespace(browser=BrowserToolConfig(headful_on_agent_use=enabled)))
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)


async def test_the_first_navigate_pops_out_before_it_navigates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The switch on, the browser not up: the first acting call sets the driver
    flag before the goto, so the launch that follows is headful -- not a launch
    that relaunches. The second navigate asks again of nobody."""
    b = get_browser()
    monkeypatch.setattr(tools_mod, "current_owner", lambda: "session:c1")
    _patch_switch(monkeypatch, enabled=True)
    calls: list[tuple[str, dict[str, Any]]] = []
    _stub_actions(b, calls, {"url": "https://a.test/", "title": "A", "started": True})

    async def set_headful(headful: bool) -> dict[str, Any]:
        calls.append(("set_headful", {"headful": headful}))
        return {}

    b.set_headful = set_headful  # type: ignore[method-assign]

    await BrowserNavigateTool().execute(url="a.test")
    await BrowserNavigateTool().execute(url="b.test")

    assert calls[0] == ("set_headful", {"headful": True})
    assert [name for name, _ in calls].count("set_headful") == 1


async def test_a_new_tab_pops_out_too(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    monkeypatch.setattr(tools_mod, "current_owner", lambda: "session:c1")
    _patch_switch(monkeypatch, enabled=True)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def set_headful(headful: bool) -> dict[str, Any]:
        calls.append(("set_headful", {"headful": headful}))
        return {}

    async def tab_new(url: str | None, **kw: Any) -> dict[str, Any]:
        calls.append(("tab_new", {"url": url, **kw}))
        return {"url": "about:blank", "title": "", "started": True}

    b.set_headful = set_headful  # type: ignore[method-assign]
    b.tab_new = tab_new  # type: ignore[method-assign]

    await BrowserTabsTool().execute(action="new")

    assert calls == [("set_headful", {"headful": True}), ("tab_new", {"url": None, "owner": "session:c1"})]


async def test_with_the_switch_off_nothing_pops_out(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    monkeypatch.setattr(tools_mod, "current_owner", lambda: "session:c1")
    _patch_switch(monkeypatch, enabled=False)
    calls: list[tuple[str, dict[str, Any]]] = []
    _stub_actions(b, calls, {"url": "https://a.test/", "title": "A", "started": True})

    async def set_headful(headful: bool) -> dict[str, Any]:
        calls.append(("set_headful", {"headful": headful}))
        return {}

    b.set_headful = set_headful  # type: ignore[method-assign]

    out = await BrowserNavigateTool().execute(url="a.test")

    assert out.ok
    assert [name for name, _ in calls] == ["goto", "snapshot"]
