"""Read a finished or in-flight DAG run back out of its on-disk run dir.

The live ``dag_*`` progress events are the only thing the web UI sees while a
run executes, and they exist for exactly one page-session: nothing replays them,
and a gateway-mode tool result carries no manifest metadata. The run dir written
by :class:`~raven.agent.subagent_dag._store.DagRunStore` is the durable record,
so reading it back is what lets a reloaded page show the graph again -- and what
lets a node's rendered prompt and full output be shown on demand instead of only
the leaf ``terminal_outputs`` the manifest inlines.

An in-flight run has a ``graph.json`` but no ``manifest.json`` yet (that is
written once, in ``_finalize``), so structure and per-node state are read
separately: structure always from ``graph.json``, state from ``manifest.json``
when it exists and from the caller's overlay (the instance registry) when it
does not.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Both ids are minted by raven itself (``make_run_id`` / the graph schema's
# ``_ID_PATTERN``), but they arrive here straight off a web request, so they
# are re-checked before being joined into a path. Without this a crafted id
# would walk out of the run dir and read arbitrary files.
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


def run_dir_of(backend: Any, root: str, run_id: str) -> str:
    """The run-scoped directory for ``run_id`` under a DAG history ``root``.

    ``root`` is the full ``.../subagents/mas_dag`` path, matching what
    :class:`~raven.agent.subagent_dag._store.DagRunStore` writes to. The id is
    validated before it is joined in.
    """
    return backend.join_path(root, _check_run_id(run_id))


async def read_run(backend: Any, root: str, run_id: str) -> dict:
    """Rebuild one run's manifest-shaped payload from its run dir.

    The returned ``files`` list matches the shape the tool's
    ``dag_run_completed`` event publishes, so a consumer can feed it to the same
    renderer. ``prompt_template`` and ``inputs`` are added per node -- they live
    only in ``graph.json``, and together they are what the caller shows as a
    node's input: the template alone leaves every ``{{ inputs.k }}`` unexplained.

    ``finalized`` says whether ``manifest.json`` was present. When it was not,
    every node reports ``pending`` and the caller is expected to overlay live
    state (the instance registry) on top.

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
                "prompt_file": entry.get("prompt_file"),
                "output_file": entry.get("output_file"),
                "error": entry.get("error"),
                "prompt_template": node.get("prompt_template"),
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
        "finalized": manifest is not None,
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
    """
    rdir = run_dir_of(backend, root, run_id)
    _check_node_id(node_id)
    prompt_file = backend.join_path(rdir, f"{node_id}.prompt.md")
    output_file = backend.join_path(rdir, f"{node_id}.out.md")
    prompt = await _read_text(backend, prompt_file)
    output = await _read_text(backend, output_file)
    manifest = await _read_json(backend, backend.join_path(rdir, "manifest.json"))
    entry = manifest.get(node_id) or {} if isinstance(manifest, dict) else {}
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
        "transcript": await _read_transcript(backend, backend.join_path(rdir, f"{node_id}.transcript.jsonl")),
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
