"""Whether this process may serve A2A at all.

The second of the two gates the boundary needs. The outbound half rides the tool
registry -- ``a2a_send`` is simply not built in a sub-agent -- but serving a port
does not pass through the registry, so this is its own check.

The signal is ``RAVEN_SUBAGENT``, which the host merges into every ``kind: acp``
child it launches. All five products under ``agents/`` are ``kind: acp`` and none
overrides it, so the seam covers them and covers a future product for free.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aiohttp import web
from loguru import logger

from raven.agent.subagent.role import is_subagent_process
from raven.config.schema import A2aConfig

REFUSAL = (
    "this raven was launched as a sub-agent, and a sub-agent does not serve A2A: "
    "it is reached over ACP by the host that started it"
)


def refuse_if_subagent() -> str | None:
    """The reason this process may not serve A2A, or None if it may."""
    return REFUSAL if is_subagent_process() else None


def may_mount(config: A2aConfig) -> bool:
    """Whether this process may mount the A2A face: enabled, and not a sub-agent."""
    if not config.server.enabled:
        return False
    reason = refuse_if_subagent()
    if reason is not None:
        logger.info("not mounting the A2A face: {}", reason)
        return False
    return True


def mount_if_allowed(app: web.Application, config: A2aConfig, *, handler: Any) -> bool:
    """Mount the A2A routes onto `app` when enabled and permitted. Returns whether it did."""
    if not may_mount(config):
        return False
    from raven.a2a.routes_aiohttp import add_a2a_routes

    add_a2a_routes(app, config, handler)
    return True


def mount_gateway_face(
    app: web.Application,
    config: A2aConfig,
    *,
    agent_loop_factory: Callable[[], Any | None],
) -> Any | None:
    """Build this process's A2A handler and mount it on a gateway app, or return None.

    The assembly lives here, not at the call site, so the rpc surface imports this one
    module and nothing else out of ``raven/a2a/``. CONTEXT.md's surfaces law forbids a
    served surface from reaching into a sibling surface's insides, and the repo's answer
    for a legitimate hosting edge is a facade plus a roster guard -- the way ``raven/acp/``
    reaches rpc only through ``raven.rpc.bootstrap``.

    The handler is bound to a factory that re-resolves the loop per call rather than to a
    loop: at gateway-boot time the loop may not exist yet, and a later hot-reload swaps it
    in place. Building it behind `may_mount` is also what makes the default-OFF face cost
    nothing -- a2a-sdk is never imported in a gateway that does not serve A2A.
    """
    if not may_mount(config):
        return None
    from raven.a2a.runtime import build_request_handler, make_run_turn_from_factory

    handler = build_request_handler(config, make_run_turn_from_factory(agent_loop_factory))
    return handler if mount_if_allowed(app, config, handler=handler) else None
