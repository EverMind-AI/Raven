"""aiohttp WebSocket + static transport for `raven serve`.

Browser auth bootstrap (Jupyter-style): the launcher mints a one-time nonce
and opens ``http://127.0.0.1:<port>/auth#<nonce>``. The fragment never enters
the HTTP request line or server logs; the auth page POSTs it to
``/auth/exchange`` which burns the nonce and sets an HttpOnly SameSite=Strict
session cookie. The ``/rpc`` WebSocket upgrade and any future ``/files/*``
endpoints require that cookie (or an ``X-Raven-Token`` header for local
tooling), plus an Origin check when a browser sends one.

Multi-connection from day one (plan D3): every authenticated socket receives
all notification frames; request/response frames stay on the socket that sent
the request; concurrent-turn conflicts are already guarded by -32003.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web
from loguru import logger

DEFAULT_PORT = 18792
_PORT_PROBE_SPAN = 20
# A sign-in nonce is a live credential until redeemed. Long enough for a
# browser to launch and load, short enough that one left in scrollback is
# worthless by the time anyone reads it.
_NONCE_TTL_S = 120.0

_AUTH_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Raven</title></head>
<body style="font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0">
<p id="s">Signing in to Raven...</p>
<script>
(async () => {
  const nonce = location.hash.slice(1);
  history.replaceState(null, '', '/auth');
  const r = await fetch('/auth/exchange', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({nonce}),
  });
  if (r.ok) { location.replace('/'); }
  else { document.getElementById('s').textContent = 'Sign-in link expired. Re-run: raven serve --open'; }
})();
</script></body></html>"""

_COOKIE_NAME = "raven_session"


