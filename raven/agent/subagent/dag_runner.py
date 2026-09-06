"""The deterministic ready-set scheduler for a sub-agent DAG (Raven-native).

Ported from the RavenX reference ``_dag/_runner.py`` but decoupled from
AgentScope: node execution goes through a :class:`SubagentBackend`
(``run(task, *, task_id, workspace, executor) -> str``) — the same adapter layer
the native subagent uses — instead of an AgentScope tool yielding
``ToolChunk``s, so ``_run_node`` just awaits a string result. Progress events go
through a plain ``ProgressPublisher`` callback (no spine, no scheduler).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from raven.agent.subagent import activity
from raven.agent.subagent.dag_adjudication import (
    CONTINUE,
    REPLAN,
    REPORT_DELIVERY_ATTEMPTS,
    AdjudicationDesk,
    deliver_report,
)
from raven.agent.subagent.dag_capabilities import AgentCapabilities
from raven.agent.subagent.dag_graph import DagNodeSpec, SubAgentDagSpec, graph_deps, validate_and_order
from raven.agent.subagent.dag_render import render_prompt
from raven.agent.subagent.dag_store import (
    DagRunStore,
    SessionNodes,
    index_guard,
    make_run_id,
    node_live_key,
    read_session_nodes,
)
from raven.agent.subagent.dag_verdict import Verdict
from raven.agent.subagent.instances import get_registry, hold_handle
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent_memory import (
    TRACE_BUDGET_S,
    EverosIdentity,
    prime_from_turn,
    record_memories,
    trace_session_id,
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


# (run_id, node_id, report, origin, *, awaiting_decision) -> awaitable. How a
# node's report reaches the main agent; the host supplies
# ``SubagentManager.announce_dag_exception``. ``awaiting_decision`` is False for a
# report the node has already been failed on, and the keyword is required rather
# than defaulted: the wrong value tells the model to answer a closed desk.
class ExceptionAnnouncer(Protocol):
    async def __call__(
        self, run_id: str, node_id: str, report: str, origin: dict, *, awaiting_decision: bool
    ) -> None: ...


# asyncio only holds a weak reference to a running task, so a fire-and-forget
# record has to be kept alive by its scheduler until it finishes.
_RECORD_TASKS: set[asyncio.Task] = set()


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
    replanned_into: str | None = None


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
    everos_for: "Callable[[str], EverosIdentity | None] | None" = None,
    mode_for: "Callable[[str, str | None, str], str | None] | None" = None,
    capabilities: dict[str, AgentCapabilities] | None = None,
    desk: AdjudicationDesk | None = None,
    adjudication_timeout_s: float = 600.0,
    judge_node: "Callable[..., Awaitable[Any]] | None" = None,
    announce_exception: ExceptionAnnouncer | None = None,
    max_continuations: int = 2,
    origin: dict | None = None,
    control_reachable: "Callable[[], bool] | None" = None,
    released: asyncio.Event | None = None,
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
    (raven/agent/subagent/history.py).

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
    sub-agent's prompt. See
    :func:`raven.agent.subagent.prompt_paths.check_confined`.

    Node ids are unique across the session, not just across this graph: the
    session index under ``run_root`` is read before validation, so a graph
    reusing an id an earlier run already took is refused, and one naming a node
    an earlier run *completed* resolves to that run's output, needing no
    ``depends_on`` entry for it. Such an entry is allowed and satisfied on
    sight; ``dag_graph.graph_deps`` is what keeps it out of the scheduling below,
    which only knows this run's statuses. Those two are separate questions --
    see :class:`.dag_store.SessionNodes`.

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

    ``desk``, when given, is where a node the graph suspended (``status[nid]
    == "exception"``) waits for the main agent's decision: once the ready set
    is empty with at least one node suspended, the run blocks in
    ``_await_adjudications`` on that node's answer instead of ending there.
    Leaving ``desk`` unset keeps the old behavior exactly -- an empty ready
    set always ends the run. ``adjudication_timeout_s`` bounds that wait per
    node, so one answer that never arrives fails just that node instead of
    blocking the run forever.

    A ``replan`` answer interrupts the round in flight rather than waiting for
    it to drain: it gives every unfinished node a terminal status naming the
    successor run and finalizes this one. The successor itself is submitted
    by the caller, not here -- this run only reports its id back as
    ``DagRunResult.replanned_into``.

    ``judge_node``, when given, is called after a node completes or fails to
    decide whether it actually accomplished its task; a bad verdict suspends it
    (the same ``exception`` status ``desk`` waits on) rather than letting it
    complete or fail outright, as long as ``desk`` and ``announce_exception``
    are both set and the node has continuations left under ``max_continuations``
    -- otherwise the verdict fails the node instead. ``announce_exception``
    delivers the report to the main agent through the same ``origin`` the host
    used to reach this run, the moment the node is suspended rather than only
    at the end of the run.

    ``released``, when given, is the event the host sets when the turn that owns
    this run has ended. Until it is set the adjudication wait has no deadline;
    ``adjudication_timeout_s`` is measured from the release. ``None`` clocks the
    wait from entry, which is what a backgrounded run wants.
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
    replanned_into: str | None = None

    # Built before the `try` below, not inside it: the cancellation handler reads
    # `status`, and a handler that covers the first await has to be able to.
    status: dict[str, str] = {nid: "pending" for nid in by_id}
    output_paths: dict[str, str] = {}
    errors: dict[str, str] = {}
    continuations: dict[str, str] = {}
    attempts: dict[str, int] = {}
    prompt_written: set[str] = set()
    node_started_at: dict[str, int] = {}
    node_ended_at: dict[str, int] = {}
    node_activity: dict[str, dict] = {}
    gate = semaphore if semaphore is not None else asyncio.Semaphore(max_concurrency)
    # This run's own memory-record pollers, so an outer cancellation of this
    # run's task (below) can reap the ones it already scheduled for completed
    # nodes -- `_RECORD_TASKS` is process-global and reachable by no
    # cancellation path at all.
    record_tasks: set[asyncio.Task] = set()

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
                # The line the whole graph was dispatched with. Per-node
                # summaries ride below; this one has no other way to reach a
                # reader, and the sheet above the composer is titled by it.
                "task_summary": spec.task_summary,
                "nodes": [
                    {
                        "id": node.id,
                        "subagent": node.subagent,
                        "node_summary": node.node_summary,
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
            if desk is not None and desk.replanned.is_set():
                plan = desk.take_plan()
                if plan is not None:
                    await _apply_replan(
                        plan,
                        status=status,
                        errors=errors,
                        published_terminal=published_terminal,
                        node_started_at=node_started_at,
                        node_ended_at=node_ended_at,
                        by_id=by_id,
                        session_key=session_key,
                        run_id=store.run_id,
                        progress_publisher=progress_publisher,
                    )
                    replanned_into = plan.run_id
                desk.replanned.clear()
                break
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
                suspended = [nid for nid, st in status.items() if st == "exception"]
                if not suspended or desk is None:
                    break
                # Nothing else can run and every suspended node has already given
                # its slot back, so waiting here costs the graph nothing.
                await _await_adjudications(
                    desk,
                    status,
                    errors,
                    continuations,
                    timeout_s=adjudication_timeout_s,
                    cancel=cancel,
                    released=released,
                )
                continue
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
                        everos_for=everos_for,
                        mode_for=mode_for,
                        capabilities=capabilities,
                        session_key=session_key,
                        subagents_root=subagents_root,
                        record_tasks=record_tasks,
                        attempts=attempts,
                        continuations=continuations,
                        desk=desk,
                        judge_node=judge_node,
                        announce_exception=announce_exception,
                        max_continuations=max_continuations,
                        origin=origin,
                        dependents=dependents,
                        adjudication_timeout_s=adjudication_timeout_s,
                        control_reachable=control_reachable,
                    )
                    for nids in groups.values()
                ),
                cancel,
                interrupt=desk.replanned if desk is not None else None,
            )

        result = await _finalize(
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
        result.replanned_into = replanned_into
        return result
    except asyncio.CancelledError:
        # `/stop` and the shutdown sweep stop a background run by cancelling its
        # task rather than setting `cancel`, so `_finalize` never runs. Without
        # this, the ids claimed at `init` would keep their index entry with no
        # `status`, and `read_session_nodes` would report them `running` forever:
        # neither reusable nor readable, for a run that is definitively over --
        # and the two refusals that produces contradict each other.
        _mark_stopped(status)
        await _record_outcome(store, status, cancelled=True)
        # This run's own memory-record pollers, scheduled for nodes that had
        # already completed before this cancellation landed, would otherwise
        # keep polling with nothing left to reap them.
        pending_records = [t for t in record_tasks if not t.done()]
        for t in pending_records:
            t.cancel()
        if record_tasks:
            await asyncio.gather(*record_tasks, return_exceptions=True)
        raise


async def _run_ready_groups(coros: Any, cancel: asyncio.Event | None, interrupt: asyncio.Event | None = None) -> None:
    """Await one scheduling round's group tasks.

    With no signal to race, this is a plain ``asyncio.gather`` (if that
    gather is itself cancelled from outside, gather cancels every task it is
    waiting on, same as before this function existed). With ``cancel``
    and/or ``interrupt`` given, races the round's group tasks against
    whichever signals are set, so an in-flight node is cancelled the instant
    either one fires, releasing its semaphore slot immediately rather than
    waiting for it to finish on its own.

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
    signals = [asyncio.ensure_future(e.wait()) for e in (cancel, interrupt) if e is not None]
    if not signals:
        await asyncio.gather(*tasks)
        return
    try:
        pending: set[asyncio.Future] = {*tasks, *signals}
        while True:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(s in done for s in signals) or all(t.done() for t in tasks):
                break
    finally:
        for signal in signals:
            if not signal.done():
                signal.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*signals, *tasks, return_exceptions=True)


