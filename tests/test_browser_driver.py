"""The browser driver and its RPC surface, without launching Chromium.

Everything here is about the contract the front end and the agent depend on:
nothing starts a browser unless someone navigates, a missing optional extra is
reported rather than raised, and the input verbs reach the driver unchanged.
A real Chromium run belongs in tests/integration, not in the unit suite.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import pytest

from raven.browser import driver as driver_module
from raven.browser.driver import Browser, BrowserUnavailableError, _Owner, get_browser
from raven.rpc.methods import browser as rpc_browser


@pytest.fixture(autouse=True)
def fresh_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets its own Browser; the module holds one per process."""
    monkeypatch.setattr(driver_module, "_BROWSER", None)
    monkeypatch.setattr(
        rpc_browser,
        "_watch",
        {"send": None, "renewed": 0.0, "size": None, "quality": 70, "pending": None, "pump": None},
    )


def test_get_browser_is_one_page_per_process() -> None:
    """The agent's tools and the panel have to reach the same page."""
    assert get_browser() is get_browser()


def test_probe_reports_a_missing_package_with_a_recovery_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing package is a sentence the user can act on, not an ImportError.

    No `playwright install` here: without the library that command cannot run.
    The honest fix is reinstalling (install.sh's engines carry the library) or,
    from a source checkout, syncing the extras."""
    import builtins

    real_import = builtins.__import__

    def no_playwright(name: str, *a: Any, **k: Any) -> Any:
        if name == "playwright":
            raise ImportError("nope")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_playwright)

    ok, why = Browser.probe()

    assert ok is False
    assert "install.sh" in why
    assert "uv sync --all-extras" in why
    assert "playwright install chromium" not in why


async def test_state_does_not_start_a_browser() -> None:
    """The panel asks for state on every draw; that must stay free."""
    b = get_browser()

    state = await b.state()

    assert state["started"] is False
    assert b.started is False


async def test_frame_rpc_does_not_start_a_browser() -> None:
    """browser.frame is polled. A poll must never be what launches Chromium."""
    out = await rpc_browser.browser_frame({})

    assert out["ok"] is True
    assert out["started"] is False
    assert "jpeg" not in out


async def test_open_rpc_needs_a_url_or_an_action() -> None:
    out = await rpc_browser.browser_open({})

    assert out["ok"] is False
    assert "url or action" in out["error"]


async def test_input_rpc_refuses_when_no_page_is_open() -> None:
    """Forwarding a reader's click into nothing is an error, not a launch."""
    out = await rpc_browser.browser_input({"kind": "click", "x": 10, "y": 10})

    assert out["ok"] is False
    assert out["error"] == "no page is open"


async def test_input_rpc_rejects_an_unknown_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    monkeypatch.setattr(type(b), "started", property(lambda self: True))

    out = await rpc_browser.browser_input({"kind": "levitate"})

    assert out["ok"] is False
    assert "unknown input kind" in out["error"]


async def test_input_rpc_maps_each_verb_to_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel's four gestures have to arrive as the driver's four calls."""
    b = get_browser()
    monkeypatch.setattr(type(b), "started", property(lambda self: True))
    seen: list[tuple[str, dict[str, Any]]] = []

    async def rec(name: str, **kw: Any) -> dict[str, Any]:
        seen.append((name, kw))
        return {"url": "u", "title": "t", "started": True}

    monkeypatch.setattr(b, "click", lambda **kw: rec("click", **kw))
    monkeypatch.setattr(b, "type_text", lambda text, **kw: rec("type", text=text, **kw))
    monkeypatch.setattr(b, "press", lambda key: rec("press", key=key))
    monkeypatch.setattr(b, "scroll", lambda dx, dy: rec("scroll", dx=dx, dy=dy))

    await rpc_browser.browser_input({"kind": "click", "x": 3, "y": 4})
    await rpc_browser.browser_input({"kind": "text", "text": "hi", "ref": "ref_1"})
    await rpc_browser.browser_input({"kind": "key", "key": "Enter"})
    await rpc_browser.browser_input({"kind": "scroll", "dx": 0, "dy": 120})

    assert [name for name, _ in seen] == ["click", "type", "press", "scroll"]
    assert seen[0][1]["x"] == 3
    assert seen[1][1] == {"text": "hi", "ref": "ref_1"}
    assert seen[2][1] == {"key": "Enter"}
    assert seen[3][1] == {"dx": 0, "dy": 120}


async def test_input_rpc_raw_verbs_are_fire_and_forget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live-view stream (drag, hover, IME text) maps to the raw driver
    calls and answers a bare ok -- a state readback per pointer move would be
    latency the screencast already made redundant."""
    b = get_browser()
    monkeypatch.setattr(type(b), "started", property(lambda self: True))
    seen: list[tuple[str, Any]] = []

    async def mouse(action: str, **kw: Any) -> None:
        seen.append((action, kw))

    async def key_event(key: str, action: str = "press") -> None:
        seen.append((action, key))

    async def insert_text(text: str) -> None:
        seen.append(("insert", text))

    monkeypatch.setattr(b, "mouse", mouse)
    monkeypatch.setattr(b, "key_event", key_event)
    monkeypatch.setattr(b, "insert_text", insert_text)

    outs = [
        await rpc_browser.browser_input({"kind": "down", "x": 5, "y": 6, "count": 2}),
        await rpc_browser.browser_input({"kind": "move", "x": 7, "y": 8}),
        await rpc_browser.browser_input({"kind": "up", "x": 7, "y": 8}),
        await rpc_browser.browser_input({"kind": "wheel", "dx": 0, "dy": 120}),
        await rpc_browser.browser_input({"kind": "keydown", "key": "Shift"}),
        await rpc_browser.browser_input({"kind": "text", "text": "你好"}),
    ]

    assert all(o == {"ok": True} for o in outs)
    assert [s[0] for s in seen] == ["down", "move", "up", "wheel", "down", "insert"]
    assert seen[0][1]["count"] == 2
    assert seen[4] == ("down", "Shift")
    assert seen[5] == ("insert", "你好")


async def test_typed_text_with_a_ref_still_fills_the_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent-facing verb keeps its shape: text + ref fills that element."""
    b = get_browser()
    monkeypatch.setattr(type(b), "started", property(lambda self: True))
    seen: dict[str, Any] = {}

    async def type_text(text: str, **kw: Any) -> dict[str, Any]:
        seen.update({"text": text, **kw})
        return {"url": "u", "title": "t", "started": True}

    monkeypatch.setattr(b, "type_text", type_text)

    await rpc_browser.browser_input({"kind": "text", "text": "hi", "ref": "ref_2"})

    assert seen == {"text": "hi", "ref": "ref_2"}


def test_watch_is_registered_only_with_a_notification_sink() -> None:
    """The TUI pipe has no panel to stream to; only serve gets browser.watch."""
    from raven.rpc.dispatcher import Dispatcher

    bare = Dispatcher()
    rpc_browser.register_browser_methods(bare)
    wired = Dispatcher()

    async def sink(frame: dict[str, Any]) -> None: ...

    rpc_browser.register_browser_methods(wired, send_frame=sink)

    assert "browser.watch" not in bare._handlers
    assert "browser.watch" in wired._handlers


async def test_watch_never_starts_a_browser() -> None:
    """A panel coming up to watch nothing must not launch Chromium."""

    async def sink(frame: dict[str, Any]) -> None: ...

    out = await rpc_browser._browser_watch({"on": True, "width": 800, "height": 600}, sink)

    assert out["ok"] is True
    assert out["started"] is False
    assert get_browser().started is False


async def test_watch_renewal_with_the_same_size_does_not_restream(monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel heartbeats its lease; a heartbeat must not restart the cast."""
    b = get_browser()
    monkeypatch.setattr(type(b), "started", property(lambda self: True))
    monkeypatch.setattr(type(b), "streaming", property(lambda self: True))
    monkeypatch.setattr(type(b), "viewport", property(lambda self: (800, 600)))
    calls: list[dict[str, Any]] = []

    async def start_stream(on_frame: Any, **kw: Any) -> None:
        calls.append(kw)

    monkeypatch.setattr(b, "start_stream", start_stream)

    async def sink(frame: dict[str, Any]) -> None: ...

    p = {"on": True, "width": 800, "height": 600, "quality": 55}
    first = await rpc_browser._browser_watch(p, sink)
    second = await rpc_browser._browser_watch(p, sink)
    resized = await rpc_browser._browser_watch({**p, "width": 900}, sink)

    assert first["watching"] and second["watching"] and resized["watching"]
    assert len(calls) == 2
    assert calls[1]["width"] == 900


def test_frame_packets_are_binary_with_a_json_header() -> None:
    """The wire shape the panel decodes: magic, header length, header, JPEG."""
    import base64
    import json
    import struct

    jpeg = b"\xff\xd8fakejpeg"
    pkt = rpc_browser._packet(base64.b64encode(jpeg).decode(), "https://x.test", 640, 480, True)

    assert pkt[:4] == rpc_browser.FRAME_MAGIC
    (hl,) = struct.unpack(">I", pkt[4:8])
    head = json.loads(pkt[8 : 8 + hl])
    assert head == {"url": "https://x.test", "vw": 640, "vh": 480, "loading": True}
    assert pkt[8 + hl :] == jpeg


async def test_the_pump_sends_the_newest_frame_and_drops_the_stale_one() -> None:
    """Frames that land while a send is on the wire replace the pending slot:
    the reader gets the page as it is now, never a backlog replay."""
    import asyncio

    sent: list[bytes] = []
    gate = asyncio.Event()

    async def slow_send(pkt: bytes) -> None:
        sent.append(pkt)
        await gate.wait()

    rpc_browser._watch["pending"] = b"frame-1"
    task = asyncio.ensure_future(rpc_browser._pump(slow_send))
    await asyncio.sleep(0)
    rpc_browser._watch["pending"] = b"frame-2"
    rpc_browser._watch["pending"] = b"frame-3"
    gate.set()
    await task

    assert sent == [b"frame-1", b"frame-3"]


async def test_mode_rpc_reports_headful_without_starting_a_browser() -> None:
    """Asking for the current mode (a no-op flip) must not launch Chromium."""
    out = await rpc_browser.browser_mode({"headful": False})

    assert out["ok"] is True
    assert out["headful"] is False
    assert get_browser().started is False


async def test_set_headful_relaunches_and_carries_the_url_over(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    seen: list[str] = []

    async def close() -> None:
        seen.append("close")
        b._s = driver_module._State()

    async def goto(url: str) -> dict[str, Any]:
        seen.append(f"goto:{url}")
        return {"url": url, "title": "", "started": True, "headful": b._headful}

    monkeypatch.setattr(b, "close", close)
    monkeypatch.setattr(b, "goto", goto)
    b._s.page = object()
    monkeypatch.setattr(type(b), "url", property(lambda self: "https://x.test/a"))

    out = await b.set_headful(True)

    assert seen == ["close", "goto:https://x.test/a"]
    assert b._headful is True
    assert out["headful"] is True


async def test_close_is_safe_before_anything_started() -> None:
    """The panel's close button must not need a browser to have existed."""
    out = await rpc_browser.browser_close({})

    assert out == {"ok": True, "started": False}


async def test_driver_raises_unavailable_when_the_extra_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    monkeypatch.setattr(Browser, "probe", staticmethod(lambda: (False, "playwright is not installed")))

    with pytest.raises(BrowserUnavailableError):
        await b._ensure()


async def test_launch_reports_missing_chromium_with_the_interpreter_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The binary hint names sys.executable: that venv has playwright in both a
    uv tool install and a source checkout, while `uv sync` exists only in the
    latter."""
    import shlex
    import sys
    import types

    class _Starter:
        async def start(self) -> None:
            raise RuntimeError("Executable doesn't exist at /nowhere/chrome")

    fake_api = types.ModuleType("playwright.async_api")
    fake_api.async_playwright = _Starter
    fake_pkg = types.ModuleType("playwright")
    fake_pkg.async_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)

    b = get_browser()
    with pytest.raises(BrowserUnavailableError) as exc:
        await b._ensure()

    why = str(exc.value)
    assert "Chromium is not installed" in why
    assert f"{shlex.quote(sys.executable)} -m playwright install chromium" in why


# ── tabs ────────────────────────────────────────────────────────────────


class _FakePage:
    def __init__(self, url: str = "about:blank", title: str = "") -> None:
        self.url = url
        self._title = title
        self._closed = False
        self._raven_loading = False

    def is_closed(self) -> bool:
        return self._closed

    async def title(self) -> str:
        return self._title

    async def close(self) -> None:
        self._closed = True

    async def bring_to_front(self) -> None:
        pass


class _FakeContext:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages


def _with_pages(b: Browser, pages: list[_FakePage], active: int = 0) -> None:
    b._s.context = _FakeContext(pages)
    b._s.page = pages[active]


async def test_tabs_lists_every_open_page_with_the_active_flag() -> None:
    b = get_browser()
    p1, p2 = _FakePage("https://a.test", "A"), _FakePage("https://b.test", "B")
    p2._raven_loading = True
    _with_pages(b, [p1, p2], active=1)

    tabs = await b.tabs()

    assert [(t["url"], t["active"], t["loading"]) for t in tabs] == [
        ("https://a.test", False, False),
        ("https://b.test", True, True),
    ]


async def test_tab_activate_switches_the_shared_page_and_restreams(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    p1, p2 = _FakePage("https://a.test"), _FakePage("https://b.test")
    _with_pages(b, [p1, p2], active=0)
    streams: list[str] = []

    async def restream() -> None:
        streams.append("restream")

    monkeypatch.setattr(b, "_restream", restream)
    monkeypatch.setattr(b, "_state", lambda error=None, page=None: _fake_state(b))

    await b.tab_activate(1)

    assert b._s.page is p2
    assert streams == ["restream"]


async def test_tab_close_of_the_active_tab_moves_to_a_neighbour(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    p1, p2 = _FakePage("https://a.test"), _FakePage("https://b.test")
    _with_pages(b, [p1, p2], active=1)

    async def restream() -> None:
        pass

    monkeypatch.setattr(b, "_restream", restream)
    monkeypatch.setattr(b, "_state", lambda error=None, page=None: _fake_state(b))

    await b.tab_close(1)

    assert p2._closed is True
    assert b._s.page is p1


async def test_tab_close_of_the_last_tab_closes_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    p1 = _FakePage("https://a.test")
    _with_pages(b, [p1], active=0)
    closed: list[bool] = []

    async def full_close() -> None:
        closed.append(True)
        b._s.context = None
        b._s.page = None

    monkeypatch.setattr(b, "close", full_close)

    out = await b.tab_close(0)

    assert closed == [True]
    assert out["started"] is False


async def _fake_state(b: Browser) -> dict[str, Any]:
    return {"url": b._s.page.url if b._s.page else "", "title": "", "started": True, "headful": False}


async def test_tabs_rpc_lists_without_starting_a_browser() -> None:
    out = await rpc_browser.browser_tabs({"action": "list"})

    assert out["ok"] is True
    assert out["tabs"] == []
    assert out["started"] is False


# ── the verbs, on a fake page ──────────────────────────────────────────
#
# The real-Chromium suite (tests/integration/test_browser_tools_real_web.py)
# is the acceptance for these; it needs a browser and a desktop, so what the
# unit suite pins here is the branching around each verb: which page a call
# lands on, what an owner inherits, and what a failure reports.


class _FakeLocator:
    def __init__(self, page: "_ActingPage", ref: str) -> None:
        self._page = page
        self._ref = ref

    async def evaluate(self, js: str, **kw: Any) -> Any:
        return self._page.opens_tab

    async def click(self, **kw: Any) -> None:
        if self._page.raises:
            raise RuntimeError(self._page.raises)
        self._page.acts.append(("click", self._ref))

    async def fill(self, text: str, **kw: Any) -> None:
        self._page.acts.append(("fill", self._ref, text))


class _FakeMouse:
    def __init__(self, page: "_ActingPage") -> None:
        self._page = page

    async def click(self, x: float, y: float, **kw: Any) -> None:
        self._page.acts.append(("mouse_click", x, y))

    async def wheel(self, dx: float, dy: float) -> None:
        self._page.acts.append(("wheel", dx, dy))

    async def move(self, x: float, y: float) -> None:
        self._page.acts.append(("move", x, y))

    async def down(self, **kw: Any) -> None:
        self._page.acts.append(("down",))

    async def up(self, **kw: Any) -> None:
        self._page.acts.append(("up",))


class _FakeKeyboard:
    def __init__(self, page: "_ActingPage") -> None:
        self._page = page

    async def type(self, text: str) -> None:
        self._page.acts.append(("type", text))

    async def press(self, key: str) -> None:
        self._page.acts.append(("press", key))

    async def down(self, key: str) -> None:
        self._page.acts.append(("keydown", key))

    async def up(self, key: str) -> None:
        self._page.acts.append(("keyup", key))

    async def insert_text(self, text: str) -> None:
        self._page.acts.append(("insert", text))


class _ActingPage(_FakePage):
    """A page the acting verbs can be driven against."""

    def __init__(self, url: str = "https://a.test", title: str = "A") -> None:
        super().__init__(url, title)
        self.acts: list[tuple[Any, ...]] = []
        self.raises: str = ""
        self.opens_tab = False
        self.handlers: dict[str, list[Any]] = {}
        self.mouse = _FakeMouse(self)
        self.keyboard = _FakeKeyboard(self)

    def set_default_timeout(self, ms: int) -> None:
        pass

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def goto(self, url: str, **kw: Any) -> None:
        if self.raises:
            raise RuntimeError(self.raises)
        self.url = url
        self.acts.append(("goto", url))

    async def go_back(self, **kw: Any) -> None:
        self.acts.append(("back",))

    async def go_forward(self, **kw: Any) -> None:
        self.acts.append(("forward",))

    async def reload(self, **kw: Any) -> None:
        if self.raises:
            raise RuntimeError(self.raises)
        self.acts.append(("reload",))

    async def wait_for_load_state(self, state: str, **kw: Any) -> None:
        self.acts.append(("settled", state))

    async def evaluate(self, js: str, *args: Any) -> Any:
        if self.raises:
            raise RuntimeError(self.raises)
        return {"url": self.url, "title": self._title, "text": "body text", "refs": []}

    async def screenshot(self, **kw: Any) -> bytes:
        self.acts.append(("shot", kw.get("full_page")))
        return b"\xff\xd8jpeg"


def _driving(b: Browser, pages: list[_FakePage], active: int = 0) -> None:
    """A driver whose pages are fakes and whose stream and history are inert."""
    _with_pages(b, pages, active=active)

    async def ensure() -> Any:
        return b._s.page

    async def restream() -> None:
        pass

    async def reach(page: Any = None) -> tuple[bool, bool]:
        return True, False

    async def settle() -> None:
        pass

    b._ensure = ensure  # type: ignore[method-assign]
    b._restream = restream  # type: ignore[method-assign]
    b._history_reach = reach  # type: ignore[method-assign]
    b._settle = settle  # type: ignore[method-assign]


async def test_goto_reports_the_page_it_landed_on_and_an_error_as_an_error() -> None:
    b = get_browser()
    page = _ActingPage()
    _driving(b, [page])

    out = await b.goto("https://ok.test/", owner="run:a")
    assert out["url"] == "https://ok.test/" and "error" not in out
    assert out["can_back"] is True and out["can_forward"] is False

    page.raises = "net::ERR_NAME_NOT_RESOLVED"
    bad = await b.goto("https://nope.test/", owner="run:a")
    assert bad["error"] == "net::ERR_NAME_NOT_RESOLVED"


async def test_a_refused_target_never_starts_a_browser() -> None:
    b = get_browser()
    page = _ActingPage()
    _driving(b, [page])

    out = await b.goto("file:///etc/passwd", owner="run:a")

    assert "error" in out
    assert page.acts == [], "the refusal must come before the page is touched"


async def test_history_moves_go_where_they_are_asked() -> None:
    b = get_browser()
    page = _ActingPage()
    _driving(b, [page])

    for direction, act in (("back", ("back",)), ("forward", ("forward",)), ("reload", ("reload",))):
        page.acts.clear()
        await b.go(direction, owner="run:a")
        assert page.acts == [act]

    page.raises = "boom"
    assert (await b.go("reload", owner="run:a"))["error"] == "boom"


async def test_click_takes_a_ref_a_point_or_neither() -> None:
    b = get_browser()
    page = _ActingPage()
    _driving(b, [page])

    await b.click(ref="ref_3", owner="run:a")
    assert ("click", '[data-raven-ref="ref_3"]') in page.acts

    await b.click(x=10, y=20, owner="run:a")
    assert ("mouse_click", 10, 20) in page.acts

    out = await b.click(owner="run:a")
    assert out["error"] == "click needs a ref or x/y"

    page.raises = "element is not visible"
    assert (await b.click(ref="ref_3", owner="run:a"))["error"] == "element is not visible"


async def test_typing_fills_a_ref_types_into_focus_and_submits() -> None:
    b = get_browser()
    page = _ActingPage()
    _driving(b, [page])

    await b.type_text("Ada", ref="ref_1", owner="run:a")
    assert ("fill", '[data-raven-ref="ref_1"]', "Ada") in page.acts

    page.acts.clear()
    await b.type_text("loose text", owner="run:a")
    assert page.acts == [("type", "loose text")]

    page.acts.clear()
    await b.type_text("query", submit=True, owner="run:a")
    assert ("press", "Enter") in page.acts and ("settled", "domcontentloaded") in page.acts


async def test_press_and_scroll_report_the_page_afterwards() -> None:
    b = get_browser()
    page = _ActingPage()
    _driving(b, [page])

    out = await b.press("Escape", owner="run:a")
    assert ("press", "Escape") in page.acts and out["url"] == page.url

    out = await b.scroll(dx=0, dy=400, owner="run:a")
    assert ("wheel", 0, 400) in page.acts and "error" not in out


async def test_a_readers_raw_input_needs_no_owner_and_no_readback() -> None:
    b = get_browser()
    page = _ActingPage()
    _driving(b, [page])

    await b.mouse("move", x=5, y=6)
    await b.mouse("down")
    await b.mouse("up")
    await b.mouse("wheel", dx=1, dy=2)
    await b.key_event("a", action="down")
    await b.key_event("a", action="up")

    assert page.acts == [("move", 5, 6), ("down",), ("up",), ("wheel", 1, 2), ("keydown", "a"), ("keyup", "a")]


async def test_a_snapshot_failure_is_a_state_with_an_error() -> None:
    b = get_browser()
    page = _ActingPage()
    _driving(b, [page])
    page.raises = "Execution context was destroyed"

    out = await b.snapshot(owner="run:a")

    assert out["error"] == "Execution context was destroyed"


async def test_a_screenshot_is_base64_and_leaves_the_front_tab_alone() -> None:
    b = get_browser()
    first, second = _ActingPage("https://a.test", "A"), _ActingPage("https://b.test", "B")
    _driving(b, [first, second], active=1)
    b._s.owners["run:a"] = _Owner(first, time.monotonic())

    shot = await b.screenshot(quality=70, owner="run:a")

    assert base64.b64decode(shot) == b"\xff\xd8jpeg"
    assert b._s.page is second, "a read must not move what the panel shows"


async def test_an_owner_keeps_its_tab_and_can_hand_it_back() -> None:
    b = get_browser()
    first, second = _ActingPage("https://a.test", "A"), _ActingPage("https://b.test", "B")
    _driving(b, [first, second])

    assert b.url_for("run:a") == "https://a.test", "an unbound owner reads the active tab"
    b._s.owners["run:a"] = _Owner(second, time.monotonic())
    assert b.url_for("run:a") == "https://b.test"

    b.release("run:a")
    assert b.url_for("run:a") == "https://a.test"
    assert not second.is_closed(), "releasing a binding leaves the tab open"


async def test_a_popup_moves_the_owner_that_opened_it() -> None:
    b = get_browser()
    opener, popup = _ActingPage("https://a.test", "A"), _ActingPage("https://p.test", "P")

    async def opened_by() -> Any:
        return opener

    popup.opener = opened_by  # type: ignore[attr-defined]
    _driving(b, [opener])
    b._s.owners["run:a"] = _Owner(opener, time.monotonic())
    b._s.context.pages.append(popup)

    await b._adopt(popup)

    assert b._s.owners["run:a"].page is popup
    assert b._s.page is popup


async def test_the_touch_stamp_is_the_readers_and_not_an_owners() -> None:
    b = get_browser()
    page = _ActingPage()
    _driving(b, [page])
    when = time.monotonic()

    await b.click(x=1, y=1, owner="run:a")
    assert b.touched_since(when) is False, "an owner's act is not the reader's hand"

    await b.mouse("move", x=2, y=2)
    assert b.touched_since(when) is True


async def test_a_tab_whose_title_cannot_be_read_is_still_listed() -> None:
    b = get_browser()
    page = _ActingPage("https://a.test", "A")

    async def no_title() -> str:
        raise RuntimeError("Execution context was destroyed")

    page.title = no_title  # type: ignore[method-assign]
    _driving(b, [page])

    tabs = await b.tabs()

    assert tabs == [{"index": 0, "url": "https://a.test", "title": "", "active": True, "loading": False}]


async def test_the_tab_verbs_open_switch_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    b = get_browser()
    first, second = _ActingPage("https://a.test", "A"), _ActingPage("https://b.test", "B")
    _driving(b, [first, second])

    opened = _ActingPage("about:blank", "")

    async def new_page() -> Any:
        b._s.context.pages.append(opened)
        return opened

    b._s.context.new_page = new_page  # type: ignore[attr-defined]

    await b.tab_new(owner="run:a")
    assert b._s.owners["run:a"].page is opened
    assert b._s.page is opened

    switched = await b.tab_activate(1, owner="run:a")
    assert b._s.page is second and "error" not in switched

    closed = await b.tab_close(1, owner="run:a")
    assert second.is_closed() and "error" not in closed

    assert (await b.tab_activate(9, owner="run:a"))["error"] == "no tab 9"
    assert (await b.tab_close(9, owner="run:a"))["error"] == "no tab 9"


async def test_the_tab_limit_answers_rather_than_opening_the_twenty_first() -> None:
    b = get_browser()
    pages = [_ActingPage(f"https://{i}.test") for i in range(driver_module.MAX_TABS)]
    _driving(b, pages)

    out = await b.tab_new()

    assert "tab limit reached" in out["error"]


async def test_a_stream_that_cannot_restart_after_a_tab_switch_says_so_and_carries_on() -> None:
    """The reader loses the live view, not the session: a failed restream is
    logged where a raise would have taken the tab switch down with it."""
    b = get_browser()
    page = _ActingPage()
    _with_pages(b, [page])
    started: list[int] = []

    async def start_stream(sink: Any, **kw: Any) -> None:
        started.append(1)
        raise RuntimeError("target closed")

    b.start_stream = start_stream  # type: ignore[method-assign]
    b._s.on_frame = lambda *a: None

    await b._restream()

    assert started == [1]


async def test_stopping_a_stream_that_is_already_gone_is_not_an_error() -> None:
    b = get_browser()
    page = _ActingPage()
    _with_pages(b, [page])

    class _Cdp:
        async def send(self, method: str, params: Any = None) -> Any:
            raise RuntimeError("session closed")

        async def detach(self) -> None:
            raise RuntimeError("session closed")

    b._s.cdp = _Cdp()

    await b.stop_stream()

    assert b._s.cdp is None and b._s.on_frame is None


async def test_an_owner_cannot_close_the_users_unheld_tab() -> None:
    """An unowned tab is the reader's, not idle: the panel's tab may hold a
    login the user was asked to complete, and closing the last tab closes the
    whole browser -- an auto-approved call must never take it out silently."""
    b = get_browser()
    users, own = _ActingPage("https://login.test", "L"), _ActingPage("https://b.test", "B")
    _driving(b, [users, own], active=0)
    b._s.owners["run:a"] = _Owner(own, time.monotonic())

    refused = await b.tab_close(0, owner="run:a")

    assert "user's tab" in refused["error"]
    assert not users.is_closed()

    allowed = await b.tab_close(1, owner="run:a")
    assert own.is_closed() and "error" not in allowed

    by_reader = await b.tab_close(0)
    assert users.is_closed() and "error" not in by_reader


async def test_a_read_that_must_open_a_tab_leaves_the_panel_alone() -> None:
    """The delegation case: the active tab is held by another owner, so the
    reader-owner's first snapshot has to open its own tab -- and the panel must
    not follow it. The context's "page" event fires for that tab exactly as it
    does for a popup, so this drives the un-stubbed adoption path."""
    b = get_browser()
    held = _ActingPage("https://parent.test", "P")
    _driving(b, [held])
    b._s.owners["run:parent"] = _Owner(held, time.monotonic())
    streams: list[str] = []

    async def restream() -> None:
        streams.append("restream")

    b._restream = restream  # type: ignore[method-assign]

    class _SpawningContext(_FakeContext):
        async def new_page(self) -> Any:
            page = _ActingPage("about:blank", "")
            self.pages.append(page)
            b._on_new_page(page)
            return page

    b._s.context = _SpawningContext([held])

    out = await b.snapshot(owner="run:child")
    for task in list(b._s.adopting):
        await task

    assert not out.get("error")
    assert b._s.page is held, "a read must not move what the panel shows"
    assert streams == [], "a read must not restream the panel"
    assert b._s.owners["run:child"].page is not held, "the read got its own tab"


async def test_an_acting_owner_that_opens_a_tab_still_fronts_it() -> None:
    b = get_browser()
    held = _ActingPage("https://parent.test", "P")
    _driving(b, [held])
    b._s.owners["run:parent"] = _Owner(held, time.monotonic())
    streams: list[str] = []

    async def restream() -> None:
        streams.append("restream")

    b._restream = restream  # type: ignore[method-assign]

    class _SpawningContext(_FakeContext):
        async def new_page(self) -> Any:
            page = _ActingPage("about:blank", "")
            self.pages.append(page)
            b._on_new_page(page)
            return page

    b._s.context = _SpawningContext([held])

    page = await b._page_for("run:child", act=True)
    for task in list(b._s.adopting):
        await task

    assert b._s.page is page and page is not held
    assert streams == ["restream"], "an act fronts the new tab exactly once"