class WsGateway:
    """Owns the session token, one-time nonces, and the live WS connection set."""

    def __init__(self) -> None:
        # Two credentials, deliberately not the same value.
        #
        # ``session_token`` is the shared secret. It mints nonces, and a holder
        # can issue itself durable credentials, so it never leaves the process
        # except into ~/.raven/serve.json (0600) for a relauncher to read.
        #
        # ``session_cookie`` is what the browser holds. Cookies ignore port
        # (RFC 6265 section 8.5), so anything the user's browser talks to on
        # another 127.0.0.1 port receives this value -- a Vite dev server, an
        # Electron app, a package's postinstall listener. Handing that a
        # revocable per-process session id costs an attacker one open browser;
        # handing it the shared secret would cost the nonce-minting endpoint too.
        #
        # RAVEN_SERVE_* seed both for local tooling, e2e tests, and the
        # `system.upgrade` relaunch, which must keep an open browser signed in.
        import os

        self.session_token = os.environ.get("RAVEN_SERVE_TOKEN") or secrets.token_hex(32)
        self.session_cookie = os.environ.get("RAVEN_SERVE_COOKIE") or secrets.token_urlsafe(32)
        self._nonces: dict[str, float] = {}
        self._sockets: set[web.WebSocketResponse] = set()
        self.dispatcher: Any = None
        self.port: int = DEFAULT_PORT

    def mint_nonce(self) -> str:
        """Issue a one-time sign-in nonce, valid for :data:`_NONCE_TTL_S`.

        The TTL is the point: an unredeemed nonce is a live credential, and one
        printed to the terminal because no browser opened would otherwise stay
        redeemable for the life of the process -- recoverable days later from
        scrollback or a shared screen.
        """
        now = time.monotonic()
        self._nonces = {n: exp for n, exp in self._nonces.items() if exp > now}
        nonce = secrets.token_urlsafe(24)
        self._nonces[nonce] = now + _NONCE_TTL_S
        return nonce

    def burn_nonce(self, nonce: str) -> bool:
        expiry = self._nonces.pop(nonce, None)
        return expiry is not None and expiry > time.monotonic()

    # ---- send_frame sink for the RPC stack ---------------------------------

    async def broadcast(self, frame: dict[str, Any] | bytes) -> None:
        if not self._sockets:
            return
        # bytes go out as a binary WS message: a screencast frame is an image,
        # and base64-inside-JSON costs a third more wire and a decode per frame.
        if isinstance(frame, bytes | bytearray):
            for ws in list(self._sockets):
                try:
                    await ws.send_bytes(bytes(frame))
                except Exception:
                    self._sockets.discard(ws)
            return
        data = json.dumps(frame, ensure_ascii=False)
        for ws in list(self._sockets):
            try:
                await ws.send_str(data)
            except Exception:
                self._sockets.discard(ws)

    # ---- auth helpers -------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        """Either credential opens the authenticated surface.

        compare_digest on both: the timing difference is not practically
        reachable here, but a credential check that short-circuits on the
        first wrong byte is not worth keeping when the fix is free.
        """
        cookie = request.cookies.get(_COOKIE_NAME)
        if cookie is not None and secrets.compare_digest(cookie, self.session_cookie):
            return True
        header = request.headers.get("X-Raven-Token")
        return header is not None and secrets.compare_digest(header, self.session_token)

    def _origin_ok(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        return origin in (
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        )

    # ---- handlers ------------------------------------------------------------

    async def handle_health(self, request: web.Request) -> web.Response:
        from raven.tui_rpc.methods.system import SCHEMA_VERSION, SERVER_VERSION

        return web.json_response(
            {"ok": True, "service": "raven-serve", "server_version": SERVER_VERSION, "schema_version": SCHEMA_VERSION}
        )

    async def handle_auth_page(self, request: web.Request) -> web.Response:
        return web.Response(text=_AUTH_PAGE, content_type="text/html")

    async def handle_auth_exchange(self, request: web.Request) -> web.Response:
        if not self._origin_ok(request):
            raise web.HTTPForbidden(reason="bad origin")
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="expected JSON body") from None
        nonce = body.get("nonce") if isinstance(body, dict) else None
        token = body.get("token") if isinstance(body, dict) else None
        # A client already holding the shared secret may trade it for the
        # cookie directly (local tooling / e2e); browsers go through the nonce.
        token_ok = isinstance(token, str) and secrets.compare_digest(token, self.session_token)
        nonce_ok = isinstance(nonce, str) and self.burn_nonce(nonce)
        if not (nonce_ok or token_ok):
            raise web.HTTPForbidden(reason="invalid or used nonce")
        resp = web.json_response({"ok": True})
        resp.set_cookie(
            _COOKIE_NAME,
            self.session_cookie,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return resp

    async def handle_mint_nonce(self, request: web.Request) -> web.Response:
        """Trade the shared secret for a fresh one-time nonce.

        Lets a relauncher (a second `raven serve --open` against an
        already-running gateway, or a desktop wrapper) bootstrap a new browser
        session without restarting the server. Requires the X-Raven-Token
        header; never cookie-callable, so a page script cannot mint nonces.
        """
        header = request.headers.get("X-Raven-Token")
        if header is None or not secrets.compare_digest(header, self.session_token):
            raise web.HTTPUnauthorized(reason="missing or invalid token")
        return web.json_response({"nonce": self.mint_nonce()})

    async def handle_file(self, request: web.Request) -> web.StreamResponse:
        """Serve one local file for the page's viewer.

        Every response is sandboxed by CSP. A file the agent produced can be
        HTML or SVG, and those are script carriers: served same-origin without
        this header, opening one would hand the page's own origin -- and with it
        the session cookie and the RPC socket -- to whatever the agent wrote.
        The sandbox directive gives the response an opaque origin instead, both
        in an iframe and in a tab the reader opened themselves.
        """
        from raven.tui_rpc.files import MAX_VIEW_BYTES, content_type_for, resolve_readable, sandbox_for

        if not self._origin_ok(request):
            raise web.HTTPForbidden(reason="bad origin")
        if not self._authorized(request):
            raise web.HTTPUnauthorized(reason="missing or invalid session")
        raw = request.query.get("path", "")
        try:
            path = resolve_readable(raw)
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from None
        except PermissionError as exc:
            raise web.HTTPForbidden(reason=str(exc)) from None
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise web.HTTPNotFound(reason=str(exc)) from None
        except OSError as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from None
        if path.stat().st_size > MAX_VIEW_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_VIEW_BYTES, actual_size=path.stat().st_size)
        return web.FileResponse(
            path,
            headers={
                "Content-Type": content_type_for(path),
                "Content-Disposition": "inline",
                "Content-Security-Policy": sandbox_for(path),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        if not self._origin_ok(request):
            raise web.HTTPForbidden(reason="bad origin")
        if not self._authorized(request):
            raise web.HTTPUnauthorized(reason="missing or invalid session")

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._sockets.add(ws)
        logger.info("serve: ws client connected ({} active)", len(self._sockets))
        pending: set[asyncio.Task] = set()
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_str(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": None,
                                "error": {"code": -32700, "message": "parse_error"},
                            }
                        )
                    )
                    continue
                task = asyncio.create_task(self._dispatch_one(ws, frame))
                pending.add(task)
                task.add_done_callback(pending.discard)
        finally:
            self._sockets.discard(ws)
            for task in pending:
                task.cancel()
            logger.info("serve: ws client disconnected ({} active)", len(self._sockets))
        return ws

    async def _dispatch_one(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
        response = await self.dispatcher.dispatch(frame)
        if frame.get("id") is None:
            return
        try:
            payload = json.dumps(response, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            # A handler returned something JSON can't carry. Silently dropping
            # the frame leaves the client's call pending forever; answer with
            # an internal error instead.
            logger.warning("serve: response for '{}' not serializable: {}", frame.get("method"), e)
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": frame.get("id"),
                    "error": {"code": -32603, "message": "internal_error", "data": {"detail": str(e)}},
                }
            )
        try:
            await ws.send_str(payload)
        except Exception as e:  # noqa: BLE001 — client gone mid-send; nothing to answer
            logger.debug("serve: response send for '{}' failed: {}", frame.get("method"), e)


async def handle_oauth_callback(request: web.Request) -> web.Response:
    """MCP OAuth loopback redirect. Unauthenticated by design: the browser
    arrives here from the authorization server, carrying only the one-shot
    ``state`` that raven itself minted seconds ago.

    Being unauthenticated *and* same-origin with /rpc is what makes the CSP
    mandatory rather than decorative: the authorization server chooses the
    redirect, so it chooses what lands in the query string, and this origin is
    the one the session cookie is attached to.
    """
    from raven.agent.tools.mcp_oauth import CALLBACK_CSP, resolve_callback

    matched, html = resolve_callback(dict(request.query))
    return web.Response(
        text=html,
        content_type="text/html",
        status=200 if matched else 400,
        headers={
            "Content-Security-Policy": CALLBACK_CSP,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


def build_app(gateway: WsGateway, static_dir: Path | None) -> web.Application:
    app = web.Application()
    app.router.add_get("/health", gateway.handle_health)
    app.router.add_get("/auth", gateway.handle_auth_page)
    app.router.add_post("/auth/exchange", gateway.handle_auth_exchange)
    app.router.add_post("/auth/nonce", gateway.handle_mint_nonce)
    app.router.add_get("/file", gateway.handle_file)
    app.router.add_get("/rpc", gateway.handle_ws)
    app.router.add_get("/oauth/callback", handle_oauth_callback)

    if static_dir is not None and (static_dir / "index.html").exists():

        async def index(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(static_dir / "index.html")

        app.router.add_get("/", index)
        assets = static_dir / "assets"
        if assets.is_dir():
            app.router.add_static("/assets", assets)
    else:

        async def placeholder(_request: web.Request) -> web.Response:
            return web.Response(
                text="<h1>raven serve</h1><p>No front end built here; the /rpc WebSocket endpoint is live.</p>",
                content_type="text/html",
            )

        app.router.add_get("/", placeholder)

    return app


def _port_is_free(port: int) -> bool:
    """Whether the real listener could bind this port.

    SO_REUSEADDR matters: asyncio sets it on the socket it actually listens on,
    so a predecessor's socket sitting in TIME_WAIT does not block the successor.
    A probe without it is stricter than the thing it is probing for, and would
    report a port taken that binds fine a moment later.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


async def pick_port(preferred: int, *, strict: bool = False, wait_s: float = 20.0) -> int:
    """Bind-probe forward from ``preferred`` and return the first free port.

    ``strict`` waits for ``preferred`` itself instead of moving on, and raises if
    it never frees up. A relaunch (see ``system.upgrade``) has to come back on
    the port the open browser is already pointed at: a different port strands
    that page just as surely as no server at all, and the predecessor's socket
    is often still closing when the successor starts.
    """
    import asyncio

    if strict:
        deadline = asyncio.get_running_loop().time() + max(0.0, wait_s)
        while True:
            if _port_is_free(preferred):
                return preferred
            if asyncio.get_running_loop().time() >= deadline:
                raise OSError(f"port {preferred} did not become free within {wait_s:g}s")
            await asyncio.sleep(0.25)

    for port in range(preferred, preferred + _PORT_PROBE_SPAN):
        if _port_is_free(port):
            return port
    raise OSError(f"no free port in {preferred}..{preferred + _PORT_PROBE_SPAN - 1}")


__all__ = ["DEFAULT_PORT", "WsGateway", "build_app", "pick_port"]