async def _apply_replan(
    plan: Any,
    *,
    status: dict[str, str],
    errors: dict[str, str],
    published_terminal: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    by_id: dict[str, DagNodeSpec],
    session_key: str | None,
    run_id: str,
    progress_publisher: ProgressPublisher | None,
) -> None:
    """Give this run's unfinished nodes their outcome, the replan being their cause.

    Publishes each transition here rather than leaving it to the loop's own sweep:
    that sweep only announces `skipped` and `cancelled`, and widening it to
    `failed` would double-publish every node that failed on its own merit --
    `_run_node` already announces those and does not record them as published.

    A node already terminal on its own merit -- completed, failed, or skipped by
    an unrelated cascade -- is untouched. A completed node's output is what the
    new run references; a failed or skipped node's status and error are the real
    reason it ended, and overwriting them with the replan's would erase that.
    """
    reason = f"Superseded by replan into run {plan.run_id}: {plan.reason}"
    now = _now_ms()
    for nid, st in status.items():
        # Only the unfinished. A node already terminal on its own merit -- failed, or skipped
        # by an unrelated cascade -- keeps its status and its reason: overwriting them with
        # the replan's would erase why it actually ended, which is the one thing someone
        # reading this run afterwards is looking for. `_mark_stopped` guards the same way.
        if st not in ("running", "exception", "pending"):
            continue
        status[nid] = "skipped" if st == "pending" else ("cancelled" if st == "running" else "failed")
        errors[nid] = reason
        published_terminal.add(nid)
        node_started_at.setdefault(nid, now)
        node_ended_at[nid] = now
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {"run_id": run_id, "node": nid, "status": status[nid]},
        )
        await _write_node_status(session_key, run_id, nid, by_id[nid].subagent, status[nid])


