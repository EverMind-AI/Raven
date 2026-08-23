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
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.subagent import activity
from raven.agent.subagent.instances import get_registry, hold_handle
from raven.agent.subagent_dag._errors import DagValidationError
from raven.agent.subagent_dag._graph import DagNodeSpec, SubAgentDagSpec, graph_deps, validate_and_order
from raven.agent.subagent_dag._render import render_prompt
from raven.agent.subagent_dag._store import (
    DagRunStore,
    SessionNodes,
    index_guard,
    make_run_id,
    node_live_key,
    read_session_nodes,
)

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


async def _link_node_instance(
    session_key: str | None, run_id: str, node_id: str, agent: str, handle: str | None
) -> None:
    """Best-effort record that this node's instance handle belongs to this node.

    Only a stateful node has one: a stateless node runs on no handle, so its
    ``dag-node`` row is the only row it will ever have. Same never-fail-a-node
    contract as ``_write_node_status``.
    """
    if not session_key or not handle:
        return
    try:
        await asyncio.wait_for(
            get_registry().link_dag_node(session_key, agent, handle, run_id, node_id),
            timeout=_REGISTRY_WRITE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - a link must never fail or hang a node
        logger.opt(exception=True).warning("DAG registry link failed for node {} (handle={})", node_id, handle)


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
    resolve: "Callable[[DagNodeSpec], Any]",
    backend: Any,
    workdir: str,
    run_root: str,
    subagents_root: str | None = None,
    sandbox: Any = None,
    max_concurrency: int = 5,
    semaphore: asyncio.Semaphore | None = None,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
    run_id: str | None = None,
    cancel: asyncio.Event | None = None,
    state_for: "Callable[[str, str | None, str], Any] | None" = None,
    auto_instances: frozenset[str] = frozenset(),
) -> DagRunResult:
    """Run a validated DAG, passing messages through files.

    ``resolve`` maps one node to the backend that runs it, so the narrowing a
    node asks for (its ``skills``) is applied per node rather than per agent name;
    it returns ``None`` for a name the agent table does not hold. ``backend``
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

    They are both reference roots, though, not just the one: ``run_root``
    resolves ``{{ ref:@runs/<run_id>/... }}``, which is the short way for a
    graph to read an earlier run in the same conversation. It has to be a
    second root rather than a relative path from ``workdir``, because
    ``sessions/`` is a protected subtree (raven/agent/workdir.py) that no
    working directory can be aimed at -- so no relative path from one reaches
    the other.

    ``subagents_root`` widens that by one directory: ``<session_dir>/subagents``,
    the parent of ``run_root``, so a reference may also name this conversation's
    run history -- and its ``spawn`` records -- by absolute path rather than only
    through ``@runs/``. Left unset, references are confined to ``workdir`` plus
    ``@runs/``.

    It is deliberately *not* agent home. Agent home also holds ``user_memory/``,
    ``skills/`` and every other conversation's transcript and sub-agent history;
    ``workdir.py`` keeps those three out of the agent's file surface as
    ``_PROTECTED_SUBTREES``, and a DAG graph is LLM-authored and auto-run, so a
    root that spanned them would render their contents into a third-party
    sub-agent's prompt. See :func:`._paths.check_confined`.

    Node ids are unique across the session, not just across this graph: the
    session index under ``run_root`` is read before validation, so a graph
    reusing an id an earlier run already took is refused, and one naming a node
    an earlier run *completed* resolves to that run's output, needing no
    ``depends_on`` entry for it. Such an entry is allowed and satisfied on
    sight; ``_graph.graph_deps`` is what keeps it out of the scheduling below,
    which only knows this run's statuses. Those two are separate questions --
    see :class:`._store.SessionNodes`.

    ``semaphore`` caps how many nodes dispatch at once. Pass one in to share the
    cap with everything else that runs a sub-agent -- concurrent runs, and
    ``spawn`` -- rather than letting each run hold its own; ``max_concurrency``
    only sizes the private fallback built when none is given.

    ``session_key`` scopes each node's stateful ``instance`` handle (see
    :mod:`raven.agent.subagent.instances`) to this DAG's conversation, the same
    way ``spawn`` scopes its ``instance`` handle. It also scopes this run's node
    status rows in that same registry, so the caller must pass ``run_id`` (rather
    than let one be minted internally) if it needs to key a cancellation signal
    to the same id the registry records.

    ``cancel``, when set at any point during the run, cancels any node task
    currently in flight so its semaphore slot is released, records each such
    node as ``cancelled`` and every other not-yet-terminal node as
    ``skipped``; the run still finishes normally and returns a result
    describing what stopped, rather than raising.
    """
    if semaphore is None and max_concurrency < 1:
        raise DagValidationError("max_concurrency must be >= 1")
    roots = (workdir, subagents_root) if subagents_root else (workdir,)
    # Read, validate and claim under one guard. Splitting them would let two
    # concurrent runs both read an index without node 'x', both pass the
    # uniqueness check, and both claim it -- leaving two nodes answering to one
    # name, which is exactly what the id being unique is supposed to rule out.
    async with index_guard(run_root):
        session_nodes = await read_session_nodes(backend, run_root)
        validate_and_order(spec, roots, session_nodes)
        by_id: dict[str, DagNodeSpec] = {node.id: node for node in spec.nodes}
        # Resolved once per node, here, rather than per dispatch: a narrowed backend
        # is built for the node that asked for it, and the unknown-name check below
        # is then the same lookup the dispatch will use rather than a second one
        # that could disagree with it.
        node_backends: dict[str, Any] = {}
        for node in spec.nodes:
            resolved = resolve(node)
            if resolved is None:
                raise DagValidationError(f"node '{node.id}' names unknown sub-agent '{node.subagent}'")
            node_backends[node.id] = resolved

        store = DagRunStore(backend, run_root, run_id or make_run_id())
        await store.init(spec.model_dump_json(), [node.id for node in spec.nodes])

    published_terminal: set[str] = set()

    # Built before the `try` below, not inside it: the cancellation handler reads
    # `status`, and a handler that covers the first await has to be able to.
    status: dict[str, str] = {nid: "pending" for nid in by_id}
    output_paths: dict[str, str] = {}
    errors: dict[str, str] = {}
    prompt_written: set[str] = set()
    node_started_at: dict[str, int] = {}
    node_ended_at: dict[str, int] = {}
    node_activity: dict[str, dict] = {}
    gate = semaphore if semaphore is not None else asyncio.Semaphore(max_concurrency)

    deps: dict[str, list[str]] = {nid: graph_deps(node, by_id) for nid, node in by_id.items()}
    dependents: dict[str, list[str]] = {nid: [] for nid in by_id}
    for nid, in_graph in deps.items():
        for dep in in_graph:
            dependents[dep].append(nid)

    # `store.init` above made this run's claim on its node ids durable, so every
    # await from here on has to be covered: a stop delivered on any of them would
    # otherwise leave those ids recorded as still being written, for a run that is
    # over. That includes the run-started publish -- it goes to a host sink (a
    # websocket fan-out, the TUI RPC broadcast), so it genuinely suspends, and the
    # shutdown sweep cancels every in-flight run at once including one that has
    # only just started.
    try:
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
        while True:
            _cascade_failures(deps, status)
            if cancel is not None and cancel.is_set():
                _mark_stopped(status)
            for nid, st in status.items():
                if st not in ("skipped", "cancelled") or nid in published_terminal:
                    continue
                published_terminal.add(nid)
                now = _now_ms()
                # A cancelled node already has a real start time from its
                # dispatch; only a skipped one needs both stamps invented.
                node_started_at.setdefault(nid, now)
                node_ended_at[nid] = now
                await _emit(
                    progress_publisher,
                    "dag_node_updated",
                    {"run_id": store.run_id, "node": nid, "status": st},
                )
                await _write_node_status(session_key, store.run_id, nid, by_id[nid].subagent, st)
            ready = [
                nid
                for nid, st in status.items()
                if st == "pending" and all(status[d] == "completed" for d in deps[nid])
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
                        node_backends=node_backends,
                        store=store,
                        backend=backend,
                        workdir=workdir,
                        roots=roots,
                        session_nodes=session_nodes,
                        sandbox=sandbox,
                        output_paths=output_paths,
                        status=status,
                        errors=errors,
                        prompt_written=prompt_written,
                        node_started_at=node_started_at,
                        node_ended_at=node_ended_at,
                        node_activity=node_activity,
                        semaphore=gate,
                        progress_publisher=progress_publisher,
                        state_for=state_for,
                        session_key=session_key,
                        subagents_root=subagents_root,
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
            node_activity,
            store,
            session_key,
            auto_instances,
        )
    except asyncio.CancelledError:
        # `/stop` and the shutdown sweep stop a background run by cancelling its
        # task rather than setting `cancel`, so `_finalize` never runs. Without
        # this, the ids claimed at `init` would keep their index entry with no
        # `status`, and `read_session_nodes` would report them `running` forever:
        # neither reusable nor readable, for a run that is definitively over --
        # and the two refusals that produces contradict each other.
        _mark_stopped(status)
        await _record_outcome(store, status, cancelled=True)
        raise


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


