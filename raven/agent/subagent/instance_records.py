"""Read an instance's conversation out of its three record directories.

The older of the two answers to "what did this instance say". The other is the
instance log (:mod:`raven.agent.subagent.instance_log`), which every lane appends
to as a turn lands and which ``subagents.instance.history`` prefers. A
conversation that ran before that file existed has no log, and stitching these
directories together is the only account of it there is.

It lives here rather than inside the RPC handler because it is a second source
for one question, and naming it is the first step to being able to remove it. The
second step is not a migration that writes these records into logs: ``finish``
writes a record's terminal metadata and appends to the log as two separate
statements, so no reader outside that process can distinguish a finished record
from one whose rows are already written, and a migration that guesses puts the
turn in an append-only file twice. That needs the two writes to become one
critical section -- for which this repository already has the idiom, the
fcntl-locked sibling file that ``MemoryStore.locked`` uses.

Every read here tolerates a missing or unreadable file rather than raising: a
record directory is an audit trail written by a run that may have been killed
half way, and one unreadable call must not cost the reader the other nine.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from raven.agent.subagent.dag_store import REGISTRY_FILENAME
from raven.agent.subagent.history import node_file_in
from raven.config.agent_names import canonical_agent_name

__all__ = [
    "dag_exchanges",
    "direct_exchanges",
    "spawn_exchanges",
    "stitched_turns",
]


def _same_agent(stored: Any, requested: str) -> bool:
    return isinstance(stored, str) and canonical_agent_name(stored) == canonical_agent_name(requested)


def stitched_turns(
    direct: Path,
    dag: Path,
    nodes: Path,
    agent: str,
    handle: str,
    *,
    assignments: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every turn the record directories hold for one instance, oldest first.

    The three lanes are read together because an instance's conversation is
    spread across all of them: the same handle can be dispatched by ``spawn``,
    run as a DAG node, and then direct-chatted. Sorted by when each call
    started, which is the only clock the three have in common. ``nodes`` is the
    flat node root both delegation lanes now write into -- only ``direct``
    still owns a directory per call.

    ``assignments`` maps ``(run id, node id)`` to that node's ``dag-node``
    registry row, for the nodes that were reassigned. It is what lets this
    reader resolve the current attempt the same way the RPC listings do; without
    it the graph's submitted agent is the only name on disk.
    """
    exchanges: list[tuple[int, list[dict[str, Any]]]] = [
        *direct_exchanges(direct),
        *spawn_exchanges(nodes, agent, handle),
        *dag_exchanges(dag, nodes, agent, handle, assignments=assignments),
    ]
    exchanges.sort(key=lambda pair: pair[0])
    return [turn for _at, turns in exchanges for turn in turns]


def _exchange(
    call_id: str, prompt: Path | None, out: Path | None, started: int, ended: int
) -> tuple[int, list[dict[str, Any]]]:
    """One prompt/output pair as the turns the client renders.

    An exchange with no output is a turn that failed or is still running; it
    yields the prompt alone rather than being dropped, so the view shows what
    was asked either way.

    ``None`` for either path is a caller saying the file does not exist, which
    is different from a path that happens to be missing: a finalized manifest
    records ``None`` for a node that failed before it rendered, and guessing a
    path for that reads whatever later task took the id back.
    """
    turns: list[dict[str, Any]] = []
    asked = _read_text(prompt) if prompt is not None else None
    answered = _read_text(out) if out is not None else None

    if asked is not None:
        turns.append(
            {"call_id": call_id, "role": "user", "content": asked, "at_ms": started, "prompt_path": str(prompt)}
        )
    if answered is not None:
        turns.append(
            {"call_id": call_id, "role": "assistant", "content": answered, "at_ms": ended, "out_path": str(out)}
        )
    return (started, turns)


def direct_exchanges(root: Path) -> list[tuple[int, list[dict[str, Any]]]]:
    """The direct-chat calls under this instance's own directory.

    No agent/handle test: the path already names them.
    """
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


def spawn_exchanges(nodes: Path, agent: str, handle: str) -> list[tuple[int, list[dict[str, Any]]]]:
    """The spawns that addressed this instance.

    Matched on the record's own ``agent`` / ``handle``, which the manager
    writes into every spawn's meta -- never on the id, which the model chose
    for what the task produces and which says nothing about who ran it.

    Which ids belong to a spawn comes from the registry beside the flat node
    root: both surfaces write their artifacts into that one directory, so the
    files alone cannot tell a spawn from a graph node.
    """
    registry = _read_json(nodes.parent / REGISTRY_FILENAME).get("nodes")
    if not isinstance(registry, dict):
        return []

    out: list[tuple[int, list[dict[str, Any]]]] = []
    for node_id, entry in registry.items():
        if not isinstance(node_id, str) or not isinstance(entry, dict) or entry.get("kind") != "spawn":
            continue
        meta = _read_json(node_file_in(nodes, node_id, "meta.json"))
        if not _same_agent(meta.get("agent"), agent) or meta.get("handle") != handle:
            continue
        started = int(meta.get("started_at_ms") or entry.get("started_at_ms") or 0)
        ended = int(meta.get("ended_at_ms") or entry.get("ended_at_ms") or started)
        out.append(
            _exchange(
                node_id,
                node_file_in(nodes, node_id, "prompt.md"),
                node_file_in(nodes, node_id, "out.md"),
                started,
                ended,
            )
        )
    return out


