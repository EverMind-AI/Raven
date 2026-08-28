# -*- coding: utf-8 -*-
"""Shared helper: emit a sub-agent instance status event.

Used by both the CLI and OpenAI sub-agent tools to push a live +
durable ``subagent_instance_updated`` event. No-op when there is no
publisher (standalone/tests) or no ``handle`` (stateless calls).
"""
from collections.abc import Awaitable, Callable

from .._logging import logger


async def emit_instance_status(
    publisher: Callable[[str, dict], Awaitable[None]] | None,
    *,
    handle: str | None,
    agent_id: str | None,
    prototype: str,
    transport: str,
    status: str,
    action: str | None,
) -> None:
    """Publish one instance status transition, swallowing failures.

    Args:
        publisher (`Callable[[str, dict], Awaitable[None]] | None`):
            The progress publisher, or ``None`` to no-op.
        handle (`str | None`):
            The instance handle; ``None`` (stateless) no-ops.
        agent_id (`str | None`):
            The CLI/HTTP session id (may be ``None`` before creation).
        prototype (`str`):
            The sub-agent prototype (tool) name.
        transport (`str`):
            One of ``"cli"`` / ``"codex"`` / ``"openai"``.
        status (`str`):
            One of ``"running"`` / ``"completed"`` / ``"failed"``.
        action (`str | None`):
            ``"create"`` / ``"resume"`` when known, else ``None``.
    """
    if publisher is None or handle is None:
        return
    try:
        await publisher(
            "subagent_instance_updated",
            {
                "handle": handle,
                "agent_id": agent_id,
                "prototype": prototype,
                "transport": transport,
                "status": status,
                "action": action,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("subagent_instance publish failed", exc_info=True)