def _tally(status: dict[str, str]) -> dict:
    """Count this run's nodes by terminal state."""
    return {
        "total": len(status),
        "completed": sum(1 for s in status.values() if s == "completed"),
        "failed": sum(1 for s in status.values() if s == "failed"),
        "skipped": sum(1 for s in status.values() if s == "skipped"),
        "cancelled": sum(1 for s in status.values() if s == "cancelled"),
    }


def _route_available(control_reachable: "Callable[[], bool] | None") -> bool:
    """Whether the main agent has a way to call `resolve_dag_node`.

    ``None`` means the host never wired the predicate, which has to keep reading
    as reachable: every runner fixture that predates the predicate omits it, and
    reading absent as unreachable would turn all of them into instant failures. A
    predicate that raises reads as unreachable, matching how the graph tool's
    acceptance text already treats one.
    """
    if control_reachable is None:
        return True
    try:
        return bool(control_reachable())
    except Exception:  # noqa: BLE001 - an unanswerable predicate is not a reachable route
        return False


def _exception_report(
    *,
    run_id: str,
    node: DagNodeSpec,
    verdict: Any,
    attempt: int,
    remaining: int,
    blocked: list[str],
    timeout_s: float,
    route_available: bool = True,
) -> str:
    """The text the main agent is woken with. Everything it needs to decide, once.

    The blocked list is not decoration: without it the agent is choosing between
    continuing and abandoning with no idea what abandoning costs.
    """
    lines = [
        f"DAG run {run_id}: node '{node.id}' ({node.subagent}) did not accomplish its task.",
        f"task: {node.node_summary}",
        f"category: {verdict.category or 'other'}",
        f"what is missing: {verdict.what_is_missing or '(the judge did not say)'}",
    ]
    if verdict.evidence:
        lines.append(f"evidence: {verdict.evidence}")
    if not verdict.evidence_complete:
        lines.append("evidence is incomplete: this sub-agent's transport publishes no per-step transcript.")
    lines.append(f"blocked while this waits: {', '.join(blocked) if blocked else '(no dependents)'}")
    if remaining <= 0:
        lines.append(
            f"This was attempt {attempt}; the continuation limit is reached, the node has failed and "
            "no adjudication is being awaited. Its dependents are skipped. This node will not be "
            "revisited; submit a fresh run_subagent_dag graph for the work if it still matters."
        )
        return "\n".join(lines)
    if not route_available:
        # The announce is not gated on suspension, so this text still reaches the
        # model for a node that has already failed. Naming a call would ask it to
        # answer something that accepts no answer, through a route it does not have.
        lines.append(
            f"attempt {attempt}; {remaining} continuation(s) left, but there is no route for you to "
            "answer this node (tool_call is not available), so it has failed and its dependents are "
            "skipped. This node will not be revisited; submit a fresh run_subagent_dag graph for the "
            "work if it still matters."
        )
        return "\n".join(lines)
    lines.append(
        f"attempt {attempt}; {remaining} continuation(s) left; "
        f"deciding within {timeout_s:g}s, restarted by each decision"
    )
    # The invocation rather than the bare call: `resolve_dag_node` is hidden from the
    # provider schema, so a model that reads its own tool list finds no such tool and
    # reports it cannot answer -- which spends the whole timeout and then reads as the
    # agent declining a question it was never able to make.
    lines.append(
        f'Answer by calling tool_call with name "resolve_dag_node" and arguments '
        f'{{"run_id": "{run_id}", "node_id": "{node.id}", "decision": "continue", '
        f'"message": "<what the node should try next>"}}, or the same with "decision": "abandon" '
        "to give up on this node and everything waiting on it. "
        "resolve_dag_node is not in your tool list; tool_call is how you reach it. "
        "Ask the user first if only they can supply what is missing."
    )
    lines.append(
        f'Or replan: tool_call with name "resolve_dag_node" and arguments '
        f'{{"run_id": "{run_id}", "node_id": "{node.id}", "decision": "replan", '
        f'"message": "<why the plan is changing>", "nodes": [<the graph to run instead>]}}. '
        "That stops this run and starts a new one from your nodes. Use it when what is "
        "missing is the plan rather than something you can hand this node: a step that "
        "cannot work as wired, a step nothing in the graph performs, work on the wrong "
        "sub-agent. Nodes this run completed are referenced, not re-declared -- name one in "
        "depends_on and read it with {{ <id>.output }}; everything else needs a new id."
    )
    return "\n".join(lines)


