"""`system.*` RPC handlers — handshake, ping, version.

These handlers are invoked by the dispatcher with a plain `params: dict` and
must return a plain `result: dict`. Validation uses Pydantic v2 models from
`raven/rpc/models.py` when available; otherwise we inline a lightweight
semver guard so the dispatcher can be tested standalone.
"""

from __future__ import annotations

import importlib.metadata as _md
import os
import re
import sys
import time
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.rpc import LOCAL_CHANNEL
from raven.rpc.errors import ConfigValidationError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


# ----------------------------------------------------------------------------
# Versioning
# ----------------------------------------------------------------------------
# server_version: the IPC bridge protocol implementation version. Bumped when
#   we ship a new wire-compatible release.
# schema_version: matches OpenRPC `info.version` in `rpc-schema/openrpc.json`.
# raven_version: the raven package version (from installed metadata).
SERVER_VERSION = "0.1.0"
SCHEMA_VERSION = "0.1.0"
SERVER_CAPABILITIES = ["jsonrpc-2.0", "subscriptions", "cli-dispatch"]

# Lenient semver: <major>.<minor>.<patch> with optional `-prerelease` / `+build`.
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def _raven_version() -> str:
    try:
        return _md.version("raven")
    except _md.PackageNotFoundError:
        # Editable install in CI may not register metadata; fall back to a
        # well-known sentinel rather than crashing the handshake.
        return "0.0.0+unknown"


# ----------------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------------


async def system_hello(params: dict, *, channel: str = LOCAL_CHANNEL) -> dict:
    """`system.hello` — initial handshake. Validates client_version semver.

    Spec: §3.7 `system.hello` — errors -32011 if client_version invalid.

    ``channel`` is the routing tag this dispatcher's turns run on, and the prefix
    of the default session key handed back here. It must match the
    ``default_channel`` given to ``register_turn_methods`` on the same
    dispatcher: a client is told one thing at handshake and its turns run on the
    other, and nothing else in the process notices.

    ``surface`` (optional) names the front end behind this connection -- the
    vocabulary the trace layer already uses (``"tui"``, ``"page"``, ``"shell"``).
    It is recorded on the connection's own state, never process-wide: one
    gateway serves the page, the shell, and relayed terminals at once, and each
    connection's turns must carry its own name. Declaring changes no session
    key -- the page and the terminal keep sharing the ``tui`` pool.
    """
    client_version = params.get("client_version")
    if not isinstance(client_version, str) or not client_version:
        raise ConfigValidationError(
            "client_version is required",
            data={"field": "client_version", "reason": "missing"},
        )
    if not _SEMVER_RE.match(client_version):
        raise ConfigValidationError(
            f"client_version '{client_version}' is not a valid semver",
            data={"field": "client_version", "value": client_version},
        )

    surface = params.get("surface")
    if surface is not None:
        from raven.rpc.connection import SURFACE_RE, declare_surface

        if not isinstance(surface, str) or not SURFACE_RE.match(surface):
            raise ConfigValidationError(
                "surface must be a short lowercase token (e.g. 'tui', 'page', 'shell')",
                data={"field": "surface", "value": surface},
            )
        declare_surface(surface)

    client_capabilities = params.get("client_capabilities", []) or []
    # pid distinguishes concurrent `raven tui` processes sharing one log file.
    logger.info(
        "rpc: handshake — pid={} client_version={} client_capabilities={} surface={}",
        os.getpid(),
        client_version,
        client_capabilities,
        surface,
    )
    return {
        "server_version": SERVER_VERSION,
        "server_capabilities": list(SERVER_CAPABILITIES),
        "session": {
            "default_channel": channel,
            "default_session_key": f"{channel}:default",
        },
        # The host's OS family, not the client's: reveal-in-file-manager runs
        # where the gateway runs, so the wording has to match that machine.
        "platform": "mac" if sys.platform == "darwin" else "windows" if sys.platform.startswith("win") else "linux",
    }


