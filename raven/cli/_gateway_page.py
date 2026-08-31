"""Mount the served page inside the gateway process.

`raven serve` standalone assembles WsGateway + build_rpc_stack + the aiohttp
app around an AgentLoop it builds itself. This module performs the same
assembly around the gateway's existing loop, so the browser page and the IM
channels run on one engine. Every piece is serve's own, imported rather than
copied: the auth model, the app routes, the serve.json shape `raven web` and
the GUI shell read, the update announcer.

Deliberately NOT ported from `_serve_main`: ``set_surface("page")``. It is
process-wide, and this process serves IM channels and the page at once, so a
global declaration would mislabel every channel turn's trace. Instead each
client names itself per connection in ``system.hello`` (the page sends
``page``, the shell ``shell``, the TUI relay ``tui``); a connection that
declares nothing falls back to the channel name.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class PageMount:
    port: int
    url: str
    emitter: Any
    outlet: Any
    # The page's own ask_user / deep-research broker (build_rpc_stack's). The
    # host wires a RoutingQuestionBroker over this and its channel broker so
    # the page answers its own sessions without swallowing the IM round-trip.
    question_broker: Any
    teardown: Callable[[], Awaitable[None]]
    direct_targets: dict[str, dict[str, str]]


async def _standalone_serve_owner() -> tuple[int, int] | None:
    """(pid, port) of a live standalone `raven serve` recorded in serve.json.

    ``None`` unless three facts line up, mirroring ``_gateway_hosted_page``
    read the other way: serve.json names a pid that is not ours, that pid is
    alive, and the recorded port's /health answers as ``raven-serve``. A stale
    file (dead pid, or the port re-used by a stranger) reads as None, and
    mounting over it is then correct.
    """
    import json
    import os

    import aiohttp

    from raven.cli.serve_commands import _pid_alive, _state_path

    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        pid, port = int(data["pid"]), int(data["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid <= 0 or pid == os.getpid() or port <= 0 or not _pid_alive(pid):
        return None
    timeout = aiohttp.ClientTimeout(total=2)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as health:
                if health.status == 200 and (await health.json()).get("service") == "raven-serve":
                    return (pid, port)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
        return None
    return None


async def mount_page(agent_loop: Any, preferred_port: int) -> PageMount | None:
    """Serve the page on ``agent_loop``, on the `raven serve` port policy.

    Returns ``None`` when a live standalone `raven serve` already owns
    serve.json: mounting over it would put two engines on one agent home and
    clobber the file `raven web` finds the running one through. The gateway
    then runs its IM channels only; the page stays the standalone serve's.
    (`raven serve` performs the same check in the other direction and yields
    to a page-hosting gateway -- see ``_gateway_hosted_page``.)

    Must be called AFTER the gateway's own broker/sink wiring: the stack
    applies the page's streaming sinks (dag progress, mcp events) to the
    shared loop, and last write wins is the intent for those -- the page is
    the surface that renders a progress stream. The question brokers are NOT
    left last-write-wins: the host re-binds ask_user / deep-research through a
    ``RoutingQuestionBroker`` over this mount's ``question_broker`` and its
    channel broker, so IM conversations keep their round-trip while the page
    answers its own.
    """
    from aiohttp import web
    from loguru import logger

    from raven.cli._console_feature import register_console_feature
    from raven.cli.serve_commands import (
        _announce_updates,
        _write_serve_state,
        adopt_stored_cookie,
        port_strict,
        resolve_ui_dist,
    )
    from raven.rpc.bootstrap import build_rpc_stack
    from raven.rpc.serve_control import SERVE
    from raven.rpc.spine import RpcOutlet
    from raven.rpc.transports.ws import WsGateway, build_app, pick_port

    register_console_feature()
    owner = await _standalone_serve_owner()
    if owner is not None:
        owner_pid, owner_port = owner
        logger.warning(
            "gateway page: a standalone `raven serve` (pid {}) already serves the page on port {}; "
            "skipping the page mount -- the gateway runs IM channels only",
            owner_pid,
            owner_port,
        )
        return None

    ws_gateway = WsGateway()
    adopt_stored_cookie(ws_gateway)
    # Same port policy as standalone serve, strict flag included: a relaunch
    # under an open tab (the web supervisor's retry, `system.upgrade`) has to
    # come back on the port that tab is pointed at, and this mount is now what
    # `raven web` supervises -- reading the flag in only one of the two places
    # is how a restart would strand the page it was restarting for.
    bound_port = await pick_port(preferred_port, strict=port_strict())
    ws_gateway.port = bound_port

    stack = await build_rpc_stack(ws_gateway.broadcast, agent_loop=agent_loop)
    ws_gateway.dispatcher = stack.dispatcher

    app = build_app(ws_gateway, resolve_ui_dist(), deliverables=stack.deliverables)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", bound_port)
    try:
        await site.start()
    except OSError:
        await stack.teardown()
        await runner.cleanup()
        raise

    state_path = _write_serve_state(bound_port, ws_gateway.session_token, ws_gateway.session_cookie)
    SERVE.arm_hosted(bound_port, ws_gateway.session_token, ws_gateway.session_cookie)

    try:
        from raven.updates.update_notice import maybe_refresh_async

        maybe_refresh_async()
    except Exception as exc:
        logger.debug("gateway page: update check skipped ({})", exc)

    stop = asyncio.Event()
    announcer = asyncio.create_task(_announce_updates(ws_gateway.broadcast, stop))

    # The page's outlet for the HOST's hubs. The page's own turns stream
    # through the stack's private hub; this second outlet (same emitter, same
    # direct-target map) is what lets a turn the gateway runs -- a tui cron
    # job, a subagent announce -- deliver to the page, because the hub routes
    # each outbound by source.channel to the outlet with that name.
    outlet = RpcOutlet("tui", stack.emitter, stack.direct_targets)

    async def teardown() -> None:
        stop.set()
        announcer.cancel()
        SERVE.disarm()
        if state_path is not None:
            _unlink_if_ours(state_path)
        try:
            await stack.teardown()
        finally:
            await runner.cleanup()

    return PageMount(
        port=bound_port,
        url=f"http://127.0.0.1:{bound_port}",
        emitter=stack.emitter,
        outlet=outlet,
        question_broker=stack.question_broker,
        teardown=teardown,
        direct_targets=stack.direct_targets,
    )


def _unlink_if_ours(state_path: Any) -> None:
    """Remove serve.json only while it still names this process.

    Another engine may have (re)written the file since we did -- a standalone
    serve started later, or a supervisor restart. Deleting their state file
    would leave a live engine undiscoverable and invite a third one.
    """
    import json
    import os

    try:
        recorded = int(json.loads(state_path.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    if recorded == os.getpid():
        state_path.unlink(missing_ok=True)


__all__ = ["PageMount", "mount_page"]
