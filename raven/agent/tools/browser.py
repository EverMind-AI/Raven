"""The model's hands on the shared browser: ``browser_*`` tools over :mod:`raven.browser`.

One tool per verb rather than one tool with an ``action`` enum, because the
permission gate rules by tool name: a deployment can leave reading and
navigating free and put ``browser_click`` / ``browser_type`` on the ask tier
without the tools knowing. Every tool talks to the same :class:`Browser` the
``browser.*`` RPC drives, so what the model does is what the panel shows.

Two things this module owns that the driver does not:

* **Who is acting.** Each call names an owner -- the sub-agent run when there
  is one, else the conversation -- and the driver binds that owner to a tab.
  Two agents in one process therefore work in two tabs, and a reader who
  clicks in the panel is a third hand the tools can tell apart.
* **What the model reads back.** Every acting call returns the page as it is
  afterwards (url, title, the actionable refs, the visible text) so the next
  call can be aimed without a separate read; ``browser_screenshot`` is the one
  that costs an image and is asked for on purpose.
"""

from __future__ import annotations

import base64
import time
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

from raven.contracts.tool import Tool, ToolResult
from raven.permissions.builtin import BROWSER_SITE_PARAM as SITE_PARAM
from raven.utils.images import image_block, text_block

# What an acting call hands back beside the state: enough page text to aim the
# next call, not the whole page -- that is what browser_snapshot is for.
ACTION_TEXT_CHARS = 3_000
SNAPSHOT_TEXT_CHARS = 8_000
MAX_REFS_SHOWN = 120

# Carried by browser_navigate alone. Every tool's description is paid for on
# every turn of every conversation, and the one place the model decides whether
# a page is its business at all is when it opens one.
HANDOFF_NOTE = (
    "A login, CAPTCHA or payment is the user's to do: ask_user to finish it in the Browser "
    "panel, then browser_snapshot to read the page as they left it."
)

# ``SITE_PARAM`` is injected by ``cast_params`` on the acting tools. The
# permission gate sees a tool name and its parameters, nothing else, and what a
# person approves for a click is the site, not the ref id -- so the site the
# call will land on is written into the parameters before the gate reads them.
# ``raven.permissions.builtin.session_keys`` keys a browser grant on it, and
# the approval prompt shows it.


def _browser():
    from raven.browser import get_browser

    return get_browser()


# The pop-out is a first-use convenience, decided once per process: the driver's
# set_headful is idempotent, but re-asking on every acting call would re-pop the
# window out each time the reader folded it back into the panel. After the first
# use the mode is the reader's to control, and a config change takes a restart.
_headful_attempted = False


async def _pop_out_for_agent_use(b: Any) -> None:
    """Pop the browser into a real window before the model's first acting call,
    when ``tools.browser.headfulOnAgentUse`` says so.

    Only the tools that can start the browser call this (a navigate, a new
    tab); click and type presuppose a page already open. With Chromium not up
    yet it only sets the driver's flag, so the first goto launches headful
    directly instead of launching headless and relaunching.
    """
    global _headful_attempted
    if _headful_attempted:
        return
    _headful_attempted = True
    from raven.config import load_config

    try:
        enabled = load_config().tools.browser.headful_on_agent_use
    except Exception as exc:  # noqa: BLE001 - a config this build cannot read must not take navigation down
        logger.debug("browser tools: could not read the headful-on-agent-use switch: {}", exc)
        return
    if enabled:
        await b.set_headful(True)


def current_owner() -> str:
    """Who this call acts for: the sub-agent run in flight, else the conversation.

    Checked in that order because a sub-agent's task inherits the parent turn's
    context variables, conversation id included; the run is the finer identity
    and the one that must not share a tab with its parent.
    """
    from raven.agent.subagent import activity
    from raven.token_wise import usage_context

    run = activity.current()
    if run is not None:
        return f"run:{id(run):x}"
    cid = usage_context.session_key()
    return f"session:{cid}" if cid else "session:default"