def _graph_nodes(run: Path, nodes_root: Path) -> dict[str, Any]:
    """A manifest-shaped view of ``graph.json``, for a run that never finalized.

    The manifest is written when a run finalizes, so a run killed mid-flight
    holds only its ``graph.json`` -- reading nothing left every node of a dead
    run invisible here, the finished ones included. The graph knows each node's
    agent and instance but no clocks: a node's prompt file lives at
    ``<nodes_root>/<node>.prompt.md``, the same flat convention
    :class:`~raven.agent.subagent.dag_store.DagRunStore` writes to, and its own
    mtime is the only reading of when the node started.
    """
    graph = _read_json(run / "graph.json")
    nodes: dict[str, Any] = {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        node_id = str(node["id"])
        try:
            started = int(node_file_in(nodes_root, node_id, "prompt.md").stat().st_mtime * 1000)
        except OSError:
            started = 0
        nodes[node_id] = {
            "subagent": node.get("subagent"),
            "instance": node.get("instance"),
            "started_at": started,
            "ended_at": started,
            # Named here rather than derived by the reader: this is the one
            # place that knows the run never finalized, so the flat root is
            # the only honest answer for it. Every other entry comes from a
            # manifest, whose paths are authoritative even when they are null.
            "prompt_file": str(node_file_in(nodes_root, node_id, "prompt.md")),
            "output_file": str(node_file_in(nodes_root, node_id, "out.md")),
        }
    return nodes


def _named(value: Any) -> Path | None:
    """A manifest's recorded path, or ``None`` when it records that there is none."""
    return Path(value) if isinstance(value, str) and value else None


def _graph_node(run: Path, node_id: str) -> dict[str, Any]:
    """The node as ``graph.json`` submitted it, or ``{}`` when the run holds none.

    The original identity is where a reassigned node's archived attempts belong:
    the manifest (and the registry row) name whichever agent ran it last.
    """
    graph = _read_json(run / "graph.json")
    for node in graph.get("nodes") or []:
        if isinstance(node, dict) and node.get("id") == node_id:
            return node
    return {}


def _archived_exchange(
    call_id: str, run: Path, node_id: str, nodes_root: Path, attempt: int, fallback_at: int
) -> tuple[int, list[dict[str, Any]]] | None:
    """One attempt's archived prompt and output, or ``None`` when neither is on disk.

    Exact artifacts by node and attempt -- the only honest answer for an agent
    that ran an earlier attempt of a node someone else finished. No instance is
    invented for them: the archive is addressed the way the runner wrote it.
    """
    prompt = node_file_in(nodes_root, f"{node_id}.attempt-{attempt}", "prompt.md")
    out = node_file_in(nodes_root, f"{node_id}.attempt-{attempt}", "out.md")
    if not prompt.is_file() and not out.is_file():
        return None
    started = _mtime_ms(prompt) or fallback_at
    ended = _mtime_ms(out) or started
    turns = _exchange(call_id, prompt if prompt.is_file() else None, out if out.is_file() else None, started, ended)[1]
    return (started, turns) if turns else None


def _mtime_ms(path: Path) -> int:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return 0


def dag_exchanges(
    root: Path,
    nodes_root: Path,
    agent: str,
    handle: str,
    *,
    assignments: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> list[tuple[int, list[dict[str, Any]]]]:
    """The DAG nodes that ran against this instance.

    A node names its handle in ``instance``; when it names none the cli backend
    falls back to the node id, which is then what the registry row carries -- so
    both have to match, or a fan-out's exchanges are invisible to the instance
    it actually bound.

    ``nodes_root`` is where a node's prompt and output live, and it is
    ``_graph_nodes`` that applies it: whichever entry source answers, its
    recorded paths are taken as they stand. A finalized manifest is
    authoritative even where it records ``None`` -- that is a node which failed
    before it rendered, and deriving a path for it reaches whatever later task
    took the id back, answering for a node that provably wrote nothing.

    ``assignments`` is the registry record of a node that was reassigned. The
    latest flat artifact then belongs to the agent running it *now*, so the
    graph's submitted agent must not be given it merely because the graph named
    it. What that agent did run -- its earlier attempts -- is reachable by node
    and attempt, and only when the graph's own handle names the instance being
    asked about: without that, there is no record of which old instance owned
    those attempts, and guessing one would attach them to the wrong conversation.
    """
    try:
        runs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []

    out: list[tuple[int, list[dict[str, Any]]]] = []
    for run in runs:
        manifest = _read_json(run / "manifest.json") or _graph_nodes(run, nodes_root)
        for node_id, node in manifest.items():
            if not isinstance(node, dict):
                continue
            record = (assignments or {}).get((run.name, node_id))
            current_agent = node.get("subagent") if record is None else record.get("agent")
            current_instance = node.get("instance") if record is None else record.get("nodeInstance")
            if _same_agent(current_agent, agent) and (current_instance or node_id) == handle:
                started = int(node.get("started_at") or 0)
                out.append(
                    _exchange(
                        f"{run.name}/{node_id}",
                        _named(node.get("prompt_file")),
                        _named(node.get("output_file")),
                        started,
                        int(node.get("ended_at") or started),
                    )
                )
                continue
            if record is None:
                continue
            history = [a for a in record.get("attemptAgents") or [] if isinstance(a, str)]
            if not history:
                continue
            submitted = _graph_node(run, node_id)
            if not _same_agent(submitted.get("subagent"), agent) or (submitted.get("instance") or node_id) != handle:
                continue
            for attempt, attempt_agent in enumerate(history, start=1):
                if not _same_agent(attempt_agent, agent):
                    continue
                archived = _archived_exchange(
                    f"{run.name}/{node_id}#{attempt}",
                    run,
                    node_id,
                    nodes_root,
                    attempt,
                    int(node.get("started_at") or 0),
                )
                if archived is not None:
                    out.append(archived)
    return out


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
