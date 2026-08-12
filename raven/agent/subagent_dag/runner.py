"""The deterministic ready-set scheduler for a sub-agent DAG (Raven-native).

Ported from the RavenX reference ``_dag/_runner.py`` but decoupled from
AgentScope: node execution goes through a :class:`SubagentBackend`
(``run(task, *, task_id, workspace, executor) -> str``) — the same adapter layer
the native subagent uses (req4/req5) — instead of an AgentScope tool yielding
``ToolChunk``s, so ``_run_node`` just awaits a string result. Progress events go
through a plain ``ProgressPublisher`` callback (no spine, no scheduler).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.subagent.instances import get_registry
from raven.agent.subagent_dag._errors import DagValidationError
from raven.agent.subagent_dag._graph import DagNodeSpec, SubAgentDagSpec, validate_and_order
from raven.agent.subagent_dag._render import render_prompt
from raven.agent.subagent_dag._store import DagRunStore, make_run_id

# In-context cap for terminal outputs returned to the main agent; the on-disk
# .out.md always holds the full text.
_MAX_OUTPUT_CHARS = 128000
_MAX_ERROR_CHARS = 500

# A registry write is ordinary async file I/O behind a lock and should complete
# in well under this; the bound exists only so a wedged registry (lock
# contention, a stuck disk) can never hang the node -- or, in _finalize's
# reconciliation pass, the whole run -- indefinitely.
_REGISTRY_WRITE_TIMEOUT_S = 2.0


def _now_ms() -> int:
    return int(time.time() * 1000)


ProgressPublisher = Callable[[str, dict], Awaitable[None]]


async def _emit(publisher: ProgressPublisher | None, name: str, value: dict) -> None:
    """Publish one progress event, swallowing any publisher failure."""
    if publisher is None:
        return
    try:
        await publisher(name, value)
    except Exception:  # noqa: BLE001 - progress must never fail a node
        logger.opt(exception=True).warning("DAG progress publish failed: {}", name)


async def _write_node_status(session_key: str | None, run_id: str, node_id: str, agent: str, status: str) -> None:
    """Best-effort registry write for one node's status, swallowing any failure.

    A status row is never worth failing -- or hanging -- a node over, the same
    reasoning ``_emit`` already applies to progress events: a registry write
    (unlike ``InstanceRegistry``'s own internal ``OSError`` handling on flush)
    can also raise on read -- e.g. a corrupt/non-UTF-8 registry file surfaces
    as a ``UnicodeDecodeError`` from ``_load`` -- and it can stall behind lock
    contention; neither may abort or block the node (or, when this runs from
    ``_finalize``'s reconciliation pass, the whole completed run).
    """
    if not session_key:
        return
    try:
        await asyncio.wait_for(
            get_registry().upsert_dag_node(session_key, run_id, node_id, agent, status),
            timeout=_REGISTRY_WRITE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - a status row must never fail or hang a node
        logger.opt(exception=True).warning("DAG registry write failed for node {} (status={})", node_id, status)


@dataclass
class DagRunResult:
    """The outcome of one DAG run, returned to the main agent."""

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
    run_root: str,
    sandbox: Any = None,
    max_concurrency: int = 5,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
    run_id: str | None = None,
    cancel: asyncio.Event | None = None,
) -> DagRunResult:
    """Run a validated DAG, passing messages through files.

    ``subagents`` maps a node's ``subagent`` name to a SubagentBackend. ``backend``
    is the duck-typed file backend (read_file/write_file/join_path/abspath/
    file_exists). ``sandbox`` is an optional executor handed to each node backend
    (third-party CLI/OpenAI backends ignore it; a raven-loop node backend uses it).

    ``workdir`` and ``run_root`` are two different places and must not be
    collapsed back into one. ``workdir`` is the session's working directory: it
    is each node sub-agent's cwd, and what ``{{ ref:<path> }}`` resolves
    against, so it has to be where the user's files are. ``run_root`` is this
    session's DAG history root, where the prompt/output records are written --
    an audit trail that outlives whatever the working directory is pointed at
    (raven/agent/subagent_history.py).
    ``session_key`` scopes each node's stateful ``instance`` handle (see
    :mod:`raven.agent.subagent.instances`) to this DAG's conversation, the same
    way ``spawn`` scopes its ``instance`` handle. It also scopes this run's node
    status rows in that same registry, so the caller must pass ``run_id`` (rather
    than let one be minted internally) if it needs to key a cancellation signal
    to the same id the registry records.

    ``cancel``, when set at any point during the run, marks every node not
    already terminal as ``skipped`` and cancels any node task currently in
    flight so its semaphore slot is released; the run still finishes normally
    and returns a result describing what was skipped, rather than raising.
    """
    if max_concurrency < 1:
        raise DagValidationError("max_concurrency must be >= 1")
    validate_and_order(spec)
    by_id: dict[str, DagNodeSpec] = {node.id: node for node in spec.nodes}
    for node in spec.nodes:
        if subagents.get(node.subagent) is None:
            raise DagValidationError(f"node '{node.id}' names unknown sub-agent '{node.subagent}'")

    store = DagRunStore(backend, run_root, run_id or make_run_id())
    await store.init(spec.model_dump_json())

    await _emit(
        progress_publisher,
        "dag_run_started",
        {
            "run_id": store.run_id,
            "nodes": [
                {"id": node.id, "subagent": node.subagent, "depends_on": node.depends_on, "instance": node.instance}
                for node in spec.nodes
            ],
        },
    )
    published_skips: set[str] = set()

    status: dict[str, str] = {nid: "pending" for nid in by_id}
    output_paths: dict[str, str] = {}
    errors: dict[str, str] = {}
    prompt_written: set[str] = set()
    node_started_at: dict[str, int] = {}
    node_ended_at: dict[str, int] = {}
    semaphore = asyncio.Semaphore(max_concurrency)

    dependents: dict[str, list[str]] = {nid: [] for nid in by_id}
    for nid, node in by_id.items():
        for dep in node.depends_on:
            dependents[dep].append(nid)

    while True:
        _cascade_failures(by_id, status)
        if cancel is not None and cancel.is_set():
            # A node still "running" here was cancelled mid-flight (see
            # _run_ready_groups below) and never reached a terminal status
            # because CancelledError bypasses _run_node's except-Exception.
            for nid, st in status.items():
                if st in ("pending", "running"):
                    status[nid] = "skipped"
        for nid, st in status.items():
            if st == "skipped" and nid not in published_skips:
                published_skips.add(nid)
                node_started_at[nid] = _now_ms()
                node_ended_at[nid] = _now_ms()
                await _emit(
                    progress_publisher,
                    "dag_node_updated",
                    {"run_id": store.run_id, "node": nid, "status": "skipped"},
                )
                await _write_node_status(session_key, store.run_id, nid, by_id[nid].subagent, "skipped")
        ready = [
            nid
            for nid, st in status.items()
            if st == "pending" and all(status[d] == "completed" for d in by_id[nid].depends_on)
        ]
        if not ready:
            break
        # Nodes sharing a stateful instance run sequentially (id order); independent
        # nodes each form a singleton group and run concurrently under the semaphore.
        groups: dict[str, list[str]] = {}
        for nid in ready:
            inst = by_id[nid].instance
            key = inst if inst is not None else f"\x00node\x00{nid}"
            groups.setdefault(key, []).append(nid)
        await _run_ready_groups(
            (
                _run_group(
                    sorted(nids),
                    by_id=by_id,
                    subagents=subagents,
                    store=store,
                    backend=backend,
                    workdir=workdir,
                    sandbox=sandbox,
                    output_paths=output_paths,
                    status=status,
                    errors=errors,
                    prompt_written=prompt_written,
                    node_started_at=node_started_at,
                    node_ended_at=node_ended_at,
                    semaphore=semaphore,
                    progress_publisher=progress_publisher,
                    session_key=session_key,
                )
                for nids in groups.values()
            ),
            cancel,
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
        session_key,
    )


async def _run_ready_groups(coros: Any, cancel: asyncio.Event | None) -> None:
    """Await one scheduling round's group tasks.

    With no ``cancel`` this is a plain ``asyncio.gather`` (if that gather is
    itself cancelled from outside, gather cancels every task it is waiting on,
    same as before this function existed). With a ``cancel``, races the
    round's group tasks against a ``cancel.wait()`` task so an in-flight node
    can be cancelled the instant the signal fires, releasing its semaphore
    slot immediately rather than waiting for it to finish on its own.

    The reap -- cancelling every not-yet-done task and awaiting all of them --
    lives in a ``finally`` so it still runs even if the race itself is
    interrupted by an *outer* cancellation (a tool-call timeout, turn abort,
    or gateway shutdown cancelling the task this coroutine is running in).
    Unlike ``asyncio.gather``, plain ``asyncio.wait`` does NOT cancel the
    futures it is waiting on, so without this ``finally`` an outer
    cancellation would leave an in-flight node's child process running,
    unsupervised, forever. ``return_exceptions=True`` on the final reap keeps
    a node's own exception (already handled inside ``_run_node``) or a stray
    one from a registry write from escaping and aborting an otherwise
    cleanly-cancelled run.
    """
    tasks = [asyncio.ensure_future(c) for c in coros]
    if cancel is None:
        await asyncio.gather(*tasks)
        return
    cancel_wait = asyncio.ensure_future(cancel.wait())
    try:
        pending: set[asyncio.Future] = {*tasks, cancel_wait}
        while True:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait in done or all(t.done() for t in tasks):
                break
    finally:
        if not cancel_wait.done():
            cancel_wait.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(cancel_wait, *tasks, return_exceptions=True)


def _cascade_failures(by_id: dict[str, DagNodeSpec], status: dict[str, str]) -> None:
    """Mark pending nodes with a failed/skipped dependency as skipped."""
    changed = True
    while changed:
        changed = False
        for nid, node in by_id.items():
            if status[nid] != "pending":
                continue
            if any(status[d] in ("failed", "skipped") for d in node.depends_on):
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
    sandbox: Any,
    output_paths: dict[str, str],
    status: dict[str, str],
    errors: dict[str, str],
    prompt_written: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    semaphore: asyncio.Semaphore,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
) -> None:
    """Run one instance-group's nodes sequentially, in id order."""
    for nid in nids:
        await _run_node(
            by_id[nid],
            subagents[by_id[nid].subagent],
            store=store,
            backend=backend,
            workdir=workdir,
            sandbox=sandbox,
            output_paths=output_paths,
            status=status,
            errors=errors,
            prompt_written=prompt_written,
            node_started_at=node_started_at,
            node_ended_at=node_ended_at,
            semaphore=semaphore,
            progress_publisher=progress_publisher,
            session_key=session_key,
        )


async def _run_node(
    node: DagNodeSpec,
    agent_backend: Any,
    *,
    store: DagRunStore,
    backend: Any,
    workdir: str,
    sandbox: Any,
    output_paths: dict[str, str],
    status: dict[str, str],
    errors: dict[str, str],
    prompt_written: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    semaphore: asyncio.Semaphore,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
) -> None:
    """Render, dispatch to the node's backend, and record one node."""
    async with semaphore:
        started_at_ms = _now_ms()
        node_started_at[node.id] = started_at_ms
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {"run_id": store.run_id, "node": node.id, "status": "running", "started_at": started_at_ms},
        )
        await _write_node_status(session_key, store.run_id, node.id, node.subagent, "running")
        try:
            prompt = await render_prompt(node, backend=backend, cwd=workdir, output_paths=output_paths)
            prompt_path = store.prompt_path(node.id)
            output_path = store.output_path(node.id)
            await store.write_text(prompt_path, prompt)
            prompt_written.add(node.id)
            result = await agent_backend.run(
                prompt,
                task_id=node.id,
                workspace=Path(workdir),
                executor=sandbox,
                session_key=session_key,
                instance=node.instance,
            )
            await store.write_text(output_path, result or "")
            status[node.id] = "completed"
            output_paths[node.id] = output_path
        except Exception as exc:  # noqa: BLE001 - record and continue
            logger.opt(exception=True).warning("DAG node {} failed: {}", node.id, exc)
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
        await _write_node_status(session_key, store.run_id, node.id, node.subagent, status[node.id])


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
    session_key: str | None = None,
) -> DagRunResult:
    """Assemble the result, write the manifest, and append the index.

    Also reconciles every node's registry row to its final status. This is
    the only place that unconditionally re-asserts a status regardless of
    what earlier writes landed, so it repairs the case where a node's own
    terminal write raced a concurrent registry flush and lost, or where an
    outer cancellation interrupted a write mid-flight -- either of which would
    otherwise leave a row stuck at a stale, non-terminal status forever. The
    write is idempotent, so re-asserting an already-correct row is a no-op.
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

    if session_key:
        # Concurrent, not sequential: each call already has its own bounded
        # timeout (_REGISTRY_WRITE_TIMEOUT_S), so awaiting them one at a time
        # would make teardown scale as N x that timeout under a contended
        # registry. Run them all at once so the worst case is roughly one
        # timeout window regardless of node count.
        await asyncio.gather(
            *(
                _write_node_status(session_key, store.run_id, node.id, node.subagent, status[node.id])
                for node in spec.nodes
            )
        )

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