def _site(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.lower()


def render_state(state: dict[str, Any], *, touched: bool = False) -> str:
    lines = [f"url: {state.get('url') or '(no page)'}"]
    if state.get("title"):
        lines.append(f"title: {state['title']}")
    tab = state.get("tab")
    count = state.get("tab_count")
    if tab is not None and count:
        lines.append(f"tab: {tab + 1} of {count}")
    if state.get("loading"):
        lines.append("loading: the page is still loading; read it again before acting on it")
    if touched:
        lines.append(
            "note: the user interacted with the browser since your last action; the page may differ from what you expect"
        )
    if state.get("error"):
        lines.append(f"error: {state['error']}")
    return "\n".join(lines)


def render_snapshot(snap: dict[str, Any], *, text_chars: int, touched: bool = False) -> str:
    out = [render_state(snap, touched=touched)]
    refs = snap.get("refs") or []
    if refs:
        out.append("")
        shown = refs[:MAX_REFS_SHOWN]
        out.append(f"refs ({len(refs)}{', first ' + str(len(shown)) + ' shown' if len(shown) < len(refs) else ''}):")
        for r in shown:
            piece = f"{r['ref']} [{r.get('role', '')}] {r.get('name', '')!r}"
            if r.get("value"):
                piece += f" value={r['value']!r}"
            if r.get("href"):
                piece += f" -> {r['href']}"
            if r.get("disabled"):
                piece += " (disabled)"
            out.append(piece)
    text = (snap.get("text") or "").strip()
    if text:
        out.append("")
        if len(text) > text_chars:
            text = (
                text[:text_chars]
                + f"\n... ({len(snap['text']) - text_chars} more chars; browser_snapshot returns more)"
            )
        out.append("text:")
        out.append(text)
    console = [c for c in snap.get("console") or [] if c.get("type") in ("error", "warning")]
    if console:
        out.append("")
        out.append("console:")
        out.extend(f"[{c['type']}] {c['text']}" for c in console[-5:])
    return "\n".join(out)


class _BrowserTool(Tool):
    """What every browser tool shares: the driver, the owner, the readback."""

    timeout_seconds = 90.0

    # The last time this owner acted, so a readback can say whether a hand
    # other than the model's touched the browser in between.
    _acted: dict[str, float] = {}

    @staticmethod
    def configured() -> bool:
        from raven.browser.driver import Browser

        return Browser.probe()[0]

    def _owner(self) -> str:
        return current_owner()

    def _mark(self, owner: str) -> None:
        _BrowserTool._acted[owner] = time.monotonic()

    def _touched(self, owner: str) -> bool:
        last = _BrowserTool._acted.get(owner)
        return last is not None and _browser().touched_since(last)

    async def _readback(self, owner: str, state: dict[str, Any], *, acted: bool) -> ToolResult:
        """State plus a compact snapshot; the snapshot is skipped on an error
        so a failure reads as one and not as a page description."""
        touched = self._touched(owner)
        if acted:
            self._mark(owner)
        if state.get("error"):
            text = render_state(state, touched=touched)
            return ToolResult(model_text=f"Error: {state['error']}\n{text}", ok=False, display_text=state["error"])
        b = _browser()
        snap = await b.snapshot(owner=owner)
        merged = {**state, **{k: v for k, v in snap.items() if k not in ("error",)}}
        text = render_snapshot(merged, text_chars=ACTION_TEXT_CHARS, touched=touched)
        if snap.get("error"):
            text += f"\n(snapshot failed: {snap['error']})"
        return ToolResult(model_text=text, display_text=merged.get("title") or merged.get("url") or "")

    async def _guard(self, coro):
        from raven.browser import BrowserBusyError, BrowserUnavailableError

        try:
            return await coro
        except BrowserUnavailableError as exc:
            return ToolResult(model_text=f"Error: the browser is unavailable: {exc}", ok=False, retryable=False)
        except BrowserBusyError as exc:
            return ToolResult(model_text=f"Error: {exc}", ok=False)


class _ActingTool(_BrowserTool):
    """A tool the permission gate may put on the ask tier, keyed by site."""

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        out = dict(params)
        out.pop(SITE_PARAM, None)
        site = _site(_browser().url_for(self._owner()))
        if site:
            out[SITE_PARAM] = site
        return out


class BrowserNavigateTool(_BrowserTool):
    @property
    def name(self) -> str:
        return "browser_navigate"

    @property
    def description(self) -> str:
        return (
            "Open an http(s) URL in the shared browser, or move through its history. Returns the page: "
            "url, title, its actionable elements with a ref each (for browser_click / browser_type) and "
            "its visible text. The user watches this page in the Browser panel and can act on it too. " + HANDOFF_NOTE
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The address to open (a bare domain is completed to https)."},
                "action": {
                    "type": "string",
                    "enum": ["back", "forward", "reload"],
                    "description": "Move through history instead of opening a url.",
                },
            },
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        if not (params.get("url") or "").strip() and not params.get("action"):
            return ["url or action is required"]
        return []

    async def execute(self, url: str | None = None, action: str | None = None, **kwargs: Any) -> ToolResult:
        owner = self._owner()

        async def run() -> ToolResult:
            b = _browser()
            await _pop_out_for_agent_use(b)
            if action:
                state = await b.go(action, owner=owner)
            else:
                state = await b.goto(url.strip(), owner=owner)
            return await self._readback(owner, state, acted=True)

        return await self._guard(run())

    def display_call(self, args: dict[str, Any]) -> str | None:
        return args.get("url") or args.get("action")


class BrowserSnapshotTool(_BrowserTool):
    @property
    def name(self) -> str:
        return "browser_snapshot"

    @property
    def description(self) -> str:
        return (
            "Read the current page: url, title, actionable elements with refs, visible text, console "
            "errors. Cheap; call it whenever the page may have moved -- the user acted in the panel, or "
            "a result said it was still loading. Refs are renumbered every read: use the latest only."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "max_text_chars": {
                    "type": "integer",
                    "minimum": 200,
                    "maximum": 20000,
                    "description": f"How much page text to return (default {SNAPSHOT_TEXT_CHARS}).",
                }
            },
        }

    async def execute(self, max_text_chars: int = SNAPSHOT_TEXT_CHARS, **kwargs: Any) -> ToolResult:
        owner = self._owner()

        async def run() -> ToolResult:
            b = _browser()
            if not b.started:
                return ToolResult(
                    model_text="No page is open. Call browser_navigate with a url first.",
                    display_text="no page open",
                )
            snap = await b.snapshot(owner=owner)
            touched = self._touched(owner)
            if snap.get("error"):
                return ToolResult(model_text=f"Error: {snap['error']}\n{render_state(snap)}", ok=False)
            return ToolResult(
                model_text=render_snapshot(snap, text_chars=max_text_chars, touched=touched),
                display_text=snap.get("title") or snap.get("url") or "",
            )

        return await self._guard(run())


