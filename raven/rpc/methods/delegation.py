"""``delegation.*`` and the ``subagent.*`` cancel-verb RPC handlers.

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

``subagent.cancel_session`` is the session-wide sweep the gateway's IM
``/stop`` always had and the terminal dialect's clients did not;
``subagent.cancel_instance`` stops the spawns behind one rendered
(agent, handle) pair -- the address an instances panel actually holds, where
a background instance chat has no turn for ``turn.cancel`` to reach.
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


async def subagent_cancel_session(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``subagent.cancel_session`` -- stop every background sub-agent a session left running.

    Adopted background DAG runs sit in the same session index as spawns, so
    the sweep reaches them too. ``cancelled`` counts the tasks that were
    live; zero is a valid answer for a quiet session, not an error.
    """
    session_key = str(params.get("session_key") or "").strip()
    if not session_key:
        return {"cancelled": 0, "session_key": ""}
    manager = _manager(agent_loop_factory)
    return {"cancelled": await manager.cancel_by_session(session_key), "session_key": session_key}


async def subagent_cancel_instance(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``subagent.cancel_instance`` -- stop the spawns behind one (agent, handle).

    ``found`` mirrors ``subagent.interrupt``: an instance that finished
    between the panel rendering the row and the user pressing the key is not
    an error. An empty ``session_key`` addresses the manager's default lane,
    the same normalization the manager itself applies.
    """
    session_key = str(params.get("session_key") or "").strip()
    agent = str(params.get("agent") or "").strip()
    handle = str(params.get("handle") or "").strip()
    if not agent or not handle:
        return {"found": False, "session_key": session_key, "agent": agent, "handle": handle}
    manager = _manager(agent_loop_factory)
    found = await manager.cancel_by_instance(session_key, agent, handle)
    return {"found": found, "session_key": session_key, "agent": agent, "handle": handle}


def register_delegation_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register ``delegation.status`` / ``delegation.pause`` and the three
    ``subagent.*`` cancel verbs (interrupt / cancel_session / cancel_instance)."""

    async def _status(params: dict) -> dict:
        return await delegation_status(params, agent_loop_factory=agent_loop_factory)

    async def _pause(params: dict) -> dict:
        return await delegation_pause(params, agent_loop_factory=agent_loop_factory)

    async def _interrupt(params: dict) -> dict:
        return await subagent_interrupt(params, agent_loop_factory=agent_loop_factory)

    async def _cancel_session(params: dict) -> dict:
        return await subagent_cancel_session(params, agent_loop_factory=agent_loop_factory)

    async def _cancel_instance(params: dict) -> dict:
        return await subagent_cancel_instance(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("delegation.status", _status)
    dispatcher.register("delegation.pause", _pause)
    dispatcher.register("subagent.interrupt", _interrupt)
    dispatcher.register("subagent.cancel_session", _cancel_session)
    dispatcher.register("subagent.cancel_instance", _cancel_instance)


__all__ = [
    "MAX_SPAWN_DEPTH",
    "delegation_status",
    "delegation_pause",
    "subagent_interrupt",
    "subagent_cancel_session",
    "subagent_cancel_instance",
    "register_delegation_methods",
]