def _tally(status: dict[str, str]) -> dict:
    """Count this run's nodes by terminal state."""
    return {
        "total": len(status),
        "completed": sum(1 for s in status.values() if s == "completed"),
        "failed": sum(1 for s in status.values() if s == "failed"),
        "skipped": sum(1 for s in status.values() if s == "skipped"),
        "cancelled": sum(1 for s in status.values() if s == "cancelled"),
    }


async def _record_outcome(store: DagRunStore, status: dict[str, str], *, cancelled: bool = False) -> None:
    """Write this run's per-node outcome into the session index.

    Per-node status, not just the tally: a later graph may name one of these
    nodes, and whether that reference is legal -- and what to advise when it is
    not -- turns on that node's own outcome, not the run's. Every id claimed at
    ``init`` has to appear here, or ``read_session_nodes`` keeps reporting it as
    still being written.

    Reached from the cancellation path too, where nothing else would record an
    outcome. A failure there must not replace the ``CancelledError`` being
    propagated, so it is logged and swallowed -- the same call is best-effort in
    both directions, since a wedged index is never worth losing a stop over.
    """
    entry = {"run_id": store.run_id, "summary": _tally(status), "status": dict(status)}
    try:
        async with index_guard(store.root):
            await store.upsert_index(entry)
    except Exception:  # noqa: BLE001 - see above
        if not cancelled:
            raise
        logger.opt(exception=True).warning("DAG index write failed for cancelled run {}", store.run_id)