class BrowserScreenshotTool(_BrowserTool):
    @property
    def name(self) -> str:
        return "browser_screenshot"

    @property
    def description(self) -> str:
        return (
            "Look at the current page. For layout or visual state that browser_snapshot's text cannot "
            "show; prefer browser_snapshot for finding what to click. Image coordinates are CSS pixels, "
            "so they can go to browser_click as x/y."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the whole scrollable page instead of the viewport (default false).",
                }
            },
        }

    async def execute(self, full_page: bool = False, **kwargs: Any) -> ToolResult:
        owner = self._owner()

        async def run() -> ToolResult:
            from raven.agent.tools import media

            b = _browser()
            if not b.started:
                return ToolResult(
                    model_text="No page is open. Call browser_navigate with a url first.",
                    display_text="no page open",
                )
            raw = base64.b64decode(await b.screenshot(quality=70, full=bool(full_page), owner=owner))
            payload, mime, meta = media.prepare_image(raw, "image/jpeg")
            state = await b.snapshot(owner=owner)
            summary = (
                f"Screenshot of {state.get('url', '')} ({meta['width']}x{meta['height']} px"
                + (", downscaled" if meta.get("resized") else "")
                + f", ~{meta.get('tokens', '?')} tokens). "
                + ("The whole page. " if full_page else "The viewport. ")
                + f"Title: {state.get('title', '')!r}."
            )
            return ToolResult(
                model_text=summary,
                display_text=f"{meta['width']}x{meta['height']} {state.get('title') or state.get('url') or ''}",
                blocks=[text_block(summary), image_block(media.to_data_uri(payload, mime))],
            )

        return await self._guard(run())