async def system_ping(params: dict) -> dict:
    """`system.ping` — RTT probe. Returns server timestamp in ms."""
    return {
        "pong": True,
        "server_time_ms": int(time.time() * 1000),
    }


def _cached_update() -> tuple[bool, str] | None:
    """`(available, latest_version)` from the update-check cache, or None.

    Reads the same cache the TUI status bar reads, and stays silent under the
    same conditions (opted out, no cache yet, install that cannot self-upgrade).
    """
    try:
        from raven.cli.update_notice import read_cache, update_notice

        if update_notice(_raven_version()) is None:
            return None
        cache = read_cache() or {}
        latest = cache.get("latest_version")
        return (True, latest) if isinstance(latest, str) else None
    except Exception:
        return None


async def _announce_update(send_frame: Any, latest: str) -> None:
    """Push ``system.update_available`` to every client ``send_frame`` reaches.

    The same notification ``serve_commands._announce_updates`` sends on its poll,
    frame for frame, because it is the same fact arriving by a different route.
    """
    try:
        await send_frame({"method": "system.update_available", "params": {"latest_version": latest}})
    except Exception:
        logger.debug("rpc: could not announce {} to open clients", latest)


async def system_version(params: dict, *, send_frame: Any = None) -> dict:
    """`system.version` — versions for diagnostics, plus any pending upgrade.

    ``check: true`` fetches the latest release before answering, off the event
    loop. The cached answer can be a poll interval old, and a reader acting on a
    version number needs it to be the number they will get -- so the served page
    asks with ``check: true`` twice over: once behind the settings button, and
    once on every boot, after its first paint has already gone out on the cached
    answer. Anything tuning this path (a throttle, a spinner, a quota guard) is
    tuning something that runs per page load, not one button.

    A check that finds a newer build also announces it on ``send_frame`` when the
    transport wired one. Without that, whatever asked for the check is the only
    thing that learns the answer -- so one window's settings button would refresh
    the cache while every other window on the same gateway kept showing the old
    version until it happened to ask again.

    Announcing a version the poll already announced is harmless: the client
    writes one element's text (``showUpNote`` in ``ui-web/src/live/210-update-notice.js``), so a repeat
    lands on the banner already on screen. That is why this path keeps no memory
    of what the announcer has sent -- sharing that state across two schedules
    would buy nothing a re-rendered banner does not already give.
    """
    result = {
        "server_version": SERVER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "raven_version": _raven_version(),
    }
    checked = params.get("check") is True
    if checked:
        import asyncio

        try:
            from raven.cli.update_notice import check_for_update

            await asyncio.to_thread(check_for_update, _raven_version())
        except Exception:
            pass
    pending = _cached_update()
    if pending is not None:
        result["update_available"], result["latest_version"] = pending
        if checked and send_frame is not None:
            await _announce_update(send_frame, result["latest_version"])
    return result


