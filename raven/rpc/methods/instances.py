"""``subagents.instance*`` RPC handlers: the sub-agent instances a session used.

A different noun from ``methods/subagents.py``, which configures *which*
sub-agents exist. This group is about the instances a session has actually
talked to -- the things the direct-chat surface addresses.

Every read here goes through something already shared with the web RPC (the
instance registry, ``reconcile_instance_rows``, the record directories the
runtime writes) rather than deriving a second answer, so the two surfaces cannot
come to disagree about what an instance is or which ones are still alive.

The one write here is ``subagents.instance.create``: the user's own way to
start an instance, since every other creation path runs for the main agent.

No cancel method, and since a direct chat runs on its own lane
(``raven.spine.turn.direct_lane``) that now means it cannot be cancelled at all:
``turn.cancel`` looks up the *session's* turn, which is the main agent's. That is
the decision, not an oversight -- a sub-agent that is answering is left to finish
and its record is the evidence either way (see the concurrent-direct-chats
design, D3). ``SubagentManager.chat`` still unwinds cleanly if the lane is torn
down for another reason (shutdown, session delete): the record is finished
``cancelled`` and the registry row follows. The web surface keeps its own
(``raven.subagents.instances.cancel``) because a spawn there is a background task
with no turn to cancel.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent.subagent import activity as run_activity
from raven.agent.subagent.direct_chat import direct_root
from raven.agent.subagent.instance_log import message_rows
from raven.agent.subagent.instance_records import stitched_turns
from raven.agent.subagent.instances import get_registry, reconcile_instance_rows
from raven.agent.subagent.tool_vocabulary import normalize_row
from raven.agent.subagent_dag.live import live_run_ids
from raven.agent.subagent_history import dag_root, spawn_root
from raven.rpc.methods.session import _wire_tool_calls

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


def _loop(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    """The live agent loop, or ``None``.

    Unlike ``dag.get``'s equivalent this never raises. A session with no loop
    (no provider configured, or the demo runner) has no instances either, and
    an empty strip is the honest answer -- the client is drawing a status band,
    not performing an action that could fail.
    """
    if agent_loop_factory is None:
        return None
    try:
        return agent_loop_factory()
    except Exception:
        return None


def _manager(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    return getattr(_loop(agent_loop_factory), "subagents", None)


def _session_dir(agent_loop_factory: "AgentLoopFactory | None", session_key: str) -> Path | None:
    """The metadata directory of one chat session, or ``None`` without a manager.

    Routed through the manager so this resolves a session exactly the way the
    code that wrote the records did, rather than as a second derivation that
    could drift (see ``SubagentManager._session_dir``).
    """
    manager = _manager(agent_loop_factory)
    if manager is None:
        return None
    try:
        return manager._session_dir(session_key)
    except Exception:
        return None


def _collapse_dag_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per invocation: a stateful DAG node's two rows become one.

    Such a node owns both a ``dag-node`` row, which carries its status, and an
    ordinary row for the handle its sub-agent committed under. Neither alone is
    reportable: the ordinary row never receives a status, because the DAG path
    does not go through ``spawn``, and the ``dag-node`` handle names a node
    rather than a conversation, so it cannot be resumed. The pair is therefore
    reported as the addressable row wearing the node's status.

    A stateless node runs on no handle and keeps its ``dag-node`` row -- with
    no ordinary row to fall back to, dropping it would take that node off the
    list altogether. This is why the two are paired rather than one ``kind``
    being filtered out wholesale.
    """
    by_node: dict[tuple[str, str], dict[str, Any]] = {}
    # A node that declares no instance handle commits under its task id, which is
    # the node id. Rows written before the link existed carry no ``nodeId``, so
    # that equality is the only thing left to pair them by -- and it is an exact
    # match on a name the runner chose, not a guess at ``mint_handle``'s slug.
    by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        run_id, node_id = row.get("runId"), row.get("nodeId")
        if row.get("kind") == "dag-node" and isinstance(run_id, str) and isinstance(node_id, str):
            by_node[(run_id, node_id)] = row
            by_name.setdefault((str(row.get("agent")), node_id), []).append(row)

    def paired(row: dict[str, Any]) -> dict[str, Any] | None:
        run_id, node_id = row.get("runId"), row.get("nodeId")
        if isinstance(run_id, str) and isinstance(node_id, str):
            return by_node.get((run_id, node_id))
        # Only when exactly one node answers to the name: two runs of one graph
        # in a session both hold a node called `synthesize`, and a handle that
        # cannot say which of them it ran is not evidence about either.
        same = by_name.get((str(row.get("agent")), str(row.get("handle"))), [])
        return same[0] if len(same) == 1 else None

    claimed: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("kind") == "dag-node":
            continue
        node = paired(row)
        if node is None:
            out.append(row)
            continue
        claimed.add((str(node.get("runId")), str(node.get("nodeId"))))
        out.append(
            {
                **row,
                "status": node.get("status") or row.get("status"),
                "runId": node.get("runId"),
                "nodeId": node.get("nodeId"),
            }
        )
    out.extend(node for key, node in by_node.items() if key not in claimed)
    return sorted(out, key=lambda r: r.get("updatedAtMs") or 0, reverse=True)


