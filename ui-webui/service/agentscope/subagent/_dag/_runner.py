# -*- coding: utf-8 -*-
"""The deterministic ready-set scheduler for a sub-agent DAG."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..._logging import logger
from ...message import TextBlock, ToolResultState
from .._tool import _MAX_OUTPUT_CHARS
from ._errors import DagValidationError
from ._graph import DagNodeSpec, SubAgentDagSpec, validate_and_order
from ._render import render_prompt
from ._store import DagRunStore, make_run_id

_MAX_ERROR_CHARS = 500


def _now_ms() -> int:
    """Return the current wall-clock time in epoch milliseconds.

    Used as the ``started_at`` / ``ended_at`` annotation on each node
    transition so the frontend can show per-node running time without
    having to track it from event-arrival time (which is lost on
    reload).

    Returns:
        `int`:
            Floor of ``time.time() * 1000``.
    """
    return int(time.time() * 1000)


ProgressPublisher = Callable[[str, dict], Awaitable[None]]


async def _emit(
    publisher: ProgressPublisher | None,
    name: str,
    value: dict,
) -> None:
    """Publish one progress event, swallowing any publisher failure.

    Args:
        publisher (`ProgressPublisher | None`):
            The progress callback, or ``None`` to no-op.
        name (`str`):
            The event name (``dag_run_started`` / ``dag_node_updated``).
        value (`dict`):
            The event payload.
    """
    if publisher is None:
        return
    try:
        await publisher(name, value)
    except Exception:  # noqa: BLE001 - progress must never fail a node
        logger.warning("DAG progress publish failed: %s", name, exc_info=True)


@dataclass
class DagRunResult:
    """The outcome of one DAG run, returned to the main agent.

    Attributes:
        run_id (`str`):
            The run id.
        dir (`str`):
            The run-scoped directory holding all message files.
        terminal_outputs (`list[dict]`):
            ``{"node": id, "text": <output, capped>}`` for each completed
            terminal (sink) node.
        files (`list[dict]`):
            ``{"node", "subagent", "depends_on", "instance", "status",
            "started_at", "ended_at", "prompt_file", "output_file",
            "error"}`` per node. ``started_at`` / ``ended_at`` are
            epoch milliseconds; absent for nodes that never ran
            (cascaded skip with no synthetic timing).
        summary (`dict`):
            Counts: ``total`` / ``completed`` / ``failed`` / ``skipped``.
    """

    run_id: str
    dir: str
    terminal_outputs: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


async def run_dag(
    spec: SubAgentDagSpec,
    *,
    subagents: dict[str, Any],
    backend: Any,
    workdir: str,
    max_concurrency: int = 5,
    progress_publisher: ProgressPublisher | None = None,
) -> DagRunResult:
    """Run a validated DAG, passing messages through files.

    Args:
        spec (`SubAgentDagSpec`):
            The graph to execute.
        subagents (`dict[str, Any]`):
            Sub-agent name to its ``CliSubAgentTool``.
        backend (`BackendBase`):
            Session workspace backend for all file I/O.
        workdir (`str`):
            Session working directory.
        max_concurrency (`int`, defaults to `5`):
            Maximum concurrent sub-agent processes.
        progress_publisher (`ProgressPublisher | None`, optional):
            Callback invoked with ``(name, value)`` for live progress
            events. Failures are swallowed and never fail a node.

    Returns:
        `DagRunResult`:
            The run outcome for the main agent.

    Raises:
        `DagValidationError`:
            When the graph is invalid, a node names an unknown sub-agent,
            or ``max_concurrency`` is less than 1.
    """
    if max_concurrency < 1:
        raise DagValidationError("max_concurrency must be >= 1")
    validate_and_order(spec)
    by_id: dict[str, DagNodeSpec] = {node.id: node for node in spec.nodes}
    for node in spec.nodes:
        tool = subagents.get(node.subagent)
        if tool is None:
            raise DagValidationError(
                f"node '{node.id}' names unknown sub-agent "
                f"'{node.subagent}'",
            )

    store = DagRunStore(backend, workdir, make_run_id())
    await store.init(spec.model_dump_json())

    await _emit(
        progress_publisher,
        "dag_run_started",
        {
            "run_id": store.run_id,
            "nodes": [
                {
                    "id": node.id,
                    "subagent": node.subagent,
                    "depends_on": node.depends_on,
                    "instance": node.instance,
                }
                for node in spec.nodes
            ],
        },
    )
    published_skips: set[str] = set()

    status: dict[str, str] = {nid: "pending" for nid in by_id}
    output_paths: dict[str, str] = {}
    errors: dict[str, str] = {}
    prompt_written: set[str] = set()
    # Per-node wall-clock timing annotations (epoch ms); populated by
    # ``_run_node`` and persisted in the manifest so the frontend can
    # render "X seconds" on each DAG node even after a reload.
    node_started_at: dict[str, int] = {}
    node_ended_at: dict[str, int] = {}
    semaphore = asyncio.Semaphore(max_concurrency)

    dependents: dict[str, list[str]] = {nid: [] for nid in by_id}
    for nid, node in by_id.items():
        for dep in node.depends_on:
            dependents[dep].append(nid)

    while True:
        _cascade_failures(by_id, status)
        for nid, st in status.items():
            if st == "skipped" and nid not in published_skips:
                published_skips.add(nid)
                node_started_at[nid] = _now_ms()
                node_ended_at[nid] = _now_ms()
                await _emit(
                    progress_publisher,
                    "dag_node_updated",
                    {
                        "run_id": store.run_id,
                        "node": nid,
                        "status": "skipped",
                    },
                )
        ready = [
            nid
            for nid, st in status.items()
            if st == "pending"
            and all(status[d] == "completed" for d in by_id[nid].depends_on)
        ]
        if not ready:
            break
        # Group ready nodes by stateful instance: nodes sharing one
        # instance run sequentially (deterministic, sorted by id) so they
        # never interleave create/resume nor co-occupy concurrency slots.
        # Independent nodes each form a singleton group and run
        # concurrently, bounded by the shared semaphore.
        groups: dict[str, list[str]] = {}
        for nid in ready:
            inst = by_id[nid].instance
            key = inst if inst is not None else f"\x00node\x00{nid}"
            groups.setdefault(key, []).append(nid)
        await asyncio.gather(
            *(
                _run_group(
                    sorted(nids),
                    by_id=by_id,
                    subagents=subagents,
                    store=store,
                    backend=backend,
                    workdir=workdir,
                    output_paths=output_paths,
                    status=status,
                    errors=errors,
                    prompt_written=prompt_written,
                    node_started_at=node_started_at,
                    node_ended_at=node_ended_at,
                    semaphore=semaphore,
                    progress_publisher=progress_publisher,
                )
                for nids in groups.values()
            ),
        )

    return await _finalize(
        spec,
        by_id,
        status,
        errors,
        output_paths,
        prompt_written,
        dependents,
        node_started_at,
        node_ended_at,
        store,
    )


def _cascade_failures(
    by_id: dict[str, DagNodeSpec],
    status: dict[str, str],
) -> None:
    """Mark pending nodes with a failed/skipped dependency as skipped.

    Args:
        by_id (`dict[str, DagNodeSpec]`):
            Nodes keyed by id.
        status (`dict[str, str]`):
            Mutable per-node status map.
    """
    changed = True
    while changed:
        changed = False
        for nid, node in by_id.items():
            if status[nid] != "pending":
                continue
            if any(
                status[d] in ("failed", "skipped") for d in node.depends_on
            ):
                status[nid] = "skipped"
                changed = True


async def _run_group(
    nids: list[str],
    *,
    by_id: dict[str, DagNodeSpec],
    subagents: dict[str, Any],
    store: DagRunStore,
    backend: Any,
    workdir: str,
    output_paths: dict[str, str],
    status: dict[str, str],
    errors: dict[str, str],
    prompt_written: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    semaphore: asyncio.Semaphore,
    progress_publisher: ProgressPublisher | None = None,
) -> None:
    """Run one instance-group's nodes sequentially, in id order.

    Args:
        nids (`list[str]`):
            Nodes in this group, pre-sorted for deterministic order.
        by_id (`dict[str, DagNodeSpec]`):
            Nodes keyed by id.
        subagents (`dict[str, Any]`):
            Sub-agent name to its tool.
        store (`DagRunStore`):
            The run store.
        backend (`BackendBase`):
            Backend for rendering file reads.
        workdir (`str`):
            Session working directory (rendering cwd).
        output_paths (`dict[str, str]`):
            Completed dependency outputs (updated on success).
        status (`dict[str, str]`):
            Per-node status map (updated).
        errors (`dict[str, str]`):
            Per-node error text (updated on failure).
        prompt_written (`set[str]`):
            Node ids whose prompt file was actually written
            (updated on success).
        node_started_at (`dict[str, int]`):
            Per-node "running" timestamps in epoch ms; populated by
            ``_run_node`` and read by ``_finalize`` for the manifest.
        node_ended_at (`dict[str, int]`):
            Per-node terminal timestamps in epoch ms; same as above.
        semaphore (`asyncio.Semaphore`):
            Global concurrency cap, acquired per node.
        progress_publisher (`ProgressPublisher | None`, optional):
            Callback for live per-node progress events.
    """
    for nid in nids:
        await _run_node(
            by_id[nid],
            subagents[by_id[nid].subagent],
            store=store,
            backend=backend,
            workdir=workdir,
            output_paths=output_paths,
            status=status,
            errors=errors,
            prompt_written=prompt_written,
            node_started_at=node_started_at,
            node_ended_at=node_ended_at,
            semaphore=semaphore,
            progress_publisher=progress_publisher,
        )


async def _run_node(
    node: DagNodeSpec,
    tool: Any,
    *,
    store: DagRunStore,
    backend: Any,
    workdir: str,
    output_paths: dict[str, str],
    status: dict[str, str],
    errors: dict[str, str],
    prompt_written: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    semaphore: asyncio.Semaphore,
    progress_publisher: ProgressPublisher | None = None,
) -> None:
    """Render, dispatch, and record one node.

    Args:
        node (`DagNodeSpec`):
            The node to run.
        tool (`CliSubAgentTool`):
            The sub-agent that runs it.
        store (`DagRunStore`):
            The run store.
        backend (`BackendBase`):
            Backend for rendering file reads.
        workdir (`str`):
            Session working directory (rendering cwd).
        output_paths (`dict[str, str]`):
            Completed dependency outputs (updated on success).
        status (`dict[str, str]`):
            Per-node status map (updated).
        errors (`dict[str, str]`):
            Per-node error text (updated on failure).
        prompt_written (`set[str]`):
            Node ids whose prompt file was actually written to disk
            (updated on success).
        node_started_at (`dict[str, int]`):
            Per-node "running" timestamps in epoch ms (mutated).
        node_ended_at (`dict[str, int]`):
            Per-node terminal timestamps in epoch ms (mutated).
        semaphore (`asyncio.Semaphore`):
            Global concurrency cap.
        progress_publisher (`ProgressPublisher | None`, optional):
            Callback for live per-node progress events.
    """
    async with semaphore:
        started_at_ms = _now_ms()
        node_started_at[node.id] = started_at_ms
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {
                "run_id": store.run_id,
                "node": node.id,
                "status": "running",
                "started_at": started_at_ms,
            },
        )
        try:
            prompt = await render_prompt(
                node,
                backend=backend,
                cwd=workdir,
                output_paths=output_paths,
            )
            prompt_path = store.prompt_path(node.id)
            output_path = store.output_path(node.id)
            await store.write_text(prompt_path, prompt)
            prompt_written.add(node.id)
            last = None
            async for chunk in tool.call(
                prompt=prompt,
                instance=node.instance,
                prompt_file=prompt_path,
                output_file=output_path,
            ):
                last = chunk
            if last is None or last.state == ToolResultState.ERROR:
                status[node.id] = "failed"
                if (
                    last is not None
                    and last.content
                    and isinstance(last.content[0], TextBlock)
                ):
                    errors[node.id] = last.content[0].text
                elif last is not None:
                    errors[node.id] = "sub-agent error"
                else:
                    errors[node.id] = "sub-agent produced no output"
            else:
                status[node.id] = "completed"
                output_paths[node.id] = output_path
        except Exception as exc:  # noqa: BLE001 - record and continue
            logger.warning(
                "DAG node %s failed: %s",
                node.id,
                exc,
                exc_info=True,
            )
            status[node.id] = "failed"
            errors[node.id] = str(exc)
        ended_at_ms = _now_ms()
        node_ended_at[node.id] = ended_at_ms
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {
                "run_id": store.run_id,
                "node": node.id,
                "status": status[node.id],
                "started_at": started_at_ms,
                "ended_at": ended_at_ms,
            },
        )


async def _finalize(
    spec: SubAgentDagSpec,
    by_id: dict[str, DagNodeSpec],
    status: dict[str, str],
    errors: dict[str, str],
    output_paths: dict[str, str],
    prompt_written: set[str],
    dependents: dict[str, list[str]],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    store: DagRunStore,
) -> DagRunResult:
    """Assemble the result, write the manifest, and append the index.

    Args:
        spec (`SubAgentDagSpec`):
            The executed spec.
        by_id (`dict[str, DagNodeSpec]`):
            Nodes keyed by id.
        status (`dict[str, str]`):
            Final per-node status.
        errors (`dict[str, str]`):
            Per-node error text.
        output_paths (`dict[str, str]`):
            Completed node output paths.
        prompt_written (`set[str]`):
            Node ids whose prompt file was actually written to disk.
        dependents (`dict[str, list[str]]`):
            Node id to its dependent ids.
        node_started_at (`dict[str, int]`):
            Per-node "running" timestamps in epoch ms.
        node_ended_at (`dict[str, int]`):
            Per-node terminal timestamps in epoch ms.
        store (`DagRunStore`):
            The run store.

    Returns:
        `DagRunResult`:
            The assembled run result.
    """
    files: list[dict] = []
    manifest: dict = {}
    for node in spec.nodes:
        nid = node.id
        prompt_file = store.prompt_path(nid) if nid in prompt_written else None
        output_file = output_paths.get(nid)
        error = errors.get(nid)
        if error is not None and len(error) > _MAX_ERROR_CHARS:
            error = error[:_MAX_ERROR_CHARS] + "... (truncated)"
        files.append(
            {
                "node": nid,
                "subagent": node.subagent,
                "depends_on": node.depends_on,
                "instance": node.instance,
                "status": status[nid],
                "started_at": node_started_at.get(nid),
                "ended_at": node_ended_at.get(nid),
                "prompt_file": prompt_file,
                "output_file": output_file,
                "error": error,
            },
        )
        manifest[nid] = {
            "status": status[nid],
            "subagent": node.subagent,
            "depends_on": node.depends_on,
            "instance": node.instance,
            "started_at": node_started_at.get(nid),
            "ended_at": node_ended_at.get(nid),
            "prompt_file": prompt_file,
            "output_file": output_file,
            "error": errors.get(nid),
        }

    terminal_outputs: list[dict] = []
    for nid in by_id:
        if not dependents[nid] and status[nid] == "completed":
            text = await store.read_text(output_paths[nid])
            if len(text) > _MAX_OUTPUT_CHARS:
                text = text[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"
            terminal_outputs.append({"node": nid, "text": text})

    summary = {
        "total": len(by_id),
        "completed": sum(1 for s in status.values() if s == "completed"),
        "failed": sum(1 for s in status.values() if s == "failed"),
        "skipped": sum(1 for s in status.values() if s == "skipped"),
    }
    await store.write_manifest(manifest)
    await store.append_index({"run_id": store.run_id, "summary": summary})
    return DagRunResult(
        run_id=store.run_id,
        dir=store.run_dir,
        terminal_outputs=terminal_outputs,
        files=files,
        summary=summary,
    )
