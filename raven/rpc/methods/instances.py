"""``subagents.instance*`` RPC handlers: the sub-agent instances a session used.

A different noun from ``methods/subagents.py``, which configures *which*
sub-agents exist. This group is about the instances a session has actually
talked to -- the things the direct-chat surface addresses.

Every read here goes through something already shared with the web RPC (the
instance registry, ``reconcile_instance_rows``, the record directories the
runtime writes) rather than deriving a second answer, so the two surfaces cannot
come to disagree about what an instance is or which ones are still alive.

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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent.subagent.direct_chat import direct_root
from raven.agent.subagent.instances import get_registry, reconcile_instance_rows
from raven.agent.subagent_history import dag_root, spawn_root

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
    tools = getattr(loop, "tools", None)
    dag_tool = tools.get("run_subagent_dag") if tools is not None else None
    handoff = getattr(loop, "_direct_handoff", None)
    session_key = str(params.get("session_key") or "")

    return {
        "instances": reconcile_instance_rows(
            get_registry().list_instances(session_key),
            live_handles=(lambda key: manager.live_handles(key) if manager is not None else set()),
            active_run_ids=(lambda: set(dag_tool.active_run_ids()) if dag_tool is not None else set()),
        ),
        "pending_handoff_count": handoff.pending_count(session_key) if handoff is not None else 0,
    }


async def instances_history(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """One instance's direct chat, flattened to alternating user/assistant turns.

    Three sources, merged and ordered by when each exchange started:

    * ``direct/<agent>/<handle>/`` -- turns typed here;
    * ``spawn/<call_id>/`` -- what the main agent already asked this instance;
    * ``mas_dag/<run>/`` -- the same, for an instance a DAG run addressed.

    All three, because an instance is nearly always *spawned* before anyone
    switches into it, and a view that showed only the direct turns would open
    empty on the exchange the user is looking at it for. Read from the record
    directories rather than from ``messages.json``: the records exist for every
    kind, while the state file is absent for a ``cli`` instance and carries tool
    turns the user never typed.

    An instance with no records at all is not an error -- an unaddressed handle
    is simply empty.
    """
    session_dir = _session_dir(agent_loop_factory, str(params.get("session_key") or ""))
    if session_dir is None:
        return {"turns": []}

    agent = str(params.get("agent") or "")
    handle = str(params.get("handle") or "")

    exchanges: list[tuple[int, list[dict[str, Any]]]] = [
        *_direct_exchanges(direct_root(session_dir, agent, handle)),
        *_spawn_exchanges(spawn_root(session_dir), agent, handle),
        *_dag_exchanges(dag_root(session_dir), agent, handle),
    ]
    exchanges.sort(key=lambda pair: pair[0])
    return {"turns": [turn for _at, turns in exchanges for turn in turns]}


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

    dispatcher.register("subagents.instances", _list)
    dispatcher.register("subagents.instance.history", _history)
    dispatcher.register("subagents.instance.forget", instances_forget)


__all__ = [
    "instances_forget",
    "instances_history",
    "instances_list",
    "register_instance_methods",
]