def _mark_resumable(rows: list[dict[str, Any]], manager: Any) -> list[dict[str, Any]]:
    """Say per row whether a conversation can be opened with it.

    Answered here rather than left to each front end: statefulness is a
    property of the agent's configured backend, so a reader guessing from
    ``kind`` calls an acp instance unresumable -- and every surface would have
    to guess the same way independently. A ``dag-node`` row is never
    addressable whatever its agent can do.
    """

    def resumable(row: dict[str, Any]) -> bool:
        if row.get("kind") == "dag-node" or manager is None:
            return False
        try:
            return bool(manager.declared_stateful(row.get("agent")))
        except Exception:  # noqa: BLE001 - an unreadable roster is not a resumable one
            return False

    return [{**row, "resumable": resumable(row)} for row in rows]


async def instances_list(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """Every instance this session has used, most recently updated first."""
    loop = _loop(agent_loop_factory)
    manager = getattr(loop, "subagents", None)
    handoff = getattr(loop, "_direct_handoff", None)
    session_key = str(params.get("session_key") or "")

    reconciled = reconcile_instance_rows(
        get_registry().list_instances(session_key),
        live_handles=(lambda key: manager.live_handles(key) if manager is not None else set()),
        # Every graph tool, not just the registered one -- see
        # ``raven.agent.subagent_dag.live``.
        active_run_ids=(lambda: live_run_ids(loop)),
    )
    return {
        "instances": _mark_resumable(_collapse_dag_rows(reconciled), manager),
        "pending_handoff_count": handoff.pending_count(session_key) if handoff is not None else 0,
    }


async def instances_create(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """Create one instance of a sub-agent, and tell the main agent it happened.

    The only creation path the *user* drives. Every other one runs for the main
    agent -- a spawn binds a handle to a delegated task, a DAG node to a graph
    node -- so without this an agent nothing has been delegated to cannot be
    direct-chatted at all.

    The row and its two refusals belong to ``SubagentManager.create_instance``;
    what belongs here is the announcement, because a creation is a client action
    where a turn is something the loop ran. The row is read back out of the
    registry rather than assembled here, so what the client draws is what was
    actually persisted.

    Unlike every read in this module, a session with no agent loop raises rather
    than degrading to empty: this is an action, and an empty answer would leave
    the client announcing an instance that was never made.
    """
    loop = _loop(agent_loop_factory)
    manager = getattr(loop, "subagents", None)
    if manager is None:
        raise RuntimeError("Cannot create a sub-agent instance: no agent loop in this session.")

    session_key = str(params.get("session_key") or "")
    created = await manager.create_instance(session_key=session_key, agent=str(params.get("agent") or ""))

    row = next(
        (
            r
            for r in get_registry().list_instances(session_key)
            if r.get("agent") == created.agent and r.get("handle") == created.handle
        ),
        None,
    )
    if row is None:
        raise RuntimeError(f"Sub-agent instance {created.agent}/{created.handle} was not recorded.")

    # Announced only once the row has been read back: the main agent must not be
    # told about an instance whose creation the caller was told had failed.
    handoff = getattr(loop, "_direct_handoff", None)
    if handoff is not None:
        handoff.record_created(session_key, created)

    return {"instance": row}


async def instances_history(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """One instance's whole conversation, in the transcript wire shape.

    Preferred source is the instance's own log
    (:mod:`raven.agent.subagent.instance_log`): every lane that addressed the
    instance appends to it as a turn lands -- ``spawn``, a DAG node, a direct
    chat -- so it is the union, in order, and it holds *what the run did on the
    way*: the thought, the tool call, the tool's answer.

    A conversation that ran before that file existed has no log, and is read
    instead by stitching its three record directories back together
    (:mod:`raven.agent.subagent.instance_records`). That is a second source for
    one question, which is worth removing -- but not by migrating the records
    into logs behind a live writer's back. ``finish`` writes a record's terminal
    metadata and appends to the log as two separate steps, so no reader outside
    that process can tell a finished record from one whose rows have already been
    written, and a migration that guesses duplicates turns into an append-only
    file. Closing it needs the two writes to become one critical section; until
    then this keeps both sources and prefers the log.

    An instance with no records at all is not an error -- an unaddressed handle
    is simply empty.
    """
    session_dir = _session_dir(agent_loop_factory, str(params.get("session_key") or ""))
    if session_dir is None:
        return {"turns": []}

    agent = str(params.get("agent") or "")
    handle = str(params.get("handle") or "")

    logged = message_rows(session_dir, agent, handle)
    if logged:
        turns = _log_turns(logged)
    else:
        turns = stitched_turns(
            direct_root(session_dir, agent, handle), spawn_root(session_dir), dag_root(session_dir), agent, handle
        )

    # Whatever this instance is doing right now, which no file holds yet: the
    # log is written when the turn lands, so until then the only account of the
    # steps is the activity this process is collecting. Appended rather than
    # merged -- an in-flight turn is by definition the last one.
    live = run_activity.live_instance(str(params.get("session_key") or ""), agent, handle)
    if live is not None:
        # The record directories hold the running turn's prompt file from the
        # moment it opens, so the fallback reading above yields it as a settled
        # row -- and the live rows carry it too. Left in, the view showed the
        # question twice. Dropped here rather than at the source because this is
        # the only place that knows a turn is still running. Exactly one row: a
        # turn is one turn, and an *earlier* prompt with no reply is a run that
        # crashed -- still worth showing, and not this one.
        if turns and turns[-1].get("role") == "user":
            turns.pop()
        turns.extend(_live_turns(live, len(turns)))
    return {"turns": turns}


def _live_turns(activity: Any, offset: int) -> list[dict[str, Any]]:
    """The turn running now, as ``DirectTurn``s marked ``live``.

    The collector republishes its whole transcript on every update from the
    agent, so this is a snapshot of the turn so far rather than a delta -- which
    is what lets a reader poll it without keeping state.

    The whole turn: its prompt, its steps, and the answer text so far. All three,
    because for two of the three lanes this read is the *only* thing that carries
    any of it -- the wire tags an instance on the four events of a direct turn,
    so a ``spawn`` or a DAG node reaches the conversation view through nothing
    else. An earlier version withheld the answer text on the grounds that
    ``token.delta`` already delivers it; that holds for a direct turn and for
    neither of the others, and it left a spawned turn showing its tool calls and
    never a word the agent said.

    Marked because the client has to tell the two apart: a settled row is part of
    the record and stays put, while these are replaced wholesale by the next
    snapshot and by the record itself once the turn lands.
    """
    rows = [m for m in list(getattr(activity, "transcript", None) or []) if isinstance(m, dict)]
    prompt = getattr(activity, "prompt", None)
    if isinstance(prompt, str) and prompt:
        # Stamped with the run's own start. Unstamped, `_ms_of` reports 0 -- a
        # valid instant -- and the page dated the question a reader had just
        # asked to 1970. The steps beside it stay unstamped: this knows when the
        # turn began and not when each of them happened, and a clock invented per
        # row would move on every poll.
        began = getattr(activity, "started_at_ms", None)
        row: dict[str, Any] = {"role": "user", "content": prompt}
        if isinstance(began, int) and began > 0:
            row["timestamp"] = datetime.fromtimestamp(began / 1000).isoformat()
        rows.insert(0, row)
    turns = _log_turns(rows)
    for index, turn in enumerate(turns):
        turn["call_id"] = f"live-{offset + index}"
        turn["live"] = True
    return turns


def _log_turns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Instance-log rows as ``DirectTurn``s, steps included.

    ``content`` stays the text key rather than becoming ``session.resume``'s
    ``text``: this method has its own model and two clients already read
    ``content``, so aligning the name would break both to gain nothing a reader
    can see. What is new is what rides along -- the thought, the calls, and the
    id a ``tool`` row answers -- which is the half of a turn the stitched
    reading never had.

    ``call_id`` and ``at_ms`` are what the record-directory reading addressed a
    turn by. A log row has neither, so the timestamp it was written with stands
    in for the clock and the row's index for the id: both are required fields,
    and a reader sorting or keying on them still gets something monotonic.
    """
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        row = normalize_row(row)
        role = row.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        content = row.get("content")
        turn: dict[str, Any] = {
            "call_id": f"log-{index}",
            "role": role,
            "content": content if isinstance(content, str) else "",
            "at_ms": _ms_of(row.get("timestamp")),
        }
        if isinstance(row.get("reasoning_content"), str) and row["reasoning_content"].strip():
            turn["reasoning_content"] = row["reasoning_content"]
        if isinstance(row.get("tool_call_id"), str):
            turn["tool_call_id"] = row["tool_call_id"]
        calls = _wire_tool_calls(row.get("tool_calls"))
        if calls:
            turn["tool_calls"] = calls
        out.append(turn)
    return out


def _ms_of(timestamp: Any) -> int:
    """A log row's wall clock as epoch ms, or 0 when it has none.

    Rounded, not truncated: a fractional second has no exact binary form, so
    ``1.001 * 1000`` is ``1000.9999...`` and truncating loses the millisecond.
    Every row read from a log went through that.
    """
    if not isinstance(timestamp, str):
        return 0
    try:
        return round(datetime.fromisoformat(timestamp).timestamp() * 1000)
    except ValueError:
        return 0


def _paired_handle(rows: list[dict[str, Any]], agent: str, handle: str) -> str | None:
    """The ``dag-node`` record the named row was reported *with*, if any.

    Read back through ``_collapse_dag_rows`` rather than off the row's own
    ``runId``: what a client asks to forget is one row of the collapsed list,
    and the pairing that produced it is the only thing that says which status
    record would otherwise survive. Rows written before ``link_dag_node``
    existed carry no ``runId`` at all and are paired by name, so reading the
    field here would miss exactly those.

    ``None`` for the unpaired ``dag-node`` row of a stateless node, whose own
    handle is already the one being forgotten.
    """
    for row in _collapse_dag_rows(rows):
        if row.get("agent") != agent or row.get("handle") != handle:
            continue
        run_id, node_id = row.get("runId"), row.get("nodeId")
        if not isinstance(run_id, str) or not isinstance(node_id, str):
            return None
        paired = f"{run_id}/{node_id}"
        return None if paired == handle else paired
    return None


async def instances_forget(params: dict[str, Any]) -> dict[str, Any]:
    """Drop one instance's registry row.

    Both records where the row is a collapsed pair. A stateful DAG node is
    listed as one row and has to be forgotten as one: dropping only the
    addressable handle leaves the node's status record behind, and the next
    ``instances`` reports *that* on its own -- so the row the reader dismissed
    came back, unaddressable, on the following refresh.

    The record directories stay: they are the audit trail, and the handoff the
    main agent was given names paths inside them.
    """
    registry = get_registry()
    session_key = str(params.get("session_key") or "")
    agent = str(params.get("agent") or "")
    handle = str(params.get("handle") or "")

    paired = _paired_handle(registry.list_instances(session_key), agent, handle)
    removed = await registry.forget(session_key, agent, handle)
    if paired is not None:
        removed = await registry.forget(session_key, agent, paired) or removed
    return {"removed": bool(removed)}


def register_instance_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register the ``subagents.instance*`` methods on a dispatcher instance."""

    async def _list(params: dict[str, Any]) -> dict[str, Any]:
        return await instances_list(params, agent_loop_factory=agent_loop_factory)

    async def _history(params: dict[str, Any]) -> dict[str, Any]:
        return await instances_history(params, agent_loop_factory=agent_loop_factory)

    async def _create(params: dict[str, Any]) -> dict[str, Any]:
        return await instances_create(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("subagents.instances", _list)
    dispatcher.register("subagents.instance.create", _create)
    dispatcher.register("subagents.instance.history", _history)
    dispatcher.register("subagents.instance.forget", instances_forget)


__all__ = [
    "instances_create",
    "instances_forget",
    "instances_history",
    "instances_list",
    "register_instance_methods",
]
