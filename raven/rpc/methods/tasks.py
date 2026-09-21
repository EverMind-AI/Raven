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

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent.subagent import activity as run_activity
from raven.agent.subagent.activity import merge_file_change
from raven.agent.subagent.dag_store import REGISTRY_FILENAME, RUNNING, UNRECORDED, node_live_key
from raven.agent.subagent.history import dag_root, nodes_root, session_history_root, spawn_live_key
from raven.agent.subagent.instances import get_registry
from raven.rpc.methods.instances import _graph_of
from raven.rpc.methods.session import _safe_invoke_factory
from raven.rpc.methods.subagent import (
    _label_from_prompt,
    _live_dag_run_ids,
    _NodeFiles,
    _read_json,
    _read_meta,
    _session_dir,
    _spawn_node_ids,
)

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

    from .turn import AgentLoopFactory

_MAX_ERROR_CHARS = 500

# A node status a run nothing is executing any more must not keep reading as
# non-terminal; the three-state rule is `read_run_reconciled`'s (`dag_resume.py`).
_NOT_LIVE_PENDING = ("pending", "running", "exception")

_SPAWN_META_STATUS = {
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "aborted": "failed",
    "cancelled": "cancelled",
}

_COUNT_KEYS = ("pending", "running", "completed", "failed", "skipped", "cancelled", "interrupted", "exception")


