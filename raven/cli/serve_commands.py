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
dies. That child is `raven gateway` wherever it can be: it serves this same page
on its own loop (``_gateway_page.mount_page``) and, unlike standalone `serve`,
also runs the IM channel adapters -- so what the entrances page offers is
reachable from the page instead of being a switch nothing acts on. Where a
gateway already holds the instance lock (channels there, page separate --
``gateway.page.enabled = false``), the child is `serve` instead, since a second
gateway cannot start at all; the page then reaches those adapters over
``gateway.live_probe``. A page whose engine is gone is worse than one that never opened -- the tab
is still there, still looks live, and every send fails. `raven web --stop` is how
you end it, and `--foreground` is the old behaviour for someone debugging.

A page is served from ``<repo>/ui-web/dist`` or the wheel's packaged copy when one is
present; with none built, ``/`` answers with a short notice and the WebSocket
endpoint stays live. `web` refuses instead, because the page is the whole point
of it.

``ui-web/`` is one front end, not two: the desktop window is a browser view of the
same page a browser gets, so nothing here distinguishes them and nothing should.
"""

from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Optional

import typer

from raven.rpc.serve_control import SERVE
from raven.utils import asyncio_runner as bounded_asyncio

# The repository's source tree, and the copy inside an installed wheel. The two
# names differ on purpose: `ui-web/` is what this repository calls the page's
# sources, `raven/ui/dist` is the installed layout the build hook writes and
# `_install_guard` reads back out of the RECORD.
_UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui-web"
_PACKAGED_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"

SERVED_PAGE_SURFACE = "page"
"""What this host calls itself in a trace. See ``raven.tracing.set_surface``.

Not "web": that name belonged to the retired web channel, a different front end on its
own channel, and two different things under one label is worse than no label.
"""


def port_strict() -> bool:
    """Whether this launch must reclaim its exact port rather than probe forward.

    Set by the two callers that relaunch under an open browser tab -- the web
    supervisor's second attempt onwards, and ``system.upgrade`` -- because a tab
    is pointed at a port and coming back on another one strands it as surely as
    not coming back at all. Read here rather than at each site so the page mount
    inside the gateway and standalone `raven serve` cannot disagree about it.
    """
    import os

    return os.environ.get("RAVEN_SERVE_PORT_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}


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

    The mode is applied at the temp file's creation, not by a chmod afterwards:
    writing the token first and narrowing the mode second leaves it
    world-readable for the span in between, which is exactly long enough for
    anything watching the directory. ``atomic_replace(mode=0o600)`` holds that
    guarantee and additionally makes the swap tear-proof.
    """
    import json
    import os

    from raven.utils.atomic_io import atomic_replace

    try:
        path = _state_path()
        state: dict[str, object] = {"port": port, "token": token, "pid": os.getpid()}
        if cookie:
            state["cookie"] = cookie
        atomic_replace(path, json.dumps(state), mode=0o600)
        return path
    except OSError:
        return None


_UPDATE_FIRST_CHECK_S = 90.0
"""When the announcer first looks, covering the boot-time race: the page asks
``system.version`` before the launch refresh's daemon thread has finished, so a
release that landed since the last cache write would otherwise wait a day."""

_UPDATE_POLL_BETA_S = 60.0
"""How often a resident gateway re-checks on the beta channel.

A beta lands whenever a maintainer runs ``make beta``, and every tester it is
for is on another machine with no inbound path -- so this poll is the only thing
that carries a build to them, and its interval is how long they wait. It is
affordable at a minute because the check is a conditional request against a
23-byte pointer that answers 304 while nothing has changed (see
``beta_channel.read_pointer``), against a registry that is ours."""

_UPDATE_POLL_STABLE_S = 30 * 60.0
"""How often it re-checks on the stable channel. Deliberately left slow.

A stable release lands on a cadence of days, and its check reads GitHub's
release-page redirect rather than our own registry -- so a minute would spend
someone else's budget, from every install behind a shared egress, to learn
nothing 43,000 times a month. The cached path shows a notice one launch late and
a resident gateway has no next launch, which is what this interval is for; it
does not need to be the beta channel's."""

_UPDATE_BACKOFF_CEILING_S = 30 * 60.0
"""Where the failure backoff stops growing.

Consecutive failures double the delay from the channel's own cadence up to this,
so a gateway that is offline, or a registry that has started refusing, settles
at the stable cadence instead of retrying every minute for as long as it stays
up. One answer -- even a 304 -- puts it straight back."""

