"""Whether this process may serve A2A at all.

The second of the two gates the boundary needs. The outbound half rides the tool
registry -- ``a2a_send`` is simply not built in a sub-agent -- but serving a port
does not pass through the registry, so this is its own check.

The signal is ``RAVEN_SUBAGENT``, which the host merges into every ``kind: acp``
child it launches. All five products under ``agents/`` are ``kind: acp`` and none
overrides it, so the seam covers them and covers a future product for free.
"""

from __future__ import annotations

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


def mount_if_allowed(app: web.Application, config: A2aConfig, *, handler: Any) -> bool:
    """Mount the A2A routes onto `app` when enabled and permitted. Returns whether it did."""
    if not config.server.enabled:
        return False
    reason = refuse_if_subagent()
    if reason is not None:
        logger.info("not mounting the A2A face: {}", reason)
        return False
    from raven.a2a.routes_aiohttp import add_a2a_routes

    add_a2a_routes(app, config, handler)
    return True
