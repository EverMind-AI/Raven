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

A page in a browser tab outlives the terminal that opened it, so the engine
behind it has to as well: `web` leaves a resident gateway (detached, its own
session, logging to ``web.log``) under a supervisor that brings it back if it
dies. A page whose engine is gone is worse than one that never opened -- the tab
is still there, still looks live, and every send fails. `raven web --stop` is how
you end it, and `--foreground` is the old behaviour for someone debugging.

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
from contextlib import suppress
from pathlib import Path
from typing import Optional

import typer

_UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
_PACKAGED_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"

SERVED_PAGE_SURFACE = "page"
"""What this host calls itself in a trace. See ``raven.tracing.set_surface``.

Not "web": that name belongs to ``raven/web_rpc``, a different front end on its
own channel, and two different things under one label is worse than no label.
"""


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


def _stored_cookie() -> Optional[str]:
    """The session cookie the last gateway used, if one was recorded.

    Read back so a restart does not sign out every open tab. The supervisor
    exists to restart the gateway, and a fresh cookie on each start meant that
    doing its job logged the user out -- residency the user could not see,
    because their tab showed "not authenticated" either way.
    """
    import json

    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cookie = data.get("cookie") if isinstance(data, dict) else None
    return cookie if isinstance(cookie, str) and cookie else None


def adopt_stored_cookie(gateway: object) -> None:
    """Carry the previous gateway's cookie into this one.

    Env wins: ``RAVEN_SERVE_COOKIE`` is how ``system.upgrade`` hands the exact
    session to its replacement, and how tests pin one.

    Durability is the point and also the cost. The cookie used to die with the
    process, which made it revocable by accident rather than by policy; it now
    outlives a restart, so a value that leaks -- cookies ignore port, so any
    other 127.0.0.1 listener the browser talks to receives it -- stays usable
    until the file is deleted. The shared token in the same file is strictly
    more powerful (it mints nonces), so this does not widen what losing
    serve.json costs; it only lengthens the life of the weaker credential.
    """
    import os

    if os.environ.get("RAVEN_SERVE_COOKIE"):
        return
    stored = _stored_cookie()
    if stored:
        gateway.session_cookie = stored  # type: ignore[attr-defined]