def _mark_stopped(status: dict[str, str]) -> None:
    """Give every unfinished node the outcome the stop actually gave it.

    A `running` node was cut off mid-flight and never reached a terminal
    status, because CancelledError bypasses _run_node's except-Exception. A
    `pending` one was never dispatched, which is what `skipped` means
    everywhere else.
    """
    for nid, st in status.items():
        if st == "running":
            status[nid] = "cancelled"
        elif st == "pending":
            status[nid] = "skipped"


def _cascade_failures(deps: dict[str, list[str]], status: dict[str, str]) -> None:
    """Mark pending nodes with a failed/skipped dependency as skipped."""
    changed = True
    while changed:
        changed = False
        for nid, in_graph in deps.items():
            if status[nid] != "pending":
                continue
            if any(status[d] in ("failed", "skipped") for d in in_graph):
                status[nid] = "skipped"
                changed = True


async def _run_group(
    nids: list[str],
    *,
    by_id: dict[str, DagNodeSpec],
    node_backends: dict[str, Any],
    store: DagRunStore,
    backend: Any,
    workdir: str,
    roots: tuple[str, ...],
    session_nodes: SessionNodes,
    sandbox: Any,
    output_paths: dict[str, str],
    status: dict[str, str],
    errors: dict[str, str],
    prompt_written: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    node_activity: dict[str, dict],
    semaphore: asyncio.Semaphore,
    state_for: "Callable[[str, str | None, str], Any] | None" = None,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
    subagents_root: str | None = None,
) -> None:
    """Run one instance-group's nodes sequentially, in id order."""
    for nid in nids:
        await _run_node(
            by_id[nid],
            node_backends[nid],
            store=store,
            backend=backend,
            workdir=workdir,
            roots=roots,
            session_nodes=session_nodes,
            sandbox=sandbox,
            output_paths=output_paths,
            status=status,
            errors=errors,
            prompt_written=prompt_written,
            node_started_at=node_started_at,
            node_ended_at=node_ended_at,
            node_activity=node_activity,
            semaphore=semaphore,
            state_for=state_for,
            progress_publisher=progress_publisher,
            session_key=session_key,
            subagents_root=subagents_root,
        )


