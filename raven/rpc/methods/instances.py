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

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent.subagent import activity as run_activity
from raven.agent.subagent.direct_chat import direct_root
from raven.agent.subagent.instances import get_registry, reconcile_instance_rows
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

    return {
        "instances": reconcile_instance_rows(
            get_registry().list_instances(session_key),
            live_handles=(lambda key: manager.live_handles(key) if manager is not None else set()),
            # Every graph tool, not just the registered one -- see
            # ``raven.agent.subagent_dag.live``.
            active_run_ids=(lambda: live_run_ids(loop)),
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

    return {"instance": row}


async def instances_history(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """One instance's whole conversation, in the transcript wire shape.

    Preferred source is the instance's own log
    (:mod:`raven.agent.subagent.instance_log`): it is written turn by turn, in
    order, across every lane that addressed the instance, and it holds *what the
    run did on the way* -- the thought, the tool call, the tool's answer -- which
    is the half of a turn the older reading could never show.

    The fallback stitches the three record directories together and yields a
    prompt and a final output per call:

    * ``direct/<agent>/<handle>/`` -- turns typed here;
    * ``spawn/<call_id>/`` -- what the main agent already asked this instance;
    * ``mas_dag/<run>/`` -- the same, for an instance a DAG run addressed.

    All three, because an instance is nearly always *spawned* before anyone
    switches into it, and a view that showed only the direct turns would open
    empty on the exchange the user is looking at it for. It stays because a
    conversation that ran before the instance log existed has no log to read, and
    a two-message view of it beats an empty one.

    Both paths yield ``DirectTurn`` rows, so a client that only reads ``role`` and
    ``content`` is unaffected by the richer source; the step fields ride along
    for one that draws them.

    An instance with no records at all is not an error -- an unaddressed handle
    is simply empty.
    """
    session_dir = _session_dir(agent_loop_factory, str(params.get("session_key") or ""))
    if session_dir is None:
        return {"turns": []}

    agent = str(params.get("agent") or "")
    handle = str(params.get("handle") or "")

    logged = _instance_log_messages(session_dir, agent, handle)
    if logged:
        turns = _log_turns(logged)
    else:
        exchanges: list[tuple[int, list[dict[str, Any]]]] = [
            *_direct_exchanges(direct_root(session_dir, agent, handle)),
            *_spawn_exchanges(spawn_root(session_dir), agent, handle),
            *_dag_exchanges(dag_root(session_dir), agent, handle),
        ]
        exchanges.sort(key=lambda pair: pair[0])
        turns = [turn for _at, turns in exchanges for turn in turns]

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
        rows.insert(0, {"role": "user", "content": prompt})
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
    """A log row's wall clock as epoch ms, or 0 when it has none."""
    if not isinstance(timestamp, str):
        return 0
    try:
        return int(datetime.fromisoformat(timestamp).timestamp() * 1000)
    except ValueError:
        return 0


def _instance_log_messages(session_dir: Path, agent: str, handle: str) -> list[dict[str, Any]]:
    """The instance log's message rows, header skipped.

    The header is the one tagged record in the file; everything else is a message,
    which is what makes the file the same grammar as a session log. A line that
    will not parse is skipped rather than fatal, for the reason
    ``_map_to_wire`` skips a malformed stored message: one bad line must not
    empty a conversation.
    """
    from raven.agent.subagent.instance_log import transcript_path

    path = transcript_path(session_dir, agent, handle)
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle_file:
            for line in handle_file:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and "_type" not in row and row.get("role"):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _exchange(call_id: str, prompt: Path, out: Path, started: int, ended: int) -> tuple[int, list[dict[str, Any]]]:
    """One prompt/output pair as the turns the client renders.

    An exchange with no output is a turn that failed or is still running; it
    yields the prompt alone rather than being dropped, so the view shows what
    was asked either way.
    """
    turns: list[dict[str, Any]] = []
    asked = _read_text(prompt)
    answered = _read_text(out)

    if asked is not None:
        turns.append(
            {"call_id": call_id, "role": "user", "content": asked, "at_ms": started, "prompt_path": str(prompt)}
        )
    if answered is not None:
        turns.append(
            {"call_id": call_id, "role": "assistant", "content": answered, "at_ms": ended, "out_path": str(out)}
        )
    return (started, turns)


def _direct_exchanges(root: Path) -> list[tuple[int, list[dict[str, Any]]]]:
    try:
        calls = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []

    out: list[tuple[int, list[dict[str, Any]]]] = []
    for call in calls:
        meta = _read_json(call / "meta.json")
        started = int(meta.get("started_at_ms") or 0)
        out.append(
            _exchange(call.name, call / "prompt.md", call / "out.md", started, int(meta.get("ended_at_ms") or started))
        )
    return out


def _spawn_exchanges(root: Path, agent: str, handle: str) -> list[tuple[int, list[dict[str, Any]]]]:
    """The spawns that addressed this instance.

    Matched on the record's own ``agent`` / ``handle``, which the manager writes
    into every spawn's meta -- never on the directory name, which is a call id.
    """
    try:
        calls = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []

    out: list[tuple[int, list[dict[str, Any]]]] = []
    for call in calls:
        meta = _read_json(call / "meta.json")
        if meta.get("agent") != agent or meta.get("handle") != handle:
            continue
        started = int(meta.get("started_at_ms") or 0)
        out.append(
            _exchange(call.name, call / "prompt.md", call / "out.md", started, int(meta.get("ended_at_ms") or started))
        )
    return out


def _dag_exchanges(root: Path, agent: str, handle: str) -> list[tuple[int, list[dict[str, Any]]]]:
    """The DAG nodes that ran against this instance.

    A node names its handle in ``instance``; when it names none the cli backend
    falls back to the node id, which is then what the registry row carries -- so
    both have to match, or a fan-out's exchanges are invisible to the instance
    it actually bound.
    """
    try:
        runs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []

    out: list[tuple[int, list[dict[str, Any]]]] = []
    for run in runs:
        manifest = _read_json(run / "manifest.json")
        for node_id, node in manifest.items():
            if not isinstance(node, dict) or node.get("subagent") != agent:
                continue
            if (node.get("instance") or node_id) != handle:
                continue
            started = int(node.get("started_at") or 0)
            out.append(
                _exchange(
                    f"{run.name}/{node_id}",
                    Path(node.get("prompt_file") or run / f"{node_id}.prompt.md"),
                    Path(node.get("output_file") or run / f"{node_id}.out.md"),
                    started,
                    int(node.get("ended_at") or started),
                )
            )
    return out


async def instances_forget(params: dict[str, Any]) -> dict[str, Any]:
    """Drop one instance's registry row.

    The record directories stay: they are the audit trail, and the handoff the
    main agent was given names paths inside them.
    """
    removed = await get_registry().forget(
        str(params.get("session_key") or ""),
        str(params.get("agent") or ""),
        str(params.get("handle") or ""),
    )
    return {"removed": bool(removed)}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


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