class BrowserClickTool(_ActingTool):
    @property
    def name(self) -> str:
        return "browser_click"

    @property
    def description(self) -> str:
        return (
            "Click an element by the ref a snapshot gave it, or a point by x/y (CSS pixels, as in "
            "browser_screenshot). Waits for any navigation it starts and returns the page afterwards."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "An element ref from the latest snapshot, e.g. ref_12."},
                "x": {"type": "integer", "description": "Viewport x in CSS pixels (with y, instead of ref)."},
                "y": {"type": "integer", "description": "Viewport y in CSS pixels (with x, instead of ref)."},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Default left."},
            },
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        if not params.get("ref") and (params.get("x") is None or params.get("y") is None):
            return ["ref, or both x and y, is required"]
        return []

    async def execute(
        self,
        ref: str | None = None,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        **kwargs: Any,
    ) -> ToolResult:
        owner = self._owner()

        async def run() -> ToolResult:
            state = await _browser().click(ref=ref, x=x, y=y, button=button or "left", owner=owner)
            return await self._readback(owner, state, acted=True)

        return await self._guard(run())

    def display_call(self, args: dict[str, Any]) -> str | None:
        return args.get("ref") or f"({args.get('x')}, {args.get('y')})"


class BrowserTypeTool(_ActingTool):
    @property
    def name(self) -> str:
        return "browser_type"

    @property
    def description(self) -> str:
        return (
            "Type into the current page and return it afterwards. With a ref the field is cleared and "
            "filled, without one the text goes to whatever has focus; submit=true presses Enter. Never "
            "type a password or payment detail the user did not give you here -- hand that step to them."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to type."},
                "ref": {"type": "string", "description": "The field's ref from the latest snapshot."},
                "submit": {"type": "boolean", "description": "Press Enter after typing (default false)."},
            },
            "required": ["text"],
        }

    async def execute(self, text: str, ref: str | None = None, submit: bool = False, **kwargs: Any) -> ToolResult:
        owner = self._owner()

        async def run() -> ToolResult:
            state = await _browser().type_text(text, ref=ref, submit=bool(submit), owner=owner)
            return await self._readback(owner, state, acted=True)

        return await self._guard(run())

    def display_call(self, args: dict[str, Any]) -> str | None:
        text = str(args.get("text", ""))
        return (text[:40] + "...") if len(text) > 40 else text


class BrowserPressTool(_ActingTool):
    @property
    def name(self) -> str:
        return "browser_press"

    @property
    def description(self) -> str:
        return (
            "Press a key or key combination on the current page, e.g. Enter, Escape, Tab, ArrowDown, "
            "Control+a. Returns the page afterwards."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "A key name or combination."}},
            "required": ["key"],
        }

    async def execute(self, key: str, **kwargs: Any) -> ToolResult:
        owner = self._owner()

        async def run() -> ToolResult:
            state = await _browser().press(key, owner=owner)
            return await self._readback(owner, state, acted=True)

        return await self._guard(run())