async def _write_node_transcript(store: DagRunStore, node_id: str, did: Any) -> None:
    """Persist what the node did on the way, one message per line.

    Its own file rather than a manifest field: the manifest is re-read on every
    poll of the graph, and only an opened node reads this. Failing to write it
    must not fail the node -- the account of a run is worth less than the run.
    """
    messages = getattr(did, "transcript", None)
    if isinstance(messages, list) and messages:
        try:
            await store.write_text(
                store.transcript_path(node_id),
                "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages),
            )
        except Exception as exc:  # noqa: BLE001 - an audit trail may not break the run
            logger.warning("DAG node {} transcript could not be written: {}", node_id, exc)


async def _add_node_to_instance_log(
    subagents_root: str | None,
    node: DagNodeSpec,
    did: Any,
    session_key: str | None,
    error: str | None = None,
) -> None:
    """Add this node's turn to the instance's own conversation.

    A node is one turn of an instance that a ``spawn`` call or a direct chat may
    also have talked to, so it belongs in the same file as those. Addressed from
    ``subagents_root`` -- ``<session_dir>/subagents``, which the caller already
    resolved -- rather than derived from the run directory, whose depth differs
    between the real layout and a store pointed somewhere else.
    """
    if not subagents_root:
        return
    handle = getattr(node, "instance", None) or node.id
    try:
        from raven.agent.subagent_history import add_turn_to_instance_log

        add_turn_to_instance_log(
            Path(subagents_root).parent,
            meta={"agent": node.subagent, "handle": handle, "session_key": session_key or ""},
            # Read off the activity rather than from the caller's `prompt`, which
            # is unbound when rendering it raised. A turn with no question of its
            # own is not merely missing a row: `foldDirectTurns` starts a message
            # at a `user` row, so the next turn's steps and answer merge into this
            # one -- and the live read does emit the question, so it appeared
            # while the node ran and vanished when it landed.
            prompt=getattr(did, "prompt", None),
            error=error,
            activity=did,
            kind="dag",
        )
    except Exception as exc:  # noqa: BLE001 - an audit trail may not break the run
        logger.warning("DAG node {} could not be added to its instance log: {}", node.id, exc)


