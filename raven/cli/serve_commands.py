"""Typer subcommands: `raven serve` and `raven web` — the gateway, and the page.

`serve` runs the shared RPC stack (the same engine assembly `raven tui` builds,
see ``raven/rpc/bootstrap.py``) behind an aiohttp WebSocket at ``/rpc``, so a
browser-based front end reaches the same runtime the terminal does. It holds the
terminal, which is what a service manager or a second machine's client wants.

`web` is what a person types. It opens the page in a browser, and it attaches to
the gateway already running rather than starting a second one -- two engines on
one agent home would race over the same sessions and the same store. The file
``serve`` writes for exactly this (``serve.json``: port and token, 0600) is how
the two find each other, and the nonce that authenticates the browser is minted
by the running process, so the token never reaches the page.

A page is served from ``<repo>/ui/dist`` or the wheel's packaged copy when one is
present; with none built, ``/`` answers with a short notice and the WebSocket
endpoint stays live. `web` refuses instead, because the page is the whole point
of it.

``ui/`` is one front end, not two: the desktop window is a browser view of the
same page a browser gets, so nothing here distinguishes them and nothing should.
"""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Optional

import typer

_UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
_PACKAGED_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


def resolve_ui_dist() -> Optional[Path]:
    """Locate the built ui page: wheel-packaged copy first, then source tree."""
    for candidate in (_PACKAGED_UI_DIST, _UI_DIR / "dist"):
        if (candidate / "index.html").exists():
            return candidate
    return None


def _state_path() -> Path:
    """Where the running gateway leaves its port and token.

    Through the one resolver rather than reading the variable again: this file
    is how ``raven web`` finds the engine, so the two disagreeing is the exact
    failure it exists to prevent -- a state file written cwd-relative while the
    agent home is elsewhere, and a second engine started on top of the first.
    """
    from raven.config.loader import raven_home

    return raven_home() / "serve.json"


def _write_serve_state(port: int, token: str) -> Optional[Path]:
    """Persist {port, token, pid} to ~/.raven/serve.json, 0600 from creation.

    ``raven web`` reads it to mint a fresh auth nonce against the running serve
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
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
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

    from raven.rpc.bootstrap import build_rpc_stack
    from raven.rpc.transports.ws import WsGateway, build_app, pick_port

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

    app = build_app(gateway, resolve_ui_dist())
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


def _read_serve_state() -> Optional[tuple[int, str]]:
    """The port and token of the gateway recorded as running, if any.

    The counterpart of ``_write_serve_state``, which has been writing this file
    for a relauncher to read since before there was one. Absent, unreadable or
    malformed all mean the same thing here -- nothing to attach to -- because the
    caller's next move is the same either way.
    """
    import json

    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        port, token = int(data["port"]), str(data["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (port, token) if port > 0 and token else None


async def _attach(port: int, token: str) -> Optional[str]:
    """A one-time auth URL against the gateway on ``port``, or None if it is gone.

    Health first, and the answer has to name this service: a stale file can point
    at a port something else has since taken, and handing that stranger our token
    is worse than starting a second gateway. The nonce is then minted by the
    running process, so the browser lands authenticated without the token ever
    reaching the page.
    """
    import aiohttp

    base = f"http://127.0.0.1:{port}"
    timeout = aiohttp.ClientTimeout(total=2)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base}/health") as health:
                if health.status != 200 or (await health.json()).get("service") != "raven-serve":
                    return None
            async with session.post(f"{base}/auth/nonce", headers={"X-Raven-Token": token}) as minted:
                if minted.status != 200:
                    # A live gateway of ours that will not mint: the recorded
                    # token is not its token. Starting a second engine would
                    # leave two, so this is reported rather than worked around.
                    raise PermissionError(f"the gateway on port {port} refused the recorded token")
                return f"{base}/auth#{(await minted.json())['nonce']}"
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
        return None


def _attached_url() -> Optional[str]:
    """Auth URL for the gateway already running, or None if there is not one.

    Owns the loop for the probe so ``_web`` stays synchronous: starting a gateway
    runs a loop of its own, and the two must not nest.
    """
    recorded = _read_serve_state()
    if recorded is None:
        return None
    return asyncio.run(_attach(*recorded))


def _web(port: int) -> None:
    """``raven web`` -- open the page, starting a gateway only if none is up."""
    if resolve_ui_dist() is None:
        typer.echo("No page is built. Run `python ui/build.py`, or install raven from a release wheel.")
        raise typer.Exit(1)

    try:
        url = _attached_url()
    except PermissionError as exc:
        typer.echo(f"error: {exc}; stop it and run `raven web` again")
        raise typer.Exit(1) from None

    if url is not None:
        typer.echo(f"attaching to the gateway already running: {url.split('/auth#')[0]}")
        if not webbrowser.open(url):
            typer.echo(f"could not open a browser automatically; visit: {url}")
        return

    _run(port, open_browser=True)


def register(app: typer.Typer) -> None:
    # Two verbs, and the second is not another way to be running -- it is how you
    # avoid being run twice. `serve` is the gateway: it starts an engine and holds
    # the terminal, which is what a launchd job or a second machine's client
    # wants. `web` is the page: it attaches to the engine already running when
    # there is one, and only starts an engine when there is not.
    @app.command("serve")
    def serve(
        port: int = typer.Option(18792, "--port", help="Preferred port; probes forward if taken."),
        open_browser: bool = typer.Option(False, "--open", help="Open the served page in a browser."),
    ) -> None:
        """Run the headless Raven gateway (WebSocket RPC, plus a page when one is built)."""
        _run(port, open_browser)

    @app.command("web")
    def web(
        port: int = typer.Option(
            18792,
            "--port",
            help="Port to start on when nothing is running. A gateway already up keeps its own port.",
        ),
    ) -> None:
        """Open Raven in a browser, reusing the running gateway or starting one."""
        _web(port)


__all__ = ["SERVE", "register", "resolve_ui_dist"]