def _head(path: Path, limit: int) -> str | None:
    """The first ``limit`` characters of a file, or None when it does not exist."""
    try:
        if not path.is_file():
            return None
        with path.open(encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return None


def _clip(text: Any) -> str | None:
    """An in-memory string capped at the wire's error-length promise."""
    return text[:_MAX_ERROR_CHARS] if isinstance(text, str) else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _tool_counts(source: dict[str, Any]) -> tuple[int | None, int | None]:
    """``(tool_call_count, tool_failure_count)``, null where the lane never said.

    ``as_meta`` writes ``tool_failures`` only when there were some, so a lane
    that listed its calls and named no failure had zero, not an unknown number.
    """
    calls = source.get("tool_calls")
    failures = source.get("tool_failures")
    call_count = len(calls) if isinstance(calls, list) else None
    if isinstance(failures, list):
        return call_count, len(failures)
    return call_count, (0 if call_count is not None else None)


def _files_of(source: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per path, folding a pre-fold record's duplicate entries.

    A record written before ``merge_file_change`` landed in the recorder
    (raven/agent/subagent/activity.py) still holds one entry per call; folding
    here as well as there means every ``meta.json`` on disk reads the same way
    regardless of which era wrote it.
    """
    raw = source.get("files")
    if not isinstance(raw, list):
        return []
    folded: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            merge_file_change(folded, dict(entry))
    return folded


def _overlay_live(node: dict[str, Any], live: Any) -> None:
    """Fill a running node's usage, tool counts and files from the activity
    being collected for it in this process.

    The record on disk carries those only once the run finishes (``as_meta``
    is written by ``finish``), so without this a node reads as reporting
    nothing for the whole of its run. Only what the lane has said so far is
    taken: a lane that has not spoken keeps its null.
    """
    # A node that is not running has an account of its own on disk (or none);
    # an entry still in the index for it is a run this reader is not describing.
    if live is None or node["status"] != "running":
        return
    for key in ("tokens_in", "tokens_out"):
        if node[key] is None:
            node[key] = _int_or_none(getattr(live, key, None))
    calls = getattr(live, "tool_calls", None)
    if node["tool_call_count"] is None and calls:
        node["tool_call_count"] = len(calls)
        node["tool_failure_count"] = len(getattr(live, "tool_failures", None) or [])
    if not node["files"]:
        node["files"] = _files_of({"files": list(getattr(live, "files", None) or [])})


def _counts(statuses: list[str]) -> dict[str, int]:
    counts = {key: 0 for key in _COUNT_KEYS}
    for status in statuses:
        if status in counts:
            counts[status] += 1
    counts["total"] = len(statuses)
    return counts


def _task_status(statuses: list[str]) -> str:
    """The task-status ladder, first rule that matches (contract section 2.5)."""
    present = set(statuses)
    if "failed" in present:
        return "failed"
    if "interrupted" in present:
        return "interrupted"
    if present & {"pending", "running", "exception"}:
        return "running"
    if present & {"cancelled", "skipped"}:
        return "cancelled"
    return "completed"


_RUN_ID_STAMP_RE = re.compile(r"^(\d{8}T\d{6})(\d{6})Z-")


def _run_id_epoch_ms(run_id: str) -> int | None:
    """The moment a run id was minted, from its UTC prefix; None for any other id."""
    match = _RUN_ID_STAMP_RE.match(run_id)
    if match is None:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(stamp.timestamp() * 1000) + int(match.group(2)) // 1000


def _sort_key(row: dict[str, Any]) -> tuple[int, str]:
    """Newest first. A dag row no node has started yet -- the first seconds of
    every run -- is placed by the moment its id was minted, so a just-dispatched
    run heads the list instead of falling below every finished one; a spawn row
    with no clock at all sorts last. Ties break on the id."""
    started = row.get("started_at")
    if started is None and row.get("kind") == "dag":
        started = _run_id_epoch_ms(str(row["id"]))
    return (started if isinstance(started, int) else 0, row["id"])


def _manager_of(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    return getattr(_safe_invoke_factory(agent_loop_factory), "subagents", None)


def _live_spawn_handles(agent_loop_factory: "AgentLoopFactory | None", session_key: str) -> set[tuple[str, str]]:
    """The (agent, handle) pairs this gateway process is still running for one session."""
    manager = _manager_of(agent_loop_factory)
    if manager is None:
        return set()
    try:
        return manager.live_handles(session_key)
    except Exception:  # noqa: BLE001 - liveness is advisory, never fatal
        return set()


def _spawn_row(files: "_NodeFiles", agent_loop_factory: "AgentLoopFactory | None", session_key: str) -> dict[str, Any]:
    meta = _read_meta(files)
    raw_status = str(meta.get("status") or "")
    error_present = files.path("error.md").is_file()
    has_output = files.path("out.md").is_file()

    status = _SPAWN_META_STATUS.get(raw_status)
    if status is None:
        status = "failed" if error_present else ("completed" if has_output else "running")
    agent = str(meta.get("agent") or "")
    handle = str(meta.get("handle") or "")
    if status == "running" and (agent, handle) not in _live_spawn_handles(agent_loop_factory, session_key):
        status = "interrupted"

    # `.error.md` and `.out.md` are written by the same `finish`, never both --
    # except an `aborted` run, which stops on a terminal safety decision and
    # writes its explanation to `.out.md` (`ABORTED_ACTION_RESULT`), leaving no
    # `.error.md` at all.
    if error_present:
        error = _head(files.path("error.md"), _MAX_ERROR_CHARS)
    elif raw_status == "aborted":
        error = _head(files.path("out.md"), _MAX_ERROR_CHARS)
    else:
        error = None

    started_at = _int_or_none(meta.get("started_at_ms"))
    ended_at = _int_or_none(meta.get("ended_at_ms"))
    call_count, failure_count = _tool_counts(meta)

    node: dict[str, Any] = {
        "node_id": files.node_id,
        "node_summary": meta.get("task_summary") or None,
        "agent": agent,
        "instance": meta.get("handle") or None,
        "status": status,
        "depends_on": [],
        "started_at": started_at,
        "ended_at": ended_at,
        "error": error,
        "tokens_in": _int_or_none(meta.get("tokens_in")),
        "tokens_out": _int_or_none(meta.get("tokens_out")),
        "tool_call_count": call_count,
        "tool_failure_count": failure_count,
        "has_output": has_output,
        "prompt_template": None,
        "files": _files_of(meta),
    }
    _overlay_live(node, run_activity.live(spawn_live_key(files.root, files.node_id)))
    task_summary = meta.get("task_summary") or meta.get("label") or _label_from_prompt(files) or None
    return {
        "id": files.node_id,
        "kind": "spawn",
        "task_summary": task_summary,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "agent": meta.get("agent"),
        "handle": meta.get("handle"),
        "counts": _counts([status]),
        "nodes": [node],
    }


def _spawn_rows(
    session_dir: Path, agent_loop_factory: "AgentLoopFactory | None", session_key: str, only_id: str | None
) -> list[dict[str, Any]]:
    root = nodes_root(session_dir)
    ids = _spawn_node_ids(session_dir)
    if only_id is not None:
        ids = [nid for nid in ids if nid == only_id]
    return [_spawn_row(_NodeFiles(root, nid), agent_loop_factory, session_key) for nid in ids]


def _dag_node_state(
    run_id: str,
    node_id: str,
    manifest_entry: dict[str, Any] | None,
    registry_nodes: dict[str, Any],
    instance_rows_by_node: dict[str, dict[str, Any]],
) -> tuple[str, int | None, int | None]:
    """One node's status and timestamps, first source that has a value.

    Manifest (a finalized run) first, then the session's own node registry
    (which is where a hard stop's ``cancelled`` / ``skipped`` land -- the
    manifest never gets written for one), then the instance registry's
    ``dag-node`` row, then ``pending`` for a node that never got even that far.
    """
    if manifest_entry is not None:
        status = manifest_entry.get("status")
        if isinstance(status, str) and status:
            return status, _int_or_none(manifest_entry.get("started_at")), _int_or_none(manifest_entry.get("ended_at"))

    reg_entry = registry_nodes.get(node_id)
    # The registry is per session and an id can be claimed again by a later
    # run once its first owner is done, so an entry another run wrote says
    # nothing about this one.
    if isinstance(reg_entry, dict) and reg_entry.get("run_id") in (None, run_id):
        status = reg_entry.get("status")
        # `unrecorded` is a sentinel `dag_store.read_session_nodes` computes
        # for an entry with no status of its own, not a value ever written --
        # but a raw entry could still lack the key, so it is treated the same.
        # `running` here is `claim_node`'s: every node of a run is claimed at
        # start, so it says "this run owns the id", not "this node is
        # executing" -- whether it is falls to the instance row below.
        if isinstance(status, str) and status and status not in (UNRECORDED, RUNNING):
            return status, _int_or_none(reg_entry.get("started_at_ms")), _int_or_none(reg_entry.get("ended_at_ms"))

    row = instance_rows_by_node.get(node_id)
    if isinstance(row, dict):
        status = row.get("status")
        if isinstance(status, str) and status:
            ended = row.get("updatedAtMs") if status not in _NOT_LIVE_PENDING else None
            return status, _int_or_none(row.get("createdAtMs")), _int_or_none(ended)

    return "pending", None, None


def _dag_has_output(node_id: str, manifest_entry: dict[str, Any] | None, registry_entry: Any, nodes_dir: Path) -> bool:
    output_file = manifest_entry.get("output_file") if manifest_entry else None
    if output_file:
        try:
            return Path(output_file).is_file()
        except OSError:
            return False
    if isinstance(registry_entry, dict) and "has_output" in registry_entry:
        return bool(registry_entry.get("has_output"))
    return (nodes_dir / f"{node_id}.out.md").is_file()


def _replan_of(graph: dict[str, Any]) -> dict[str, Any] | None:
    """The link ``graph.json`` carries when a replan superseded this run."""
    replan = graph.get("replan")
    if not isinstance(replan, dict) or not replan.get("run_id"):
        return None
    return {
        "run_id": str(replan["run_id"]),
        "from_node": replan.get("from_node"),
        "reason": replan.get("reason"),
        "started": bool(replan.get("started")),
        "error": replan.get("error"),
    }


def _dag_row(
    session_dir: Path,
    run_dir: Path,
    registry_nodes: dict[str, Any],
    instance_rows: list[dict[str, Any]],
    nodes_dir: Path,
    live_runs: set[str],
) -> dict[str, Any] | None:
    graph = _graph_of(session_dir, run_dir.name)
    if not isinstance(graph.get("nodes"), list):
        # No readable graph, no task: a dir mid-write (init writes the graph
        # first) or a corrupt one must not read as a completed run of no steps.
        return None
    manifest = _read_json(run_dir / "manifest.json")
    live = run_dir.name in live_runs
    by_node = {
        row.get("nodeId"): row
        for row in instance_rows
        if row.get("kind") == "dag-node" and row.get("runId") == run_dir.name
    }

    graph_nodes = graph["nodes"]
    nodes: list[dict[str, Any]] = []
    statuses: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for gnode in graph_nodes:
        if not isinstance(gnode, dict) or not isinstance(gnode.get("id"), str):
            continue
        nid = gnode["id"]
        entry = manifest.get(nid) if isinstance(manifest.get(nid), dict) else None
        live_key = node_live_key(run_dir.name, nid)
        # A node that finished while its run has not has no manifest entry yet;
        # its account is what the runner set aside at the node's end.
        account = entry if entry is not None else (run_activity.settled(live_key) or {})
        status, started, ended = _dag_node_state(run_dir.name, nid, entry, registry_nodes, by_node)
        if status in _NOT_LIVE_PENDING and not live:
            status = "interrupted"
        if status == "skipped":
            # Never ran: the registry stamps it with the moment the run was
            # finalized, which is not a clock this node ever had.
            started = ended = None
        call_count, failure_count = _tool_counts(account)
        node: dict[str, Any] = {
            "node_id": nid,
            "node_summary": gnode.get("node_summary") or None,
            "agent": ((entry or {}).get("subagent") or gnode.get("subagent") or ""),
            "instance": (entry.get("instance") if entry is not None and "instance" in entry else gnode.get("instance")),
            "status": status,
            "depends_on": list(gnode.get("depends_on") or []),
            "started_at": started,
            "ended_at": ended,
            "error": _clip((entry or {}).get("error")),
            "tokens_in": _int_or_none(account.get("tokens_in")),
            "tokens_out": _int_or_none(account.get("tokens_out")),
            "tool_call_count": call_count,
            "tool_failure_count": failure_count,
            "has_output": _dag_has_output(nid, entry, registry_nodes.get(nid), nodes_dir),
            "prompt_template": gnode.get("prompt_template") or None,
            "files": _files_of(account),
        }
        if gnode.get("inputs") is not None:
            node["inputs"] = gnode["inputs"]
        if gnode.get("skills") is not None:
            node["skills"] = list(gnode["skills"])
        if gnode.get("mcps") is not None:
            node["mcps"] = list(gnode["mcps"])
        _overlay_live(node, run_activity.live(live_key))
        nodes.append(node)
        statuses.append(status)
        if isinstance(started, int):
            starts.append(started)
        if isinstance(ended, int):
            ends.append(ended)

    replan = _replan_of(graph)
    if replan is not None:
        # `_apply_replan` marks the exception node(s) `failed` before
        # `_finalize` runs, so the ladder below would already read this row
        # `failed` -- but a replan that started reads as a hand-off, not a
        # failure, and one that never started names why in `replan.error`.
        task_status = "cancelled" if replan["started"] else "failed"
    else:
        task_status = _task_status(statuses)

    row: dict[str, Any] = {
        "id": run_dir.name,
        "kind": "dag",
        "task_summary": graph.get("task_summary") or None,
        "status": task_status,
        "started_at": min(starts) if starts else None,
        "ended_at": max(ends) if ends and task_status != "running" else None,
        "agent": None,
        "handle": None,
        "counts": _counts(statuses),
        "nodes": nodes,
    }
    if replan is not None:
        row["replan"] = replan
    return row


def _dag_rows(
    session_dir: Path,
    session_key: str,
    agent_loop_factory: "AgentLoopFactory | None",
    only_id: str | None,
) -> list[dict[str, Any]]:
    try:
        run_dirs = sorted(
            (p for p in dag_root(session_dir).iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True
        )
    except OSError:
        return []
    if only_id is not None:
        run_dirs = [p for p in run_dirs if p.name == only_id]
    if not run_dirs:
        return []

    registry = _read_json(session_history_root(session_dir) / REGISTRY_FILENAME)
    registry_nodes = registry.get("nodes") if isinstance(registry.get("nodes"), dict) else {}
    try:
        instance_rows = get_registry().list_instances(session_key)
    except Exception:  # noqa: BLE001 - the overlay is an improvement, not a dependency
        instance_rows = []
    live_runs = _live_dag_run_ids(agent_loop_factory)
    nodes_dir = nodes_root(session_dir)

    rows = (_dag_row(session_dir, run_dir, registry_nodes, instance_rows, nodes_dir, live_runs) for run_dir in run_dirs)
    return [row for row in rows if row is not None]


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

    try:
        session_dir = _session_dir(session_key, agent_loop_factory)
    except OSError:
        return {"tasks": []}

    kind = params.get("kind")
    want = kind if kind in ("spawn", "dag") else None
    raw_id = params.get("id")
    task_id = str(raw_id) if want is not None and raw_id else None

    tasks: list[dict[str, Any]] = []
    if want in (None, "spawn"):
        tasks.extend(_spawn_rows(session_dir, agent_loop_factory, session_key, task_id if want == "spawn" else None))
    if want in (None, "dag"):
        tasks.extend(_dag_rows(session_dir, session_key, agent_loop_factory, task_id if want == "dag" else None))

    tasks.sort(key=_sort_key, reverse=True)
    return {"tasks": tasks}


def register_tasks_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    async def _list(params: dict) -> dict:
        return await tasks_list(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("tasks.list", _list)


__all__ = ["register_tasks_methods", "tasks_list"]