async def _apply_verdict(
    node: DagNodeSpec,
    *,
    store: DagRunStore,
    status: dict[str, str],
    errors: dict[str, str],
    output_paths: dict[str, str],
    node_output: str | None,
    attempt: int,
    desk: "AdjudicationDesk | None",
    judge_node: "Callable[..., Awaitable[Any]]",
    announce_exception: ExceptionAnnouncer | None,
    max_continuations: int,
    origin: dict | None,
    dependents: dict[str, list[str]],
    adjudication_timeout_s: float,
    control_reachable: "Callable[[], bool] | None" = None,
) -> None:
    """Turn a finished node's verdict into a status, and report a bad one.

    The last attempt reports too: the agent learns this line of the graph is dead
    while other branches are still running, rather than at the closing announce an
    hour later.
    """
    if status[node.id] not in ("completed", "failed"):
        return
    crashed = status[node.id] == "failed"
    try:
        verdict = await judge_node(
            node=node,
            store=store,
            output=node_output or "",
            error=errors.get(node.id, ""),
            crashed=crashed,
        )
    except Exception as exc:  # noqa: BLE001 - matches _verdict.judge's own fail-open contract
        logger.opt(exception=True).warning("DAG node {} verdict call raised, judging it accomplished: {}", node.id, exc)
        verdict = Verdict(accomplished=True)
    if verdict.accomplished:
        return
    reason = verdict.what_is_missing or "The node did not accomplish its task."
    errors[node.id] = reason
    # A node that did not accomplish its task must not be readable as anyone's
    # input, whatever happens next.
    output_paths.pop(node.id, None)
    remaining = max_continuations - (attempt - 1)
    answerable = _route_available(control_reachable)
    report = _exception_report(
        run_id=store.run_id,
        node=node,
        verdict=verdict,
        attempt=attempt,
        remaining=remaining,
        blocked=sorted(dependents.get(node.id, [])),
        timeout_s=adjudication_timeout_s,
        route_available=answerable,
    )
    # `origin` is part of this predicate because whoever is going to be asked is
    # reached through it: a node suspended on a report that cannot be delivered
    # waits out its whole timeout and then blames the answerer for not answering a
    # question they never received. The same reasoning applies to the return leg:
    # the model answers by naming a schema-hidden tool through `tool_call`, so
    # without that route the report is delivered to someone who cannot reply.
    deliverable = announce_exception is not None and answerable
    suspending = remaining > 0 and desk is not None and deliverable and origin is not None
    # Opened before the announce, not after: the announce can be answered
    # synchronously (a host that dispatches the turn inline, every test that
    # resolves from its announcer), and a desk opened afterwards would refuse
    # that answer and leave the node waiting out its whole timeout.
    if suspending:
        status[node.id] = "exception"
        desk.open(node.id)
    else:
        status[node.id] = "failed"
        if not answerable and remaining > 0:
            # Appended, not substituted: this is the run record's own `error` field,
            # which the manifest carries and `dag_status` renders, so it is the only
            # place either fact reaches a reader asking about the *node*. The report
            # is durable too -- it persists as the injected turn -- but it is found by
            # reading the conversation, not by reading the run. Why nobody could be
            # asked and why the node failed are different facts, and someone looking
            # at the node is usually after the second.
            errors[node.id] = (
                f"{reason} There is no route to answer this node (tool_call is not "
                "available to the agent), so it could not be adjudicated."
            )
    if announce_exception is not None and origin is not None:
        # The node's fate must not depend on delivery, so the retries are inside and
        # what comes back is the failure that outlived them.
        failure = await deliver_report(
            partial(announce_exception, store.run_id, node.id, report, origin, awaiting_decision=suspending),
            what=f"node {node.id}",
        )
        if failure is not None and suspending:
            # The desk is open and the node is waiting on a decision the agent
            # was never told to make. Left alone this stalls for the full
            # adjudication timeout and then blames the agent for not answering
            # a question it never received, instead of the real cause.
            desk.close(node.id)
            status[node.id] = "failed"
            errors[node.id] = (
                f"The exception report could not be delivered in {REPORT_DELIVERY_ATTEMPTS} attempts: {failure}"
            )


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
    everywhere else. An `exception` node was waiting on an adjudication that is
    never coming now, and would otherwise outlive the run that owns it.
    """
    for nid, st in status.items():
        if st in ("running", "exception"):
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
            # `exception` is deliberately absent: a suspended node's dependents
            # must stay pending, because the adjudication may yet continue it.
            if any(status[d] in ("failed", "skipped") for d in in_graph):
                status[nid] = "skipped"
                changed = True


async def _await_adjudications(
    desk: AdjudicationDesk,
    status: dict[str, str],
    errors: dict[str, str],
    continuations: dict[str, str],
    *,
    timeout_s: float,
    cancel: asyncio.Event | None,
    released: asyncio.Event | None = None,
) -> None:
    """Block until every suspended node has an answer, the wait runs out, or one
    answer replans the graph.

    Reached only when nothing else in the graph can run: the report went to the
    main agent the moment the node was suspended, so this wait costs the graph
    nothing it could otherwise be doing.

    Every open node waits against one deadline, all at once rather than one
    after another -- N nodes awaited in series would cost N times `timeout_s`,
    contradicting the cap `adjudication_timeout_s` is configured against.

    That deadline measures silence, not the whole round: each decision that
    lands restarts it for the nodes still waiting. Reports reach the agent one
    turn at a time, because a conversation lane is serial, so a fixed deadline
    for the round charged a node for the time its report spent queued behind
    another node's -- far enough down the queue and a node timed out having
    never been asked at all. Silence is the thing worth failing on: it is what
    says nobody is coming, which one deadline for the round cannot distinguish
    from an agent steadily working through the queue.

    There is no deadline at all while ``released`` is given and unset: the run
    is bound to a turn that is still running, and that turn is the liveness
    signal -- the agent is in a tool loop that has the report in hand, and may
    be asking the user something only they know. The clock starts when the
    event fires, which is the moment the turn ended without deciding.

    A continued node goes back to `pending` with its message parked in
    ``continuations`` -- its dependencies are still `completed`, so the next pass
    of the ready set picks it up like any other node. Abandoned and timed-out
    nodes take the ordinary failure path, which cascades to their dependents.

    A replan ends the round for every node at once, answered or not, because the
    graph they belong to is being replaced. They stay `exception` and
    ``_apply_replan`` gives them their outcome on the next pass of the loop.

    `status` and the desk are two independent records of what is suspended. A
    node this call was never given a desk entry for (`exception` in `status`
    but not `desk.is_open`) can never be resolved -- nothing will ever call
    `resolve_dag_node` for it -- so it is failed outright before the wait
    below, instead of returning with nothing changed and inviting the caller
    to loop back here with no `await` in between.
    """
    for nid, st in status.items():
        if st == "exception" and not desk.is_open(nid):
            status[nid] = "failed"
            errors[nid] = "No adjudication was ever opened for this node, so it could not be resolved."
    open_nodes = sorted(desk.open_nodes())
    if not open_nodes:
        return
    waiters = {nid: desk.waiter(nid) for nid in open_nodes}
    node_tasks = {nid: asyncio.create_task(event.wait()) for nid, event in waiters.items()}
    stop = asyncio.create_task(cancel.wait()) if cancel is not None else None
    unbind = asyncio.create_task(released.wait()) if released is not None and not released.is_set() else None
    # Raced alongside the per-node waiters, because one node's replan ends the
    # whole round: the graph these nodes belong to is being replaced, so a
    # sibling's answer can no longer change anything. Waiting for it is not
    # merely wasted -- on a bound run there is no deadline, and the lane that
    # would answer the sibling is the same one blocked in `resolve_dag_node`
    # awaiting this run's task, so the two wait on each other forever.
    replan = asyncio.create_task(desk.replanned.wait())
    loop = asyncio.get_running_loop()
    deadline: float | None = None if unbind is not None else loop.time() + timeout_s
    cancelled = False
    replanned = False
    try:
        node_pending = set(node_tasks.values())
        while node_pending:
            budget = None if deadline is None else max(0.0, deadline - loop.time())
            waiting_on: set[asyncio.Future] = {*node_pending, replan}
            if stop is not None:
                waiting_on.add(stop)
            if unbind is not None and not unbind.done():
                waiting_on.add(unbind)
            done, _ = await asyncio.wait(waiting_on, timeout=budget, return_when=asyncio.FIRST_COMPLETED)
            if stop is not None and stop in done:
                cancelled = True
                break
            if replan in done:
                replanned = True
                break
            if unbind is not None and unbind in done:
                deadline = loop.time() + timeout_s
            decided = done & node_pending
            node_pending -= decided
            still_bound = unbind is not None and not unbind.done()
            if decided:
                deadline = None if still_bound else loop.time() + timeout_s
            elif deadline is not None and loop.time() >= deadline:
                break
        if cancelled:
            return
        for nid in open_nodes:
            event = waiters[nid]
            answer = desk.take(nid) if event.is_set() else None
            if answer is None:
                desk.close(nid)
                if replanned:
                    # Never asked, rather than asked and ignored: left at
                    # "exception" for `_apply_replan` to name the successor as
                    # its cause, for the same reason the REPLAN answer below is.
                    # Calling it a timeout would blame the agent for a silence
                    # its own replan is what ended.
                    continue
                status[nid] = "failed"
                errors[nid] = f"Nothing was decided for {timeout_s:g}s, so the node timed out."
                continue
            if answer.decision == CONTINUE and answer.message:
                continuations[nid] = answer.message
                status[nid] = "pending"
            elif answer.decision == CONTINUE:
                status[nid] = "failed"
                errors[nid] = (
                    "The main agent chose to continue but sent no message, so the node failed instead of resuming."
                )
            elif answer.decision == REPLAN:
                # Left at "exception": `_apply_replan`, at the top of the next loop
                # pass, is the single writer of a replanned node's terminal status
                # and reason (naming the successor run). Writing "failed" here would
                # both misname the cause and make `_apply_replan`'s own terminal-node
                # guard skip this node as already resolved, since it checks this same
                # `status` dict and only converts "running"/"exception"/"pending".
                continue
            else:
                status[nid] = "failed"
                errors[nid] = "The main agent abandoned this node."
    finally:
        if stop is not None and not stop.done():
            stop.cancel()
        if unbind is not None and not unbind.done():
            unbind.cancel()
        if not replan.done():
            replan.cancel()
        # Reached on every exit, including a hard Task.cancel() on the run
        # itself (as opposed to setting the soft `cancel` Event) raising
        # CancelledError right out of asyncio.wait above: that route skips
        # past the per-node close/take calls in the loop entirely, so without
        # this an open node's wait task would keep running unsupervised, and
        # the desk would keep telling is_open yes for a run that no longer
        # exists to act on its answer.
        for task in node_tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(
            *node_tasks.values(),
            *([stop] if stop is not None else []),
            *([unbind] if unbind is not None else []),
            replan,
            return_exceptions=True,
        )
        for nid in open_nodes:
            desk.close(nid)


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
    everos_for: "Callable[[str], EverosIdentity | None] | None" = None,
    mode_for: "Callable[[str, str | None, str], str | None] | None" = None,
    capabilities: dict[str, AgentCapabilities] | None = None,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
    subagents_root: str | None = None,
    record_tasks: "set[asyncio.Task] | None" = None,
    attempts: dict[str, int] | None = None,
    continuations: dict[str, str] | None = None,
    desk: "AdjudicationDesk | None" = None,
    judge_node: "Callable[..., Awaitable[Any]] | None" = None,
    announce_exception: ExceptionAnnouncer | None = None,
    max_continuations: int = 2,
    origin: dict | None = None,
    dependents: dict[str, list[str]] | None = None,
    adjudication_timeout_s: float = 600.0,
    control_reachable: "Callable[[], bool] | None" = None,
) -> None:
    """Run one instance-group's nodes sequentially, in id order."""
    for nid in nids:
        await _run_node(
            by_id[nid],
            node_backends[nid],
            by_id=by_id,
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
            everos_for=everos_for,
            mode_for=mode_for,
            capabilities=capabilities,
            progress_publisher=progress_publisher,
            session_key=session_key,
            subagents_root=subagents_root,
            record_tasks=record_tasks,
            attempts=attempts,
            continuations=continuations,
            desk=desk,
            judge_node=judge_node,
            announce_exception=announce_exception,
            max_continuations=max_continuations,
            origin=origin,
            dependents=dependents,
            adjudication_timeout_s=adjudication_timeout_s,
            control_reachable=control_reachable,
        )