_UPDATE_BACKOFF_STEPS = 5
"""Doublings after which the delay is at the ceiling anyway (60s << 30min), so
the exponent stops there rather than growing all week in an offline process."""


def _update_poll_seconds() -> float:
    """The cadence for the channel this install is on, read per tick.

    Per tick and not once at startup: joining or leaving the beta channel is a
    file appearing or being deleted, and a resident gateway that outlives the
    change should follow it without a restart.
    """
    from raven.updates import beta_channel

    return _UPDATE_POLL_BETA_S if beta_channel.is_active() else _UPDATE_POLL_STABLE_S


def _update_delay(failures: int) -> float:
    """How long to wait before the next check, given consecutive failures.

    Each failure doubles the channel's cadence, to a ceiling of
    ``_UPDATE_BACKOFF_CEILING_S`` (30 minutes) -- so the beta channel's minute
    degrades 1, 2, 4, 8, 16, 30 and stays there, and the stable channel's half
    hour is already at the ceiling and never grows. ``failures=0`` is the plain
    cadence, which is what one answer restores.
    """
    return min(_update_poll_seconds() * 2**failures, _UPDATE_BACKOFF_CEILING_S)


async def _announce_updates(broadcast, stop: asyncio.Event) -> None:
    """Tell every open tab when a newer build lands, instead of waiting for a reload.

    The page learns about updates from ``system.version``, which it asks once at
    boot -- correct for a process that restarts, invisible for one that stays up
    for days. This pushes the same fact over the socket the page already holds;
    the client shows the same banner either way.

    Announces each version once. A tab that connects later still learns of it,
    because the check writes the cache ``system.version`` reads.

    On the beta channel this loop is the whole delivery mechanism -- a tester's
    gateway hears about a build no other way -- so it runs on that channel's
    minute cadence and backs off only when the registry stops answering.
    """
    from importlib import metadata

    from raven.updates.update_notice import Checked, check_now

    try:
        current = metadata.version("raven")
    except Exception:
        return

    announced: str | None = None
    failures = 0
    delay = _UPDATE_FIRST_CHECK_S
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass
        try:
            checked = await asyncio.to_thread(check_now, current)
        except Exception:
            checked = Checked(None, reached=False)
        failures = 0 if checked.reached else min(failures + 1, _UPDATE_BACKOFF_STEPS)
        delay = _update_delay(failures)
        latest = checked.latest
        if latest is None or latest == announced:
            continue
        announced = latest
        try:
            await broadcast({"method": "system.update_available", "params": {"latest_version": latest}})
        except Exception:
            announced = None


async def _serve_main(port: int, open_browser: bool) -> None:

    from aiohttp import web
    from loguru import logger

    from raven.cli._console_feature import register_console_feature
    from raven.rpc.bootstrap import build_rpc_stack
    from raven.rpc.transports.ws import WsGateway, build_app, pick_port

    register_console_feature()

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
    bound_port = await pick_port(port, strict=port_strict())
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

    app = build_app(
        gateway,
        resolve_ui_dist(),
        deliverables=stack.deliverables,
        agent_loop_factory=lambda: stack.agent_loop,
    )
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
        from raven.updates.update_notice import maybe_refresh_async

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


INCOMPLETE_INSTALL_EXIT = 75
"""Exit status for "this environment is not one to serve from" (EX_TEMPFAIL).

For an *external* supervisor -- the GUI shell, which respawns unconditionally
and is the thing that loses this race. `_supervise` below deliberately does not
read it: a permanently incomplete installation should exhaust its crash-loop
counter and stop, and the transient case never reaches the counter because
`_refuse_incomplete_install` sleeps through the window first."""

_UPGRADE_WAIT_S = 300.0
"""How long a start that landed inside an upgrade waits it out.

Not an immediate exit. ``_supervise`` gives up after ``_CRASH_LOOP_GIVE_UP``
failures faster than ``_HEALTHY_RUN_S``, and an install window is comfortably
short enough to burn through all five -- so a process that refuses instantly
would turn a ten-second upgrade into a supervisor that has stopped trying.
Sitting out the window in one process costs nothing and ends in one refusal.
Sized like ``PARENT_EXIT_TIMEOUT_S``: the same install is at the other end."""

_UPGRADE_POLL_S = 1.0


