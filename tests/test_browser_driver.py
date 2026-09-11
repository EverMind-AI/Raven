"""The browser driver and its RPC surface, without launching Chromium.

Everything here is about the contract the front end and the agent depend on:
nothing starts a browser unless someone navigates, a missing optional extra is
reported rather than raised, and the input verbs reach the driver unchanged.
A real Chromium run belongs in tests/integration, not in the unit suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from raven.browser import driver as driver_module
from raven.browser.driver import Browser, BrowserUnavailableError, get_browser
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
    monkeypatch.setattr(b, "_state", lambda error=None: _fake_state(b))

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
    monkeypatch.setattr(b, "_state", lambda error=None: _fake_state(b))

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