async def _write_node_transcript(store: DagRunStore, node_id: str, did: Any, attempt: int = 1) -> None:
    """Persist what the node did on the way, one message per line.

    Its own file rather than a manifest field: the manifest is re-read on every
    poll of the graph, and only an opened node reads this. Failing to write it
    must not fail the node -- the account of a run is worth less than the run.

    Written twice, to the latest-attempt file the judge reads and to this
    attempt's own archive. Without the second, a continued node's earlier
    transcript is overwritten and the evidence its verdict rested on is gone,
    while that same attempt's prompt and output stay readable.
    """
    messages = getattr(did, "transcript", None)
    # An empty list is published, not absent: a backend that answered directly
    # this time says so by publishing nothing. Skipping the write would leave the
    # previous attempt's transcript in place, and the judge -- which reads the
    # latest file to see the attempt in front of it -- would be handed the last
    # one's evidence and told it was complete.
    if isinstance(messages, list):
        text = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
        for path in (store.transcript_path(node_id), store.attempt_transcript_path(node_id, attempt)):
            try:
                await store.write_text(path, text)
            except Exception as exc:  # noqa: BLE001 - an audit trail may not break the run
                logger.warning("DAG node {} transcript could not be written to {}: {}", node_id, path, exc)


async def _previous_output(store: DagRunStore, node_id: str) -> str:
    """The last attempt's answer, for a stateless node's follow-up prompt."""
    try:
        return await store.read_text(store.output_path(node_id))
    except Exception:  # noqa: BLE001 - a missing prior answer is not worth failing the retry
        return "(the previous attempt left no output)"