def _refuse_incomplete_install() -> None:
    """Stop before binding a port if this environment is not whole.

    ``build_app`` chooses the page route once, so a serve that starts while uv
    is still writing the environment does not merely start slowly -- it answers
    ``/`` with the placeholder until it is restarted, on an installation that
    finished correctly seconds later. The process cannot fix that by waiting and
    carrying on either: it has already imported half of the old build, and the
    rest would come off disk from the new one. Refusing is the only answer that
    stays correct, and it gives a supervisor something honest to retry.
    """
    import time

    from raven.updates.install_guard import inspect_install

    fault = inspect_install()
    if fault is None:
        return

    if fault.reason == "upgrading":
        typer.echo(f"raven serve: {fault.detail}; waiting for it to finish", err=True)
        deadline = time.monotonic() + _UPGRADE_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(_UPGRADE_POLL_S)
            fault = inspect_install()
            if fault is None or fault.reason != "upgrading":
                break

    if fault is None:
        # The upgrade landed while this process waited, and the installation is
        # sound -- but not for this process to run. It already holds half of the
        # build that was replaced, and every import still to come would load off
        # disk from the other one.
        typer.echo("raven serve: the upgrade finished; start Raven again to run it.", err=True)
    elif fault.reason == "upgrading":
        typer.echo(
            "raven serve: the upgrade has not finished; not starting on a half-written installation.",
            err=True,
        )
    else:
        typer.echo(f"raven serve: {fault.detail}; not starting.", err=True)
        typer.echo(
            "Repair it by rerunning the installer: curl -fsSL https://raven.evermind.ai/install.sh | sh",
            err=True,
        )
    raise typer.Exit(INCOMPLETE_INSTALL_EXIT)


def _run(port: int, open_browser: bool) -> None:
    _refuse_incomplete_install()
    try:
        bounded_asyncio.run(_serve_main(port, open_browser))
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

    from raven.gateway.lock import read_status

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
#     raven web  ->  detached `raven web --supervise`  ->  `raven gateway` (child)
#
# ...or `raven serve` as the child where a gateway already holds the lock; see
# `_gateway_argv`, which decides that per launch.
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
    from raven.config.loader import raven_home

    return raven_home() / "web.json"


def _web_log_path() -> Path:
    from raven.config.loader import raven_home

    return raven_home() / "web.log"


def _write_web_state(port: int) -> None:
    """Record this supervisor's pid, so ``--stop`` has something to stop.

    Separate from ``serve.json``: that one names the gateway, this one names what
    keeps bringing the gateway back. Stopping the gateway alone would just make
    the supervisor start another.
    """
    import json
    import os

    from raven.utils.atomic_io import atomic_replace

    try:
        atomic_replace(_web_state_path(), json.dumps({"pid": os.getpid(), "port": port}))
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


def _gateway_holds_the_lock() -> bool:
    """Whether a `raven gateway` is already running for this instance.

    Unreadable reads as yes, which is the safer way round: taken for no when the
    answer is yes, the page child tries for a lock it cannot have and never opens
    a page at all, while taken for yes when the answer is no the page opens on an
    engine without channels -- which is exactly where this surface was before,
    and something a person can put right with one `raven gateway`.
    """
    import time

    try:
        from raven.gateway.lock import read_status

        return read_status(time.time()) is not None
    except Exception:
        return True


