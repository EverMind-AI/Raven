"""``browser.*`` RPC: the reader's half of the shared page.

The agent drives the page through its tools; the panel drives it through here.
Both reach the same :class:`raven.browser.driver.Browser`, so a click in the
panel and a click from a tool are the same event to Chromium -- which is what
makes "take over and finish the login yourself" work.

While the panel is visible it holds a **watch**: the page's CDP screencast is
on and every paint is pushed to the sockets as a ``browser.frame``
notification, which is what makes the panel feel like a browser instead of a
slideshow. The watch is leased, not owned -- the panel renews it every few
seconds, and a lease nobody renews (page refreshed mid-watch, panel closed
uncleanly) expires server-side, so a page left open behind a dead panel stops
encoding JPEGs on its own. ``browser.state`` is cheap and never starts
Chromium, so the panel can ask what is going on before committing to
launching anything.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
from typing import Any

from raven import browser as browser_mod
from raven.browser import BrowserUnavailableError


def get_browser():
    """The process-wide page, resolved late.

    Through the module rather than a name bound at import: `raven.browser` is
    what a test patches, and a symbol imported here binds whichever function was
    in place the first time this module was imported -- so a fake installed for
    one test leaked into every later caller, depending on import order.
    """
    return browser_mod.get_browser()


WATCH_LEASE_S = 45.0

# Binary frame packet: magic + u32 header length + JSON header + raw JPEG.
# Frames ride the RPC socket as binary messages -- see WsGateway.broadcast.
FRAME_MAGIC = b"RVF1"

_watch: dict[str, Any] = {"send": None, "renewed": 0.0, "size": None, "quality": 70, "pending": None, "pump": None}


def _packet(jpeg_b64: str, url: str, vw: int, vh: int, loading: bool) -> bytes:
    head = json.dumps({"url": url, "vw": vw, "vh": vh, "loading": loading}).encode()
    return FRAME_MAGIC + struct.pack(">I", len(head)) + head + base64.b64decode(jpeg_b64)


async def _pump(send_frame: Any) -> None:
    """Drain ``pending``, always sending the newest frame.

    A paint that lands while the previous send is still on the wire replaces
    the pending slot instead of queueing behind it -- the reader wants the
    page as it is now, and a backlog of stale frames is exactly what makes a
    streamed view feel like it is dragging.
    """
    while (pkt := _watch["pending"]) is not None:
        _watch["pending"] = None
        try:
            await send_frame(pkt)
        except Exception:
            break


def _err(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc)}


async def browser_state(params: dict[str, Any]) -> dict[str, Any]:
    """Where the page is, plus whether a browser can be started at all."""
    return {"ok": True, **(await get_browser().state())}


async def browser_open(params: dict[str, Any]) -> dict[str, Any]:
    """Open a URL, or move through history with ``action``."""
    url = params.get("url")
    action = params.get("action")
    b = get_browser()
    try:
        if action in {"back", "forward", "reload"}:
            state = await b.go(action)
        elif action == "stop":
            state = await b.stop_loading()
        elif isinstance(url, str) and url.strip():
            state = await b.goto(url.strip())
        else:
            return {"ok": False, "error": "url or action is required"}
    except BrowserUnavailableError as exc:
        return {"ok": False, "error": str(exc), "available": False}
    return {"ok": True, **state}


async def browser_tabs(params: dict[str, Any]) -> dict[str, Any]:
    """Tab strip operations: list / new / activate / close.

    ``list`` never starts Chromium. ``new`` may (it is the panel's ⌘T), and
    an optional ``url`` navigates the fresh tab in the same call.
    """
    b = get_browser()
    action = params.get("action") or "list"
    try:
        if action == "new":
            state = await b.tab_new(params.get("url"))
        elif action == "activate":
            state = await b.tab_activate(int(params.get("index") or 0))
        elif action == "close":
            state = await b.tab_close(int(params.get("index") or 0))
        elif action == "list":
            state = {}
        else:
            return {"ok": False, "error": f"unknown tabs action: {action!r}"}
    except BrowserUnavailableError as exc:
        return {"ok": False, "error": str(exc), "available": False}
    except Exception as exc:
        return _err(exc)
    tabs = await b.tabs() if b.started else []
    return {"ok": True, "tabs": tabs, "started": b.started, **state}


async def browser_frame(params: dict[str, Any]) -> dict[str, Any]:
    """One JPEG of the page, plus where it is.

    Returns ``started: False`` rather than launching Chromium: the panel polls
    this, and a poll must never be the thing that starts a browser.
    """
    b = get_browser()
    if not b.started:
        return {"ok": True, "started": False, **(await b.state())}
    quality = params.get("quality")
    try:
        shot = await b.screenshot(quality=int(quality) if quality else 55)
        state = await b.state()
    except BrowserUnavailableError as exc:
        return {"ok": False, "error": str(exc), "available": False}
    except Exception as exc:
        return _err(exc)
    return {"ok": True, "started": True, "jpeg": shot, **state}


async def browser_read(params: dict[str, Any]) -> dict[str, Any]:
    """The page's actionable elements -- the panel uses them for hit testing."""
    b = get_browser()
    if not b.started:
        return {"ok": True, "started": False, "refs": []}
    try:
        snap = await b.snapshot()
    except BrowserUnavailableError as exc:
        return {"ok": False, "error": str(exc), "available": False}
    except Exception as exc:
        return _err(exc)
    return {"ok": True, "started": True, **snap}


async def browser_input(params: dict[str, Any]) -> dict[str, Any]:
    """A reader's pointer, typing, key or scroll, in page coordinates.

    The raw kinds (``move``/``down``/``up``/``wheel``/``keydown``/``keyup``/
    ``text``) return only ``ok`` -- they arrive as a stream while the
    screencast is already showing their effect, so a state readback per event
    would just be latency.
    """
    b = get_browser()
    if not b.started:
        return {"ok": False, "error": "no page is open"}
    kind = params.get("kind")
    try:
        if kind in {"move", "down", "up"}:
            await b.mouse(
                kind,
                x=params.get("x"),
                y=params.get("y"),
                button=params.get("button") or "left",
                count=int(params.get("count") or 1),
            )
            return {"ok": True}
        if kind == "wheel":
            await b.mouse("wheel", dx=float(params.get("dx") or 0), dy=float(params.get("dy") or 0))
            return {"ok": True}
        if kind in {"keydown", "keyup"}:
            await b.key_event(str(params.get("key") or ""), action=kind.removeprefix("key"))
            return {"ok": True}
        if kind == "text" and not params.get("ref"):
            await b.insert_text(str(params.get("text") or ""))
            return {"ok": True}
        if kind == "click":
            state = await b.click(
                ref=params.get("ref"),
                x=params.get("x"),
                y=params.get("y"),
                button=params.get("button") or "left",
            )
        elif kind == "text":
            state = await b.type_text(str(params.get("text") or ""), ref=params.get("ref"))
        elif kind == "key":
            state = await b.press(str(params.get("key") or ""))
        elif kind == "scroll":
            state = await b.scroll(int(params.get("dx") or 0), int(params.get("dy") or 0))
        else:
            return {"ok": False, "error": f"unknown input kind: {kind!r}"}
    except BrowserUnavailableError as exc:
        return {"ok": False, "error": str(exc), "available": False}
    except Exception as exc:
        return _err(exc)
    return {"ok": True, **state}


async def browser_mode(params: dict[str, Any]) -> dict[str, Any]:
    """Pop the page out into a real Chromium window, or pull it back.

    ``headful=true`` relaunches Chromium headed at the same URL: native
    scrolling, IME and 60fps for the reader, while the agent keeps driving
    the very same session. ``headful=false`` folds it back into the panel.
    """
    b = get_browser()
    try:
        state = await b.set_headful(bool(params.get("headful")))
    except BrowserUnavailableError as exc:
        return {"ok": False, "error": str(exc), "available": False}
    except Exception as exc:
        return _err(exc)
    return {"ok": True, **state}


async def browser_close(params: dict[str, Any]) -> dict[str, Any]:
    """Shut Chromium down. The next navigation starts a fresh one."""
    _watch["send"] = None
    await get_browser().close()
    return {"ok": True, "started": False}


def _frame_sink(send_frame: Any) -> Any:
    """The screencast callback: broadcast one frame, or expire the lease."""

    def sink(jpeg: str, meta: dict[str, Any]) -> None:
        if time.monotonic() - _watch["renewed"] > WATCH_LEASE_S:
            _watch["send"] = None
            asyncio.ensure_future(get_browser().stop_stream())
            return
        b = get_browser()
        _watch["pending"] = _packet(jpeg, b.url, b.viewport[0], b.viewport[1], b.loading)
        if _watch["pump"] is None or _watch["pump"].done():
            _watch["pump"] = asyncio.ensure_future(_pump(send_frame))

    return sink


async def _browser_watch(params: dict[str, Any], send_frame: Any) -> dict[str, Any]:
    """Lease (or release) the live view.

    ``on=true`` with the panel's CSS size resizes the page's viewport to match
    and starts the screencast; repeating the call with the same size only
    renews the lease, so the panel can heartbeat without restarting the
    stream. Never starts Chromium: a panel coming up to watch nothing is told
    ``started: false`` and asks again after it opens a page.
    """
    b = get_browser()
    if not params.get("on"):
        _watch["send"] = None
        if b.started:
            await b.stop_stream()
        return {"ok": True, "watching": False}
    if not b.started:
        return {"ok": True, "watching": False, "started": False}
    width = int(params.get("width") or 0) or None
    height = int(params.get("height") or 0) or None
    quality = max(1, min(100, int(params.get("quality") or 70)))
    size = (width, height) if width and height else None
    _watch["renewed"] = time.monotonic()
    same = b.streaming and _watch["send"] is not None and _watch["size"] == size and _watch["quality"] == quality
    if not same:
        _watch["send"] = send_frame
        _watch["size"] = size
        _watch["quality"] = quality
        try:
            await b.start_stream(_frame_sink(send_frame), width=width, height=height, quality=quality)
        except BrowserUnavailableError as exc:
            return {"ok": False, "error": str(exc), "available": False}
        except Exception as exc:
            return _err(exc)
    return {"ok": True, "watching": True, "started": True, "vw": b.viewport[0], "vh": b.viewport[1]}


def register_browser_methods(dispatcher, *, send_frame: Any = None) -> None:
    """Register the ``browser.*`` handlers.

    ``send_frame`` is the transport's notification sink (the WS broadcast).
    Without one -- the TUI's pipe transport has no panel to stream to --
    ``browser.watch`` is simply not registered and the caller falls back to
    pulling frames.
    """
    dispatcher.register("browser.state", browser_state)
    dispatcher.register("browser.open", browser_open)
    dispatcher.register("browser.tabs", browser_tabs)
    dispatcher.register("browser.frame", browser_frame)
    dispatcher.register("browser.read", browser_read)
    dispatcher.register("browser.input", browser_input)
    dispatcher.register("browser.mode", browser_mode)
    dispatcher.register("browser.close", browser_close)
    if send_frame is not None:

        async def watch(params: dict[str, Any]) -> dict[str, Any]:
            return await _browser_watch(params, send_frame)

        dispatcher.register("browser.watch", watch)


__all__ = ["register_browser_methods"]
