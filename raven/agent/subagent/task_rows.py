"""A conversation's delegated work as tasks: one row per ``spawn`` call and per graph run.

``tasks.list`` answers with these rows, the Task Board page weighs its cards by
them, and the board's recall reports a linked Task's status from them -- one
reader, so none of the three can say a run still runs when another says it
stopped. They are read from the stores a task leaves behind -- the run dir, the
session node registry (``subagents/nodes.json``) and the instance registry --
and the node files, so a reload answers the same as a live page. What this
process is still running is the caller's to say: only the loop knows it, and a
reader left to guess calls a run a restart cut off still running. The design is
``docs/specs/2026-09-18-desk-tasks-list-design.md``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven.agent.subagent import activity as run_activity
from raven.agent.subagent.activity import merge_file_change
from raven.agent.subagent.dag_reader import branch_fields, current_assignment, registry_outcome
from raven.agent.subagent.dag_store import REGISTRY_FILENAME, RUNNING, UNRECORDED, node_live_key
from raven.agent.subagent.history import dag_root, nodes_root, session_history_root, spawn_live_key
from raven.agent.subagent.instances import get_registry
from raven.config.agent_names import canonical_agent_name

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

_COUNT_KEYS = (
    "pending",
    "running",
    "completed",
    "failed",
    "skipped",
    "cancelled",
    "interrupted",
    "exception",
    "not_executed",
)

# The shape `DagRunStore` mints, matched rather than trusted: a run id off a
# registry row is joined into a path here, and a row is a file anything with
# write access to the home directory can put a segment into.
_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z-[0-9a-f]{8}$")


def _read_json(path: Path) -> dict[str, Any]:
    """One JSON object off disk, or an empty one. Absent, unreadable and
    malformed are the same answer here: every caller renders a row either way."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class _NodeFiles:
    """One node's artifacts, wherever the flat namespace put them.

    A node used to own a directory and now owns a filename prefix, so what a
    reader is handed is no longer a path it can join onto. Every read goes
    through :meth:`path` for the reason the writer has one namer: the prefix
    convention lives in one place.
    """

    root: Path
    node_id: str

    def path(self, name: str) -> Path:
        return self.root / f"{self.node_id}.{name}"


def _spawn_node_ids(session_dir: Path) -> list[str]:
    """Node ids this session's spawns claimed, oldest first.

    Ordered by the registry's own clock rather than by the id: an id the model
    chose carries no time, where the minted call id it replaced sorted
    chronologically for free. Sorting the names would shuffle the panel.
    """
    registry = _read_json(session_history_root(session_dir) / REGISTRY_FILENAME)
    nodes = registry.get("nodes")
    if not isinstance(nodes, dict):
        return []
    spawned = [
        (int(entry.get("started_at_ms") or 0), node_id)
        for node_id, entry in nodes.items()
        if isinstance(entry, dict) and entry.get("kind") == "spawn" and isinstance(node_id, str)
    ]
    return [node_id for _at, node_id in sorted(spawned)]


def _read_meta(files: _NodeFiles) -> dict[str, Any]:
    meta = _read_json(files.path("meta.json"))
    if isinstance(meta.get("agent"), str):
        meta["agent"] = canonical_agent_name(meta["agent"])
    return meta


def _label_from_prompt(files: _NodeFiles) -> str:
    """First line of the prompt, for a call whose meta carries no summary.

    Read only in that case: one extra file per row would otherwise be paid on
    every poll of a panel that is polling every few seconds.
    """
    try:
        with files.path("prompt.md").open(encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()[:80]
    except OSError:
        return ""


_GRAPH_CACHE: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}


def _graph_of(session_dir: Path, run_id: str) -> dict[str, Any]:
    """One run's ``graph.json``, memoized on ``(mtime_ns, size)``.

    Read rather than copied onto the registry row: the graph is where the two
    lines were written, so a row cannot disagree with the run it belongs to, and
    the three writers that rebuild a registry record wholesale cannot drop them.
    A run's graph is written once and then changes at most once more, when a
    replan notes its successor on it, so the stamp check is what makes a
    two-second poll cost one ``stat`` per run. Only the two title fields are read
    here and both are written at init, so today the second write changes nothing
    this function returns -- the stamp matters for the reader that eventually
    wants the note, not for this one.

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


def _tool_call_count(source: dict[str, Any]) -> int | None:
    """How many tools the run called, or null where its lane never listed them.

    A lane that wrote ``tool_calls`` named every call it made, so the length is
    the count rather than an estimate; one that never wrote it leaves the field
    null. Failures are the subagent's to report, so no failure tally is derived
    here.
    """
    calls = source.get("tool_calls")
    return len(calls) if isinstance(calls, list) else None


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
    """Fill a running node's usage, tool-call count and files from the activity
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
    """The task-status ladder, first rule that matches (contract section 2.5).

    ``not_executed`` is on no rung: a branch that was not taken is not a state
    that stopped the run, so a graph whose only oddity is one reads as completed.
    """
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