def _gateway_argv(port: int) -> list[str]:
    """The command that serves the page, and what the supervisor re-runs.

    ``gateway`` rather than ``serve`` where it can be. Both serve the page -- the
    gateway hosts it on its own loop (see ``_gateway_page.mount_page``) -- but
    only the gateway runs the IM channel adapters, and standalone `raven serve`
    builds no ``ChannelManager`` at all. Left on ``serve``, everything the
    entrances page offers was unreachable from the page: no adapter to start, so
    no sign-in code could ever be minted, and every enabled entrance read "state
    unknown" forever.

    ``--page-port`` pins the page to the port the open tab is on and mounts it
    even where ``gateway.page.enabled`` was turned off, since a process started
    for the page's sake with no page is doing nothing anybody asked for.

    Where a gateway already holds the lock, ``serve`` is the only thing that CAN
    run: a second gateway exits on the lock, and a supervisor would spend its
    crash budget discovering that. ``gateway.page.enabled = false`` is a
    supported way to run -- channels in the gateway, page separate -- and in it
    the page reaches the adapters over ``gateway.live_probe``, which finds the
    incumbent through the same lock. So this is not a downgrade: it is the shape
    that configuration asked for.

    Re-read on every launch rather than decided once, so a child that lost a race
    for the lock comes back the other way instead of retrying into the same wall.

    Through ``-m raven`` rather than ``sys.argv[0]``: the console script may have
    been invoked by a name that is a shim, a symlink or a relative path that only
    resolved in the caller's working directory, and the supervisor outlives that
    directory. The interpreter running this process is the one thing that is
    certain to still be there.
    """
    import sys

    if _gateway_holds_the_lock():
        return [sys.executable, "-m", "raven", "serve", "--port", str(port)]
    return [sys.executable, "-m", "raven", "gateway", "--page-port", str(port)]


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

    delay = 1.0
    fast_failures = 0
    target = port
    try:
        # Recorded inside the guard rather than before it. The handler above is
        # armed the moment it is installed, so a SIGTERM arriving between the
        # write and this `try` raised KeyboardInterrupt where no `finally` could
        # see it -- leaving behind the very stale `web.json` this function
        # exists to prevent. `raven web` followed straight away by `--stop` is
        # exactly that timing.
        _write_web_state(port)
        print(f"raven web: supervising the gateway on port {port} (pid {os.getpid()})", flush=True)
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
                    f"(last exit {code}); giving up. Run `raven gateway` to see why.",
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


def _await_attach(
    timeout_s: float = _ATTACH_PATIENCE_S,
    *,
    gone: Optional[Callable[[], bool]] = None,
) -> Optional[str]:
    """Poll until the freshly started gateway answers, then mint an auth URL.

    Polls the recorded state each time rather than reading it once: the port is
    not known until the gateway has bound one and written it down, and on a
    contended port that is not the port that was asked for.

    The wait is abandoned as soon as whatever was supposed to bring the page up
    is known to be gone: nothing is coming, and a process that already exited
    will not start a gateway by being waited on. Two callers, two ways to know:

    - supervised: ``web.json``, and only once the supervisor has been SEEN alive.
      It records its own pid after it starts, so reading its absence too early
      would abandon the wait on a supervisor that simply has not written itself
      down yet.
    - foreground (``gone``): the child's own exit. There is no supervisor state
      to watch there, so without this the wait sat out its full ceiling on a
      child that had already died -- two minutes and a half of a terminal that
      had printed the error and then said nothing.
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
        if gone is not None:
            if gone():
                return None
        elif _read_web_state() is not None:
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
        typer.echo("No page is built. Run `python ui-web/build.py`, or install raven from a release wheel.")
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
        #
        # Through the same child the supervisor runs, not an in-process serve.
        # Building the standalone stack here left --foreground on the one engine
        # that has no ChannelManager, so the page it opened was the inert
        # entrances page this change exists to fix -- with the fix applying only
        # to the detached path, which is not the one anybody debugging uses.
        _run_foreground(port)
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


def _run_foreground(port: int) -> None:
    """Hold the terminal while the page's own child engine runs.

    Foreground semantics are the child's, and they survive being a child: its
    output is this terminal's (stdio is inherited, not piped), and Ctrl-C reaches
    it because it shares this process group. What this function adds is the
    browser, opened once the child has bound a port and written it down -- the
    same wait `raven web` does for a supervised one.
    """
    import subprocess

    _refuse_incomplete_install()
    try:
        proc = subprocess.Popen(_gateway_argv(port))  # noqa: S603 - see _gateway_argv
    except OSError as exc:
        typer.echo(f"could not start the engine: {exc}")
        raise typer.Exit(1) from None
    try:
        # Watches the child, because there is no supervisor state to watch: a
        # child that dies before writing serve.json -- a lost lock race, a config
        # it cannot load, an import that fails -- otherwise left this wait to sit
        # out its whole ceiling with the error already on screen.
        url = _await_attach(gone=lambda: proc.poll() is not None)
        if url is None and proc.poll() is None:
            typer.echo("the engine did not come up")
        elif url is not None:
            _open(url)
        proc.wait()
    except KeyboardInterrupt:
        # The signal already reached the child; waiting is how this process
        # outlives it long enough to not orphan it.
        with suppress(KeyboardInterrupt):
            proc.wait()
        typer.echo("raven web stopped")
    if proc.returncode:
        raise typer.Exit(proc.returncode)


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
