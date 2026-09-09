"""``dag.get`` / ``dag.node`` RPC handlers.

A ``run_subagent_dag`` run publishes ``dag.*`` progress events while it executes,
and nothing replays them. These two read the run dir instead, so a TUI that was
not listening for every frame is not stuck with whatever it happened to catch:

* ``dag.get`` rebuilds (or repairs) a whole graph. An unfinalized run has no
  manifest, so the instance registry is overlaid on top -- see
  :func:`~raven.agent.subagent.dag_resume.read_run_reconciled`, shared with the
  sub-agent DAG control tools so a resumed graph reads the same from inside a
  turn and from this method.
* ``dag.node`` pulls one node's *rendered* prompt and its output, neither of
  which the graph carries: the manifest inlines only the leaf nodes' text, and
  the tool result the transcript keeps is clamped to 200 chars.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent.subagent import activity as run_activity
from raven.agent.subagent.dag_live import live_run_ids
from raven.agent.subagent.dag_reader import DagReadError
from raven.agent.subagent.dag_resume import read_run_reconciled
from raven.agent.subagent.dag_store import node_live_key
from raven.agent.subagent.history import dag_root, nodes_root
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.tool_vocabulary import normalize_row
from raven.rpc.errors import InternalError
from raven.rpc.methods.session import _map_to_wire
from raven.rpc.methods.subagent import _session_dir

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


def _dag_tool(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    """The live DAG tool, or a typed error naming why there is none.

    The tool is only registered when third-party sub-agents are configured, so
    "not configured" is a normal state -- but one the client can do nothing about
    mid-call, and it must not read as an empty run.
    """
    return _dag_tool_of(agent_loop_factory() if agent_loop_factory is not None else None)


def _dag_tool_of(loop: Any) -> Any:
    """Same, for a caller that already resolved the loop and needs both."""
    tools = getattr(loop, "tools", None) if loop is not None else None
    tool = tools.get("run_subagent_dag") if tools is not None else None
    if tool is None:
        raise InternalError("no live run_subagent_dag tool (configure third-party sub-agents first)")
    return tool


async def dag_get(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """One run's structure and per-node state, read back off disk."""
    loop = agent_loop_factory() if agent_loop_factory is not None else None
    tool = _dag_tool_of(loop)
    # The reader raises its own ValueError subclass for a malformed id or a run
    # dir that is gone. Untyped, that reaches the client as a -32603 traceback
    # instead of a message it can show.
    try:
        run = await read_run_reconciled(
            tool,
            params.get("run_id", ""),
            params.get("session_key"),
            # Across every graph tool. The registered one does not know a run the
            # playbook engine dispatched, and answering "not live" overlays
            # interrupted on every node of it that had not finished -- the same
            # symptom the instance rows had, on the path that reads a run back.
            live_runs=lambda: live_run_ids(loop),
        )
    except (DagReadError, DagValidationError) as exc:
        raise InternalError(str(exc)) from exc
    return {"run": run}


