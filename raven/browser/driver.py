"""A single Chromium page, driven over CDP by both the agent and the reader.

Design notes worth keeping:

* **The page is shared, not per-caller.** The agent's tools and the GUI panel
  address one page. That is what lets a reader log in by hand and the agent
  continue on the other side of the login.
* **Reading is structural, not visual.** ``snapshot`` walks the DOM for the
  handful of things a caller acts on and hands back stable ``ref`` ids. Asking
  the model to find a button in a JPEG costs far more and works less often, so
  screenshots are for showing the reader and for final visual checks.
* **Nothing starts until something asks.** Chromium is launched on the first
  navigation, so a Raven that never opens a page never pays for one.
* **Playwright is an extra.** Every entry point raises ``BrowserUnavailableError``
  with an install hint the reader can actually run rather than an ImportError
  traceback.
"""

from __future__ import annotations

import asyncio
import base64
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.browser.policy import NavigationRefusedError, check_navigation, navigation_refusal

# sys.executable is the venv that has playwright in both install shapes -- a uv
# tool install (the engines carry it) and a source checkout -- whereas `uv sync`
# exists only in the checkout, so the hint names the interpreter itself.
CHROMIUM_INSTALL_HINT = f"{shlex.quote(sys.executable)} -m playwright install chromium"
# When the package itself is missing (install.sh's bare-raven fallback rung),
# `python -m playwright` cannot work either; point at the installers instead.
PACKAGE_INSTALL_HINT = (
    "reinstall raven via install.sh (engines carry the browser library); from a source checkout: uv sync --all-extras"
)

# One profile on disk: logins survive restarts, and -- because pop-out is a
# relaunch -- they survive the panel/window switch too.
PROFILE_DIR = Path.home() / ".raven" / "browser-profile"

DEFAULT_VIEWPORT = (1280, 800)
NAV_TIMEOUT_MS = 30_000
ACT_TIMEOUT_MS = 10_000
MAX_REFS = 200
MAX_TEXT_CHARS = 20_000
MAX_TABS = 20
"""A ceiling on open tabs.

The agent opens tabs through the same call the panel's Cmd-T uses, and a loop
that opens one per iteration costs a renderer process each -- the machine goes
down before anything in raven notices. The number is generous for a reader and
still bounded."""


class BrowserUnavailableError(RuntimeError):
    """Playwright or its Chromium is not installed."""


# The elements a caller can actually act on, plus the few that carry the page's
# structure. Everything else is noise in a snapshot.
_SNAPSHOT_JS = """
(maxRefs) => {
  const out = [];
  const seen = new WeakSet();
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=textbox],'
    + '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[contenteditable=""],'
    + '[contenteditable=true],h1,h2,h3,summary,[onclick]';
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return null;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0') return null;
    return r;
  };
  const label = (el) => {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria.trim();
    if (el.tagName === 'INPUT') {
      const ph = el.getAttribute('placeholder');
      if (ph) return ph.trim();
      if (el.labels && el.labels[0]) return el.labels[0].textContent.trim();
      return (el.getAttribute('name') || el.type || '').trim();
    }
    const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    return t.slice(0, 120);
  };
  const role = (el) => {
    const r = el.getAttribute && el.getAttribute('role');
    if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'input') return el.type === 'checkbox' || el.type === 'radio' ? el.type : 'textbox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (/^h[1-3]$/.test(tag)) return 'heading';
    return tag;
  };
  document.querySelectorAll(sel).forEach((el) => {
    if (out.length >= maxRefs || seen.has(el)) return;
    const r = vis(el);
    if (!r) return;
    seen.add(el);
    const item = {
      ref: 'ref_' + (out.length + 1),
      role: role(el),
      name: label(el),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
    };
    if (el.tagName === 'A' && el.href) item.href = el.href;
    if (el.value !== undefined && el.type !== 'password' && typeof el.value === 'string') {
      item.value = el.value.slice(0, 80);
    }
    if (el.disabled) item.disabled = true;
    el.setAttribute('data-raven-ref', item.ref);
    out.push(item);
  });
  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : '').slice(0, %MAX_TEXT%),
    refs: out,
  };
}
""".replace("%MAX_TEXT%", str(MAX_TEXT_CHARS))


@dataclass
class _State:
    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    console: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cdp: Any = None
    on_frame: Any = None
    stream_quality: int = 55
    # Why the last navigation was refused, for the next state() to report. A
    # click that goes nowhere with nothing said is indistinguishable from a
    # dead link.
    refused: str = ""


