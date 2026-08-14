"""Typer subcommand: `raven serve` — the headless RPC gateway.

Runs the shared RPC stack (the same engine assembly `raven tui` builds, see
``raven/tui_rpc/bootstrap.py``) behind an aiohttp WebSocket at ``/rpc``, so a
browser-based front end reaches the same runtime the terminal does.

A page is served from ``<repo>/ui-gui/dist`` or the wheel's packaged copy when
one is present; with none built, ``/`` answers with a short notice and the
WebSocket endpoint stays live. No front end ships in this repo yet, so that is
the normal state -- `--open` is for whoever builds one.
"""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Optional

import typer

_UI_GUI_DIR = Path(__file__).resolve().parent.parent.parent / "ui-gui"
_PACKAGED_GUI_DIST = Path(__file__).resolve().parent.parent / "ui-gui" / "dist"


def resolve_gui_dist() -> Optional[Path]:
    """Locate the built ui-gui SPA: wheel-packaged copy first, then source tree."""
    for candidate in (_PACKAGED_GUI_DIST, _UI_GUI_DIR / "dist"):
        if (candidate / "index.html").exists():
            return candidate
    return None


def _write_serve_state(port: int, token: str) -> Optional[Path]:
    """Persist {port, token, pid} to ~/.raven/serve.json, 0600 from creation.

    A relauncher reads it to mint a fresh auth nonce against the running serve
    (POST /auth/nonce with the token) instead of starting a second engine
    instance.

    The mode is applied by ``os.open``, not by a chmod afterwards: writing the
    token first and narrowing the mode second leaves it world-readable for the
    span in between, which is exactly long enough for anything watching the
    directory.
    """
    import json
    import os

    try:
        state_dir = Path(os.environ.get("RAVEN_HOME", Path.home() / ".raven"))
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "serve.json"
        payload = json.dumps({"port": port, "token": token, "pid": os.getpid()})
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        # An existing file keeps its old mode through O_CREAT; narrow it too.
        os.chmod(path, 0o600)
        return path
    except OSError:
        return None


class _ServeControl:
    """What a running gateway exposes to handlers that must restart it.

    ``system.upgrade`` needs four things the RPC layer cannot know on its own:
    which port to come back on, which shared secret and which browser session
    cookie to keep (an open browser holds the cookie, and a relauncher holds the
    token -- carrying only one of them would sign somebody out), and a way to end
    the serve loop *after* its reply has been flushed.
    """

    def __init__(self) -> None:
        self.port: Optional[int] = None
        self.token: Optional[str] = None
        self.cookie: Optional[str] = None
        self._stop: Optional[asyncio.Event] = None

    def arm(self, port: int, token: str, cookie: str, stop: asyncio.Event) -> None:
        self.port, self.token, self.cookie, self._stop = port, token, cookie, stop

    def disarm(self) -> None:
        self.port = self.token = self.cookie = self._stop = None

    @property
    def running(self) -> bool:
        return self._stop is not None

    def request_shutdown(self) -> bool:
        if self._stop is None:
            return False
        self._stop.set()
        return True


SERVE = _ServeControl()


async def _serve_main(port: int, open_browser: bool) -> None:
    import os

    from aiohttp import web
    from loguru import logger

    from raven.tui_rpc.bootstrap import build_rpc_stack
    from raven.tui_rpc.transports.ws import WsGateway, build_app, pick_port

    gateway = WsGateway()
    # A relaunch after `system.upgrade` must reclaim the exact port the open
    # browser is pointed at, so it asks for strict mode rather than letting the
    # probe wander to the next free port.
    strict = os.environ.get("RAVEN_SERVE_PORT_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}
    bound_port = await pick_port(port, strict=strict)
    gateway.port = bound_port

    # MCP OAuth redirects land on this origin's /oauth/callback; declare it
    # before the stack builds so the first connect already registers the
    # right redirect_uri with the authorization server.
    from raven.agent.tools.mcp_oauth import set_callback_base

    set_callback_base(f"http://127.0.0.1:{bound_port}")

    stack = await build_rpc_stack(gateway.broadcast)
    gateway.dispatcher = stack.dispatcher

    app = build_app(gateway, resolve_gui_dist())
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", bound_port)
    await site.start()

    stop = asyncio.Event()
    SERVE.arm(bound_port, gateway.session_token, gateway.session_cookie, stop)

    # Refresh the cached latest release once per launch, in the background. Only
    # `raven tui` did this before, so someone who only ever runs the gateway
    # never learned a newer version existed (see cli/update_notice.py).
    try:
        from raven.cli.update_notice import maybe_refresh_async

        maybe_refresh_async()
    except Exception as exc:  # never let a version check keep the gateway down
        logger.debug("serve: update check skipped ({})", exc)

    base_url = f"http://127.0.0.1:{bound_port}"
    typer.echo(f"raven serve listening on {base_url} (rpc: {base_url}/rpc)")
    state_path = _write_serve_state(bound_port, gateway.session_token)
    if stack.build_error is not None:
        typer.echo(f"warning: agent loop failed to build ({stack.build_error.message}); chat turns will error")
    if open_browser:
        nonce = gateway.mint_nonce()
        auth_url = f"{base_url}/auth#{nonce}"
        typer.echo(f"opening browser: {base_url}")
        if not webbrowser.open(auth_url):
            typer.echo(f"could not open a browser automatically; visit: {auth_url}")

    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("serve: shutting down")
        SERVE.disarm()
        if state_path is not None:
            state_path.unlink(missing_ok=True)
        try:
            await stack.teardown()
        finally:
            await runner.cleanup()


def _run(port: int, open_browser: bool) -> None:
    try:
        asyncio.run(_serve_main(port, open_browser))
    except KeyboardInterrupt:
        typer.echo("raven serve stopped")


def register(app: typer.Typer) -> None:
    # Only `serve` for now. A `gui` command that opens a browser belongs with a
    # front end to open: until one ships in this repo, `--open` would land on
    # the "assets not built" placeholder, so the convenience wrapper waits.
    @app.command("serve")
    def serve(
        port: int = typer.Option(18792, "--port", help="Preferred port; probes forward if taken."),
        open_browser: bool = typer.Option(False, "--open", help="Open the served page in a browser."),
    ) -> None:
        """Run the headless Raven gateway (WebSocket RPC, plus a page when one is built)."""
        _run(port, open_browser)


__all__ = ["SERVE", "register", "resolve_gui_dist"]
