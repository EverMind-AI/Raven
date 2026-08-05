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

_DAG_DIR = ".ravenx_dag"

# Both ids are minted by raven itself (``make_run_id`` / the graph schema's
# ``_NAME_PATTERN``), but they arrive here straight off a web request, so they
# are re-checked before being joined into a path. Without this a crafted id
# would walk out of the run dir and read arbitrary files.
_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
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


def run_dir_of(backend: Any, workdir: str, run_id: str) -> str:
    """The run-scoped directory for ``run_id`` (id validated first)."""
    return backend.join_path(workdir, _DAG_DIR, _check_run_id(run_id))


async def read_run(backend: Any, workdir: str, run_id: str) -> dict:
    """Rebuild one run's manifest-shaped payload from its run dir.

    The returned ``files`` list matches the shape the tool's
    ``dag_run_completed`` event publishes, so a consumer can feed it to the same
    renderer. ``prompt_template`` is added per node -- it lives only in
    ``graph.json``, and it is what the caller shows as a node's input.

    ``finalized`` says whether ``manifest.json`` was present. When it was not,
    every node reports ``pending`` and the caller is expected to overlay live
    state (the instance registry) on top.

    Raises:
        DagReadError: the id is malformed or the run dir holds no ``graph.json``.
    """
    rdir = run_dir_of(backend, workdir, run_id)
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
                "subagent": entry.get("subagent") or node.get("subagent"),
                "depends_on": entry.get("depends_on") or node.get("depends_on") or [],
                "instance": entry.get("instance", node.get("instance")),
                "status": entry.get("status", "pending"),
                "started_at": entry.get("started_at"),
                "ended_at": entry.get("ended_at"),
                "prompt_file": entry.get("prompt_file"),
                "output_file": entry.get("output_file"),
                "error": entry.get("error"),
                "prompt_template": node.get("prompt_template"),
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
        },
    }


async def read_node(
    backend: Any,
    workdir: str,
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
    """
    rdir = run_dir_of(backend, workdir, run_id)
    _check_node_id(node_id)
    prompt_file = backend.join_path(rdir, f"{node_id}.prompt.md")
    output_file = backend.join_path(rdir, f"{node_id}.out.md")
    prompt = await _read_text(backend, prompt_file)
    output = await _read_text(backend, output_file)
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
    }


__all__ = ["DagReadError", "read_node", "read_run", "run_dir_of"]