def _write_serve_state(port: int, token: str, cookie: str = "") -> Optional[Path]:
    """Persist {port, token, cookie, pid} to ~/.raven/serve.json, 0600 from creation.

    ``raven web`` reads it to mint a fresh auth nonce against the running serve
    (POST /auth/nonce with the token) instead of starting a second engine
    instance. ``cookie`` is what keeps an open tab signed in across a restart;
    see :func:`adopt_stored_cookie`.

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
        state: dict[str, object] = {"port": port, "token": token, "pid": os.getpid()}
        if cookie:
            state["cookie"] = cookie
        payload = json.dumps(state)
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
        self.hosted_by_gateway = False

    def arm(self, port: int, token: str, cookie: str, stop: asyncio.Event) -> None:
        self.port, self.token, self.cookie, self._stop = port, token, cookie, stop

    def arm_hosted(self, port: int, token: str, cookie: str) -> None:
        """The gateway hosts the page: record the endpoint facts, no stop event.

        ``system.upgrade``'s restart flow replaces a `raven serve` process with
        another `raven serve`; ending the gateway's loop that way would drop the
        IM channels and bring back the wrong process. So a hosted page carries
        no shutdown handle, and upgrade refuses with its own reason instead
        (see ``raven.rpc.methods.system.system_upgrade``).
        """
        self.port, self.token, self.cookie = port, token, cookie
        self.hosted_by_gateway = True

    def disarm(self) -> None:
        self.port = self.token = self.cookie = self._stop = None
        self.hosted_by_gateway = False

    @property
    def running(self) -> bool:
        return self._stop is not None

    def request_shutdown(self) -> bool:
        if self._stop is None:
            return False
        self._stop.set()
        return True


SERVE = _ServeControl()


_UPDATE_FIRST_CHECK_S = 90.0
"""When the announcer first looks, covering the boot-time race: the page asks
``system.version`` before the launch refresh's daemon thread has finished, so a
release that landed since the last cache write would otherwise wait a day."""

_UPDATE_POLL_S = 30 * 60.0
"""How often a resident gateway re-checks after that. The cached path shows a
notice one launch late, and a resident gateway has no next launch."""


async def _announce_updates(broadcast, stop: asyncio.Event) -> None:
    """Tell every open tab when a newer build lands, instead of waiting for a reload.

    The page learns about updates from ``system.version``, which it asks once at
    boot -- correct for a process that restarts, invisible for one that stays up
    for days. This pushes the same fact over the socket the page already holds;
    the client shows the same banner either way.

    Announces each version once. A tab that connects later still learns of it,
    because the check writes the cache ``system.version`` reads.
    """
    from importlib import metadata

    from raven.cli.update_notice import check_for_update

    try:
        current = metadata.version("raven")
    except Exception:
        return

    announced: str | None = None
    delay = _UPDATE_FIRST_CHECK_S
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass
        delay = _UPDATE_POLL_S
        try:
            latest = await asyncio.to_thread(check_for_update, current)
        except Exception:
            continue
        if latest is None or latest == announced:
            continue
        announced = latest
        try:
            await broadcast({"method": "system.update_available", "params": {"latest_version": latest}})
        except Exception:
            announced = None


async def _serve_main(port: int, open_browser: bool) -> None:
    import os

    from aiohttp import web
    from loguru import logger

    from raven.rpc.bootstrap import build_rpc_stack
    from raven.rpc.transports.ws import WsGateway, build_app, pick_port

    # Declared before anything can emit a span. The page runs on the terminal's
    # channel by design (one session pool), so `channel.id` cannot tell the two
    # apart in a trace and this is what does.
    from raven.tracing import set_surface

    set_surface(SERVED_PAGE_SURFACE)

    gateway = WsGateway()
    # Before anything can hand the value out: a tab opened against the previous
    # gateway holds this cookie, and re-minting it here is what made every
    # restart -- including the supervisor's own -- log the user out.
    adopt_stored_cookie(gateway)
    # A relaunch after `system.upgrade` must reclaim the exact port the open
    # browser is pointed at, so it asks for strict mode rather than letting the
    # probe wander to the next free port.
    strict = os.environ.get("RAVEN_SERVE_PORT_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}
    bound_port = await pick_port(port, strict=strict)
    gateway.port = bound_port

    # The OAuth redirect deliberately does NOT follow this port. It is baked
    # into the registration an authorization server keeps, and this port moves:
    # `pick_port` probes forward when the preferred one is taken, and a moved
    # redirect invalidates the registration, which re-authorizes a plugin the
    # user already authorized. mcp_oauth owns a fixed loopback port instead;
    # the /oauth/callback route below stays for registrations made under the
    # old scheme, which still point at a gateway port.

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
    state_path = _write_serve_state(bound_port, gateway.session_token, gateway.session_cookie)
    if stack.build_error is not None:
        typer.echo(f"warning: agent loop failed to build ({stack.build_error.message}); chat turns will error")
    if open_browser:
        nonce = gateway.mint_nonce()
        auth_url = f"{base_url}/auth#{nonce}"
        typer.echo(f"opening browser: {base_url}")
        if not webbrowser.open(auth_url):
            typer.echo(f"could not open a browser automatically; visit: {auth_url}")

    announcer = asyncio.create_task(_announce_updates(gateway.broadcast, stop))

    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("serve: shutting down")
        announcer.cancel()
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


def _gateway_hosted_page() -> Optional[tuple[int, str]]:
    """The (port, token) of a page a live `raven gateway` hosts, or None.

    Three facts have to line up: the gateway lock says a gateway is running,
    serve.json says a page is served, and the two pids match. The pid match is
    what keeps this narrow -- a stale serve.json left by a killed standalone
    serve does not turn a page-less gateway into "already hosting", and a
    standalone serve running beside a gateway keeps its own file and its own
    life (the gateway checks the same three facts the other way before
    mounting, and yields -- see ``_gateway_page._standalone_serve_owner``).
    Either of those cases reads as None, and starting is then correct.
    """
    import json
    import time

    from raven.cli._gateway_lock import read_status

    status = read_status(now=time.time())
    if status is None or status.pid <= 0:
        return None
    try:
        pid = int(json.loads(_state_path().read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid != status.pid:
        return None
    return _read_serve_state()


# ---------------------------------------------------------------------------
# The resident gateway.
#
# A browser tab is not a foreground process. It survives the terminal that
# opened it, a laptop lid, and a reboot's worth of "restore my tabs" -- so the
# engine behind it has to survive at least the first two, and be restartable
# rather than merely absent for the third. `web` therefore leaves a supervisor
# behind instead of holding the terminal itself:
#
#     raven web  ->  detached `raven web --supervise`  ->  `raven serve` (child)
#
# The supervisor exists for one reason a plain detached gateway cannot cover: a
# gateway that dies takes the page with it while the page still looks live, and
# every send fails from then on. Two processes is the price of the page staying
# true.
# ---------------------------------------------------------------------------

_RESTART_CEILING_S = 30.0
"""Longest wait between restarts. Backoff climbs to here and stays."""

_HEALTHY_RUN_S = 20.0
"""A run this long counts as having worked, which resets the backoff.

