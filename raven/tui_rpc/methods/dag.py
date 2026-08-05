"""``dag.get`` / ``dag.node`` RPC handlers.

A ``run_subagent_dag`` run publishes ``dag.*`` progress events while it executes,
and nothing replays them. These two read the run dir instead, so a TUI that was
not listening for every frame is not stuck with whatever it happened to catch:

* ``dag.get`` rebuilds (or repairs) a whole graph. An unfinalized run has no
  manifest, so the instance registry is overlaid on top -- see
  :func:`~raven.agent.subagent_dag._resume.read_run_reconciled`, shared with the
  web surface so a resumed graph means the same thing on both.
* ``dag.node`` pulls one node's *rendered* prompt and its output, neither of
  which the graph carries: the manifest inlines only the leaf nodes' text, and
  the tool result the transcript keeps is clamped to 200 chars.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.agent.subagent_dag._errors import DagValidationError
from raven.agent.subagent_dag._reader import DagReadError
from raven.agent.subagent_dag._resume import read_run_reconciled
from raven.tui_rpc.errors import InternalError

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods.session import AgentLoopFactory


def _dag_tool(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    """The live DAG tool, or a typed error naming why there is none.

    The tool is only registered when third-party sub-agents are configured, so
    "not configured" is a normal state -- but one the client can do nothing about
    mid-call, and it must not read as an empty run.
    """
    loop = agent_loop_factory() if agent_loop_factory is not None else None
    tools = getattr(loop, "tools", None) if loop is not None else None
    tool = tools.get("run_subagent_dag") if tools is not None else None
    if tool is None:
        raise InternalError("no live run_subagent_dag tool (configure third-party sub-agents first)")
    return tool


async def dag_get(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """One run's structure and per-node state, read back off disk."""
    tool = _dag_tool(agent_loop_factory)
    # The reader raises its own ValueError subclass for a malformed id or a run
    # dir that is gone. Untyped, that reaches the client as a -32603 traceback
    # instead of a message it can show.
    try:
        run = await read_run_reconciled(tool, params.get("run_id", ""), params.get("session_key"))
    except (DagReadError, DagValidationError) as exc:
        raise InternalError(str(exc)) from exc
    return {"run": run}


async def dag_node(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """One node's rendered prompt and (head of its) output."""
    tool = _dag_tool(agent_loop_factory)
    try:
        node = await tool.read_node(
            params.get("run_id", ""),
            params.get("node", ""),
            max_output_chars=int(params.get("max_output_chars") or 20000),
        )
    except (DagReadError, DagValidationError) as exc:
        raise InternalError(str(exc)) from exc
    return {"node": node}


def register_dag_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register ``dag.get`` / ``dag.node`` on a dispatcher instance."""

    async def _get(params: dict) -> dict:
        return await dag_get(params, agent_loop_factory=agent_loop_factory)

    async def _node(params: dict) -> dict:
        return await dag_node(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("dag.get", _get)
    dispatcher.register("dag.node", _node)
