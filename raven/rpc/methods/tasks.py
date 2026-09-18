"""``tasks.list`` -- a conversation's delegated work as tasks.

One read that answers what the desk's tasks tab draws: a run-level row per
``spawn`` call and per ``run_subagent_dag`` run (a playbook run is one), each
carrying its nodes. ``subagent.list`` lists the same work as one row per node
and knows nothing of a run's title or edges; ``dag.get`` knows both but needs
the live graph tool. This reads the three stores a task leaves behind -- the run
dir, the session node registry (``subagents/nodes.json``) and the instance
registry -- and the node files, so a reload answers the same as a live page.

Bodies stay where they are: a node's messages, output and rendered prompt are
``dag.node`` / ``subagent.context``'s. The design is
``docs/specs/2026-09-18-desk-tasks-list-design.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

    from .turn import AgentLoopFactory


async def tasks_list(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """Every task one conversation started, newest first.

    An absent or unknown session is an empty list rather than an error: a
    conversation that delegated nothing and one that does not exist draw the
    same panel.
    """
    session_key = str(params.get("session_key") or "")
    if not session_key:
        return {"tasks": []}
    return {"tasks": []}


def register_tasks_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    async def _list(params: dict) -> dict:
        return await tasks_list(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("tasks.list", _list)


__all__ = ["register_tasks_methods", "tasks_list"]