Below it the gateway is failing at startup -- a port it cannot bind, a config it
cannot load -- and retrying at speed would only fill the log."""

_CRASH_LOOP_GIVE_UP = 5
"""Consecutive failures faster than ``_HEALTHY_RUN_S`` before the supervisor
stops trying. Something is broken that restarting cannot fix, and a process
respawning forever is worse than one that stopped and said why."""


def _web_state_path() -> Path:
    import os

    return Path(os.environ.get("RAVEN_HOME", Path.home() / ".raven")) / "web.json"


def _web_log_path() -> Path:
    import os

    return Path(os.environ.get("RAVEN_HOME", Path.home() / ".raven")) / "web.log"


def _write_web_state(port: int) -> None:
    """Record this supervisor's pid, so ``--stop`` has something to stop.

    Separate from ``serve.json``: that one names the gateway, this one names what
    keeps bringing the gateway back. Stopping the gateway alone would just make
    the supervisor start another.
    """
    import json
    import os

    try:
        path = _web_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")
    except OSError as exc:
        typer.echo(f"warning: could not record the supervisor at {_web_state_path()}: {exc}")


def _read_web_state() -> Optional[int]:
    """The pid of the running supervisor, or None if there is not one.

    A recorded pid that is not alive is the same answer as no file at all: the
    file is removed on a clean exit only, so a killed supervisor leaves one
    behind and a later ``web`` must read that as "nothing is supervising".
    """
    import json

    try:
        pid = int(json.loads(_web_state_path().read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return pid if pid > 0 and _pid_alive(pid) else None


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, and owned by somebody else. Not ours to signal, but reporting it
        # as gone would start a second supervisor beside it.
        return True
    except OSError:
        return False
    return True


def _gateway_argv(port: int) -> list[str]:
    """The command the supervisor runs and re-runs.

    Through ``-m raven`` rather than ``sys.argv[0]``: the console script may have
    been invoked by a name that is a shim, a symlink or a relative path that only
    resolved in the caller's working directory, and the supervisor outlives that
    directory. The interpreter running this process is the one thing that is
    certain to still be there.
    """
    import sys

    return [sys.executable, "-m", "raven", "serve", "--port", str(port)]


def _spawn_supervisor(port: int) -> None:
    """Start the detached supervisor, with its output going to ``web.log``.

    ``start_new_session`` is what makes it resident: without its own session the
    supervisor stays in the terminal's process group and takes the same SIGHUP
    the shell does when the window closes -- taking the page's engine with it.
    """
    import subprocess
    import sys

    log = _web_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "a", encoding="utf-8")  # noqa: SIM115 - handed to the child, closed with it
    try:
        subprocess.Popen(  # noqa: S603 - argv is this interpreter plus literals
            [sys.executable, "-m", "raven", "web", "--supervise", "--port", str(port)],
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
            start_new_session=(sys.platform != "win32"),
        )
    finally:
        handle.close()


def _bound_port_of(proc: object, timeout_s: float = 15.0) -> Optional[int]:
    """The port this gateway process bound, read out of the file it writes.

    Matched on pid, not merely on the file existing: a gateway that was killed
    leaves its ``serve.json`` behind, and adopting that stale port would send
    every restart at a port this run never had.
    """
    import json
    import time

    deadline = time.monotonic() + timeout_s
    pid = getattr(proc, "pid", None)
    while time.monotonic() < deadline:
        if getattr(proc, "poll", lambda: 0)() is not None:
            return None
        try:
            data = json.loads(_state_path().read_text(encoding="utf-8"))
            if int(data["pid"]) == pid:
                return int(data["port"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.25)
    return None


def _supervise(port: int) -> None:
    """Run the gateway, and keep running it, until it exits cleanly or is stopped.

    A zero exit is a decision, not a fault: ``system.upgrade`` shuts the gateway
    down on purpose after spawning its replacement, and ``--stop`` and a plain
    Ctrl-C do the same. Restarting on those would fight the thing that asked to
    stop, so only a non-zero exit is treated as a death worth undoing.
    """
    import os
    import signal
    import subprocess
    import sys
    import time

    # Python installs no SIGTERM handler, so the default disposition kills the
    # process where it stands and no `finally` unwinds -- which is how
    # `raven web --stop` left `web.json` behind pointing at a dead pid. Raising
    # KeyboardInterrupt turns the signal into the exit path the loop already
    # handles, so the state file is removed by the same code Ctrl-C uses.
    def _on_term(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    with suppress(ValueError):  # not the main thread: nothing to install
        signal.signal(signal.SIGTERM, _on_term)

    _write_web_state(port)
    print(f"raven web: supervising the gateway on port {port} (pid {os.getpid()})", flush=True)
    delay = 1.0
    fast_failures = 0
    target = port
    try:
        while True:
            started = time.monotonic()
            try:
                proc = subprocess.Popen(_gateway_argv(target))  # noqa: S603 - see _gateway_argv
            except OSError as exc:
                print(f"raven web: could not start the gateway: {exc}", flush=True)
                return
            # The port it asked for is not always the port it got: the first
            # launch probes forward past whatever else holds 18792. Restarting on
            # the preferred port would then bind a different one from the open
            # tab's, so the port it actually bound is what the restart aims at.
            bound = _bound_port_of(proc)
            if bound is not None and bound != target:
                print(f"raven web: the gateway bound port {bound}", flush=True)
                target = bound
            try:
                code = proc.wait()
            except KeyboardInterrupt:
                return
            ran = time.monotonic() - started
            if code == 0:
                print("raven web: the gateway exited cleanly; not restarting it", flush=True)
                return
            if ran >= _HEALTHY_RUN_S:
                delay, fast_failures = 1.0, 0
            else:
                fast_failures += 1
                delay = min(delay * 2, _RESTART_CEILING_S)
            if fast_failures >= _CRASH_LOOP_GIVE_UP:
                print(
                    f"raven web: the gateway failed {fast_failures} times without staying up "
                    f"(last exit {code}); giving up. Run `raven serve` to see why.",
                    flush=True,
                )
                return
            # Strict from the second attempt on: the browser is pointed at a
            # port, and coming back on a different one strands the open tab just
            # as surely as not coming back at all.
            os.environ["RAVEN_SERVE_PORT_STRICT"] = "1"
            print(f"raven web: the gateway exited {code} after {ran:.1f}s; restarting in {delay:.0f}s", flush=True)
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                return
    finally:
        _web_state_path().unlink(missing_ok=True)
        sys.stdout.flush()


def _stop_resident() -> bool:
    """Stop the supervisor and the gateway it keeps up. True if anything was up.

    The supervisor goes first, and by SIGTERM rather than SIGKILL, so its
    ``finally`` removes ``web.json``. Killing the gateway first would only prove
    the supervisor works.
    """
    import os
    import signal
    import time

    stopped = False
    supervisor = _read_web_state()
    if supervisor is not None:
        try:
            os.kill(supervisor, signal.SIGTERM)
            stopped = True
        except OSError as exc:
            typer.echo(f"warning: could not stop the supervisor (pid {supervisor}): {exc}")

    gateway = _read_serve_pid()
    if gateway is not None and gateway != supervisor:
        # Give the supervisor a moment to notice, so the gateway is not restarted
        # between these two signals.
        if stopped:
            time.sleep(0.4)
        try:
            os.kill(gateway, signal.SIGTERM)
            stopped = True
        except ProcessLookupError:
            pass
        except OSError as exc:
            typer.echo(f"warning: could not stop the gateway (pid {gateway}): {exc}")
    return stopped


def _read_serve_pid() -> Optional[int]:
    """The pid ``_write_serve_state`` recorded, if that process is still alive."""
    import json

    try:
        pid = int(json.loads(_state_path().read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return pid if pid > 0 and _pid_alive(pid) else None


_ATTACH_PATIENCE_S = 150.0
"""Ceiling on the wait for a gateway that is starting for the first time.