async def _add_node_to_instance_log(
    subagents_root: str | None,
    node: DagNodeSpec,
    did: Any,
    session_key: str | None,
    error: str | None = None,
    output: str | None = None,
) -> list[dict[str, Any]]:
    """Add this node's turn to the instance's own conversation, and return it.

    A node is one turn of an instance that a ``spawn`` call or a direct chat may
    also have talked to, so it belongs in the same file as those. Addressed from
    ``subagents_root`` -- ``<session_dir>/subagents``, which the caller already
    resolved -- rather than derived from the run directory, whose depth differs
    between the real layout and a store pointed somewhere else.

    Returns the turn it just logged (empty on an early return or a failed
    write), so a caller that also has to hand this node's conversation to
    everos for extraction reads back exactly what landed on disk instead of
    building its own copy that could drift from it.
    """
    if not subagents_root:
        return []
    handle = getattr(node, "instance", None) or node.id
    try:
        from raven.agent.subagent.history import add_turn_to_instance_log

        return add_turn_to_instance_log(
            Path(subagents_root).parent,
            meta={
                "agent": node.subagent,
                "handle": handle,
                "session_key": session_key or "",
                "node_summary": node.node_summary,
            },
            # Read off the activity rather than from the caller's `prompt`, which
            # is unbound when rendering it raised. A turn with no question of its
            # own is not merely missing a row: `foldDirectTurns` starts a message
            # at a `user` row, so the next turn's steps and answer merge into this
            # one -- and the live read does emit the question, so it appeared
            # while the node ran and vanished when it landed.
            prompt=getattr(did, "prompt", None),
            # Passed for the same reason `spawn` and the direct chat pass it: a
            # transport that never narrates reports no closing text, and without
            # `output` the turn then carries no answer row at all -- the reader
            # folds a message from one `user` row to the next, so the node's
            # conclusion would land inside the following turn.
            output=output,
            error=error,
            activity=did,
            kind="dag",
        )
    except Exception as exc:  # noqa: BLE001 - an audit trail may not break the run
        logger.warning("DAG node {} could not be added to its instance log: {}", node.id, exc)
        return []


