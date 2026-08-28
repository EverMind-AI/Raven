"""``subagent.*`` -- the background work one conversation handed off.

Two reads over what the delegation path already writes. A ``spawn`` records
itself in the session's own metadata directory (see
:mod:`raven.agent.subagent.history`): ``subagents/spawn/<call_id>/`` holding
``prompt.md`` written before dispatch, then ``out.md`` or ``error.md``, beside a
``meta.json``. Nothing here writes anything. The record is the audit trail the
run keeps for itself, and these two methods are the view of it the panel draws.

Graph runs are listed too, one row per node. They were deliberately left out at
first, on the grounds that ``dag.*`` already reads them behind a graph view that
shows structure a flat list cannot -- but the panel's question is "which agents
worked on this conversation", and answering it with only half of them was worse
than a little duplication. The graph is still where structure is read; this is
where a reader finds a node without knowing there was a graph. The rows carry
``run_id`` and ``node`` so opening one goes through ``dag.node``, the same reader
the graph uses, rather than a second copy of it here.

``kind`` says which of the two a row is (``spawn`` / ``dag``), because the two are
addressed differently and a client must not guess.

Singular, next to the plural ``subagents.*`` that configures which external
agents exist. The two are not variants of one name: a caller reaching for the
wrong one gets an empty list and no error, which is how these two managed to be
missing for as long as they were.

The session manager comes from the running loop when there is one, exactly as
``session.resume`` takes it. That is load-bearing rather than tidy: the group a
session's directory falls under is resolved through ``_get_session_path``, which
adopts a transcript written before project grouping existed, so a manager built
fresh here could resolve the same key to a different directory and answer with an
empty panel for a run that is on disk.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.subagent import activity as run_activity
from raven.agent.subagent.dag_live import live_run_ids
from raven.agent.subagent.history import dag_root, spawn_root
from raven.agent.subagent.instances import get_registry
from raven.agent.subagent.tool_vocabulary import normalize_row
from raven.config.loader import load_config
from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods.session import _manager_for, _map_to_wire, _safe_invoke_factory

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

    from .turn import AgentLoopFactory

_WIRE_STATUS = {
    # On the left what `SpawnRecord.finish` writes, on the right what a client
    # renders. An aborted run stopped on a terminal safety decision: it produced
    # a result explaining itself, but it did not do what it was asked, so it
    # reads as a failure rather than as a completion.
    "running": "run",
    "completed": "ok",
    "failed": "error",
    "aborted": "error",
    "cancelled": "cancelled",
}


def _iso(ms: Any) -> str | None:
    """A stored epoch-millis stamp as ISO 8601, which is what the wire carries."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _read_json(path: Path) -> dict[str, Any]:
    """One JSON object off disk, or an empty one. Absent, unreadable and
    malformed are the same answer here: every caller renders a row either way."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_meta(directory: Path) -> dict[str, Any]:
    return _read_json(directory / "meta.json")


def _outcome(directory: Path) -> tuple[str | None, bool]:
    """The answer this call produced, and whether it produced one at all.

    ``error.md`` and ``out.md`` are written by the same ``finish``, never both,
    so whichever exists is the outcome.
    """
    for name in ("out.md", "error.md"):
        path = directory / name
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace"), True
        except OSError:
            logger.warning("subagent.context: could not read {}", path)
    return None, False


def _tokens(meta: dict[str, Any]) -> int | None:
    """What this run cost, or None when the lane that ran it cannot say.

    None rather than zero, and the distinction is the point: the cli transport has
    no usage reporting at all, and a row showing "0 tokens" would be a claim about
    the agent rather than an admission about the record.
    """
    parts = [meta.get("tokens_in"), meta.get("tokens_out")]
    counted = [int(p) for p in parts if isinstance(p, (int, float))]
    return sum(counted) if counted else None


def _row(directory: Path) -> dict[str, Any]:
    meta = _read_meta(directory)
    _answer, finished = _outcome(directory)
    # Derived from the files when meta is missing or carries a status this does
    # not know: a directory that exists is a call that started, and one holding
    # its answer is a call that ended. Trusting an unreadable meta instead would
    # leave a finished run pulsing as live forever.
    status = _WIRE_STATUS.get(str(meta.get("status") or "")) or ("ok" if finished else "run")
    label = str(meta.get("task_summary") or meta.get("label") or "") or _label_from_prompt(directory)
    tools = meta.get("tool_calls")
    return {
        "id": directory.name,
        "kind": "spawn",
        "label": label,
        "status": status,
        "agent": meta.get("agent"),
        "instance": meta.get("instance"),
        "started_at": _iso(meta.get("started_at_ms")),
        "ended_at": _iso(meta.get("ended_at_ms")),
        "message_count": 2 if finished else 1,
        "tokens": _tokens(meta),
        # The count, not the names: a row shows how much happened and the
        # transcript shows what. Sending 200 tool names per row to draw one
        # number beside each would be the panel's whole payload.
        "tool_call_count": len(tools) if isinstance(tools, list) else None,
    }


def _label_from_prompt(directory: Path) -> str:
    """First line of the prompt, for a call whose meta carries no summary.

    Read only in that case: one extra file per row would otherwise be paid on
    every poll of a panel that is polling every few seconds.
    """
    try:
        with (directory / "prompt.md").open(encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()[:80]
    except OSError:
        return ""


def _call_dir(root: Path, call_id: str) -> Path | None:
    """Resolve one call id under ``root``, or None if it does not name one.

    ``call_id`` arrives off the wire. It is minted by raven (``make_call_id``),
    but that is not a reason to join it into a path unchecked: containment is
    verified after resolution, so a crafted id cannot walk out of the history
    root and hand back an arbitrary file as a transcript.
    """
    if not call_id or "/" in call_id or "\\" in call_id or call_id in {".", ".."}:
        return None
    candidate = (root / call_id).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        logger.warning("subagent.context: refused an id that escapes the history root: {!r}", call_id)
        return None
    return candidate if candidate.is_dir() else None


def _session_dir(session_id: str, agent_loop_factory: "AgentLoopFactory | None") -> Path:
    config = load_config()
    mgr = _manager_for(_safe_invoke_factory(agent_loop_factory), config)
    return mgr.session_dir(session_id)


def _root_for(session_id: str, agent_loop_factory: "AgentLoopFactory | None") -> Path:
    return spawn_root(_session_dir(session_id, agent_loop_factory))


# `manifest.json` statuses, on the left, and what a client renders on the right.
# A node that never ran reads as queued rather than as running: the graph knows
# the difference and so should the list.
_DAG_WIRE_STATUS = {
    "pending": "queued",
    "running": "run",
    "completed": "ok",
    "failed": "error",
    "skipped": "skipped",
    "cancelled": "cancelled",
    "interrupted": "error",
    "exception": "error",  # A node waiting for the caller to decide; "error" is lossy but honest about "this needs you"
}


def _live_dag_run_ids(agent_loop_factory: "AgentLoopFactory | None") -> set[str]:
    """Run ids the live DAG tool is still executing; empty when there is none.

    Tolerates every failure: no configured sub-agents means no tool, and that
    must read as "nothing is live" rather than as an error -- the caller uses
    this to decide whether a non-terminal node can still make progress.
    """
    loop = _safe_invoke_factory(agent_loop_factory)
    # Through the loop rather than the tool registry: a playbook's run is
    # dispatched by the engine's private graph tool, which is not on that
    # registry, so reading it alone reported a live run as finished.
    return live_run_ids(loop)


def _dag_rows(root: Path, session_id: str, live_runs: set[str]) -> list[dict[str, Any]]:
    """One row per node across every graph run this conversation started.

    Read straight off disk, like the spawn rows beside them, rather than through
    ``dag.get``: that one needs the live ``run_subagent_dag`` tool, which only
    exists while third-party sub-agents are configured, so a panel would lose the
    history of runs that already happened the moment the config changed.

    An in-flight run has no ``manifest.json`` yet (it is written once, at
    finalize), so its nodes read back ``pending`` off disk. The instance
    registry is overlaid for those runs -- the same reconciliation ``dag.get``
    does -- and a non-terminal node of a run nothing is executing any more is
    reported ``interrupted``: without that, a run whose gateway died mid-flight
    leaves rows saying "queued" forever.
    """
    try:
        runs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    except OSError:
        return []

    try:
        reg_rows = get_registry().list_instances(session_id)
    except Exception:  # noqa: BLE001 - the overlay is an improvement, not a dependency
        reg_rows = []
    by_run_node = {(r.get("runId"), r.get("nodeId")): r for r in reg_rows if r.get("kind") == "dag-node"}

    rows: list[dict[str, Any]] = []
    for run in runs:
        graph = _read_json(run / "graph.json")
        manifest = _read_json(run / "manifest.json")
        finalized = bool(manifest)
        live = run.name in live_runs
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                continue
            nid = node["id"]
            entry = manifest.get(nid) if isinstance(manifest.get(nid), dict) else {}
            status = str(entry.get("status") or "pending")
            if not finalized:
                reg = by_run_node.get((run.name, nid))
                if reg is not None:
                    status = str(reg.get("status") or status)
                    entry = {
                        **entry,
                        "started_at": entry.get("started_at") or reg.get("createdAtMs"),
                        "ended_at": entry.get("ended_at")
                        or (reg.get("updatedAtMs") if status not in ("pending", "running") else None),
                    }
                if status in ("pending", "running") and not live:
                    status = "interrupted"
            # A skipped node never ran: it has no transcript, no cost and no
            # clock -- a row for it pads the list with entries that open onto
            # nothing. The graph view still shows it, where "skipped because
            # its upstream failed" is legible structure rather than noise. A
            # cancelled node is the opposite case: it ran, so its row opens
            # onto a real transcript and stays.
            if status == "skipped":
                continue
            tools = entry.get("tool_calls")
            rows.append(
                {
                    # Not addressable by `id` alone -- a node id is unique inside
                    # its run and nowhere else -- so the pair travels and the id
                    # is only ever a label for the row.
                    "id": f"{run.name}/{nid}",
                    "kind": "dag",
                    "run_id": run.name,
                    "node": nid,
                    "label": str(node.get("node_summary") or "") or nid,
                    "status": _DAG_WIRE_STATUS.get(status, "run"),
                    "agent": entry.get("subagent") or node.get("subagent"),
                    "instance": entry.get("instance", node.get("instance")),
                    "started_at": _iso(entry.get("started_at")),
                    "ended_at": _iso(entry.get("ended_at")),
                    "message_count": 2 if entry.get("output_file") else 1,
                    "tokens": _tokens(entry),
                    "tool_call_count": len(tools) if isinstance(tools, list) else None,
                }
            )
    return rows


async def subagent_list(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """Every agent this conversation handed work to, newest first.

    Both kinds: one row per ``spawn`` call, and one row per node of every graph
    run. Scoped to one conversation because the record is -- the history lives
    inside that session's own directory, so there is no cross-session listing to
    give. An absent or unknown session is an empty list rather than an error: a
    conversation that has delegated nothing and one that does not exist look the
    same from here, and a panel draws the same thing for both.
    """
    session_id = str(params.get("session_id") or "")
    if not session_id:
        return {"items": []}

    try:
        session_dir = _session_dir(session_id, agent_loop_factory)
    except OSError:
        return {"items": []}

    items: list[dict[str, Any]] = []
    try:
        root = spawn_root(session_dir)
        items += [_row(d) for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)]
    except OSError:
        # No history directory yet is the ordinary case for a fresh conversation.
        pass
    items += _dag_rows(dag_root(session_dir), session_id, _live_dag_run_ids(agent_loop_factory))

    # Sorted across both kinds, because the reader's question is chronological and
    # does not distinguish them. `started_at` first; the id is the tie-break and
    # is itself time-ordered (both a call id and a run id start with a UTC stamp),
    # which is what keeps a graph's own nodes in a stable order rather than
    # shuffling on every poll -- they all share one started_at to the second.
    items.sort(key=lambda i: (i.get("started_at") or "", i.get("id") or ""), reverse=True)
    return {"items": items}


async def subagent_context(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """One call's transcript, in the shape the ordinary renderer takes.

    What was asked, what the run itself did where the transport could see it
    (the acp lane records its thoughts, tool calls and their results in
    ``transcript.jsonl``; the cli lane has no per-step visibility and keeps
    the old two-message shape), and what came back. All of it goes through
    the same wire mapping ``session.resume`` uses, so a client draws a
    delegated run with its transcript renderer instead of a second one that
    would drift.
    """
    call_id = str(params.get("id") or "")
    if not call_id:
        raise ConfigValidationError("subagent.context requires params.id", data={"field": "id"})
    # Both, and an error rather than an empty transcript when one is missing: a
    # call is addressed by (conversation, call), because the record lives inside
    # that conversation's directory. A caller that forgets the session would
    # otherwise get the same silent empty answer as a stale id, which is the
    # failure mode that kept this whole surface missing.
    session_id = str(params.get("session_id") or "")
    if not session_id:
        raise ConfigValidationError("subagent.context requires params.session_id", data={"field": "session_id"})

    try:
        directory = _call_dir(_root_for(session_id, agent_loop_factory), call_id)
    except OSError:
        directory = None
    if directory is None:
        # An id from a stale panel, or a call whose record never landed. Empty
        # rather than not-found: the caller is drawing a transcript either way.
        return {"id": call_id, "messages": [], "status": None}

    meta = _read_meta(directory)
    answer, finished = _outcome(directory)
    stored: list[dict[str, Any]] = []
    # The record keeps two clock reads: when the run started and when it ended.
    # Those ARE the two messages' times -- the prompt went in at the start, the
    # answer came back at the end -- so the transcript renderer can stamp them
    # the same way it stamps a resumed session.
    try:
        prompt_msg: dict[str, Any] = {
            "role": "user",
            "content": (directory / "prompt.md").read_text(encoding="utf-8"),
        }
        if (started := _iso(meta.get("started_at_ms"))) is not None:
            prompt_msg["timestamp"] = started
        stored.append(prompt_msg)
    except OSError:
        logger.warning("subagent.context: {} has no readable prompt", directory)
    # The run's own turns, where the transport could see them (acp writes
    # transcript.jsonl; the cli lane has no per-step visibility and leaves no
    # file). Same wire mapping as everything else here, so the client draws a
    # delegated run's tool calls with the renderer it already has.
    transcribed = False
    try:
        with (directory / "transcript.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("role"):
                    stored.append(normalize_row(entry))
                    transcribed = True
    except OSError:
        pass
    # A run in flight has no file yet -- its only account is the activity being
    # collected in this very process, so a watching panel reads that. Copied
    # entry-by-entry because the collector republishes on every update from the
    # agent, and a list mutated mid-iteration is a crash in a read-only path.
    if not transcribed and (live := run_activity.live(directory.name)) is not None:
        stored.extend(
            normalize_row(entry) for entry in list(live.transcript) if isinstance(entry, dict) and entry.get("role")
        )
    # The console tail, for the lane whose only in-flight account is its own
    # output (a cli agent streams no transcript). Gone once the run finishes:
    # the live index empties with the collecting block, and the record's answer
    # takes over.
    if (live_run := run_activity.live(directory.name)) is not None and live_run.console:
        stored.append({"role": "console", "content": live_run.console})
    if answer is not None:
        answer_msg: dict[str, Any] = {"role": "assistant", "content": answer}
        if (ended := _iso(meta.get("ended_at_ms"))) is not None:
            answer_msg["timestamp"] = ended
        stored.append(answer_msg)

    tools = meta.get("tool_calls")
    return {
        "id": call_id,
        "messages": _map_to_wire(stored, f"subagent:{call_id}"),
        "status": _WIRE_STATUS.get(str(meta.get("status") or "")) or ("ok" if finished else "run"),
        "label": str(meta.get("task_summary") or meta.get("label") or "") or _label_from_prompt(directory),
        "agent": meta.get("agent"),
        "started_at": _iso(meta.get("started_at_ms")),
        "ended_at": _iso(meta.get("ended_at_ms")),
        # What it did on the way, which is the half of a run the transcript could
        # never show: two messages in, two out, and no account of the work in
        # between. Absent when the transport that ran it has no per-step
        # visibility -- see raven/agent/subagent/activity.py.
        "tool_calls": [str(t) for t in tools] if isinstance(tools, list) else [],
        "tokens": _tokens(meta),
        "tokens_in": meta.get("tokens_in"),
        "tokens_out": meta.get("tokens_out"),
        "thought_chars": meta.get("thought_chars") or 0,
    }


def register_subagent_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    async def _list(params: dict) -> dict:
        return await subagent_list(params, agent_loop_factory=agent_loop_factory)

    async def _context(params: dict) -> dict:
        return await subagent_context(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("subagent.list", _list)
    dispatcher.register("subagent.context", _context)


__all__ = ["register_subagent_methods", "subagent_context", "subagent_list"]
