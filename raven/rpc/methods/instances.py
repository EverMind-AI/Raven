"""``subagents.instance*`` RPC handlers: the sub-agent instances a session used.

A different noun from ``methods/subagents.py``, which configures *which*
sub-agents exist. This group is about the instances a session has actually
talked to -- the things the direct-chat surface addresses.

Every read here goes through ``raven.agent.subagent.instances`` (the registry
and ``reconcile_instance_rows``) and the record directories the runtime writes,
rather than deriving a second answer here, so the TUI and the served page cannot
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
``cancelled`` and the registry row follows. A host where a spawn is a
background task with no turn to cancel reaches it over the wire through
``subagent.cancel_instance``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent.subagent import activity as run_activity
from raven.agent.subagent.dag_live import live_run_ids
from raven.agent.subagent.direct_chat import direct_root
from raven.agent.subagent.history import dag_root, spawn_root
from raven.agent.subagent.instance_log import instance_title, message_rows
from raven.agent.subagent.instance_records import stitched_turns
from raven.agent.subagent.instances import get_registry, reconcile_instance_rows
from raven.agent.subagent.tool_vocabulary import normalize_row
from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods.session import _wire_tool_calls

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


# The shape `DagRunStore` mints, matched rather than trusted: a run id off a
# registry row is joined into a path here, and a row is a file anything with
# write access to the home directory can put a segment into.
_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z-[0-9a-f]{8}$")


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
    could drift (see ``SubagentManager.session_dir_for``).
    """
    manager = _manager(agent_loop_factory)
    if manager is None:
        return None
    try:
        return manager.session_dir_for(session_key)
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


_GRAPH_CACHE: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}


def _graph_of(session_dir: Path, run_id: str) -> dict[str, Any]:
    """One run's ``graph.json``, memoized on ``(mtime_ns, size)``.

    Read rather than copied onto the registry row: the graph is where the two
    lines were written, so a row cannot disagree with the run it belongs to, and
    the three writers that rebuild a registry record wholesale cannot drop them.
    A run's graph is written once and then changes at most once more, when a
    replan notes its successor on it, so the stamp check is what makes a
    two-second poll cost one ``stat`` per run. It is load-bearing rather than
    belt-and-braces: without it that note would never be seen here.

    ``{}`` for a run whose dir is gone or whose id is not one -- a title is not
    worth failing a list over, and the caller falls back to the handle.
    """
    if not _RUN_ID_RE.match(run_id):
        return {}
    path = dag_root(session_dir) / run_id / "graph.json"
    try:
        st = path.stat()
    except OSError:
        _GRAPH_CACHE.pop(path, None)
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _GRAPH_CACHE.get(path)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(graph, dict):
        graph = {}
    _GRAPH_CACHE[path] = (stamp, graph)
    return graph


def _mark_titles(rows: list[dict[str, Any]], session_dir: Path | None) -> list[dict[str, Any]]:
    """Say what each row was dispatched for, and what dispatched it.

    Answered here rather than left to each front end, for the reason
    ``_mark_resumable`` is: four surfaces draw this list, and a join each of
    them wrote separately is four chances to join differently. Both lines exist
    already -- a node's ``node_summary`` and a graph's ``task_summary`` are
    required of the model -- so nothing here invents a title; it carries one.

    ``runTitle`` is absent rather than empty for a row that came from no graph,
    which is what lets a reader use its presence as the test for "this row has a
    source" instead of re-deriving that from ``runId``.
    """
    if session_dir is None:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        run_id, node_id = row.get("runId"), row.get("nodeId")
        title, run_title = "", ""
        if isinstance(run_id, str) and isinstance(node_id, str):
            graph = _graph_of(session_dir, run_id)
            run_title = str(graph.get("task_summary") or "")
            for node in graph.get("nodes") or []:
                if isinstance(node, dict) and node.get("id") == node_id:
                    title = str(node.get("node_summary") or "")
                    break
        if not title:
            # Not only the fallback for a spawn: a graph node whose own header
            # holds the line still answers here when its graph.json predates the
            # field or its run dir has been cleared.
            try:
                title = instance_title(session_dir, str(row.get("agent") or ""), str(row.get("handle") or ""))
            except Exception:  # noqa: BLE001 - a missing title never fails a list
                title = ""
        out.append({**row, **({"title": title} if title else {}), **({"runTitle": run_title} if run_title else {})})
    return out


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


