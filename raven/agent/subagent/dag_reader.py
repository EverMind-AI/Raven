"""Read a finished or in-flight DAG run back out of its run dir and node root.

The live ``dag_*`` progress events are the only thing the web UI sees while a
run executes, and they exist for exactly one page-session: nothing replays them,
and a gateway-mode tool result carries no manifest metadata. The run dir and
flat node root written by :class:`~raven.agent.subagent.dag_store.DagRunStore`
are the durable record, so reading them back is what lets a reloaded page show
the graph again -- and what lets a node's rendered prompt and full output be
shown on demand instead of only the leaf ``terminal_outputs`` the manifest
inlines.

An in-flight run has a ``graph.json`` but no ``manifest.json`` yet (that is
written once, in ``_finalize``), so structure and per-node state are read
separately: structure always from ``graph.json``, state from ``manifest.json``
when it exists and from the caller's overlay (the instance registry) when it
does not.

A replanned run also carries a reserved ``replan`` key there, naming the run that
replaced it; it is not part of ``SubAgentDagSpec`` and must not be, that model being
``extra="forbid"``. Readers here take the keys they want and ignore it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from raven.agent.subagent.dag_store import memory_path_in, output_path_in, prompt_path_in

# Both ids are minted by raven itself (``make_run_id`` / the graph schema's
# ``_ID_PATTERN``), but they arrive here straight off a web request, so they
# are re-checked before being joined into a path. Without this a crafted id
# would walk out of the run dir or the flat node root and read arbitrary files.
# The six optional digits are the microseconds `history_stamp` added so two
# runs minted in one second keep their order. Both forms are accepted: run
# dirs written before that change are still on disk and still readable.
_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z-[0-9a-f]{8}$")
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class DagReadError(ValueError):
    """A run/node id is malformed, or the run dir does not exist."""


def _check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise DagReadError(f"invalid run id: {run_id!r}")
    return run_id


def _check_node_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not _NODE_ID_RE.match(node_id):
        raise DagReadError(f"invalid node id: {node_id!r}")
    return node_id


async def _read_json(backend: Any, path: str) -> Any | None:
    """Parse a JSON file in the backend, returning None if absent or corrupt."""
    if not await backend.file_exists(path):
        return None
    try:
        raw = (await backend.read_file(path)).decode("utf-8", errors="replace")
        return json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


async def _read_text(backend: Any, path: str) -> str | None:
    if not await backend.file_exists(path):
        return None
    try:
        return (await backend.read_file(path)).decode("utf-8", errors="replace")
    except OSError:
        return None


async def _existing_path(backend: Any, path: str) -> str | None:
    """``path`` if it names a file that actually exists, else ``None``."""
    return path if await backend.file_exists(path) else None


def run_dir_of(backend: Any, root: str, run_id: str) -> str:
    """The run-scoped directory for ``run_id`` under a DAG history ``root``.

    ``root`` is the full ``.../subagents/mas_dag`` path, matching what
    :class:`~raven.agent.subagent.dag_store.DagRunStore` writes to. The id is
    validated before it is joined in.
    """
    return backend.join_path(root, _check_run_id(run_id))


def _artifact_root(entry: dict, nodes_root: str) -> str:
    """The directory this node's unrecorded artifacts sit beside.

    The manifest names a node's prompt and output but never its memory or its
    transcript, so those two are always derived. Derived from wherever the
    recorded pair actually is, rather than from the flat root unconditionally:
    for a run written before this session's history flattened, that is the run
    directory, and a later task holding the same id owns the flat one.
    """
    named = entry.get("output_file") or entry.get("prompt_file")
    if not isinstance(named, str):
        return nodes_root
    # Both separators, and the original kept rather than normalised: a manifest
    # written on Windows carries backslashes, and handing back a mixed-separator
    # path would be this reader's invention rather than what is on disk.
    cut = max(named.rfind("/"), named.rfind("\\"))
    return named[:cut] if cut > 0 else nodes_root


async def _artifact_paths(backend: Any, entry: dict, nodes_root: str, node_id: str) -> tuple[str | None, str | None]:
    """Where one node's prompt and output are, per the manifest or the flat root.

    The manifest is authoritative for any node it records at all -- including a
    legacy path from before this session's history flattened, and an explicit
    ``None`` for a node that was skipped or failed before it ever rendered.
    Only a node it does not carry gets a candidate derived from the flat node
    root: a node can finish, and its sibling nodes still be running, well before
    the run as a whole finalizes and writes any of this into the manifest.

    One home for the rule because two readers answer for the same node. Deriving
    unconditionally in either of them is what let a later task that took the id
    back serve its prompt and output under the old run's identity and status.
    """
    if entry:
        prompt, output = entry.get("prompt_file"), entry.get("output_file")
        return (prompt if isinstance(prompt, str) else None, output if isinstance(output, str) else None)
    return (
        await _existing_path(backend, prompt_path_in(backend, nodes_root, node_id)),
        await _existing_path(backend, output_path_in(backend, nodes_root, node_id)),
    )


async def read_run(backend: Any, root: str, run_id: str, nodes_root: str) -> dict:
    """Rebuild one run's manifest-shaped payload from its run dir.

    The returned ``files`` list matches the shape the tool's
    ``dag_run_completed`` event publishes, so a consumer can feed it to the same
    renderer. ``prompt_template`` and ``inputs`` are added per node -- they live
    only in ``graph.json``, and together they are what the caller shows as a
    node's input: the template alone leaves every ``{{ inputs.k }}`` unexplained.

    ``finalized`` says whether ``manifest.json`` was present. When it was not,
    every node reports ``pending`` and the caller is expected to overlay live
    state (the instance registry) on top.

    Args:
        backend (`BackendBase`):
            Backend supplying the environment's path semantics.
        root (`str`):
            The session's DAG history root, holding ``<run_id>/graph.json``
            and ``<run_id>/manifest.json``.
        run_id (`str`):
            The run to read.
        nodes_root (`str`):
            The session's flat node root -- where node artifacts live,
            addressed by node id alone rather than by this run's directory.

    Raises:
        DagReadError: the id is malformed or the run dir holds no ``graph.json``.
    """
    rdir = run_dir_of(backend, root, run_id)
    graph = await _read_json(backend, backend.join_path(rdir, "graph.json"))
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise DagReadError(f"no readable DAG run at {rdir}")
    manifest = await _read_json(backend, backend.join_path(rdir, "manifest.json"))
    if not isinstance(manifest, dict):
        manifest = None

    files: list[dict] = []
    for node in graph["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            continue
        nid = node["id"]
        entry = (manifest or {}).get(nid) or {}
        prompt_file, output_file = await _artifact_paths(backend, entry, nodes_root, nid)
        mem_path = await _existing_path(backend, memory_path_in(backend, _artifact_root(entry, nodes_root), nid))
        files.append(
            {
                "node": nid,
                # One spelling on both sides now -- the node field, the status
                # entry, the event payload and the web UI all say ``subagent``.
                # The ``agent`` fallback covers the one release where the node
                # field carried that name: a graph.json written then is still on
                # disk, and this reader is the only place that has to know.
                "subagent": entry.get("subagent") or node.get("subagent") or entry.get("agent") or node.get("agent"),
                "depends_on": entry.get("depends_on") or node.get("depends_on") or [],
                "instance": entry.get("instance", node.get("instance")),
                "status": entry.get("status", "pending"),
                "started_at": entry.get("started_at"),
                "ended_at": entry.get("ended_at"),
                "prompt_file": prompt_file,
                "output_file": output_file,
                "memory_file": mem_path,
                "error": entry.get("error"),
                "prompt_template": node.get("prompt_template"),
                "node_summary": node.get("node_summary"),
                # Beside the template because it is the other half of what the
                # node was asked: the template's `{{ inputs.k }}` says nothing
                # about where k came from. Only from graph.json -- the manifest
                # records what happened, not what was requested.
                "inputs": node.get("inputs") if isinstance(node.get("inputs"), dict) else None,
            }
        )

    statuses = [f["status"] for f in files]
    return {
        "run_id": run_id,
        "dir": rdir,
        "nodes_root": nodes_root,
        "finalized": manifest is not None,
        # What the whole graph was dispatched for, in the model's own words.
        # Only from graph.json: the manifest records what each node did, and no
        # node's line says what the run as a whole is. Read here rather than
        # left in the file because this reader is what every surface asks; a run
        # written before the field existed reports None, which reads the same as
        # a run that has one and is empty.
        "task_summary": graph.get("task_summary"),
        "files": files,
        "terminal_outputs": [],
        "summary": {
            "total": len(files),
            "completed": statuses.count("completed"),
            "failed": statuses.count("failed"),
            "skipped": statuses.count("skipped"),
            "cancelled": statuses.count("cancelled"),
        },
    }


async def read_node(
    backend: Any,
    root: str,
    run_id: str,
    node_id: str,
    nodes_root: str,
    *,
    max_output_chars: int = 20000,
) -> dict:
    """Read one node's rendered prompt and its output, truncating the output.

    The prompt is the *rendered* text actually handed to the sub-agent
    (placeholders already substituted), which differs from the template in
    ``graph.json``; both are worth showing. ``output`` is the head of the full
    ``.out.md`` -- the file itself is uncapped and can be megabytes, so it is
    never returned whole.

    Either field is ``None`` when its file does not exist: a node that never ran
    has no prompt, and a failed one has no output.

    Which is exactly why the manifest is read here too. "Failed and therefore no
    output" and "still running and no output yet" produce the same three fields,
    and a reader shown only those sees a node that was asked something and never
    answered -- the failure, and its reason, were on disk the whole time and
    only the run-level reader ever looked. ``status`` and ``error`` come along
    so one node can account for itself.

    Args:
        backend (`BackendBase`):
            Backend supplying the environment's path semantics.
        root (`str`):
            The session's DAG history root, holding ``<run_id>/manifest.json``.
        run_id (`str`):
            The run this node belongs to.
        node_id (`str`):
            The node to read.
        nodes_root (`str`):
            The session's flat node root -- where this node's prompt, output,
            and transcript files live, addressed by node id alone.
    """
    rdir = run_dir_of(backend, root, run_id)
    _check_node_id(node_id)
    manifest = await _read_json(backend, backend.join_path(rdir, "manifest.json"))
    entry = manifest.get(node_id) or {} if isinstance(manifest, dict) else {}
    # Read before the files, not after: the manifest is what says where they are.
    # Used as resolved, with no `or` behind it: a recorded ``None`` means this
    # node wrote nothing, and falling back to a derived candidate for it reaches
    # the flat root exactly like the unrecorded case does -- serving whatever
    # later task took the id, for a node that provably has no output.
    prompt_file, output_file = await _artifact_paths(backend, entry, nodes_root, node_id)
    prompt = await _read_text(backend, prompt_file) if prompt_file else None
    output = await _read_text(backend, output_file) if output_file else None
    total = len(output) if output is not None else 0
    truncated = total > max_output_chars
    if output is not None and truncated:
        output = output[:max_output_chars]
    return {
        "run_id": run_id,
        "node": node_id,
        "prompt": prompt,
        "prompt_file": prompt_file if prompt is not None else None,
        "output": output,
        "output_file": output_file if output is not None else None,
        "output_chars": total,
        "output_truncated": truncated,
        "status": entry.get("status") if isinstance(entry, dict) else None,
        "error": entry.get("error") if isinstance(entry, dict) else None,
        "transcript": await _read_transcript(
            backend, backend.join_path(_artifact_root(entry, nodes_root), f"{node_id}.transcript.jsonl")
        ),
    }


async def _read_transcript(backend: Any, path: str) -> list[dict]:
    """The node's own turns, or an empty list when the lane could not see any.

    Empty rather than absent for the lane with no per-step visibility: a caller
    draws the prompt and the answer either way, and the two spellings of
    "nothing here" would only invite one of them to be handled and not the
    other. One bad line is skipped rather than losing the rest -- the file is
    appended to while a run is still going.
    """
    raw = await _read_text(backend, path)
    if not raw:
        return []
    entries: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("role"):
            entries.append(entry)
    return entries


__all__ = ["DagReadError", "read_node", "read_run", "run_dir_of"]
