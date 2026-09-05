"""``delegation.*`` and ``subagent.interrupt`` RPC handlers.

ui-tui primes its spawn HUD caps from ``delegation.status`` when the agents
overlay opens (the HUD's rows themselves ride the ``subagent.status`` and
``dag.*`` events), and drives the overlay's pause and kill controls from
``delegation.pause`` / ``subagent.interrupt``. None of the three had a handler
originally, so each call came back -32601 -- and because the shared ``rpc``
helper in ``ui-tui/src/app/useMainApp.ts`` reports a rejection by writing to
the transcript, the status call in particular printed an error line into the
chat every time the agent spawned anything.

The caps are read off the live ``SubagentManager`` rather than off config, so a
runtime override (or a future hot-apply) cannot make the HUD disagree with the
gate that is actually admitting spawns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.rpc.errors import InternalError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


# Raven's sub-agents are leaves: ``RavenLoopBackend`` builds their tool set
# without the spawn tool, and no backend reports a parent id, so a spawn tree
# is exactly one level deep. The HUD needs a number to render "d1/1" against;
# reporting a cap the runtime does not enforce would be worse than this
# constant, which it does.
MAX_SPAWN_DEPTH = 1


def _manager(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    """The live ``SubagentManager``, or a typed error naming why there is none."""
    loop = agent_loop_factory() if agent_loop_factory is not None else None
    manager = getattr(loop, "subagents", None) if loop is not None else None
    if manager is None:
        raise InternalError("no live subagent manager")
    return manager


async def delegation_status(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``delegation.status`` -- the caps and pause flag the spawn HUD renders.

    ``active`` is declared optional in the client contract and the client's
    store ignores it: the overlay builds its tree from the streamed
    ``subagent.*`` events, which carry the per-spawn goal and status this
    manager does not retain. Omitted rather than filled with placeholders.
    """
    manager = _manager(agent_loop_factory)
    return {
        "max_concurrent_children": manager.max_concurrent,
        "max_spawn_depth": MAX_SPAWN_DEPTH,
        "paused": manager.paused,
    }


async def delegation_pause(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``delegation.pause`` -- set the spawn kill switch, echo the effective value.

    Echoing what took effect rather than what was asked for lets the client
    resync when two surfaces toggle the same flag.
    """
    manager = _manager(agent_loop_factory)
    return {"paused": manager.set_paused(bool(params.get("paused")))}


async def subagent_interrupt(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``subagent.interrupt`` -- cancel one running spawn by id.

    ``found: false`` covers both an unknown id and one that finished between
    the overlay rendering the row and the user pressing the key; the client
    shows those the same way, and neither is an error.
    """
    subagent_id = str(params.get("subagent_id") or "").strip()
    if not subagent_id:
        return {"found": False, "subagent_id": ""}
    manager = _manager(agent_loop_factory)
    return {
        "found": await manager.cancel_by_id(subagent_id),
        "subagent_id": subagent_id,
    }


def register_delegation_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register ``delegation.status`` / ``delegation.pause`` / ``subagent.interrupt``."""

    async def _status(params: dict) -> dict:
        return await delegation_status(params, agent_loop_factory=agent_loop_factory)

    async def _pause(params: dict) -> dict:
        return await delegation_pause(params, agent_loop_factory=agent_loop_factory)

    async def _interrupt(params: dict) -> dict:
        return await subagent_interrupt(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("delegation.status", _status)
    dispatcher.register("delegation.pause", _pause)
    dispatcher.register("subagent.interrupt", _interrupt)


__all__ = [
    "MAX_SPAWN_DEPTH",
    "delegation_status",
    "delegation_pause",
    "subagent_interrupt",
    "register_delegation_methods",
]