def _spawn_row(files: _NodeFiles, live: set[tuple[str, str]]) -> dict[str, Any]:
    meta = _read_meta(files)
    raw_status = str(meta.get("status") or "")
    error_present = files.path("error.md").is_file()
    has_output = files.path("out.md").is_file()

    status = _SPAWN_META_STATUS.get(raw_status)
    if status is None:
        status = "failed" if error_present else ("completed" if has_output else "running")
    agent = str(meta.get("agent") or "")
    handle = str(meta.get("handle") or "")
    if status == "running" and (agent, handle) not in live:
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
    call_count = _tool_call_count(meta)

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


def spawn_rows(session_dir: Path, only_id: str | None, *, live: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """One row per spawn, or the one ``only_id`` names; ``live`` is the (agent, handle) pairs in flight."""
    root = nodes_root(session_dir)
    ids = _spawn_node_ids(session_dir)
    if only_id is not None:
        ids = [nid for nid in ids if nid == only_id]
    return [_spawn_row(_NodeFiles(root, nid), live) for nid in ids]


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
        if status in ("skipped", "not_executed"):
            # Never ran: the registry stamps it with the moment the run was
            # finalized, and the runner with the moment the branch was decided,
            # neither of which is a clock this node ever had.
            started = ended = None
        call_count = _tool_call_count(account)
        # One resolver for every reader: the registry row records who is running
        # the node while the run is in flight, the manifest once it lands, and
        # the graph is the fallback for a node that never moved.
        assignment = current_assignment(entry, by_node.get(nid) if isinstance(by_node.get(nid), dict) else None, gnode)
        agent = assignment.agent or ""
        instance = assignment.instance
        attempt = {"attempt_agents": list(assignment.attempt_agents)} if assignment.attempt_agents is not None else {}
        node: dict[str, Any] = {
            "node_id": nid,
            "node_summary": gnode.get("node_summary") or None,
            "agent": canonical_agent_name(agent) if isinstance(agent, str) else agent,
            "instance": instance,
            "status": status,
            "depends_on": list(gnode.get("depends_on") or []),
            "started_at": started,
            "ended_at": ended,
            "error": _clip((entry or {}).get("error")),
            "tokens_in": _int_or_none(account.get("tokens_in")),
            "tokens_out": _int_or_none(account.get("tokens_out")),
            "tool_call_count": call_count,
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
        # The reader `dag.get` answers from, so the two reads cannot name a branch differently.
        # Until the manifest names the node, what a branch decided is on its registry row.
        node.update(branch_fields(entry if entry is not None else registry_outcome(by_node.get(nid) or {}), gnode))
        node.update(attempt)
        if attempt:
            # Only a node that moved reports the auto flag: every node's is in the
            # manifest, and returning it for a plain run would change what every
            # existing client sees for a run that never reassigned anything.
            node["instance_auto"] = bool(assignment.instance_auto)
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


def dag_rows(
    session_dir: Path,
    session_key: str,
    only_id: str | None,
    *,
    live_runs: set[str],
    instance_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One row per graph run, or the one ``only_id`` names; ``live_runs`` is the runs in flight, and
    ``instance_rows`` stands in for reading the instance registry (see ``tasks.py``'s ``_Running``)."""
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
    if instance_rows is None:
        try:
            instance_rows = get_registry().list_instances(session_key)
        except Exception:  # noqa: BLE001 - the overlay is an improvement, not a dependency
            instance_rows = []
    nodes_dir = nodes_root(session_dir)

    rows = (_dag_row(session_dir, run_dir, registry_nodes, instance_rows, nodes_dir, live_runs) for run_dir in run_dirs)
    return [row for row in rows if row is not None]


def live_spawn_handles(loop: Any, session_key: str) -> set[tuple[str, str]]:
    """The (agent, handle) pairs ``loop`` is still running for one session; none when it cannot say.

    Duck-typed for the reason ``dag_live``'s readers are: the RPC layers hold
    whatever their factory gave them, and liveness is advisory, never fatal.
    """
    manager = getattr(loop, "subagents", None)
    if manager is None:
        return set()
    try:
        return manager.live_handles(session_key)
    except Exception:  # noqa: BLE001 - liveness is advisory, never fatal
        return set()