A fresh install has to byte-compile its whole dependency set before the
gateway can bind, which is minutes of work on a cold cache -- measured at 28s
here, against a window that used to be 25s. Giving up three seconds early told
the first-run reader "the gateway did not come up" about a gateway that was
seconds from listening, and left them with a supervisor they did not know was
running. The ceiling is generous because the wait already ends the moment the
gateway answers, and because the alternative failure is far more expensive to
diagnose than a longer wait is to sit through.
"""

_ATTACH_SLOW_NOTICE_S = 8.0
"""When to admit out loud that this is taking a while."""


def _await_attach(timeout_s: float = _ATTACH_PATIENCE_S) -> Optional[str]:
    """Poll until the freshly started gateway answers, then mint an auth URL.

    Polls the recorded state each time rather than reading it once: the port is
    not known until the gateway has bound one and written it down, and on a
    contended port that is not the port that was asked for.

    The wait is abandoned early if the supervisor dies: nothing is coming, and
    a process that already exited will not start a gateway by being waited on.
    That check only arms once the supervisor has been seen alive -- it records
    its own pid after it starts, so reading its absence too early would abandon
    the wait on a supervisor that simply has not written itself down yet.
    """
    import time

    started = time.monotonic()
    deadline = started + timeout_s
    said_slow = False
    seen_supervisor = False
    while time.monotonic() < deadline:
        recorded = _read_serve_state()
        if recorded is not None:
            try:
                url = asyncio.run(_attach(*recorded))
            except PermissionError:
                raise
            if url is not None:
                return url
        if _read_web_state() is not None:
            seen_supervisor = True
        elif seen_supervisor:
            return None
        if not said_slow and time.monotonic() - started > _ATTACH_SLOW_NOTICE_S:
            typer.echo("still starting (a first run compiles its dependencies; this can take a minute)...")
            said_slow = True
        time.sleep(0.3)
    return None


def _web(port: int, *, foreground: bool = False, stop: bool = False, supervise: bool = False) -> None:
    """``raven web`` -- open the page against a gateway that stays up."""
    if stop:
        if _stop_resident():
            typer.echo("raven web stopped")
        else:
            typer.echo("nothing to stop; no resident gateway is recorded as running")
        return

    if resolve_ui_dist() is None:
        typer.echo("No page is built. Run `python ui/build.py`, or install raven from a release wheel.")
        raise typer.Exit(1)

    if supervise:
        _supervise(port)
        return

    try:
        url = _attached_url()
    except PermissionError as exc:
        typer.echo(f"error: {exc}; stop it and run `raven web` again")
        raise typer.Exit(1) from None

    if url is not None:
        typer.echo(f"attaching to the gateway already running: {url.split('/auth#')[0]}")
        _open(url)
        return

    if foreground:
        # No supervisor and no detachment: the point of --foreground is to see
        # the gateway's own output and to have Ctrl-C mean what it says.
        _run(port, open_browser=True)
        return

    # A supervisor with no gateway answering means the gateway is between
    # restarts, so waiting is the right move -- starting a second supervisor
    # would leave two racing to own the same port.
    if _read_web_state() is None:
        _spawn_supervisor(port)
    try:
        url = _await_attach()
    except PermissionError as exc:
        typer.echo(f"error: {exc}; run `raven web --stop`, then `raven web`")
        raise typer.Exit(1) from None
    if url is None:
        typer.echo(f"the gateway did not come up; see {_web_log_path()}")
        raise typer.Exit(1)
    typer.echo(f"raven is running at {url.split('/auth#')[0]} (stop it with `raven web --stop`)")
    _open(url)


def _open(url: str) -> None:
    if not webbrowser.open(url):
        typer.echo(f"could not open a browser automatically; visit: {url}")


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
        # A `raven gateway` with the page mounted is already this engine, and
        # a second engine on the same agent home would race it over the same
        # sessions and the same store. Attach instead of starting; anything
        # short of a live, ours, pid-matched page falls through to a normal
        # standalone start.
        hosted = _gateway_hosted_page()
        if hosted is not None:
            try:
                url = asyncio.run(_attach(*hosted))
            except PermissionError as exc:
                typer.echo(f"error: {exc}; stop the gateway or remove {_state_path()}, then retry")
                raise typer.Exit(1) from None
            if url is not None:
                base = url.split("/auth#")[0]
                typer.echo(f"the running raven gateway already hosts the page at {base}; not starting a second engine")
                if open_browser:
                    _open(url)
                return
        _run(port, open_browser)

    @app.command("web")
    def web(
        port: int = typer.Option(
            18792,
            "--port",
            help="Port to start on when nothing is running. A gateway already up keeps its own port.",
        ),
        stop: bool = typer.Option(False, "--stop", help="Stop the resident gateway and its supervisor."),
        foreground: bool = typer.Option(
            False,
            "--foreground",
            help="Hold the terminal instead of leaving a resident gateway (no restart on failure).",
        ),
        supervise: bool = typer.Option(
            False,
            "--supervise",
            hidden=True,
            help="Internal: be the supervisor. `raven web` starts this for you, detached.",
        ),
    ) -> None:
        """Open Raven in a browser, on a gateway that stays up after you close the terminal."""
        _web(port, foreground=foreground, stop=stop, supervise=supervise)


__all__ = ["SERVE", "register", "resolve_ui_dist"]