async def system_upgrade(params: dict) -> dict:
    """`system.upgrade` — install the latest release and restart the gateway.

    Only meaningful inside `raven serve`: the helper waits for *this* process to
    exit, so the caller must be the process being replaced. Returns as soon as
    the helper owns the install; the shutdown is scheduled a beat later so this
    reply reaches the client first.
    """
    import asyncio

    from raven.cli.serve_commands import SERVE
    from raven.cli.upgrade_commands import UpgradeError, plan_upgrade, spawn_detached_upgrade

    # The dispatcher only lifts `detail` into error.data when no data dict is
    # given, so every refusal carries its own reason AND its human sentence --
    # the GUI shows that sentence verbatim next to the fallback command.
    def _refuse(reason: str, detail: str) -> ConfigValidationError:
        return ConfigValidationError(detail, data={"reason": reason, "detail": detail})

    if SERVE.hosted_by_gateway:
        # The page is mounted inside `raven gateway`, and the relaunch below
        # would replace that whole process with a bare `raven serve` -- the IM
        # channels would silently vanish. Restarting the gateway in place is a
        # later phase; until then this refusal is the honest answer.
        raise _refuse(
            "gateway_hosted",
            "This page is hosted by `raven gateway`. Run `raven upgrade`, then restart the gateway.",
        )
    if not SERVE.running:
        raise _refuse("not_serving", "system.upgrade is only available while `raven serve` is running")

    try:
        # Network-bound and synchronous (httpx), so keep it off the event loop.
        plan = await asyncio.to_thread(plan_upgrade)
    except UpgradeError as exc:
        raise _refuse("not_upgradable", str(exc)) from exc
    except _md.PackageNotFoundError as exc:
        # No metadata means the install is mid-replacement: uv removes the old
        # environment before writing the new one, and a first upgrade sits in
        # that gap for minutes while it byte-compiles. Reporting the raw lookup
        # failure reads as a broken install and invites a second upgrade into
        # the same gap, so name what is actually happening.
        raise _refuse("in_progress", "An upgrade is already running; wait for it to finish.") from exc
    except Exception as exc:
        raise _refuse("check_failed", f"Could not check for a newer Raven release: {exc}") from exc

    relaunch: list[str] | None = None
    extra_env: dict[str, str] = {}
    raven_bin = plan.target.bin_dir / ("raven.exe" if os.name == "nt" else "raven")
    if SERVE.port is not None and raven_bin.parent.is_dir():
        relaunch = [str(raven_bin), "serve", "--port", str(SERVE.port)]
        # Same port, same credentials: the browser reconnects to the origin it
        # already has and presents the cookie it already holds, so it stays
        # signed in across the restart instead of hitting an auth wall. Both
        # values travel -- the cookie is what the browser sends, the token is
        # what a relauncher needs, and they are no longer the same string.
        extra_env["RAVEN_SERVE_PORT_STRICT"] = "1"
        if SERVE.token:
            extra_env["RAVEN_SERVE_TOKEN"] = SERVE.token
        if SERVE.cookie:
            extra_env["RAVEN_SERVE_COOKIE"] = SERVE.cookie

    try:
        spawn_detached_upgrade(
            plan,
            parent_pid=os.getpid(),
            relaunch=relaunch,
            extra_env=extra_env or None,
        )
    except UpgradeError as exc:
        raise _refuse("handoff_failed", str(exc)) from exc

    logger.info(
        "rpc: upgrade handed off — {} -> {} (relaunch={})",
        plan.current_version,
        plan.release.version,
        bool(relaunch),
    )
    asyncio.get_running_loop().call_later(_UPGRADE_EXIT_DELAY_S, SERVE.request_shutdown)
    return {
        "status": "started",
        "from_version": plan.current_version,
        "to_version": plan.release.version,
        "relaunch": relaunch is not None,
    }


# Long enough for the JSON-RPC reply to reach the client over the WebSocket,
# short enough that the helper is not left polling a live pid.
_UPGRADE_EXIT_DELAY_S = 0.75


def register_system_methods(
    dispatcher: "Dispatcher",
    *,
    channel: str = LOCAL_CHANNEL,
    send_frame: Any = None,
) -> None:
    """Register all 4 system.* methods on a dispatcher instance.

    ``send_frame`` is the transport's broadcast sink, and only a transport that
    owns one should pass it: with it, an explicit ``system.version`` check that
    finds a newer build tells every connected client. Without one -- the TUI's
    single-socket transport, the web gateway, tests -- the check answers its
    caller and nothing else, exactly as before.
    """

    async def _hello(params: dict) -> dict:
        return await system_hello(params, channel=channel)

    async def _version(params: dict) -> dict:
        return await system_version(params, send_frame=send_frame)

    dispatcher.register("system.hello", _hello)
    dispatcher.register("system.ping", system_ping)
    dispatcher.register("system.version", _version)
    dispatcher.register("system.upgrade", system_upgrade)


__all__ = [
    "system_hello",
    "system_ping",
    "system_version",
    "system_upgrade",
    "register_system_methods",
    "SERVER_VERSION",
    "SCHEMA_VERSION",
    "SERVER_CAPABILITIES",
]