class BrowserScrollTool(_BrowserTool):
    @property
    def name(self) -> str:
        return "browser_scroll"

    @property
    def description(self) -> str:
        return (
            "Scroll the current page by a number of CSS pixels (positive dy scrolls down). Returns the "
            "page afterwards; elements that scrolled into view get refs."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dy": {"type": "integer", "description": "Vertical distance; positive is down."},
                "dx": {"type": "integer", "description": "Horizontal distance; positive is right (default 0)."},
            },
            "required": ["dy"],
        }

    async def execute(self, dy: int, dx: int = 0, **kwargs: Any) -> ToolResult:
        owner = self._owner()

        async def run() -> ToolResult:
            state = await _browser().scroll(dx=int(dx or 0), dy=int(dy), owner=owner)
            return await self._readback(owner, state, acted=True)

        return await self._guard(run())


class BrowserTabsTool(_BrowserTool):
    @property
    def name(self) -> str:
        return "browser_tabs"

    @property
    def description(self) -> str:
        return (
            "List, open, switch or close tabs of the shared browser. Your calls always land on your own "
            "tab; a tab marked held is another agent's and cannot be taken. new opens a fresh tab (with "
            "an optional url) and makes it yours; activate makes an unheld tab yours."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "new", "activate", "close"],
                    "description": "Default list.",
                },
                "index": {"type": "integer", "minimum": 0, "description": "Tab index for activate / close."},
                "url": {"type": "string", "description": "For new: the url to open in the tab."},
            },
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        if params.get("action") in ("activate", "close") and params.get("index") is None:
            return ["index is required for activate and close"]
        return []

    async def execute(
        self, action: str = "list", index: int | None = None, url: str | None = None, **kwargs: Any
    ) -> ToolResult:
        owner = self._owner()

        async def run() -> ToolResult:
            b = _browser()
            state: dict[str, Any] = {}
            if action == "new":
                await _pop_out_for_agent_use(b)
                state = await b.tab_new(url, owner=owner)
            elif action == "activate":
                state = await b.tab_activate(int(index), owner=owner)
            elif action == "close":
                state = await b.tab_close(int(index), owner=owner)
            if state.get("error"):
                return ToolResult(model_text=f"Error: {state['error']}", ok=False)
            if action != "list":
                self._mark(owner)
            tabs = await b.tabs(owner=owner) if b.started else []
            if not tabs:
                return ToolResult(model_text="No tabs are open; the browser is not running.", display_text="no tabs")
            lines = []
            for t in tabs:
                marks = []
                if t.get("yours"):
                    marks.append("yours")
                if t.get("held"):
                    marks.append("held by another agent")
                if t.get("loading"):
                    marks.append("loading")
                tag = f" ({', '.join(marks)})" if marks else ""
                lines.append(f"{t['index']}: {t['title'] or '(untitled)'} - {t['url']}{tag}")
            return ToolResult(model_text="\n".join(lines), display_text=f"{len(tabs)} tab(s)")

        return await self._guard(run())


def browser_tools() -> list[Tool]:
    """Every browser tool, in the order they are registered."""
    return [
        BrowserNavigateTool(),
        BrowserSnapshotTool(),
        BrowserScreenshotTool(),
        BrowserClickTool(),
        BrowserTypeTool(),
        BrowserPressTool(),
        BrowserScrollTool(),
        BrowserTabsTool(),
    ]


BROWSER_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in browser_tools())

__all__ = [
    "BROWSER_TOOL_NAMES",
    "SITE_PARAM",
    "BrowserClickTool",
    "BrowserNavigateTool",
    "BrowserPressTool",
    "BrowserScreenshotTool",
    "BrowserScrollTool",
    "BrowserSnapshotTool",
    "BrowserTabsTool",
    "BrowserTypeTool",
    "browser_tools",
    "current_owner",
    "render_snapshot",
    "render_state",
]