async def _run_node(
    node: DagNodeSpec,
    agent_backend: Any,
    *,
    by_id: dict[str, DagNodeSpec],
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
    everos_for: "Callable[[str], EverosIdentity | None] | None" = None,
    mode_for: "Callable[[str, str | None, str], str | None] | None" = None,
    capabilities: dict[str, AgentCapabilities] | None = None,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
    subagents_root: str | None = None,
    record_tasks: "set[asyncio.Task] | None" = None,
    attempts: dict[str, int] | None = None,
    continuations: dict[str, str] | None = None,
    desk: "AdjudicationDesk | None" = None,
    judge_node: "Callable[..., Awaitable[Any]] | None" = None,
    announce_exception: ExceptionAnnouncer | None = None,
    max_continuations: int = 2,
    origin: dict | None = None,
    dependents: dict[str, list[str]] | None = None,
    adjudication_timeout_s: float = 600.0,
    control_reachable: "Callable[[], bool] | None" = None,
) -> None:
    """Render, dispatch to the node's backend, and record one node."""
    async with semaphore:
        attempt = (attempts or {}).get(node.id, 0) + 1
        if attempts is not None:
            attempts[node.id] = attempt
        follow_up = (continuations or {}).pop(node.id, None)
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
        node_output: str | None = None
        try:
            rendered_prompt = await render_prompt(
                node,
                backend=backend,
                cwd=workdir,
                output_paths=output_paths,
                runs_root=store.root,
                roots=roots,
                session_nodes=session_nodes,
                run_id=store.run_id,
                by_id=by_id,
                capabilities=capabilities,
            )
            prompt = rendered_prompt
            if follow_up is not None:
                # A node with an instance is resuming a conversation whose history
                # is already on disk, so restating the task would only compete with
                # it. A stateless one has no history at all, so the whole task has
                # to travel with the follow-up or it starts from nothing.
                previous = await _previous_output(store, node.id)
                prompt = (
                    follow_up
                    if node.instance
                    else f"{prompt}\n\nYour previous attempt returned:\n{previous}\n\nNow: {follow_up}"
                )
            prompt_path = store.prompt_path(node.id)
            output_path = store.output_path(node.id)
            if attempt == 1:
                # `prompt_path` is the task the judge and dag_status/read_node show
                # for this node, so it must stay fixed at the original render even
                # once a later attempt substitutes a follow-up into `prompt`.
                await store.write_text(prompt_path, rendered_prompt)
            await store.write_text(store.attempt_prompt_path(node.id, attempt), prompt)
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
            # The effort level, on the same terms. The `instance` goes in rather
            # than the handle this node dispatches under: a node that names none
            # has no override to find, so the resolver answers with the session's
            # tier -- clamped to this agent's menu, and falling through to the
            # agent's own default only when there is no tier to inherit -- rather
            # than each lane deciding again.
            node_mode = mode_for(session_key or "", node.subagent, node.instance) if mode_for is not None else None
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
                if node.mcps is not None:
                    state_kwargs["mcps"] = node.mcps
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
                            mode=node_mode,
                            **state_kwargs,
                        )
                    finally:
                        node_activity[node.id] = did.as_meta()
            # The whole answer when the reply cap cut one: the in-context copy of
            # a terminal output is capped again on the way out (see
            # `_terminal_outputs`), and this file is what the reader, the next
            # node's placeholder, and the record all resolve to. The per-attempt
            # archive keeps the same uncapped text, for the same reason.
            persisted = activity.persisted_output(did, result) or ""
            await store.write_text(output_path, persisted)
            await store.write_text(store.attempt_output_path(node.id, attempt), persisted)
            node_output = result
            status[node.id] = "completed"
            output_paths[node.id] = output_path
            # An earlier attempt's reason is still in `errors`, and nothing else
            # pops it: a continuation that finally succeeds would otherwise reach
            # the manifest reading `completed` with the failure that made it
            # retry still attached. Only reached when this attempt returned, so a
            # real failure of this attempt is recorded after it, not lost.
            errors.pop(node.id, None)
        except Exception as exc:  # noqa: BLE001 - record and continue
            logger.opt(exception=True).warning("DAG node {} failed: {}", node.id, exc)
            status[node.id] = "failed"
            errors[node.id] = str(exc)
        # Written before the verdict, not after: the production judge reads this
        # file back through `store.transcript_path` to build its evidence, so
        # writing it later meant a first attempt was always judged with no
        # transcript, and a continuation's judge read the previous attempt's.
        await _write_node_transcript(store, node.id, did, attempt)
        if judge_node is not None:
            await _apply_verdict(
                node,
                store=store,
                status=status,
                errors=errors,
                output_paths=output_paths,
                node_output=node_output,
                attempt=attempt,
                desk=desk,
                judge_node=judge_node,
                announce_exception=announce_exception,
                max_continuations=max_continuations,
                origin=origin,
                dependents=dependents or {},
                adjudication_timeout_s=adjudication_timeout_s,
                control_reachable=control_reachable,
            )
        turn = await _add_node_to_instance_log(subagents_root, node, did, session_key, errors.get(node.id), node_output)
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
    # Scheduled after the `async with semaphore` above has already exited, so a
    # node's slot is released the moment its own work is done -- not held for
    # however long the recorder's poll budget takes.
    identity = everos_for(node.subagent) if everos_for is not None else None
    if identity is not None:
        _schedule_node_memory(
            node, store=store, identity=identity, session_key=session_key, record_tasks=record_tasks, turn=turn
        )