async def _run_node(
    node: DagNodeSpec,
    agent_backend: Any,
    *,
    store: DagRunStore,
    backend: Any,
    workdir: str,
    roots: tuple[str, ...],
    session_nodes: SessionNodes,
    sandbox: Any,
    output_paths: dict[str, str],
    status: dict[str, str],
    errors: dict[str, str],
    prompt_written: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    node_activity: dict[str, dict],
    semaphore: asyncio.Semaphore,
    state_for: "Callable[[str, str | None, str], Any] | None" = None,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
    subagents_root: str | None = None,
) -> None:
    """Render, dispatch to the node's backend, and record one node."""
    async with semaphore:
        started_at_ms = _now_ms()
        did = None
        node_started_at[node.id] = started_at_ms
        # Set inside the gate, so a node still queued for a concurrency slot
        # stays `pending`: this is what tells a stop which nodes actually ran.
        status[node.id] = "running"
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {"run_id": store.run_id, "node": node.id, "status": "running", "started_at": started_at_ms},
        )
        await _write_node_status(session_key, store.run_id, node.id, node.subagent, "running")
        await _link_node_instance(session_key, store.run_id, node.id, node.subagent, node.instance)
        try:
            prompt = await render_prompt(
                node,
                backend=backend,
                cwd=workdir,
                output_paths=output_paths,
                runs_root=store.root,
                roots=roots,
                session_nodes=session_nodes,
            )
            prompt_path = store.prompt_path(node.id)
            output_path = store.output_path(node.id)
            await store.write_text(prompt_path, prompt)
            prompt_written.add(node.id)
            # The instance's message list, on the same terms as `spawn` and a
            # direct chat. A node that names an `instance` is asking to continue
            # that conversation; without this it started from empty and wrote
            # nothing back, so the handle bought nothing.
            node_state = (
                state_for(session_key or "", node.subagent, node.instance)
                if (state_for is not None and node.instance)
                else None
            )
            # Collected around the dispatch, exactly as a spawn does it: the
            # backend publishes into whatever is open, so a node gets the same
            # account of its tool calls and token cost that a spawned call gets,
            # with no backend knowing which of the two paths ran it. Keyed into
            # the live index for the same reason a spawn is: nothing of a node
            # reaches disk until it ends, so without this a panel watching a
            # node in flight has only its prompt to show.
            # The handle lock spans load-run-save, because the state is a whole-file
            # read-modify-write: two runs on one handle that interleave here lose
            # whichever wrote first, silently, and a later one can also read the
            # other's turns and answer as if they were its own. Same lock the direct
            # chat and the cli backend take, keyed by the handle rather than held on
            # a backend, so a spawn and a node on that handle queue against each
            # other. Re-entrant, so the cli backend's own acquire below passes
            # through.
            #
            # Taken inside the semaphore, not around it: a node waiting for the lock
            # then occupies a concurrency slot while it waits, which is a scheduling
            # cost, whereas taking it first would hold the handle while queueing for
            # a slot -- blocking every other run's nodes on that handle for longer.
            async with hold_handle(session_key or "", node.subagent, node.instance or node.id):
                state_kwargs = (
                    {"history": node_state.load(), "on_messages": node_state.save} if node_state is not None else {}
                )
                # Indexed by instance too, on the same terms and with the same
                # handle rule as `_add_node_to_instance_log`, so the conversation
                # view reaches a node's steps while it runs rather than only after
                # it lands.
                with activity.collecting(
                    live_key=node_live_key(store.run_id, node.id),
                    instance=(session_key or "", node.subagent, node.instance or node.id),
                    prompt=prompt,
                ) as did:
                    try:
                        result = await agent_backend.run(
                            prompt,
                            task_id=node.id,
                            workspace=Path(workdir),
                            executor=sandbox,
                            session_key=session_key,
                            instance=node.instance,
                            **state_kwargs,
                        )
                    finally:
                        node_activity[node.id] = did.as_meta()
            await store.write_text(output_path, result or "")
            status[node.id] = "completed"
            output_paths[node.id] = output_path
        except Exception as exc:  # noqa: BLE001 - record and continue
            logger.opt(exception=True).warning("DAG node {} failed: {}", node.id, exc)
            status[node.id] = "failed"
            errors[node.id] = str(exc)
        await _write_node_transcript(store, node.id, did)
        await _add_node_to_instance_log(subagents_root, node, did, session_key, errors.get(node.id))
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
    node_activity: dict[str, dict],
    store: DagRunStore,
    session_key: str | None = None,
    auto_instances: frozenset[str] = frozenset(),
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
                "instance_auto": nid in auto_instances,
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
            "instance_auto": nid in auto_instances,
            "started_at": node_started_at.get(nid),
            "ended_at": node_ended_at.get(nid),
            "prompt_file": prompt_file,
            "output_file": output_file,
            "error": errors.get(nid),
            # What the node did on the way, on the same terms a spawned call
            # records it: absent keys mean the transport could not say, not zero.
            **node_activity.get(nid, {}),
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

    summary = _tally(status)
    await store.write_manifest(manifest)
    await _record_outcome(store, status)
    return DagRunResult(
        run_id=store.run_id,
        dir=store.run_dir,
        terminal_outputs=terminal_outputs,
        files=files,
        summary=summary,
    )