def _drop_nodes_that_never_ran(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Leave out the graph nodes that were declared and never dispatched.

    A graph declares every node up front, and the runner writes a registry row
    for one the moment it reaches a terminal status -- including the nodes it
    never reached at all. ``skipped`` is exactly that state and says so in the
    runner's own words: ``_mark_stopped`` turns a ``pending`` node into a
    ``skipped`` one because "a `pending` one was never dispatched, which is what
    `skipped` means everywhere else", and ``_cascade_failures`` skips whatever
    depended on a node that failed.

    Such a node has no conversation, no transcript and no handle of its own, so
    a list of the instances a conversation has used is the wrong place for it:
    one failure at the top of a graph filled the panel with rows that never did
    anything. The graph sheet still draws them, out of the graph, where a node
    that did not run is worth seeing next to the one that stopped it.

    ``cancelled`` is deliberately kept. The runner distinguishes the two -- a
    cancelled node "already has a real start time from its dispatch" -- so it
    ran, however briefly, and what it managed to do before the stop is a real
    part of the conversation's history.

    Applied BEFORE ``_collapse_dag_rows``, not after. That helper pairs a legacy
    handle row -- one written before nodes carried a ``nodeId`` -- by name, and
    refuses to guess when two nodes answer to it. A skipped node makes that
    second answer: with one completed ``synthesize`` and one skipped
    ``synthesize``, the completed run's handle row paired with neither, so the
    one invocation came back twice -- once un-attributed and once as the node --
    and the skipped row was dropped only afterwards, too late to stop having
    caused it. Filtered first, it never takes part in the pairing.
    """
    return [r for r in rows if not (r.get("kind") == "dag-node" and r.get("status") == "skipped")]


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
        # ``raven.agent.subagent.dag_live``.
        active_run_ids=(lambda: live_run_ids(loop)),
    )
    return {
        "instances": _mark_titles(
            _mark_resumable(_collapse_dag_rows(_drop_nodes_that_never_ran(reconciled)), manager),
            _session_dir(agent_loop_factory, session_key),
        ),
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

    # Marked like the listing is: the client keeps drawing this one row until
    # its next `subagents.instances` refresh, and a row without `resumable`
    # falls off every surface that filters on it the moment it stops being the
    # active conversation.
    return {"instance": _mark_resumable([row], manager)[0]}


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
    elif turns and turns[-1].get("role") == "user":
        # The mirror of the pop above: a trailing prompt with no reply and
        # nothing in flight is a turn that died with its session. Marked, so a
        # reader can say so instead of leaving the question hanging.
        turns[-1] = {**turns[-1], "interrupted": True}
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
        if row.get("steer") is True:
            turn["steer"] = True
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


async def instances_forget(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """Drop one instance's registry row.

    Both records where the row is a collapsed pair. A stateful DAG node is
    listed as one row and has to be forgotten as one: dropping only the
    addressable handle leaves the node's status record behind, and the next
    ``instances`` reports *that* on its own -- so the row the reader dismissed
    came back, unaddressable, on the following refresh.

    The record directories stay: they are the audit trail, and the handoff the
    main agent was given names paths inside them.

    For an acp instance whose agent advertises ``sessionCapabilities.delete``
    the agent is told to drop the session too (``session/delete``,
    best-effort): the row and the agent's own store are two copies of the same
    binding. What the agent does with the delete is its own implementation --
    raven's deletes the stored conversation and cancels a running turn first --
    so forgetting a busy instance stops its turn. The directories here stay
    either way.

    Any mode override held against the handle goes with the row. The manager
    keys it by ``(session_key, agent, handle)`` and a handle is reusable, so
    leaving it behind would silently put a later instance of the same name at
    an effort level nobody chose for it.
    """
    registry = get_registry()
    session_key = str(params.get("session_key") or "")
    agent = str(params.get("agent") or "")
    handle = str(params.get("handle") or "")

    rows = registry.list_instances(session_key)
    row = next((r for r in rows if r.get("agent") == agent and r.get("handle") == handle), None)
    paired = _paired_handle(rows, agent, handle)
    removed = await registry.forget(session_key, agent, handle)
    if paired is not None:
        removed = await registry.forget(session_key, agent, paired) or removed
    if removed:
        manager = _manager(agent_loop_factory)
        if manager is not None:
            manager.set_instance_mode(session_key, agent, handle, None)
            if paired is not None:
                manager.set_instance_mode(session_key, agent, paired, None)
    if removed and row is not None and row.get("kind") == "acp" and row.get("agentId"):
        await _delete_acp_session(agent, str(row["agentId"]))
    return {"removed": bool(removed)}


async def _delete_acp_session(agent: str, session_id: str) -> None:
    """Tell the agent its session is gone. Best-effort, and never a launch.

    Imported here rather than at module level, so the acp pool -- and the
    subprocess layer it drags in -- stays out of the rpc import graph.
    """
    from raven.acp_client.pool import get_pool

    await get_pool().delete_session(agent, session_id)


async def instances_steer(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """Merge text into the turn one instance is running (``subagents.instance.steer``).

    The status is the manager's word (see ``SubagentManager.steer_instance``);
    a session with no live loop answers ``no_turn``, the same way the reads here
    degrade to empty: nothing is running that could be steered.
    """
    manager = _manager(agent_loop_factory)
    text = str(params.get("text") or "")
    if manager is None or not text.strip():
        return {"status": "no_turn"}
    status = await manager.steer_instance(
        str(params.get("session_key") or ""), str(params.get("agent") or ""), str(params.get("handle") or ""), text
    )
    return {"status": status}


async def instances_set_mode(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """Put one instance in an operating mode (``subagents.instance.set_mode``).

    The one thing that outranks the session's tier, and a person sets it: a
    direct chat is a continuation, so the effort level has to be changeable
    without abandoning the conversation to start a new one at a different level
    -- which is the only thing the roster could offer while the modes were
    separate agents. Nothing a dispatch names competes with it; the spawn tool
    offers no mode at all.

    Answers with this instance's own override, what it inherits when it has none,
    and the menu both came from, so a caller can render the control from this one
    reply. The two are reported apart rather than collapsed into an effective
    mode: collapsing them would make ``clear`` unobservable, since a cleared
    instance and one explicitly set to the session's tier would read identically.
    An unknown mode is the manager's
    ``ValueError``, surfaced as a refusal naming what the agent does offer,
    never a silent no-op that would leave the next turn at the old effort with
    the UI showing the new one.

    Three calls, told apart by which fields are present rather than by a
    sentinel mode id -- an agent is free to call one of its own modes
    "default", and a sentinel would take that name away from it:

    - neither field: **report** the override, what is inherited, and what is on offer;
    - ``clear: true``: drop the override. The session's tier is then what the next
      dispatch runs at, clamped to this agent's menu; only where there is no tier
      to inherit does the agent's own default run;
    - ``mode: "<id>"``: switch, from the instance's next turn on.
    """
    manager = _manager(agent_loop_factory)
    agent = str(params.get("agent") or "")
    handle = str(params.get("handle") or "")
    session_key = str(params.get("session_key") or "")
    raw = params.get("mode")
    mode = str(raw) if isinstance(raw, str) and raw else None
    reading = mode is None and not params.get("clear")
    if manager is None:
        # A read degrades to empty, as every read in this module does. A write
        # does not: answering a set with "no modes" is the silent no-op the
        # docstring above argues against, and the caller would render the mode
        # it asked for over an override that was never recorded.
        if reading:
            return {"mode": None, "inherited": None, "availableModes": []}
        raise ConfigValidationError("sub-agents are not configured, so an instance has no mode to set")
    if reading:
        return {
            "mode": manager.instance_mode(session_key, agent, handle),
            # What runs when the override is absent. Reported alongside it because
            # `mode: null` alone reads as "the agent's own default", which stopped
            # being true the moment a session tier could be inherited here.
            "inherited": manager.resolve_mode(session_key, agent, None),
            "availableModes": [
                {"id": m.id, "name": m.name, "description": m.description} for m in manager.agent_modes(agent)
            ],
        }
    rows = get_registry().list_instances(session_key)
    if not any(r.get("agent") == agent and r.get("handle") == handle for r in rows):
        # Checked only on the write path: a mode is held per instance, so one
        # set against a handle that does not exist is stored where nothing will
        # ever read it and echoed back as if it had landed.
        raise ConfigValidationError(f"no instance {agent}/{handle} in this session")
    try:
        applied = manager.set_instance_mode(session_key, agent, handle, mode)
    except ValueError as exc:
        # The same code a rejected config value gets: this is a value the
        # runtime will not accept, and flattening it to an internal error
        # would leave a person retrying something that keeps failing.
        raise ConfigValidationError(str(exc)) from exc
    return {
        "mode": applied,
        "inherited": manager.resolve_mode(session_key, agent, None),
        "availableModes": [
            {"id": m.id, "name": m.name, "description": m.description} for m in manager.agent_modes(agent)
        ],
    }


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

    async def _steer(params: dict[str, Any]) -> dict[str, Any]:
        return await instances_steer(params, agent_loop_factory=agent_loop_factory)

    async def _forget(params: dict[str, Any]) -> dict[str, Any]:
        return await instances_forget(params, agent_loop_factory=agent_loop_factory)

    async def _set_mode(params: dict[str, Any]) -> dict[str, Any]:
        return await instances_set_mode(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("subagents.instances", _list)
    dispatcher.register("subagents.instance.create", _create)
    dispatcher.register("subagents.instance.history", _history)
    dispatcher.register("subagents.instance.forget", _forget)
    dispatcher.register("subagents.instance.steer", _steer)
    dispatcher.register("subagents.instance.set_mode", _set_mode)


__all__ = [
    "instances_create",
    "instances_forget",
    "instances_history",
    "instances_list",
    "instances_set_mode",
    "instances_steer",
    "register_instance_methods",
]