async def dag_node(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """One node's rendered prompt and (head of its) output.

    Falls back to reading the run dir directly when no live tool is registered.
    The tool only exists while third-party sub-agents are configured, and a run
    that already happened does not stop having happened when that config changes
    -- without the fallback, every past node became unreadable (and
    ``subagent.list`` would list nodes nothing could open).
    """
    run_id = str(params.get("run_id") or "")
    node_id = str(params.get("node") or "")
    max_chars = int(params.get("max_output_chars") or 20000)
    try:
        tool = _dag_tool(agent_loop_factory)
    except InternalError:
        node = await _node_off_disk(run_id, node_id, max_chars, params.get("session_key"), agent_loop_factory)
        return {"node": _with_messages(node, run_id, node_id)}
    try:
        node = await tool.read_node(
            run_id,
            node_id,
            max_output_chars=max_chars,
            session_key=params.get("session_key"),
        )
    except (DagReadError, DagValidationError) as exc:
        raise InternalError(str(exc)) from exc
    return {"node": _with_messages(node, run_id, node_id)}


def _with_messages(node: dict, run_id: str, node_id: str) -> dict:
    """Add the node's transcript in the shape the ordinary renderer takes.

    A node used to reach the panel as two strings, so it drew as two bubbles
    with nothing between them -- while the equivalent spawned call drew every
    tool it called. Same wire mapping as ``subagent.context`` for the same
    reason: one renderer for both kinds of delegated work, not two that drift.
    """
    stored: list[dict[str, Any]] = []
    if node.get("prompt"):
        stored.append({"role": "user", "content": node["prompt"]})
    turns = node.get("transcript")
    if not turns:
        # Nothing has reached disk yet, which is the normal state of a node
        # being watched while it runs: its only account is the activity this
        # very process is collecting. Copied out of the live list because the
        # collector republishes it on every update from the agent.
        live = run_activity.live(node_live_key(run_id, node_id))
        turns = [m for m in list(live.transcript) if isinstance(m, dict) and m.get("role")] if live else []
    stored.extend(normalize_row(turn) for turn in turns)
    # The console tail, same as subagent.context: the only in-flight account a
    # cli-lane node has is its own output, and it is live-only by construction.
    live_run = run_activity.live(node_live_key(run_id, node_id))
    if live_run is not None and live_run.console:
        stored.append({"role": "console", "content": live_run.console})
    if node.get("output"):
        stored.append({"role": "assistant", "content": node["output"]})
    elif node.get("error"):
        # A failed node's account of itself. It has no output by definition, so
        # without this the panel showed the prompt and stopped -- the reader saw
        # a question nobody answered rather than a node that failed, and the
        # reason was sitting in the manifest the whole time.
        stored.append({"role": "assistant", "content": str(node["error"])})
    # The reader's raw provider-shaped turns do not go on the wire: a client
    # draws `messages`, and declaring the unmapped shape beside it would put a
    # second, undrawn representation of the same run in the contract.
    detail = {k: v for k, v in node.items() if k != "transcript"}
    return {**detail, "messages": _map_to_wire(stored, f"dag:{run_id}:{node_id}")}


async def _node_off_disk(
    run_id: str, node_id: str, max_chars: int, session_key: Any, agent_loop_factory: Any = None
) -> dict:
    """``read_node`` against the local filesystem instead of a workspace backend.

    The history root is under agent home, which is local for every backend that
    runs raven itself; a remote workspace backend is the one case this cannot
    serve, and that case still has the live tool. Same reader either way -- the
    file layout is not restated here.

    The factory is threaded through rather than resolved fresh: the running
    manager carries the project grouping (``raven serve`` builds it with a
    ``project_slug`` from the launch directory), and a manager built here
    without it resolves ``sessions/<channel>/`` instead of ``sessions/<slug>/``.
    That is a directory the run was never written to, so every node the lister
    offers -- and the lister *does* pass the factory -- would open empty, which
    is the exact state this fallback exists to prevent.
    """
    from raven.agent.subagent.dag_reader import read_node as read_node_off

    if not session_key:
        raise InternalError("dag.node needs session_key when no run_subagent_dag tool is live")
    try:
        session_dir = _session_dir(str(session_key), agent_loop_factory)
        root = str(dag_root(session_dir))
        node_root = str(nodes_root(session_dir))
        return await read_node_off(_LocalFiles(), root, run_id, node_id, node_root, max_output_chars=max_chars)
    except (DagReadError, DagValidationError, OSError) as exc:
        raise InternalError(str(exc)) from exc


class _LocalFiles:
    """The three calls ``dag_reader`` makes of a workspace backend, done locally."""

    async def file_exists(self, path: str) -> bool:
        return Path(path).is_file()

    async def read_file(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def join_path(self, *parts: str) -> str:
        return str(Path(*parts))


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