def _schedule_node_memory(
    node: DagNodeSpec,
    *,
    store: DagRunStore,
    identity: EverosIdentity,
    session_key: str | None,
    record_tasks: "set[asyncio.Task] | None" = None,
    turn: list[dict[str, Any]] | None = None,
) -> None:
    """Fire off this node's Memory record without waiting on it.

    Never awaited by the dispatch path: everos extraction runs an LLM, and the
    run must not wait on the host's bookkeeping (see `record_memories`'s own
    docstring, and the same reasoning `SubagentManager._schedule_memory_record`
    already applies to spawn and a direct chat). ``turn`` is only read for a
    ``trace`` identity, whose record has no other conversation to poll for.
    """
    task = asyncio.create_task(
        _record_node_memory(node, store=store, identity=identity, session_key=session_key, turn=turn)
    )
    # `_RECORD_TASKS` is a GC anchor only (asyncio holds just a weak reference
    # to a running task): the actual cancellation path is `record_tasks`,
    # this run's own set, which `run_dag`'s cancellation branch reaps.
    _RECORD_TASKS.add(task)
    task.add_done_callback(_RECORD_TASKS.discard)
    if record_tasks is not None:
        record_tasks.add(task)
        task.add_done_callback(record_tasks.discard)


async def _record_node_memory(
    node: DagNodeSpec,
    *,
    store: DagRunStore,
    identity: EverosIdentity,
    session_key: str | None,
    turn: list[dict[str, Any]] | None = None,
) -> None:
    """Write this node's Memory record, swallowing any failure.

    Written through the store rather than to a local path: a DAG run's files go
    to the DAG core's file backend, which may not be this filesystem.
    """
    if identity.source == "trace":
        # The host owns both the write and the read here, so it mints the join
        # key instead of resolving one the node's own run committed.
        session_id = trace_session_id(node.subagent, f"{store.run_id}:{node.id}")
        rows = turn or []

        async def _resolve() -> str | None:
            return session_id

        async def _prime(sid: str) -> bool:
            return await prime_from_turn(identity=identity, session_id=sid, turn=rows)

        prime, budget = _prime, TRACE_BUDGET_S
    else:

        async def _resolve() -> str | None:
            # `instance or id` mirrors CliAgentBackend.run's own derivation
            # (cli_agent.py:368), which is the handle the registry row was
            # committed under. A node naming no instance still has a row, keyed by
            # its node id, so keying on `instance` alone would drop the record for
            # the common case.
            handle = node.instance or node.id
            # `session_key or "default"` mirrors CliAgentBackend.run's own key
            # (cli_agent.py:445), which is what the registry row was committed
            # under; looking up under `session_key or ""` instead would silently
            # miss every row for a `run_dag(session_key=None)` call.
            agent_id = await get_registry().lookup(session_key or "default", node.subagent, handle)
            return f"{identity.session_prefix}{agent_id}" if agent_id else None

        prime, budget = None, None

    async def _write(text: str) -> None:
        await store.write_text(store.memory_path(node.id), text)

    try:
        kwargs = {"budget_s": budget} if budget is not None else {}
        await record_memories(
            agent=node.subagent,
            identity=identity,
            resolve_session_id=_resolve,
            write=_write,
            instance=node.instance,
            prime=prime,
            **kwargs,
        )
    except Exception:  # noqa: BLE001 - a record must never fail a node
        logger.opt(exception=True).warning("Memory record for DAG node {} failed", node.id)


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