class Browser:
    """The shared page. Every method starts Chromium if it is not up yet."""

    def __init__(self) -> None:
        self._s = _State()
        # Survives close(): the pop-out flow is close-and-relaunch, and the
        # relaunch has to remember which kind of window was asked for.
        self._headful = False
        # Also survives close(), and for a sharper reason: `_State` is one
        # session, this guards the transition *between* sessions. A relaunch
        # calls close(), which swaps in a fresh state -- so a lock living there
        # would be replaced mid-launch and stop excluding anyone at exactly the
        # moment two callers are most likely to collide. Separate from
        # `_s.lock`, which the navigation methods take after calling `_ensure`.
        self._launch_lock = asyncio.Lock()

    # ---- lifecycle ---------------------------------------------------------

    @staticmethod
    def probe() -> tuple[bool, str]:
        """Whether a page can be opened at all, and why not when it cannot."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False, f"playwright is not installed: {PACKAGE_INSTALL_HINT}"
        return True, ""

    @property
    def started(self) -> bool:
        page = self._s.page
        try:
            return page is not None and not page.is_closed()
        except Exception:
            return page is not None

    async def _ensure(self) -> Any:
        ok, why = self.probe()
        if not ok:
            raise BrowserUnavailableError(why)
        # Serialised, because the launch is the part with the most await points
        # in it and the gateway dispatches frames concurrently: two callers that
        # both found no page would each start a Chromium, the second overwriting
        # the first's handles so nothing could ever close them -- and the
        # survivor would be the one that lost the profile lock, i.e. the one
        # without the reader's logins.
        async with self._launch_lock:
            return await self._launch_once()

    async def _launch_once(self) -> Any:
        if self._s.page is not None:
            # A reader can close a popped-out window under us; a dead page
            # must relaunch, not raise TargetClosed on every call forever.
            if self.started:
                return self._s.page
            await self.close()
        from playwright.async_api import async_playwright

        w, h = DEFAULT_VIEWPORT
        launch: dict[str, Any] = {"headless": not self._headful}
        if self._headful:
            # Drop the "controlled by automated software" banner: to the
            # reader this window is simply their browser.
            launch["ignore_default_args"] = ["--enable-automation"]
            launch["args"] = [f"--window-size={w},{h + 88}"]
        ctx_opts: dict[str, Any] = {
            "viewport": {"width": w, "height": h},
            # 2x so the panel's stream is crisp on retina displays; the
            # agent's screenshots stay CSS-sized via scale="css" below.
            "device_scale_factor": 2,
            # A page that knows it is headless renders differently often
            # enough that the reader would be looking at a different site
            # than the one they would get in their own browser.
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
            ),
        }
        try:
            self._s.playwright = await async_playwright().start()
            context = None
            try:
                PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                context = await self._s.playwright.chromium.launch_persistent_context(
                    str(PROFILE_DIR), **launch, **ctx_opts
                )
            except Exception as exc:
                # Another Raven (an old TUI, a second serve) may hold the
                # profile's singleton lock; browsing must still work, just
                # without the shared cookie jar.
                text = str(exc)
                if "SingletonLock" not in text and "profile" not in text.lower():
                    raise
                logger.warning("browser: profile is busy, using a throwaway one ({})", text.splitlines()[0])
            if context is None:
                self._s.browser = await self._s.playwright.chromium.launch(**launch)
                context = await self._s.browser.new_context(**ctx_opts)
            self._s.context = context
            pages = context.pages
            self._s.page = pages[0] if pages else await context.new_page()
            self._wire(self._s.page)
            # Registered after the first page exists so it only ever fires for
            # popups. A target=_blank link becomes a new tab and the view
            # follows it -- the native behaviour, and both the agent and the
            # panel keep addressing the active tab.
            self._s.context.on("page", lambda p: asyncio.ensure_future(self._adopt(p)))
            # The URL-string check on `goto` is the only one a caller can reach,
            # and it is not the only way the page moves: a click follows an href
            # the driver never sees, a popup becomes the active tab, and goto
            # itself follows redirects the pre-check could not have seen. On a
            # cloud host that is the whole link-local refusal, since the page
            # the agent is reading is what tells it where to go next.
            await context.route("**/*", self._police)
        except Exception as exc:
            await self.close()
            hint = f"{exc}"
            if "Executable doesn't exist" in hint or "playwright install" in hint:
                raise BrowserUnavailableError(f"Chromium is not installed. Run: {CHROMIUM_INSTALL_HINT}") from None
            raise BrowserUnavailableError(f"could not start Chromium: {hint}") from None
        logger.info("browser: chromium started ({}x{})", w, h)
        return self._s.page

    async def _police(self, route: Any) -> None:
        """Refuse a navigation Chromium is about to make, whoever asked for it.

        Only *navigations* are judged -- a subresource cannot put its bytes
        where ``browser.read`` would return them -- but every request is still
        paused and resumed through here, because a route pattern is the only
        filter Playwright offers and it matches on the URL, not the resource
        kind. Two costs come with that, and they are paid on every request
        rather than on the ones this refuses:

        * a round trip into Python per request;
        * no HTTP cache. Playwright disables it for the life of any context
          with routing enabled, so a reload refetches every subresource.

        Both are real for a reader watching the panel, and the alternative is
        CDP (``Fetch.enable`` with ``resourceType: Document``), which pauses
        only documents and leaves the cache alone. It is not here because a
        CDP session is per page and would have to outlive the screencast's --
        a lifetime change that wants a real Chromium to verify, which this
        module's tests deliberately do not need.
        """
        request = route.request
        try:
            if not request.is_navigation_request():
                await route.continue_()
                return
            reason = navigation_refusal(request.url)
            if reason is None:
                await route.continue_()
                return
        except Exception:  # noqa: BLE001 - the fence must not hang a page
            # Fail open rather than leave the request neither continued nor
            # aborted: a request nobody answers hangs until the navigation times
            # out, with nothing said to anyone. Not because `goto` checked
            # first -- it did not, for the paths this exists for: a click, a
            # popup and a post-redirect hop never went through it, so here this
            # is the only line. The trade is that a throw comes from a route
            # torn down under us far more often than from a URL the check
            # chokes on, and the alternative to letting one through is hanging
            # the page. A torn-down route raises on the way out too, which is
            # why the continue is itself guarded.
            logger.debug("browser: could not police {}", getattr(request, "url", "?"))
            try:
                await route.continue_()
            except Exception:  # noqa: BLE001 - already gone
                pass
            return
        logger.warning("browser: refused navigation to {} ({})", request.url, reason)
        self._s.refused = reason
        await route.abort("blockedbyclient")

    def _on_console(self, msg: Any) -> None:
        self._s.console.append({"type": msg.type, "text": msg.text[:500]})
        del self._s.console[:-100]

    def _wire(self, page: Any) -> None:
        """Handlers every tab needs, applied once per page.

        The loading flag rides the page object itself: navigation commits set
        it, the load event clears it, and the frame header / state carry it to
        the panel's progress bar. ``framenavigated`` fires for every frame, so
        the handler filters to the main frame.
        """
        page.set_default_timeout(ACT_TIMEOUT_MS)
        page.on("console", self._on_console)
        page._raven_loading = False

        def _nav(frame: Any) -> None:
            if frame == page.main_frame:
                page._raven_loading = True

        page.on("framenavigated", _nav)
        page.on("load", lambda: setattr(page, "_raven_loading", False))
        # A page that never reaches load (SPA soft-fails, aborted loads) must
        # not spin forever; domcontentloaded is the honest lower bound.
        page.on("domcontentloaded", lambda: setattr(page, "_raven_loading", False))

    async def _restream(self) -> None:
        """Re-bind the screencast to the (new) active page."""
        if self._s.on_frame is not None:
            try:
                await self.start_stream(self._s.on_frame, quality=self._s.stream_quality)
                logger.debug("browser: restreamed onto {}", self.url)
            except Exception:
                logger.exception("browser: could not restream after tab switch")

    async def _adopt(self, page: Any) -> None:
        """A popup becomes a new tab and the view follows it."""
        if page is self._s.page or self._s.context is None:
            return
        self._wire(page)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:
            pass
        self._s.page = page
        await self._restream()

    # ---- tabs ----------------------------------------------------------------

    def _pages(self) -> list[Any]:
        ctx = self._s.context
        if ctx is None:
            return []
        return [p for p in ctx.pages if not p.is_closed()]

    async def tabs(self) -> list[dict[str, Any]]:
        """Every open tab, active flag included. Never starts Chromium."""
        out: list[dict[str, Any]] = []
        for i, p in enumerate(self._pages()):
            title = ""
            try:
                title = await p.title()
            except Exception:
                pass
            out.append(
                {
                    "index": i,
                    "url": p.url,
                    "title": title,
                    "active": p is self._s.page,
                    "loading": bool(getattr(p, "_raven_loading", False)),
                }
            )
        return out

    async def tab_new(self, url: str | None = None) -> dict[str, Any]:
        await self._ensure()
        if len(self._pages()) >= MAX_TABS:
            return await self._state(error=f"tab limit reached ({MAX_TABS}); close one first")
        page = await self._s.context.new_page()
        self._wire(page)
        self._s.page = page
        await self._restream()
        if url and url.strip():
            return await self.goto(url.strip())
        return await self._state()

    async def tab_activate(self, index: int) -> dict[str, Any]:
        pages = self._pages()
        if not (0 <= index < len(pages)):
            return await self._state(error=f"no tab {index}")
        if pages[index] is not self._s.page:
            self._s.page = pages[index]
            await self._restream()
            try:
                await self._s.page.bring_to_front()
            except Exception:
                pass
        return await self._state()

    async def tab_close(self, index: int) -> dict[str, Any]:
        """Close one tab; closing the last one closes the browser (native)."""
        pages = self._pages()
        if not (0 <= index < len(pages)):
            return await self._state(error=f"no tab {index}")
        victim = pages[index]
        was_active = victim is self._s.page
        try:
            await victim.close()
        except Exception:
            pass
        rest = self._pages()
        if not rest:
            await self.close()
            return {"url": "", "title": "", "started": False, "headful": self._headful}
        if was_active:
            self._s.page = rest[min(index, len(rest) - 1)]
            await self._restream()
        return await self._state()

    async def stop_loading(self) -> dict[str, Any]:
        """The toolbar's stop button: Page.stopLoading on the active tab."""
        page = self._s.page
        if page is None:
            return await self._state()
        try:
            cdp = self._s.cdp
            if cdp is not None:
                await cdp.send("Page.stopLoading")
            else:
                tmp = await self._s.context.new_cdp_session(page)
                await tmp.send("Page.stopLoading")
                await tmp.detach()
            page._raven_loading = False
        except Exception as exc:
            return await self._state(error=str(exc))
        return await self._state()

    async def set_headful(self, headful: bool) -> dict[str, Any]:
        """Move the page between the panel stream and a real window.

        Chromium cannot change headless-ness in place, so this is a relaunch
        that carries the URL over. Logins ride along in the persistent
        profile; only a throwaway-profile fallback (profile lock held
        elsewhere) loses them.
        """
        headful = bool(headful)
        if headful == self._headful:
            return await self.state()
        url = self.url
        if self._s.page is not None:
            await self.close()
        self._headful = headful
        if url and not url.startswith("about:"):
            return await self.goto(url)
        return await self.state()

    async def close(self) -> None:
        await self.stop_stream()
        s = self._s
        for name in ("context", "browser"):
            obj = getattr(s, name)
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        if s.playwright is not None:
            try:
                await s.playwright.stop()
            except Exception:
                pass
        self._s = _State()
        logger.info("browser: closed")

    # ---- navigation --------------------------------------------------------

    async def goto(self, url: str) -> dict[str, Any]:
        # Checked before the page is ensured: a refused target must not be the
        # thing that launches Chromium.
        try:
            target = check_navigation(url)
        except NavigationRefusedError as exc:
            return await self._state(error=str(exc))
        page = await self._ensure()
        async with self._s.lock:
            try:
                await page.goto(target, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            except Exception as exc:
                return await self._state(error=str(exc))
        return await self._state()

    async def go(self, direction: str) -> dict[str, Any]:
        page = await self._ensure()
        async with self._s.lock:
            try:
                if direction == "back":
                    await page.go_back(timeout=NAV_TIMEOUT_MS)
                elif direction == "forward":
                    await page.go_forward(timeout=NAV_TIMEOUT_MS)
                else:
                    await page.reload(timeout=NAV_TIMEOUT_MS)
            except Exception as exc:
                return await self._state(error=str(exc))
        return await self._state()

    # ---- reading -----------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        """The page as things a caller can act on, with stable refs."""
        page = await self._ensure()
        try:
            data = await page.evaluate(_SNAPSHOT_JS, MAX_REFS)
        except Exception as exc:
            return await self._state(error=str(exc))
        data["console"] = list(self._s.console[-20:])
        return data

    async def screenshot(self, quality: int = 60, full: bool = False) -> str:
        """A base64 JPEG. The panel shows it; a tool uses it to check its work.

        scale="css" keeps the image at viewport size: the context renders at
        2x for the reader's stream, and a 2x screenshot would quadruple what
        the model pays to look at the same page.
        """
        page = await self._ensure()
        raw = await page.screenshot(type="jpeg", quality=max(1, min(100, quality)), full_page=full, scale="css")
        return base64.b64encode(raw).decode("ascii")

    # ---- the reader's live view ---------------------------------------------

    @property
    def streaming(self) -> bool:
        return self._s.cdp is not None

    @property
    def url(self) -> str:
        return self._s.page.url if self._s.page else ""

    @property
    def loading(self) -> bool:
        p = self._s.page
        return bool(getattr(p, "_raven_loading", False)) if p else False

    @property
    def viewport(self) -> tuple[int, int]:
        vp = (self._s.page.viewport_size if self._s.page else None) or {}
        return vp.get("width", DEFAULT_VIEWPORT[0]), vp.get("height", DEFAULT_VIEWPORT[1])

    async def set_viewport(self, width: int, height: int) -> None:
        """Match the page to the panel, so the panel is the page -- not a
        thumbnail of some other, wider page."""
        page = await self._ensure()
        w = max(320, min(3840, int(width)))
        h = max(240, min(2400, int(height)))
        if (w, h) != self.viewport:
            await page.set_viewport_size({"width": w, "height": h})

    async def start_stream(
        self, on_frame: Any, *, width: int | None = None, height: int | None = None, quality: int = 55
    ) -> None:
        """CDP screencast: Chromium pushes a JPEG per paint to ``on_frame``.

        ``on_frame(jpeg_b64, meta)`` is called from the CDP event loop and must
        not block. Pushing (rather than the panel polling screenshots) is what
        makes typing and scrolling feel attached to the page: the next frame
        arrives when the page paints, not when the next poll comes around.
        """
        page = await self._ensure()
        if width and height:
            await self.set_viewport(width, height)
        await self.stop_stream()
        cdp = await self._s.context.new_cdp_session(page)
        self._s.cdp = cdp
        self._s.on_frame = on_frame
        self._s.stream_quality = quality

        def _frame(params: dict[str, Any]) -> None:
            sid = params.get("sessionId")
            if sid is not None:
                # Ack immediately or Chromium stops sending.
                asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": sid}))
            try:
                on_frame(params.get("data", ""), params.get("metadata") or {})
            except Exception:
                logger.exception("browser: frame sink failed")

        cdp.on("Page.screencastFrame", _frame)
        w, h = self.viewport
        await cdp.send(
            "Page.startScreencast",
            {
                # 2x the CSS viewport: the capture happens at the context's
                # device pixels, so this cap is "full retina", not upscaling.
                "format": "jpeg",
                "quality": max(1, min(100, int(quality))),
                "maxWidth": w * 2,
                "maxHeight": h * 2,
                "everyNthFrame": 1,
            },
        )

    async def stop_stream(self) -> None:
        cdp = self._s.cdp
        self._s.cdp = None
        self._s.on_frame = None
        if cdp is not None:
            for op in ("send", "detach"):
                try:
                    await (cdp.send("Page.stopScreencast") if op == "send" else cdp.detach())
                except Exception:
                    pass

    async def _history_reach(self) -> tuple[bool, bool]:
        """(can_back, can_forward) for the active tab, via CDP history.

        Uses the screencast's CDP session when one is live; otherwise a
        short-lived one. Any failure degrades to (True, True) — a wrongly
        enabled arrow no-ops, a wrongly disabled one loses a capability.
        """
        page = self._s.page
        if page is None:
            return False, False
        try:
            cdp = self._s.cdp
            own = False
            if cdp is None:
                cdp = await self._s.context.new_cdp_session(page)
                own = True
            hist = await cdp.send("Page.getNavigationHistory")
            if own:
                await cdp.detach()
            i = int(hist.get("currentIndex", 0))
            n = len(hist.get("entries") or [])
            return i > 0, i < n - 1
        except Exception:
            return True, True

    async def _state(self, error: str | None = None) -> dict[str, Any]:
        page = self._s.page
        title = ""
        if page is not None:
            try:
                title = await page.title()
            except Exception:
                pass
        can_back, can_fwd = await self._history_reach()
        out: dict[str, Any] = {
            "url": page.url if page else "",
            "title": title,
            "started": self.started,
            "headful": self._headful,
            "loading": bool(getattr(page, "_raven_loading", False)) if page else False,
            "can_back": can_back,
            "can_forward": can_fwd,
            "tab_count": len(self._pages()),
        }
        if not error and self._s.refused:
            # Reported once, then forgotten: a click that goes nowhere with
            # nothing said reads as a dead link rather than a refusal.
            error, self._s.refused = self._s.refused, ""
        if error:
            out["error"] = error
        return out

    async def state(self) -> dict[str, Any]:
        """Where the page is, without starting a browser to find out."""
        if self._s.page is None:
            ok, why = self.probe()
            return {
                "url": "",
                "title": "",
                "started": False,
                "headful": self._headful,
                "available": ok,
                "reason": why,
            }
        out = await self._state()
        out["available"] = True
        out["reason"] = ""
        return out

    # ---- acting ------------------------------------------------------------

    async def _locate(self, ref: str) -> Any:
        page = await self._ensure()
        return page.locator(f'[data-raven-ref="{ref}"]')

    async def click(
        self, ref: str | None = None, x: int | None = None, y: int | None = None, button: str = "left"
    ) -> dict[str, Any]:
        page = await self._ensure()
        async with self._s.lock:
            try:
                if ref:
                    await (await self._locate(ref)).click(timeout=ACT_TIMEOUT_MS)
                elif x is not None and y is not None:
                    await page.mouse.click(x, y, button=button)
                else:
                    return await self._state(error="click needs a ref or x/y")
                # A click that navigates needs the load to settle before the
                # caller reads the page, or it reads the old one.
                await page.wait_for_load_state("domcontentloaded", timeout=ACT_TIMEOUT_MS)
            except Exception as exc:
                return await self._state(error=str(exc))
        return await self._state()

    async def type_text(self, text: str, ref: str | None = None, submit: bool = False) -> dict[str, Any]:
        page = await self._ensure()
        async with self._s.lock:
            try:
                if ref:
                    loc = await self._locate(ref)
                    await loc.fill(text, timeout=ACT_TIMEOUT_MS)
                else:
                    await page.keyboard.type(text)
                if submit:
                    await page.keyboard.press("Enter")
                    await page.wait_for_load_state("domcontentloaded", timeout=ACT_TIMEOUT_MS)
            except Exception as exc:
                return await self._state(error=str(exc))
        return await self._state()

    async def press(self, key: str) -> dict[str, Any]:
        page = await self._ensure()
        async with self._s.lock:
            try:
                await page.keyboard.press(key)
            except Exception as exc:
                return await self._state(error=str(exc))
        return await self._state()

    async def scroll(self, dx: int = 0, dy: int = 0) -> dict[str, Any]:
        page = await self._ensure()
        async with self._s.lock:
            try:
                await page.mouse.wheel(dx, dy)
            except Exception as exc:
                return await self._state(error=str(exc))
        return await self._state()

    # ---- the reader's raw input ---------------------------------------------
    # No lock and no state readback: these arrive as a stream (a drag is dozens
    # of moves), the screencast is already reporting the result, and a lock
    # here would let one slow agent navigation freeze the reader's pointer.

    async def mouse(
        self,
        action: str,
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        count: int = 1,
        dx: float = 0,
        dy: float = 0,
    ) -> None:
        page = await self._ensure()
        m = page.mouse
        if action == "move" and x is not None:
            await m.move(x, y or 0)
        elif action == "down":
            if x is not None:
                await m.move(x, y or 0)
            await m.down(button=button, click_count=max(1, int(count)))
        elif action == "up":
            await m.up(button=button, click_count=max(1, int(count)))
        elif action == "wheel":
            await m.wheel(dx, dy)

    async def key_event(self, key: str, action: str = "press") -> None:
        page = await self._ensure()
        kb = page.keyboard
        if action == "down":
            await kb.down(key)
        elif action == "up":
            await kb.up(key)
        else:
            await kb.press(key)

    async def insert_text(self, text: str) -> None:
        """Composed text (IME input) lands as-is -- key events cannot spell it."""
        page = await self._ensure()
        await page.keyboard.insert_text(text)


_BROWSER: Browser | None = None


def get_browser() -> Browser:
    """The process-wide page. One browser per Raven, not one per caller."""
    global _BROWSER
    if _BROWSER is None:
        _BROWSER = Browser()
    return _BROWSER
