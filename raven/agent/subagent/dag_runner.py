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
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from raven.agent.subagent import activity
from raven.agent.subagent.backends.base import llm_error_reply, optional_keyword
from raven.agent.subagent.dag_adjudication import (
    CONTINUE,
    REASSIGN,
    REPLAN,
    REPORT_DELIVERY_ATTEMPTS,
    AdjudicationDesk,
    ReassignPlan,
    deliver_report,
)
from raven.agent.subagent.dag_capabilities import AgentCapabilities
from raven.agent.subagent.dag_graph import (
    DagNodeSpec,
    SubAgentDagSpec,
    graph_deps,
    guards_of,
    may_not_execute,
    validate_and_order,
)
from raven.agent.subagent.dag_guard import DONE, Atom, Guard, Value, evaluate
from raven.agent.subagent.dag_handover import (
    HandoverTurn,
    load_handover_turns,
    render_handover,
)
from raven.agent.subagent.dag_render import render_prompt
from raven.agent.subagent.dag_store import (
    NOT_EXECUTED,
    DagRunStore,
    index_guard,
    make_run_id,
    node_live_key,
    read_session_nodes,
    tally_node_statuses,
)
from raven.agent.subagent.dag_verdict import RouteUndeterminedError, Verdict
from raven.agent.subagent.instance_log import message_rows
from raven.agent.subagent.instances import get_registry, hold_handle
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent_memory import (
    TRACE_BUDGET_S,
    MemoryScope,
    prime_from_turn,
    record_memories,
    started_backend,
    trace_session_id,
)
from raven.context_engine.segments.render import dispatch_language_line
from raven.contracts.subagent_backend import SubagentNoAnswerError
from raven.observability import semconv
from raven.providers.base import bound_llm_detail
from raven.tracing import trace

# In-context cap for terminal outputs returned to the main agent; the on-disk
# .out.md always holds the full text.
_MAX_OUTPUT_CHARS = 128000
_MAX_ERROR_CHARS = 500
#: How much of an attempt's reason is kept for a later move report and handover
#: request. One line, not the whole error: this text is repeated per attempt.
_MAX_ATTEMPT_REASON_CHARS = 300

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
    """How a node's report reaches the main agent as its own turn.

    ``awaiting_decision`` says whether the node is suspended on an answer.
    ``informational`` says the node is still running and nothing about it has
    changed: a stall notice. It is a third state, not a flavour of "no decision
    pending" -- an announcer that reads ``awaiting_decision=False`` alone as
    "the node has failed" would wake the model with a false state, which is
    what the stall notice did before this keyword existed.

    ``output`` is what the node itself returned on the attempt the report is
    about -- its error, when it raised -- for a reader of the conversation, who
    is shown that rather than the brief. ``None`` for a report about no attempt
    (a stall notice). The report stays the model's whole text.
    """

    async def __call__(
        self,
        run_id: str,
        node_id: str,
        report: str,
        origin: dict,
        *,
        awaiting_decision: bool,
        informational: bool = False,
        output: str | None = None,
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


STALL_NOTICE_S = 600.0
"""How long a running node may go without a sign of life -- a tool call, a step,
console bytes -- before the main agent is told. A notice, not a timeout: nothing
is cancelled. Measured 2026-09-01: a coding node sat wedged on one dead LLM call
for 28 minutes and the only watcher was the owner's own eyes on the TUI; the
oncall side watches GPU jobs, nobody watched the nodes. Module-level so a test
can shrink it; per-graph plumbing can follow if one run ever needs another
value."""


async def _watch_stall(
    did: Any,
    *,
    run_id: str,
    node: DagNodeSpec,
    origin: dict | None,
    announce_exception: ExceptionAnnouncer | None,
    progress_publisher: ProgressPublisher | None,
    notice_s: float | None = None,
) -> None:
    """Tell the main agent when a running node stops showing signs of life.

    Cancelled by the dispatch that started it, so it never outlives its node.
    One notice per silent stretch: after announcing, it re-arms only once the
    node moves again, so a node that stays wedged is reported once rather than
    on every poll. Nothing here may fail the node -- every beat swallows its
    own errors.
    """
    quiet_s = STALL_NOTICE_S if notice_s is None else notice_s
    announced_for: int | None = None
    poll_s = max(0.01, min(30.0, quiet_s / 10))
    while True:
        await asyncio.sleep(poll_s)
        try:
            last = did.last_event_ms or did.started_at_ms
            if _now_ms() - last < quiet_s * 1000 or announced_for == last:
                continue
            announced_for = last
            minutes = max(1, int((_now_ms() - last) / 60000))
            await _emit(
                progress_publisher,
                "dag_node_stalled",
                {"run_id": run_id, "node": node.id, "quiet_ms": _now_ms() - last},
            )
            if announce_exception is None or origin is None:
                continue
            await announce_exception(
                run_id,
                node.id,
                (
                    f"Stall notice: node '{node.id}' (agent '{node.subagent}') has shown no sign of "
                    f"life for {minutes} minutes -- no tool call, no step, no output. It has not "
                    f"failed and nothing is waiting for an answer; this is information, not a "
                    f"suspension. If the rest of the run is moving, waiting is fine. If the node "
                    f"looks wedged, cancel the run and re-dispatch -- staged workspaces and instance "
                    f"handles survive a cancel. No further notice comes unless it moves and stalls "
                    f"anew."
                ),
                origin,
                # The announcer's contract requires this keyword. Without it the
                # production announcer raised TypeError, the beat below swallowed
                # it, and the notice never left this coroutine: the progress event
                # fired while the main agent heard nothing.
                awaiting_decision=False,
                # And the notice is a third state: the node is still running. An
                # announcer reading "no decision pending" alone would head the
                # injected turn with "has failed", which is the opposite of what
                # the report says; a bound foreground call drops it, on purpose,
                # because the model inside that call cannot act on anything
                # until the call returns (the progress event above still reaches
                # the panel).
                informational=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a notice must never fail the node
            logger.opt(exception=True).debug("stall watch for node {} skipped a beat", node.id)


async def _write_node_status(
    session_key: str | None,
    run_id: str,
    node_id: str,
    agent: str,
    status: str,
    *,
    reason: str | None = None,
    selected_routes: list[str] | None = None,
    route_evidence: str | None = None,
    assignment: dict[str, Any] | None = None,
) -> None:
    """Best-effort registry write for one node's status, swallowing any failure.

    A status row is never worth failing -- or hanging -- a node over, the same
    reasoning ``_emit`` already applies to progress events: a registry write
    (unlike ``InstanceRegistry``'s own internal ``OSError`` handling on flush)
    can also raise on read -- e.g. a corrupt/non-UTF-8 registry file surfaces
    as a ``UnicodeDecodeError`` from ``_load`` -- and it can stall behind lock
    contention; neither may abort or block the node (or, when this runs from
    ``_finalize``'s reconciliation pass, the whole completed run).

    ``reason``, ``selected_routes`` and ``route_evidence`` are the node's branch
    outcome, present on the same terms as on its finish event and in the manifest.
    A terminal write passes them, so a run read back before it finalizes still says
    what a branch decided.

    ``assignment`` is ``_assignment_fields``' answer for a node that moved: the
    history and the attempt identity the row must keep carrying. The record is
    rebuilt on every write, so a later write that dropped them would erase the
    only durable record of who runs the node now.
    """
    if not session_key:
        return
    # Forwarded only when present: a write with no outcome -- every write of a
    # graph without branches -- passes no keyword, only the five positional arguments.
    outcome = {
        key: value
        for key, value in (("reason", reason), ("selected_routes", selected_routes), ("route_evidence", route_evidence))
        if value is not None
    }
    if assignment and "attempt_agents" in assignment:
        outcome.update(
            attempt_agents=list(assignment["attempt_agents"]),
            instance=assignment.get("instance"),
            instance_auto=bool(assignment.get("instance_auto")),
        )
    try:
        await asyncio.wait_for(
            get_registry().upsert_dag_node(session_key, run_id, node_id, agent, status, **outcome),
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
    # Internal only, never serialized here: the node each id would dispatch as,
    # and where each graph-named instance group ended up. A capture projects
    # them onto a saved graph; no reader of a run consumes them yet.
    execution_nodes: dict[str, DagNodeSpec] = field(default_factory=dict)
    instance_assignments: dict[tuple[str, str], tuple[str, str | None]] = field(default_factory=dict)


def _node_pair(node: DagNodeSpec) -> tuple[str, str | None]:
    """The agent and handle a node dispatches under."""
    return (node.subagent, node.instance)


def _source_key(agent: str, instance: str | None, node_id: str) -> tuple[str, str]:
    """The key a node's dispatch group is staged and seeded under.

    A node naming no handle still runs alone, and two such nodes of one agent
    are not a group: their own ids keep them apart.
    """
    return (agent, instance or f"\x00node\x00{node_id}")


def _group_key(node: DagNodeSpec) -> tuple[str, str]:
    """The pair a node's sequential group is keyed by."""
    return _source_key(node.subagent, node.instance, node.id)


@dataclass
class _PendingHandover:
    """The conversation a moved group's new session is seeded with.

    Built once, after every source attempt has settled, and claimed by the first
    moved node that actually dispatches on the destination pair. ``anchor_node_id``
    names the node the move was decided for: its task is the turn the budget
    always keeps whole, and its own failure is what a source without a usable
    history falls back to.
    """

    anchor_node_id: str
    source_agent: str
    source_instance: str | None
    target_agent: str
    target_instance: str | None
    target_named: bool
    movers: frozenset[str]
    turns: tuple[HandoverTurn, ...]
    task: str
    notices: tuple[str, ...] = ()


@dataclass
class _StagedMove:
    """A move the desk accepted whose source attempts have not all settled.

    While staged its source group is unavailable for new dispatch: ready entries
    on that pair are withheld, a queued group entry returns to the scheduler
    when it reaches the gate, and the move installs only once
    ``ReassignmentState.active_attempts`` holds no attempt on the source pair.
    """

    node_id: str
    source_agent: str
    source_instance: str | None
    target_agent: str
    target_stateful: bool
    target_instance: str | None
    instance_auto: bool
    revision: int
    members: frozenset[str]
    backends: dict[str, Any]


@dataclass
class ReassignmentState:
    """One run's node assignments, and the moves that change them.

    ``effective_nodes`` and ``backends`` are the pair a dispatch reads: the
    submitted node and its resolved backend until a move installs, another
    agent's pair afterwards. Every attempt freezes the pair it started with in
    ``last_attempt_nodes``, so a later move cannot relabel work already running.
    ``attempt_agents`` and ``attempt_reasons`` are positional, one entry per
    attempt of the node; a successful attempt's reason is the empty string.

    ``original_groups`` keeps each named instance group's membership, keyed by
    every pair that group has dispatched under, and ``group_current`` maps the
    pair the graph named to where the group ended up -- terminal members stay
    where they ran, but the session a capture replays belongs to the group.
    ``pending_moves`` and ``pending_handovers`` are keyed by
    ``(source agent, source handle or node id)``: a handle for a named group,
    the node id for a node that runs alone.
    """

    effective_nodes: dict[str, DagNodeSpec] = field(default_factory=dict)
    backends: dict[str, Any] = field(default_factory=dict)
    last_attempt_nodes: dict[str, DagNodeSpec] = field(default_factory=dict)
    attempt_agents: dict[str, list[str]] = field(default_factory=dict)
    attempt_reasons: dict[str, list[str]] = field(default_factory=dict)
    reassigned: set[str] = field(default_factory=set)
    auto_instances: set[str] = field(default_factory=set)
    revision: int = 0
    original_groups: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict)
    group_current: dict[tuple[str, str], tuple[str, str | None]] = field(default_factory=dict)
    pending_moves: dict[tuple[str, str], _StagedMove] = field(default_factory=dict)
    pending_handovers: dict[tuple[str, str], _PendingHandover] = field(default_factory=dict)
    active_attempts: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    """The pair each node's in-flight attempt is running under, by node id.

    Read-only by convention, like ``status``: the runner is its only writer. A
    node listed here has not settled, so its status -- ``completed`` from the
    backend's return until the judge rules -- is still provisional, and a caller
    deciding which nodes may move reads both.
    """
    submitted_auto: set[str] = field(default_factory=set)
    dropped_moves: set[tuple[str, str]] = field(default_factory=set)
    """Keys of staged moves the run dropped without installing them.

    A move that had nothing to relocate, a refused target, a build failure or a
    cancellation drops its staged record; a caller that reserved the group needs
    to know the move will never install so it can release the reservation rather
    than hold it for the rest of the run.
    """

    status: dict[str, str] = field(default_factory=dict)
    """The run's own per-node status, bound by ``run_dag`` to the scheduler's dict.

    The same object, not a copy, so a caller asking mid-run reads current values.
    Read-only by convention: the runner is its only writer. A node is a possible
    mover exactly when this says it may still be relocated.
    """

    def effective(self, node_id: str, fallback: DagNodeSpec) -> DagNodeSpec:
        """The node as it would dispatch now."""
        return self.effective_nodes.get(node_id, fallback)

    def blocked(self, node: DagNodeSpec) -> bool:
        """Whether a staged move has taken this node's group off dispatch."""
        move = self.pending_moves.get(_group_key(node))
        return move is not None and node.id in move.members

    def handover_for(self, node: DagNodeSpec) -> "_PendingHandover | None":
        """The unclaimed seed this dispatch would plant, if any."""
        for seed in self.pending_handovers.values():
            if node.id in seed.movers and seed.target_agent == node.subagent and seed.target_instance == node.instance:
                return seed
        return None

    def claim_handover(self, seed: "_PendingHandover") -> bool:
        """Take the seed for one dispatch; False when another mover claimed it first."""
        key = _source_key(seed.source_agent, seed.source_instance, seed.anchor_node_id)
        if self.pending_handovers.get(key) is not seed:
            return False
        self.pending_handovers.pop(key)
        return True

    def release_handover(self, seed: "_PendingHandover") -> None:
        """Put an unplanted seed back, so a later destination dispatch can try again."""
        key = _source_key(seed.source_agent, seed.source_instance, seed.anchor_node_id)
        self.pending_handovers.setdefault(key, seed)

    def release_moves(self) -> None:
        """Drop every staged move and unclaimed seed; nothing will dispatch under them."""
        self.dropped_moves.update(self.pending_moves)
        self.pending_moves.clear()
        self.pending_handovers.clear()


WHEN_FALSE = "when_false"
"""Why a node was not executed: its guard is false, so its branch was not selected."""
WHEN_UNKNOWN = "when_unknown"
"""Why a node was not executed: its guard is unknown, because a decision node it names never chose."""
UPSTREAM_NOT_EXECUTED = "upstream_not_executed"
"""Why a node was not executed: no dependency it has in the graph was executed."""


@dataclass
class BranchState:
    """Everything branching needs, built once per run and threaded like ``unsettled``.

    Attributes:
        guards (`dict[str, Guard]`):
            Each guarded node's parsed ``when``, by node id.
        skippable (`frozenset[str]`):
            The nodes that may not execute (``dag_graph.may_not_execute``).
        chosen (`dict[str, tuple[str, ...]]`):
            Each decision node's selection once its verdict has landed, in declared
            order. ``()`` is a selection -- a ``select: many`` node that found no
            condition met -- and is not the same as no entry at all.
        evidence (`dict[str, str]`):
            The judge's quote supporting a selection, when it gave one.
        reasons (`dict[str, str]`):
            Why each not-executed node was not executed.
    """

    guards: dict[str, Guard]
    skippable: frozenset[str]
    chosen: dict[str, tuple[str, ...]] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)


async def run_dag(
    spec: SubAgentDagSpec,
    *,
    resolve: "Callable[[DagNodeSpec], Any]",
    backend: Any,
    workdir: str,
    run_root: str,
    nodes_root: str,
    history_root: str,
    subagents_root: str | None = None,
    sandbox_factory: "Callable[[Path], Any] | None" = None,
    max_concurrency: int = 5,
    semaphore: asyncio.Semaphore | None = None,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
    run_id: str | None = None,
    cancel: asyncio.Event | None = None,
    state_for: "Callable[[str, str | None, str], Any] | None" = None,
    auto_instances: frozenset[str] = frozenset(),
    memory_for: "Callable[[str], MemoryScope | None] | None" = None,
    mode_for: "Callable[[str, str | None, str], str | None] | None" = None,
    model_for: "Callable[[str | None, str | None, str | None], str | None] | None" = None,
    capabilities: dict[str, AgentCapabilities] | None = None,
    desk: AdjudicationDesk | None = None,
    adjudication_timeout_s: float = 600.0,
    judge_node: "Callable[..., Awaitable[Any]] | None" = None,
    on_node_start: "Callable[[str], Awaitable[None]] | None" = None,
    announce_exception: ExceptionAnnouncer | None = None,
    max_continuations: int = 2,
    origin: dict | None = None,
    control_reachable: "Callable[[], bool] | None" = None,
    control_advert: "Callable[[str], str | None] | None" = None,
    released: asyncio.Event | None = None,
    provider: Any = None,
    model: str | None = None,
    reassignment_state: ReassignmentState | None = None,
    build_reassigned: "Callable[[DagNodeSpec], Awaitable[tuple[DagNodeSpec, Any]]] | None" = None,
) -> DagRunResult:
    """Run a validated DAG, passing messages through files.

    ``resolve`` maps one node to the backend that runs it, so the narrowing a
    node asks for (its ``skills``) is applied per node rather than per agent name;
    it returns ``None`` for a name the agent table does not hold. ``backend``
    is the duck-typed file backend (read_file/write_file/join_path/abspath/
    file_exists). ``sandbox_factory`` builds the executor a node runs its exec in,
    called once per node with the graph's workdir as a Path (third-party CLI/OpenAI backends
    ignore the executor; a raven-loop node backend uses it).

    ``workdir``, ``run_root`` and ``nodes_root`` are different places and must
    not be collapsed into one. ``workdir`` is the session's working directory:
    it is each node sub-agent's cwd, and what ``{{ ref:<path> }}`` resolves
    against, so it has to be where the user's files are. ``run_root`` is this
    run's own directory, holding only ``graph.json`` and ``manifest.json`` --
    what describes the run rather than any one node. ``nodes_root`` is where
    the prompt/output records that describe each node are written instead,
    flat and keyed by node id rather than nested under a run -- an audit trail
    that outlives whatever the working directory is pointed at
    (raven/agent/subagent/history.py).

    They are not equally reachable by reference, though: ``nodes_root`` is a
    prefix-based reference root -- ``{{ ref:@nodes/<node_id>.out.md }}`` reads
    a node's file directly, the same file a bare ``{{ <node_id>.output }}``
    reads the contents of -- because ``sessions/`` is a protected subtree
    (raven/agent/workdir.py) that no working directory can be aimed at, so no
    relative path from ``workdir`` ever reaches it. ``run_root`` holds no
    prefix of its own: a node id already names one node for the whole
    conversation, so nothing needs to say which run produced it.

    ``subagents_root`` widens what a reference may resolve to by one
    directory: ``<session_dir>/subagents``, the parent of both ``run_root``
    and ``nodes_root``, reachable by absolute path rather than through a
    prefix. That is how a reference still reaches ``run_root``'s
    ``graph.json``/``manifest.json``, the ``spawn`` records beside it, and any
    node's artifacts written before this session's history was flattened.
    Left unset, references are confined to ``workdir`` plus ``@nodes/``.

    ``history_root`` is that same parent directory, ``<session_dir>/subagents``,
    taken as its own required root rather than derived from ``run_root``: it
    becomes the store's ``registry_root``, where the session's node registry
    lives.

    It is deliberately *not* agent home. Agent home also holds ``user_memory/``,
    ``skills/`` and every other conversation's transcript and sub-agent history;
    ``workdir.py`` keeps those three out of the agent's file surface as
    ``_PROTECTED_SUBTREES``, and a DAG graph is LLM-authored and auto-run, so a
    root that spanned them would render their contents into a third-party
    sub-agent's prompt. See
    :func:`raven.agent.subagent.prompt_paths.check_confined`.

    Node ids are unique across the session, not just across this graph: the
    session's node registry is read before validation, so a graph reusing an
    id an earlier run already took is refused, and one naming a node an
    earlier run *completed* resolves to that run's output, needing no
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

    ``on_node_start``, when given, is called once per attempt as the node is
    marked running and before it is rendered -- for a caller that needs to know
    where things stood before this node touched them.

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

    ``reassignment_state``, when given, is the record of which agent and handle
    every node dispatches under for this run: it starts from the submitted graph
    and accumulates each attempt's agent, the moves that have installed and the
    handovers still to plant. The caller may keep the same object to answer
    questions about this run while it is live; a state that already holds
    attempts or a revision from another run is refused, so a fresh one is
    required per run.

    ``build_reassigned``, when given, prepares one moved node for its new agent:
    it takes the node as it would dispatch there and returns that node plus an
    unstarted backend. It is called only when a move installs, and for every
    node that actually moves; without it a node can only move to a target whose
    backend the plan itself supplied.
    """
    if semaphore is None and max_concurrency < 1:
        raise DagValidationError("max_concurrency must be >= 1")
    if reassignment_state is not None and (
        reassignment_state.attempt_agents
        or reassignment_state.attempt_reasons
        or reassignment_state.last_attempt_nodes
        or reassignment_state.revision
    ):
        raise DagValidationError(
            "The supplied reassignment state already holds attempts or a revision from another run; "
            "pass a fresh state for each run."
        )
    roots = (workdir, subagents_root) if subagents_root else (workdir,)
    # One assignment record per run, whether or not the caller keeps a handle on
    # it: every dispatch reads its node and backend from here, so a caller that
    # never reassigns still takes the same path as one that does.
    state = reassignment_state if reassignment_state is not None else ReassignmentState()
    # Read, validate and claim under one guard. Splitting them would let two
    # concurrent runs both read a registry without node 'x', both pass the
    # uniqueness check, and both claim it -- leaving two nodes answering to one
    # name, which is exactly what the id being unique is supposed to rule out.
    async with index_guard(history_root):
        session_nodes = await read_session_nodes(backend, history_root)
        validate_and_order(spec, roots, session_nodes, nodes_root=nodes_root)
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
        state.effective_nodes = {node.id: node for node in spec.nodes}
        state.backends = dict(node_backends)
        state.submitted_auto = set(auto_instances)
        state.auto_instances.update(auto_instances)
        for node in spec.nodes:
            if node.instance is None:
                continue
            key = (node.subagent, node.instance)
            state.original_groups[key] = state.original_groups.get(key, frozenset()) | {node.id}
        for key, members in state.original_groups.items():
            if key not in state.group_current and not members & state.submitted_auto:
                state.group_current[key] = key

        store = DagRunStore(
            backend, run_root, run_id or make_run_id(), nodes_root=nodes_root, registry_root=history_root
        )
        await store.init(spec.model_dump_json(), [node.id for node in spec.nodes])

    published_terminal: set[str] = set()
    replanned_into: str | None = None

    # Built before the `try` below, not inside it: the cancellation handler reads
    # `status`, and a handler that covers the first await has to be able to.
    status: dict[str, str] = {nid: "pending" for nid in by_id}
    # The run's only status record, handed to the assignment state by reference:
    # a move's admission reads the scheduler's current answer, and every later
    # write here is visible to it without a second bookkeeping path.
    state.status = status
    output_paths: dict[str, str] = {}
    errors: dict[str, str] = {}
    continuations: dict[str, str] = {}
    attempts: dict[str, int] = {}
    prompt_written: set[str] = set()
    node_started_at: dict[str, int] = {}
    node_ended_at: dict[str, int] = {}
    node_activity: dict[str, dict] = {}
    gate = semaphore if semaphore is not None else asyncio.Semaphore(max_concurrency)
    # Where an accepted reassign waits between the desk and the scheduler. Only a
    # caller that supplied its own state can be handed one: the tray is what
    # makes a move apply, and a direct caller that never keeps the state must
    # keep reading a reassign as the named failure it has always been.
    reassignments: dict[str, ReassignPlan] | None = {} if reassignment_state is not None else None
    # Node ids whose status may still change. `status` reads "completed" from the
    # moment the backend returns, but the judge has not ruled yet and can turn
    # that into an exception, so a dependent dispatched inside that window
    # belongs to a node that may never have accomplished anything -- and would
    # render its placeholder from the output of a rejected attempt. Emptied per
    # node as its outcome becomes final, immediately before the wake below.
    unsettled: set[str] = set()
    # Fired by every node that reaches a final status, so a round ends on the
    # first one rather than on its last: a completed node's dependents are
    # dispatchable the moment its verdict lands, and nothing else wakes the
    # scheduler between one node landing and the whole round draining.
    settled = asyncio.Event()
    # This run's own memory-record pollers, so an outer cancellation of this
    # run's task (below) can reap the ones it already scheduled for completed
    # nodes -- `_RECORD_TASKS` is process-global and reachable by no
    # cancellation path at all.
    record_tasks: set[asyncio.Task] = set()
    # Node tasks a `resume` handed back, owned here until the next round awaits
    # them. Built before the `try` for the same reason `status` is: the
    # cancellation handler reaps them and has to be able to see them.
    carried: set[asyncio.Future] = set()
    # Which node ids each carried task is already running. `_run_node` sets
    # `running` inside the concurrency gate on purpose, so a node queued for a
    # slot still reads `pending` -- indistinguishable, from status alone, from a
    # node nobody has taken. A ready set recomputed while a task is carried would
    # dispatch it a second time.
    owned: dict[asyncio.Future, tuple[str, ...]] = {}

    deps: dict[str, list[str]] = {nid: graph_deps(node, by_id) for nid, node in by_id.items()}
    dependents: dict[str, list[str]] = {nid: [] for nid in by_id}
    for nid, in_graph in deps.items():
        for dep in in_graph:
            dependents[dep].append(nid)
    branch = BranchState(guards=guards_of(spec.nodes), skippable=may_not_execute(spec.nodes, by_id))
    publish_terminal = partial(
        _publish_terminal,
        status=status,
        branch=branch,
        published_terminal=published_terminal,
        node_started_at=node_started_at,
        node_ended_at=node_ended_at,
        by_id=by_id,
        session_key=session_key,
        run_id=store.run_id,
        progress_publisher=progress_publisher,
        state=state,
    )

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
                        # Only on a node that branches: a graph without routes or
                        # guards carries none of these keys.
                        **({"routes": list(node.routes), "select": node.select or "one"} if node.routes else {}),
                        **({"when": node.when} if node.id in branch.guards else {}),
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
                # A replan cancels in-flight work, and nodes an earlier `resume`
                # handed on sit outside the round that would have done it -- this
                # is the one loop exit that can be reached while any are running.
                # Before `_apply_replan`, so the `cancelled` it records is true.
                await _reap_carried(carried)
                carried = set()
                state.release_moves()
                # The second settle pass announces what it decides before the
                # ready set, so of `not_executed` this finds nothing; it stays
                # as the backstop for an exit that skips the sweep below. What
                # the widened states are for is this very pass's own: the
                # cascade's skips and the stop's cancels, which the break would
                # otherwise leave unannounced. NOT_EXECUTED itself only ever
                # names a guarded node or a dependency of one, so a graph
                # without routes or guards sees none of it.
                await publish_terminal(("skipped", "cancelled", NOT_EXECUTED))
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
                        state=state,
                    )
                    replanned_into = plan.run_id
                desk.replanned.clear()
                break
            # Below the stop and the replan, not beside the cascade: a decision node
            # either one cut off mid-judgement reads `completed` with no route, and
            # deciding its branches now would record a selection nobody made where the
            # stop or the replan is why they never ran. Ahead of the sweep, so the
            # sweep announces in this same pass what this pass decides -- the run's
            # last decisions included, which no later pass would announce.
            settle_branches(deps, status, branch, unsettled)
            await publish_terminal(("skipped", "cancelled", NOT_EXECUTED))
            carried_ids = {nid for nids in owned.values() for nid in nids}
            # Cleared where the desk's own flag is, and for its reason: a node
            # settling while this pass is still deciding has to leave the flag
            # set, so the round this pass goes on to dispatch wakes on it too.
            settled.clear()
            if desk is not None:
                # Cleared as the ready set is recomputed rather than once the
                # round it woke has ended: an answer landing while this pass is
                # still deciding has to leave the flag set, so that the round
                # this pass goes on to dispatch wakes on it too.
                desk.continued.clear()
            # A second pass, because the sweep above awaits: a carried decision node can
            # land its verdict meanwhile, and the ready rule reads dependencies, not
            # guards, so a node whose guard the first pass could not yet read must be
            # decided before the ready set is computed. What it decides is announced
            # here, ahead of the ready set: a pass with nothing ready leaves through the
            # break below, and a later sweep would never announce it. Publishing awaits,
            # so a verdict can land inside it; the pair then runs again and stops on the
            # first pass that decides nothing, which leaves no await between the last
            # settle and the ready set.
            while True:
                settle_branches(deps, status, branch, unsettled)
                if not any(st == NOT_EXECUTED and nid not in published_terminal for nid, st in status.items()):
                    break
                await publish_terminal((NOT_EXECUTED,))
            # A decided reassign is consumed before anything is dispatched. Only
            # the reassign branch of the wait is taken here: a decided continue
            # has nothing to apply, so it keeps waiting for the empty ready set
            # it has always been handled from.
            if reassignments is not None and _reassign_waiting(desk):
                await _await_adjudications(
                    desk,
                    status,
                    errors,
                    continuations,
                    timeout_s=adjudication_timeout_s,
                    cancel=cancel,
                    released=released,
                    reassignments=reassignments,
                )
            if reassignment_state is not None:
                await _apply_reassignments(
                    state,
                    prepared=reassignments or {},
                    status=status,
                    errors=errors,
                    continuations=continuations,
                    store=store,
                    session_key=session_key,
                    subagents_root=subagents_root,
                    cancel=cancel,
                    build_reassigned=build_reassigned,
                )
            ready = [
                nid
                for nid, st in status.items()
                if st == "pending"
                and nid not in carried_ids
                and not state.blocked(state.effective(nid, by_id[nid]))
                and all(_satisfied(d, status, unsettled) for d in deps[nid])
            ]
            if not ready:
                suspended = [nid for nid, st in status.items() if st == "exception"]
                if suspended and desk is not None:
                    # Every suspended node has already given its slot back, and
                    # anything still running was handed on rather than awaited,
                    # so waiting here costs the graph nothing it could be doing.
                    await _await_adjudications(
                        desk,
                        status,
                        errors,
                        continuations,
                        timeout_s=adjudication_timeout_s,
                        cancel=cancel,
                        released=released,
                        reassignments=reassignments,
                    )
                    continue
                if carried:
                    # A `resume` ended an earlier round while these were still
                    # running, and nothing new became dispatchable. This pass is
                    # then only the wait for them that the round would have done.
                    carried = await _run_ready_groups(
                        (),
                        cancel,
                        interrupt=desk.replanned if desk is not None else None,
                        resume=desk.continued if desk is not None else None,
                        settled=settled,
                        carried=carried,
                    )
                    owned = {task: nids for task, nids in owned.items() if task in carried}
                    continue
                state.release_moves()
                break
            # Nodes sharing a stateful instance run sequentially (id order); independent
            # nodes each form a singleton group and run concurrently under the semaphore.
            groups: dict[str, list[str]] = {}
            for nid in ready:
                inst = by_id[nid].instance
                key = inst if inst is not None else f"\x00node\x00{nid}"
                groups.setdefault(key, []).append(nid)
            dispatched: dict[asyncio.Future, tuple[str, ...]] = {}
            for nids in groups.values():
                group = sorted(nids)
                task = asyncio.ensure_future(
                    _run_group(
                        group,
                        by_id=by_id,
                        node_backends=node_backends,
                        state=state,
                        store=store,
                        backend=backend,
                        workdir=workdir,
                        roots=roots,
                        sandbox_factory=sandbox_factory,
                        output_paths=output_paths,
                        status=status,
                        errors=errors,
                        prompt_written=prompt_written,
                        node_started_at=node_started_at,
                        node_ended_at=node_ended_at,
                        node_activity=node_activity,
                        semaphore=gate,
                        branch=branch,
                        settled=settled,
                        unsettled=unsettled,
                        progress_publisher=progress_publisher,
                        state_for=state_for,
                        memory_for=memory_for,
                        mode_for=mode_for,
                        model_for=model_for,
                        capabilities=capabilities,
                        session_key=session_key,
                        subagents_root=subagents_root,
                        record_tasks=record_tasks,
                        attempts=attempts,
                        continuations=continuations,
                        desk=desk,
                        judge_node=judge_node,
                        on_node_start=on_node_start,
                        announce_exception=announce_exception,
                        max_continuations=max_continuations,
                        origin=origin,
                        dependents=dependents,
                        adjudication_timeout_s=adjudication_timeout_s,
                        control_reachable=control_reachable,
                        control_advert=control_advert,
                        provider=provider,
                        model=model,
                    )
                )
                dispatched[task] = tuple(group)
            owned.update(dispatched)
            carried = await _run_ready_groups(
                list(dispatched),
                cancel,
                interrupt=desk.replanned if desk is not None else None,
                resume=desk.continued if desk is not None else None,
                settled=settled,
                carried=carried,
            )
            owned = {task: nids for task, nids in owned.items() if task in carried}

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
            branch=branch,
            state=state,
        )
        result.replanned_into = replanned_into
        result.execution_nodes = dict(state.effective_nodes)
        result.instance_assignments = _instance_assignments(state)
        return result
    except asyncio.CancelledError:
        # `/stop` and the shutdown sweep stop a background run by cancelling its
        # task rather than setting `cancel`, so `_finalize` never runs. Without
        # this, the ids claimed at `init` would keep their registry entry with no
        # `status`, and `read_session_nodes` would report them `running` forever:
        # neither reusable nor readable, for a run that is definitively over --
        # and the two refusals that produces contradict each other.
        #
        # Nodes handed on by a `resume` are this handler's to reap: the round
        # that would have cancelled them returned them instead, so between that
        # return and the next round's await nothing else owns them. Before
        # `_mark_stopped`, so what it records as cancelled has been cancelled.
        await _reap_carried(carried)
        state.release_moves()
        _mark_stopped(status)
        await _record_outcome(store, status, cancelled=True)
        # No manifest will carry them: a stopped run's set-aside accounts
        # would otherwise outlive it for the life of the process.
        activity.forget_settled(node_live_key(store.run_id, nid) for nid in status)
        # This run's own memory-record pollers, scheduled for nodes that had
        # already completed before this cancellation landed, would otherwise
        # keep polling with nothing left to reap them.
        pending_records = [t for t in record_tasks if not t.done()]
        for t in pending_records:
            t.cancel()
        if record_tasks:
            await asyncio.gather(*record_tasks, return_exceptions=True)
        raise


async def _reap_carried(carried: Iterable[asyncio.Future]) -> None:
    """Cancel and await node tasks a `resume` handed back.

    Their round returned them instead of reaping them, so every path that ends
    the run without handing them to a further round has to do it here. An
    uncancelled group task outlives the run that owns it, holding a semaphore
    slot and a sub-agent process that nothing will ever collect.

    Two call sites are the complete set, which is what makes the absence of a
    `finally` around the carry safe: between a hand-back and the next round's
    await the only calls the loop reaches are `_emit` and `_write_node_status`, both
    of which swallow every `Exception` by construction (the flag clears and the set
    membership it reads on the way cannot raise), and `_run_ready_groups` does not
    propagate a node's own. `CancelledError` is not an `Exception` and does pass
    through those two -- and lands in the handler that is itself one of the two
    sites. So no third route out of the loop can leave a carried task behind.
    """
    tasks = list(carried)
    if not tasks:
        return
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _run_ready_groups(
    coros: Any,
    cancel: asyncio.Event | None,
    interrupt: asyncio.Event | None = None,
    resume: asyncio.Event | None = None,
    settled: asyncio.Event | None = None,
    carried: Iterable[asyncio.Future] = (),
) -> set[asyncio.Future]:
    """Await one scheduling round's group tasks; return the ones still running.

    With no signal to race, this is a plain ``asyncio.gather`` (if that
    gather is itself cancelled from outside, gather cancels every task it is
    waiting on, same as before this function existed). With ``cancel``
    and/or ``interrupt`` given, races the round's group tasks against
    whichever signals are set, so an in-flight node is cancelled the instant
    either one fires, releasing its semaphore slot immediately rather than
    waiting for it to finish on its own.

    ``resume`` is the one signal that does not end the graph's work. It says a
    decision landed that gives the scheduler something new to dispatch, so this
    round ends early and every node still running is *returned* rather than
    cancelled; the caller passes them straight back as ``carried``, which awaits
    them alongside the next round's own tasks. That is what keeps a re-dispatch
    from waiting out an unrelated sibling, and it is why the two signals cannot
    share a parameter: ``interrupt`` means stop these nodes, ``resume`` means
    stop waiting for them.

    ``settled`` is the second soft signal and is handled exactly like ``resume``.
    It says a node reached its final status for this attempt, so the ready set is
    worth recomputing: its dependents may be dispatchable now. Without it they
    wait for every sibling of the round their dependency happened to share, which
    is the same unbounded wait ``resume`` exists to prevent -- reached here on the
    path a graph takes when nothing goes wrong at all, rather than through a
    suspension.

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
    cleanly-cancelled run. Handed-on tasks are the sole exception to the reap,
    and they are only ever unreaped between two rounds: ``run_dag``'s own
    cancellation handler covers that gap.
    """
    tasks = [asyncio.ensure_future(c) for c in coros] + list(carried)
    cancel_sig = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
    interrupt_sig = asyncio.ensure_future(interrupt.wait()) if interrupt is not None else None
    resume_sig = asyncio.ensure_future(resume.wait()) if resume is not None else None
    settled_sig = asyncio.ensure_future(settled.wait()) if settled is not None else None
    signals = [s for s in (cancel_sig, interrupt_sig, resume_sig, settled_sig) if s is not None]
    if not signals:
        await asyncio.gather(*tasks)
        return set()
    stopping = [s for s in (cancel_sig, interrupt_sig) if s is not None]
    soft = [s for s in (resume_sig, settled_sig) if s is not None]
    handed_on: set[asyncio.Future] = set()
    try:
        pending: set[asyncio.Future] = {*tasks, *signals}
        while True:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            # Draining and stopping are both settled before the soft wake is
            # read: a round whose nodes have all finished has nothing to hand
            # on, and one being torn down must reap rather than leak.
            if any(s in done for s in stopping) or all(t.done() for t in tasks):
                break
            if any(s in done for s in soft):
                handed_on = {t for t in tasks if not t.done()}
                break
    finally:
        for signal in signals:
            if not signal.done():
                signal.cancel()
        reaped = [t for t in tasks if t not in handed_on]
        for task in reaped:
            if not task.done():
                task.cancel()
        await asyncio.gather(*signals, *reaped, return_exceptions=True)
    return handed_on


async def _publish_terminal(
    states: tuple[str, ...],
    *,
    status: dict[str, str],
    branch: BranchState,
    published_terminal: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    by_id: dict[str, DagNodeSpec],
    session_key: str | None,
    run_id: str,
    progress_publisher: ProgressPublisher | None,
    state: ReassignmentState | None = None,
) -> None:
    """Announce, once each, every node the scheduler itself put in one of ``states``.

    `_run_node` announces the nodes it ran; a node the scheduler skipped, cancelled
    or left unexecuted is announced here or by `_apply_replan`, never by both,
    including on the pass that meets a replan, whose exit publishes all three
    before it breaks. Two cases are announced by neither: the nodes stopped by
    cancelling the run's task, and a decision node completed but not yet
    adjudicated when a replan cancels its group.
    """
    for nid, st in status.items():
        if st not in states or nid in published_terminal:
            continue
        published_terminal.add(nid)
        now = _now_ms()
        # A cancelled node already has a real start time from its
        # dispatch; only a skipped or not-executed one needs both
        # stamps invented.
        node_started_at.setdefault(nid, now)
        node_ended_at[nid] = now
        outcome = {"reason": branch.reasons[nid]} if st == NOT_EXECUTED else {}
        # A node that moved writes under the agent whose attempt it last was:
        # a row under the submitted agent would leave the moved one behind.
        assignment = _assignment_fields(nid, state)
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {"run_id": run_id, "node": nid, "status": st, **outcome, **assignment},
        )
        await _write_node_status(
            session_key,
            run_id,
            nid,
            _current_agent(nid, state, by_id[nid].subagent),
            st,
            **outcome,
            assignment=assignment,
        )


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
    state: ReassignmentState | None = None,
) -> None:
    """Give this run's unfinished nodes their outcome, the replan being their cause.

    Publishes each transition here rather than leaving it to the loop's own sweep:
    that sweep only announces `skipped`, `cancelled` and `not_executed`, and
    widening it to `failed` would double-publish every node that failed on its own
    merit -- `_run_node` already announces those and does not record them as
    published.

    A node already terminal on its own merit -- completed, failed, or skipped by
    an unrelated cascade -- is untouched. A completed node's output is what the
    new run references; a failed or skipped node's status and error are the real
    reason it ended, and overwriting them with the replan's would erase that.
    The pass's own skipped and cancelled are published by the replan exit
    before this runs, through the same `_publish_terminal` the sweep uses.
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
        assignment = _assignment_fields(nid, state)
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {"run_id": run_id, "node": nid, "status": status[nid], **assignment},
        )
        await _write_node_status(
            session_key,
            run_id,
            nid,
            _current_agent(nid, state, by_id[nid].subagent),
            status[nid],
            assignment=assignment,
        )


def _tally(status: dict[str, str]) -> dict:
    """Count this run's nodes by terminal state."""
    return tally_node_statuses(status.values())


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


def _instance_followers(node: DagNodeSpec, state: ReassignmentState, status: Mapping[str, str]) -> list[str]:
    """The run's other unfinished nodes that share the attempt's instance handle.

    A reassign moves them with the node, so the report names them before the
    choice is made. A finished node keeps where it ran, a running one may still
    land either way, and a minted handle names no group.
    """
    if node.instance is None:
        return []
    current = _node_pair(node)
    followers: list[str] = []
    for node_id in sorted(state.original_groups.get(current, frozenset())):
        if node_id == node.id or status.get(node_id) not in ("pending", "exception", "running"):
            continue
        member = state.effective_nodes.get(node_id)
        if member is not None and _node_pair(member) != current:
            continue
        followers.append(node_id)
    return followers


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
    control_advert: "Callable[[str], str | None] | None" = None,
    attempt_agents: Sequence[str] = (),
    attempt_reasons: Sequence[str] = (),
    instance_followers: Sequence[str] = (),
) -> str:
    """The text the main agent is woken with. Everything it needs to decide, once.

    The blocked list is not decoration: without it the agent is choosing between
    continuing and abandoning with no idea what abandoning costs.

    ``attempt_agents`` is every attempt of this node in order, the last being the
    attempt that just ended; ``attempt_reasons`` is the same order without that
    one, whose reason is the report's own account of what is missing.
    ``instance_followers`` are the run's other unfinished nodes on the node's
    instance handle.
    """
    dispatched_by = attempt_agents[-1] if attempt_agents else node.subagent
    lines = [f"DAG run {run_id}: node '{node.id}' ({dispatched_by}) did not accomplish its task."]
    for index, agent in enumerate(attempt_agents[: max(attempt - 1, 0)], start=1):
        recorded = attempt_reasons[index - 1] if index - 1 < len(attempt_reasons) else _NO_REASON
        lines.append(f"attempt {index} ({agent}): {_bounded_attempt_reason(recorded)}")
    lines += [
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
        f"attempt {attempt}; {remaining} continuation(s) left (continue and reassign each spend one); "
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
        "Use continue when the task is right and something is missing that you or the user can "
        "supply -- information, a credential, a hint. Use abandon when the task is no longer "
        "wanted. Ask the user first if only they can supply what is missing."
    )
    lines.append(
        f'Or reassign: tool_call with name "resolve_dag_node" and arguments '
        f'{{"run_id": "{run_id}", "node_id": "{node.id}", "decision": "reassign", '
        f'"agent": "<roster name>", "message": "<what the new sub-agent should do>"}}. '
        "This hands the node to another sub-agent from the roster, which is shown what the node "
        "was asked and answered so far, but not the tools it ran or this verdict: say what went "
        "wrong in your message. Use reassign when the task is right and this sub-agent lacks the "
        "execution capability it needs -- a tool, access to the files, a skill, or the strength "
        "for it -- and choose by how well the roster's descriptions and tags fit the task. An "
        "agent that already tried this node is not excluded: a failure may have been chance."
    )
    if instance_followers:
        names = ", ".join(f"'{node_id}'" for node_id in instance_followers)
        lines.append(
            f"Reassign also moves the other unfinished nodes on this instance -- {names}; one still "
            "running joins only if its attempt lands unfinished. A target that takes them must be "
            "[stateful]."
        )
    lines.append(
        f'Or replan: tool_call with name "resolve_dag_node" and arguments '
        f'{{"run_id": "{run_id}", "node_id": "{node.id}", "decision": "replan", '
        f'"message": "<why the plan is changing>", "nodes": [<the graph to run instead>]}}. '
        "That stops this run and starts a new one from your nodes. Use replan when the plan "
        "itself is wrong rather than this attempt: a step that cannot work as wired, or a step "
        "nothing in the graph performs. Nodes this run completed are referenced, not re-declared "
        "-- name one in depends_on and read it with {{ <id>.output }}; everything else needs a new id."
    )
    # The invocations above carry this run's ids; this carries the field names,
    # and it is generated rather than written. A hand-written retelling is what
    # sent the model looking for a field called `action` (2026-09-03/04): the
    # examples are a shape to copy, and a shape to copy is not a schema.
    if control_advert is not None:
        try:
            declared = control_advert("resolve_dag_node")
        except Exception:  # noqa: BLE001 - an aid to the report must not fail it
            declared = None
        if declared:
            lines.append(f"The complete schema of resolve_dag_node: {declared}")
    return "\n".join(lines)


def _fail_undetermined_route(
    node_id: str, reason: str, *, status: dict[str, str], errors: dict[str, str], output_paths: dict[str, str]
) -> None:
    """Fail a decision node whose route could not be established, and withdraw its output.

    No report and no adjudication: there is no route to default to, and the failure
    only stops this node's own descendants, through the ordinary cascade.
    """
    status[node_id] = "failed"
    errors[node_id] = f"The route could not be determined: {reason}"
    output_paths.pop(node_id, None)


def _selection_fields(node_id: str, branch: BranchState) -> dict[str, Any]:
    """A decision node's recorded selection, as its finish event and its run record carry it.

    Empty for a node with nothing recorded: a graph without routes carries no
    selection keys.
    """
    if node_id not in branch.chosen:
        return {}
    fields: dict[str, Any] = {"selected_routes": list(branch.chosen[node_id])}
    if node_id in branch.evidence:
        fields["route_evidence"] = branch.evidence[node_id]
    return fields


async def _apply_verdict(
    node: DagNodeSpec,
    *,
    store: DagRunStore,
    status: dict[str, str],
    errors: dict[str, str],
    output_paths: dict[str, str],
    continuations: dict[str, str],
    node_output: str | None,
    attempt: int,
    desk: "AdjudicationDesk | None",
    judge_node: "Callable[..., Awaitable[Any]]",
    announce_exception: ExceptionAnnouncer | None,
    max_continuations: int,
    origin: dict | None,
    dependents: dict[str, list[str]],
    adjudication_timeout_s: float,
    branch: BranchState,
    control_reachable: "Callable[[], bool] | None" = None,
    control_advert: "Callable[[str], str | None] | None" = None,
    output_limited: bool = False,
    reassignment_state: ReassignmentState | None = None,
) -> str | None:
    """Turn a finished node's verdict into a status, and report a bad one.

    The last attempt reports too: the agent learns this line of the graph is dead
    while the rest of the graph is still running, rather than at the closing announce
    an hour later.

    A decision node's selection is recorded into ``branch`` here, before the node
    leaves ``unsettled``, so no guard naming it is ever read without it.

    ``reassignment_state`` supplies the report's attempt history: who tried this
    node before and why each attempt fell short. It is read from the move record
    rather than rebuilt from ``errors``, which holds only the latest one.

    Returns what the attempt left undone when it was judged not accomplished --
    never empty, and still known on the retry route, which clears ``errors`` --
    and ``None`` otherwise.
    """
    if status[node.id] not in ("completed", "failed"):
        return None
    crashed = status[node.id] == "failed"
    try:
        verdict = await judge_node(
            node=node,
            store=store,
            output=node_output or "",
            error=errors.get(node.id, ""),
            crashed=crashed,
            output_limited=output_limited,
        )
    except RouteUndeterminedError as exc:
        # Ahead of the fail-open below, which is an ordinary node's: there is no route
        # to default to, so the node fails, and its error says why. A crashed node
        # reads no route, and keeps the failure it already has.
        logger.warning("DAG decision node {} could not settle a route: {}", node.id, exc)
        if not crashed:
            _fail_undetermined_route(node.id, str(exc), status=status, errors=errors, output_paths=output_paths)
        return None
    except TypeError as exc:
        # Not fail-open, and not raised either: raising here is swallowed further
        # out and the node stays `completed`, which is the failure this guards
        # against. The call itself being wrong is not the judgement being
        # unavailable -- read as accomplished it switches the whole verdict
        # system off with nothing anywhere failing, so the node fails instead
        # and says why.
        logger.opt(exception=True).error("DAG node {} judge call does not accept what the runner passes", node.id)
        if node.routes:
            # A decision node fails for want of a route, like every other way its
            # judgement can be lost; a crashed one keeps the failure it already has.
            if not crashed:
                _fail_undetermined_route(
                    node.id,
                    f"the DAG judge could not be called: {exc}",
                    status=status,
                    errors=errors,
                    output_paths=output_paths,
                )
            return None
        status[node.id] = "failed"
        errors[node.id] = f"the DAG judge could not be called, so this node was never judged: {exc}"
        output_paths.pop(node.id, None)
        return None
    except Exception as exc:  # noqa: BLE001 - as _verdict.judge: an ordinary node fails open, a decision node fails
        if node.routes:
            # The fail-open is an ordinary node's alone. Read as accomplished, a decision
            # node would still fail for want of a route, under a reason that hides this
            # one; a crashed one keeps the failure it already has.
            logger.opt(exception=True).warning("DAG decision node {} verdict call raised: {}", node.id, exc)
            if not crashed:
                detail = bound_llm_detail(f"{type(exc).__name__}: {exc}")
                _fail_undetermined_route(
                    node.id,
                    f"the judge call raised: {detail}",
                    status=status,
                    errors=errors,
                    output_paths=output_paths,
                )
            return None
        logger.opt(exception=True).warning("DAG node {} verdict call raised, judging it accomplished: {}", node.id, exc)
        verdict = Verdict(accomplished=True)
    if verdict.accomplished:
        # A crashed node keeps the failure it has: no route is read from a node that did
        # not finish. `()` is a selection; only a missing one fails the node.
        if node.routes and not crashed:
            if verdict.routes is None:
                _fail_undetermined_route(
                    node.id,
                    "the judge reported the task accomplished without selecting a route",
                    status=status,
                    errors=errors,
                    output_paths=output_paths,
                )
            else:
                branch.chosen[node.id] = tuple(verdict.routes)
                if verdict.route_evidence:
                    branch.evidence[node.id] = verdict.route_evidence
        return None
    # Read before `errors` is overwritten with the judge's reason: a node that raised
    # returned nothing but its error, and that error is its whole account.
    said = errors.get(node.id, "") if crashed else (node_output or "")
    reason = verdict.what_is_missing or "The node did not accomplish its task."
    errors[node.id] = reason
    # A node that did not accomplish its task must not be readable as anyone's
    # input, whatever happens next.
    output_paths.pop(node.id, None)
    remaining = max_continuations - (attempt - 1)
    if verdict.follow_up and remaining > 0:
        # The judge already knows what to say, so there is nobody to ask. A
        # command's judge holds the failing output; relaying that through a
        # person, or through the main agent, would be asking them to read it
        # out -- and on an unattended run there is nobody there to read it.
        # Back to pending with the message parked, which is the same shape an
        # adjudicated continuation takes, so the ready set picks it up next pass.
        continuations[node.id] = verdict.follow_up
        status[node.id] = "pending"
        errors.pop(node.id, None)
        logger.info("DAG node {} retries on its judge's own follow-up ({} left)", node.id, remaining - 1)
        return reason
    answerable = _route_available(control_reachable)
    attempt_agents: tuple[str, ...] = ()
    attempt_reasons: tuple[str, ...] = ()
    instance_followers: tuple[str, ...] = ()
    if reassignment_state is not None:
        attempt_agents = tuple(reassignment_state.attempt_agents.get(node.id, ()))
        attempt_reasons = tuple(reassignment_state.attempt_reasons.get(node.id, ()))
        instance_followers = tuple(_instance_followers(node, reassignment_state, status))
    report = _exception_report(
        run_id=store.run_id,
        node=node,
        verdict=verdict,
        attempt=attempt,
        remaining=remaining,
        blocked=sorted(dependents.get(node.id, [])),
        timeout_s=adjudication_timeout_s,
        route_available=answerable,
        control_advert=control_advert,
        attempt_agents=attempt_agents,
        attempt_reasons=attempt_reasons,
        instance_followers=instance_followers,
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
            partial(
                announce_exception, store.run_id, node.id, report, origin, awaiting_decision=suspending, output=said
            ),
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
    return reason


async def _record_outcome(store: DagRunStore, status: dict[str, str], *, cancelled: bool = False) -> None:
    """Write this run's per-node outcome into the node registry.

    Per-node status, not just the tally: a later graph may name one of these
    nodes, and whether that reference is legal -- and what to advise when it is
    not -- turns on that node's own outcome, not the run's. Every id claimed at
    ``init`` has to appear here, or ``read_session_nodes`` keeps reporting it as
    still being written.

    Reached from the cancellation path too, where nothing else would record an
    outcome. A failure there must not replace the ``CancelledError`` being
    propagated, so it is logged and swallowed -- the same call is best-effort in
    both directions, since a wedged registry is never worth losing a stop over.
    """
    tally = _tally(status)
    summary = f"{tally['completed']}/{tally['total']} completed"
    try:
        async with index_guard(store.registry_root):
            await store.record_outcome(status, summary)
    except Exception:  # noqa: BLE001 - see above
        if not cancelled:
            raise
        logger.opt(exception=True).warning("DAG registry write failed for cancelled run {}", store.run_id)


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


def _satisfied(dep: str, status: Mapping[str, str], unsettled: set[str]) -> bool:
    """Whether a dependency lets its dependent go on: completed with its verdict final, or not executed."""
    return (status[dep] == "completed" and dep not in unsettled) or status[dep] == NOT_EXECUTED


def _atom_value(atom: Atom, status: Mapping[str, str], branch: BranchState) -> Value:
    """One atom's value: a route of a node that did not run is unknown, never false."""
    state = status.get(atom.node)
    if state == NOT_EXECUTED:
        return True if atom.name == DONE else None
    if state != "completed":
        return None
    if atom.name == DONE:
        return True
    chosen = branch.chosen.get(atom.node)
    if chosen is None:
        # Completed with no selection on record -- its verdict never landed -- so
        # nothing was decided, and nothing may be read as selected. Presence, not
        # truth: `()` is a selection, and it makes every route false.
        return None
    return atom.name in chosen


def settle_branches(
    deps: dict[str, list[str]],
    status: dict[str, str],
    branch: BranchState,
    unsettled: set[str],
) -> None:
    """Decide every pending node whose dependencies have all settled.

    A fixed-point pass, run after `_cascade_failures` and before the ready set is
    computed, so a whole dead subtree is marked in one round. The first matching rule
    decides the node:

    1. a failed or skipped dependency skips it -- `_cascade_failures` has already
       done that, and this pass leaves such a node alone;
    2. every in-graph dependency was not executed, so this node was not either;
    3. a guard decides: true runs it, false and unknown do not, and the two are
       recorded apart;
    4. otherwise it stays pending and the ready rule will take it.

    Rule 2 comes before rule 3 on purpose. `<node>.done` is true whatever the decision
    node did, and it must not revive a node every one of whose inputs lies on a dead path.

    A dependency in ``unsettled`` reads ``completed`` before its judge has ruled, so it
    has not settled: no guard is read while a route it names may still be landing.
    """
    changed = True
    while changed:
        changed = False
        for nid, st in status.items():
            if st != "pending":
                continue
            in_graph = deps[nid]
            if in_graph and all(status[d] == NOT_EXECUTED for d in in_graph):
                status[nid] = NOT_EXECUTED
                branch.reasons[nid] = UPSTREAM_NOT_EXECUTED
                changed = True
                continue
            guard = branch.guards.get(nid)
            # A dependency that failed, was skipped or is still out leaves the node to the
            # cascade or to a later pass: rule 1 holds whatever the guard would say.
            if guard is None or not all(_satisfied(d, status, unsettled) for d in in_graph):
                continue
            value = evaluate(guard, partial(_atom_value, status=status, branch=branch))
            if value is not True:
                status[nid] = NOT_EXECUTED
                branch.reasons[nid] = WHEN_FALSE if value is False else WHEN_UNKNOWN
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
    reassignments: dict[str, ReassignPlan] | None = None,
) -> None:
    """Block until every suspended node has an answer, the wait runs out, or one
    answer replans the graph.

    Reached only when nothing else can be dispatched: the report went to the
    main agent the moment the node was suspended, so this wait costs the graph
    nothing it could otherwise be doing. Nodes a `resume` handed on can still be
    running through it -- they are already tasks, and awaiting them is not what
    makes them progress -- and the round dispatched next takes them back.

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

    A reassign is not a terminal answer: the node goes back to `pending` with
    its message parked in ``continuations`` and its prepared move parked in
    ``reassignments``, and the call returns without waiting for the round's
    other nodes. An unanswered sibling stays `exception` with its waiter open,
    so a later call can still answer it; closing it here would turn a valid
    question into an artificial failure. A reassign with no prepared move --
    or a call with nowhere to park one -- fails the node with the missing move
    named, rather than retrying the old agent or calling the node abandoned.

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
    reassigned_nid: str | None = None
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
            if decided:
                # A reassign hands the node back to the scheduler immediately.
                # Waiting for the round's other nodes would hold the move behind
                # questions it does not depend on, and on a bound run those may
                # never be answered at all.
                reassigned_nid = next(
                    (
                        nid
                        for nid, task in sorted(node_tasks.items())
                        if task in decided and (answer := desk.peek(nid)) is not None and answer.decision == REASSIGN
                    ),
                    None,
                )
                if reassigned_nid is not None:
                    break
            still_bound = unbind is not None and not unbind.done()
            if decided:
                deadline = None if still_bound else loop.time() + timeout_s
            elif deadline is not None and loop.time() >= deadline:
                break
        if cancelled:
            return
        if reassigned_nid is not None:
            answer = desk.take(reassigned_nid)
            if answer is not None:
                if reassignments is None or answer.reassignment is None:
                    status[reassigned_nid] = "failed"
                    errors[reassigned_nid] = (
                        "The main agent chose to reassign but no prepared move reached the runner, "
                        "so the node failed instead of moving."
                    )
                else:
                    continuations[reassigned_nid] = answer.message or ""
                    reassignments[reassigned_nid] = answer.reassignment
                    status[reassigned_nid] = "pending"
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
            elif answer.decision == REASSIGN:
                # Only reachable when a replan ended the round in the same batch:
                # the graph is being replaced, so this node is left for
                # `_apply_replan` rather than charged as an abandonment.
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
        # A reassign returns while siblings are still questions: their waiters
        # must survive so the next call can answer them. The reassigned node's
        # own waiter was consumed by `take` above.
        if reassigned_nid is None:
            for nid in open_nodes:
                desk.close(nid)


def _reassign_waiting(desk: "AdjudicationDesk | None") -> bool:
    """Whether a decided reassign is sitting on the desk, unconsumed.

    Such an answer has to reach the scheduler before any node of its source
    group dispatches again, so the loop consumes it ahead of the ready set.
    """
    if desk is None:
        return False
    return any((answer := desk.peek(nid)) is not None and answer.decision == REASSIGN for nid in desk.open_nodes())


def _instance_assignments(state: ReassignmentState) -> dict[tuple[str, str], tuple[str, str | None]]:
    """Where each graph-named instance group ended the run, keyed by the pair the graph named.

    A capture replays the group as one session, so every member is saved under
    the pair the handle finished on -- including one minted after a hop dropped
    the graph's handle. Groups the graph named no handle for are left out: a
    minted name is not one the graph gave, so it is not a key here.
    """
    return {
        original: current
        for original, current in state.group_current.items()
        if not state.original_groups.get(original, frozenset()) & state.submitted_auto
    }


def _assignment_fields(node_id: str, state: ReassignmentState | None) -> dict[str, Any]:
    """The agent a node last actually started on, and every agent that tried it.

    Empty for a node that was never reassigned: a run without a move carries
    none of these keys, the same as before reassignment existed. The instance and
    its auto flag come off the same frozen attempt record as the agent -- a mix
    of graph and registry values would describe a node that never existed.
    """
    if state is None or node_id not in state.reassigned:
        return {}
    last = state.last_attempt_nodes.get(node_id)
    if last is None:
        # A move accepted before the node ever ran has no attempt to describe.
        # Stamping an empty history with a null instance would read as a
        # stateless attempt and replace the identity the graph submitted.
        return {}
    return {
        "attempt_agents": list(state.attempt_agents.get(node_id, [])),
        "subagent": last.subagent,
        "instance": last.instance,
        "instance_auto": node_id in state.auto_instances,
    }


def _current_agent(node_id: str, state: ReassignmentState | None, fallback: str) -> str:
    """The agent a node's durable row stands under.

    The last attempt's, once the node moved: the row follows the attempt, so a
    write under the submitted agent would leave a second row behind.
    """
    if state is not None and node_id in state.reassigned:
        last = state.last_attempt_nodes.get(node_id)
        if last is not None:
            return last.subagent
    return fallback


async def _apply_reassignments(
    state: ReassignmentState,
    *,
    prepared: dict[str, ReassignPlan],
    status: dict[str, str],
    errors: dict[str, str],
    continuations: dict[str, str],
    store: DagRunStore,
    session_key: str | None,
    subagents_root: str | None,
    cancel: asyncio.Event | None,
    build_reassigned: "Callable[[DagNodeSpec], Awaitable[tuple[DagNodeSpec, Any]]] | None",
) -> None:
    """Move accepted nodes onto their target once every source attempt has settled.

    Called once per scheduler pass. A prepared move is first *staged*: its
    source group is taken off dispatch, and the scheduler keeps running
    everything else while ``active_attempts`` still holds an attempt on that
    pair. Only when the run's own in-flight record is clear are the movers
    recomputed from ``status`` -- a sibling running when the move was accepted
    joins if its attempt landed unfinished -- the target backends are built for
    every mover, and the whole set is installed at once. Nothing here runs under
    the concurrency gate.
    """
    if cancel is not None and cancel.is_set():
        state.release_moves()
        prepared.clear()
        return
    for node_id, plan in list(prepared.items()):
        prepared.pop(node_id, None)
        _stage_move(state, plan, status=status, errors=errors, continuations=continuations)
    for key, move in list(state.pending_moves.items()):
        if _move_in_flight(state, move):
            continue
        await _install_move(
            state,
            key,
            move,
            status=status,
            errors=errors,
            continuations=continuations,
            store=store,
            subagents_root=subagents_root,
            cancel=cancel,
            build_reassigned=build_reassigned,
        )


def _stage_move(
    state: ReassignmentState,
    plan: ReassignPlan,
    *,
    status: dict[str, str],
    errors: dict[str, str],
    continuations: dict[str, str],
) -> None:
    """Park one accepted move until its source attempts settle.

    A second move of the same source group is refused rather than raced: the
    group can only have one destination at a time, and the later node is failed
    with the reason instead of silently vanishing.
    """
    key = _source_key(plan.source_agent, plan.source_instance, plan.node_id)
    if key in state.pending_moves:
        _fail_node(
            status,
            errors,
            continuations,
            plan.node_id,
            f"The group of node '{plan.node_id}' already has a move in progress, so this one was not applied.",
        )
        return
    if plan.source_instance is not None:
        members = state.original_groups.get((plan.source_agent, plan.source_instance), frozenset({plan.node_id}))
    else:
        members = frozenset({plan.node_id})
    state.pending_moves[key] = _StagedMove(
        node_id=plan.node_id,
        source_agent=plan.source_agent,
        source_instance=plan.source_instance,
        target_agent=plan.target_agent,
        target_stateful=plan.target_stateful,
        target_instance=plan.target_instance,
        instance_auto=plan.instance_auto,
        revision=plan.revision,
        members=members | {plan.node_id},
        backends=dict(plan.backends),
    )
    state.revision = max(state.revision, plan.revision)


def _fail_node(
    status: dict[str, str],
    errors: dict[str, str],
    continuations: dict[str, str],
    node_id: str,
    reason: str,
) -> None:
    """Fail one node on the spot, dropping the message parked for its next attempt."""
    continuations.pop(node_id, None)
    if status.get(node_id) in ("pending", "exception", "running"):
        status[node_id] = "failed"
    errors[node_id] = reason


def _move_in_flight(state: ReassignmentState, move: _StagedMove) -> bool:
    """Whether any node of this run is still running an attempt on the move's source pair."""
    source = (move.source_agent, move.source_instance)
    return any(nid in state.active_attempts and state.active_attempts[nid] == source for nid in move.members)


def _move_movers(state: ReassignmentState, move: _StagedMove, status: dict[str, str]) -> list[str]:
    """The members the move actually relocates now that their attempts have landed.

    Finished nodes keep the agent that did the work. A member that was running
    when the move was accepted joins when its attempt landed pending (its judge
    asked for another attempt) or suspended (it is still a question).
    """
    source = (move.source_agent, move.source_instance)
    movers: list[str] = []
    for nid in sorted(move.members):
        if status.get(nid) not in ("pending", "exception"):
            continue
        node = state.effective_nodes.get(nid)
        if node is not None and _node_pair(node) == source:
            movers.append(nid)
    return movers


async def _install_move(
    state: ReassignmentState,
    key: tuple[str, str],
    move: _StagedMove,
    *,
    status: dict[str, str],
    errors: dict[str, str],
    continuations: dict[str, str],
    store: DagRunStore,
    subagents_root: str | None,
    cancel: asyncio.Event | None,
    build_reassigned: "Callable[[DagNodeSpec], Awaitable[tuple[DagNodeSpec, Any]]] | None",
) -> None:
    """Build every target, then install the whole move in one step.

    A group is never left half-built: any refusal here fails the node the move
    was decided for and leaves every sibling on the agent it already has.
    """
    movers = _move_movers(state, move, status)
    if not movers:
        state.pending_moves.pop(key, None)
        state.dropped_moves.add(key)
        return
    original = state.original_groups.get((move.source_agent, move.source_instance), move.members)
    if len(original) > 1 and not move.target_stateful:
        state.pending_moves.pop(key, None)
        state.dropped_moves.add(key)
        _fail_node(
            status,
            errors,
            continuations,
            move.node_id,
            f"Node '{move.node_id}' shares instance '{move.source_instance}' with "
            f"{len(original) - 1} other node(s) in this run, so it can only move to a stateful sub-agent; "
            f"'{move.target_agent}' is not one and the move was not applied.",
        )
        return
    built: dict[str, tuple[DagNodeSpec, Any]] = {}
    try:
        for nid in movers:
            node = state.effective_nodes[nid]
            target_node = node.model_copy(update={"subagent": move.target_agent, "instance": move.target_instance})
            if build_reassigned is not None:
                built[nid] = await build_reassigned(target_node)
            else:
                backend = move.backends.get(nid)
                if backend is None:
                    raise DagValidationError(
                        f"no backend was prepared for node '{nid}' on sub-agent '{move.target_agent}'"
                    )
                built[nid] = (target_node, backend)
        seed = await _load_handover_seed(
            state,
            move,
            movers,
            store=store,
            subagents_root=subagents_root,
        )
    except Exception as exc:  # noqa: BLE001 - an unusable target fails the move, not the run
        state.pending_moves.pop(key, None)
        state.dropped_moves.add(key)
        _fail_node(
            status,
            errors,
            continuations,
            move.node_id,
            f"Node '{move.node_id}' could not move to '{move.target_agent}': {exc}",
        )
        return
    if cancel is not None and cancel.is_set():
        state.pending_moves.pop(key, None)
        state.dropped_moves.add(key)
        return
    if _move_in_flight(state, move):
        # A source attempt started while the targets were being built; keep the
        # move staged and let the next scheduler wake-up retry the install.
        return
    for nid, (node, backend) in built.items():
        state.effective_nodes[nid] = node
        state.backends[nid] = backend
        state.reassigned.add(nid)
        if move.instance_auto:
            state.auto_instances.add(nid)
    members = state.original_groups.get((move.source_agent, move.source_instance), move.members)
    if move.target_instance is not None:
        state.original_groups[(move.target_agent, move.target_instance)] = members
    # Matched by the graph's own group, not by the pair that group sits on: a hop
    # to a stateless target drops the handle, and the pair a later hop starts
    # from then names no group at all. Every hop rewrites the value, so a group
    # moved twice still reports one start and one end.
    target = (move.target_agent, move.target_instance)
    for original in state.group_current:
        if move.node_id in state.original_groups.get(original, frozenset()):
            state.group_current[original] = target
    state.revision += 1
    state.pending_moves.pop(key, None)
    if seed is not None:
        state.pending_handovers[_source_key(move.source_agent, move.source_instance, move.node_id)] = seed


class _DestinationOccupiedError(DagValidationError):
    """The target agent already holds a conversation under the handle the move named."""


async def _destination_occupied(subagents_root: str | None, session_key: str | None, agent: str, handle: str) -> bool:
    """Whether another lane already opened this pair's session.

    Both records the design names: message rows in the Instance Log, or a
    binding in the instance registry. A registry row that only links the node
    to the handle carries no session id and is not a conversation.
    """
    if subagents_root:
        try:
            if message_rows(Path(subagents_root).parent, agent, handle):
                return True
        except Exception:  # noqa: BLE001 - an unreadable log is not proof of an empty one
            logger.opt(exception=True).warning("DAG destination log could not be read for {}", agent)
    try:
        bound = await get_registry().lookup(session_key or "default", agent, handle)
    except Exception:  # noqa: BLE001 - an unreadable registry is not proof of an empty binding
        logger.opt(exception=True).warning("DAG destination binding could not be read for {}", agent)
        return False
    return bound is not None


async def _plant_handover(
    seed: _PendingHandover,
    *,
    node: DagNodeSpec,
    store: DagRunStore,
    attempt: int,
    next_message: str,
    session_key: str | None,
    subagents_root: str | None,
) -> str:
    """Render the seed onto this attempt's prompt, under the destination handle's lock.

    A graph-named handle is checked again here, at first use: a session opened
    for that pair while the move waited must not receive this run's history.
    A minted handle is fresh by construction and needs no check.
    """
    if seed.target_named and seed.target_instance is not None:
        if await _destination_occupied(subagents_root, session_key, node.subagent, seed.target_instance):
            raise _DestinationOccupiedError(
                f"sub-agent '{node.subagent}' already holds a conversation under instance "
                f"'{seed.target_instance}'; node '{node.id}' was not dispatched there. Choose another "
                "sub-agent, or replan with a new instance name."
            )
    rendered = render_handover(
        seed.turns,
        task=seed.task,
        to_agent=node.subagent,
        next_message=next_message,
    )
    await store.write_text(store.attempt_prompt_path(node.id, attempt), rendered.text)
    return rendered.text


async def _announce_handover_notices(
    seed: _PendingHandover,
    *,
    run_id: str,
    node: DagNodeSpec,
    origin: dict | None,
    announce_exception: ExceptionAnnouncer | None,
) -> None:
    """Tell the main agent when the seed could not carry the whole source conversation.

    An informational notice, like a stall report: nothing about the node's state
    changed and nothing is waiting for an answer.
    """
    if not seed.notices or announce_exception is None or origin is None:
        return
    for notice in seed.notices:
        try:
            await announce_exception(run_id, node.id, notice, origin, awaiting_decision=False, informational=True)
        except Exception:  # noqa: BLE001 - a notice must never fail the node
            logger.opt(exception=True).warning("DAG handover notice for node {} was not delivered", node.id)


async def _load_handover_seed(
    state: ReassignmentState,
    move: _StagedMove,
    movers: list[str],
    *,
    store: DagRunStore,
    subagents_root: str | None,
) -> "_PendingHandover | None":
    """Read the source conversation once every source attempt is on disk.

    For a named instance the turns come from the member whose archived attempts
    prove the log holds its latest turn; picking that member is what keeps a
    complete conversation from being read as incomplete. When no member proves
    it, the node the move was decided for falls back to its own archived
    attempts, which carries the source's bounded notice. The task the budget
    keeps whole is always that node's, whichever member proved the log.
    """
    session_dir = Path(subagents_root).parent if subagents_root else None
    loaded = None
    if move.source_instance is not None:
        # The Instance Log proves it holds a node's latest turn only when that
        # node's prompt is its last user row, so the member that landed last is
        # the one that can be read as the whole conversation. Any other member
        # falls back to its own archives, which is the incomplete transfer the
        # notice is for; that fallback is kept for the node the move was
        # decided for, and only when no member proves the log complete.
        candidates = [move.node_id, *(nid for nid in sorted(move.members) if nid != move.node_id)]
        fallback = None
        anchor_failure: Exception | None = None
        for anchor in candidates:
            attempts = state.attempt_agents.get(anchor, [])
            if not attempts:
                continue
            try:
                candidate = await load_handover_turns(
                    store,
                    anchor,
                    session_dir=session_dir,
                    source_agent=move.source_agent,
                    source_instance=move.source_instance,
                    attempt_agents=attempts,
                )
            except Exception as exc:  # noqa: BLE001 - a member whose own archives are unreadable cannot prove the log
                if anchor == move.node_id:
                    anchor_failure = exc
                continue
            if not candidate.notices:
                loaded = candidate
                break
            if anchor == move.node_id:
                fallback = candidate
        if loaded is None:
            loaded = fallback
        if loaded is None and anchor_failure is not None:
            raise anchor_failure
    if loaded is None:
        attempts = state.attempt_agents.get(move.node_id, [])
        if not attempts:
            raise DagValidationError(f"node '{move.node_id}' has no recorded attempt to carry into the new session")
        loaded = await load_handover_turns(
            store,
            move.node_id,
            session_dir=None,
            source_agent=move.source_agent,
            source_instance=None,
            attempt_agents=attempts,
        )
    # The budget always keeps the task of the node the move was decided for
    # whole, whichever member's turn proved the log complete.
    task = await store.read_text(store.prompt_path(move.node_id))
    return _PendingHandover(
        anchor_node_id=move.node_id,
        source_agent=move.source_agent,
        source_instance=move.source_instance,
        target_agent=move.target_agent,
        target_instance=move.target_instance,
        target_named=move.target_instance is not None and not move.instance_auto,
        movers=frozenset(movers),
        turns=loaded.turns,
        task=task,
        notices=loaded.notices,
    )


async def _start_attempt_row(
    state: ReassignmentState,
    node: DagNodeSpec,
    replaced: DagNodeSpec | None,
    *,
    store: DagRunStore,
    session_key: str | None,
    status: dict[str, str],
    errors: dict[str, str],
    continuations: dict[str, str],
    progress_publisher: ProgressPublisher | None,
    node_ended_at: dict[str, int],
    submitted_agent: str,
) -> bool:
    """Give the node's durable row the agent about to run it; False when it cannot.

    The row is keyed by agent, so the switch has to happen before the attempt
    starts: a write under the new agent alone would leave the old row behind and
    one node would be listed twice. A node that has not moved (or whose previous
    attempt already ran on this agent) needs nothing.

    A row the transaction could not write is not a status update to swallow: the
    attempt it was about to name never happened, so the destination backend is
    never called. The saved attempt is rolled back to the one that really ran and
    the node fails through the ordinary failure path -- its reason is bounded
    like any other error and its durable row stays the complete row it was.
    """
    if not session_key:
        return True
    if node.id not in state.reassigned:
        return True
    if replaced is not None and replaced.subagent == node.subagent:
        return True
    try:
        await asyncio.wait_for(
            get_registry().move_dag_node(
                session_key,
                run_id=store.run_id,
                node_id=node.id,
                old_agent=replaced.subagent if replaced is not None else node.subagent,
                new_agent=node.subagent,
                status="running",
                instance=node.instance,
                instance_auto=node.id in state.auto_instances,
                attempt_agents=list(state.attempt_agents.get(node.id, [])),
            ),
            timeout=_REGISTRY_WRITE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - an unrecorded attempt is not dispatched
        state.attempt_agents[node.id].pop()
        if replaced is None:
            state.last_attempt_nodes.pop(node.id, None)
        else:
            state.last_attempt_nodes[node.id] = replaced
        _fail_node(
            status,
            errors,
            continuations,
            node.id,
            _bounded_attempt_reason(f"node '{node.id}' could not record its attempt on '{node.subagent}': {exc}"),
        )
        node_ended_at[node.id] = _now_ms()
        fields = _assignment_fields(node.id, state)
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {"run_id": store.run_id, "node": node.id, "status": status[node.id], **fields},
        )
        await _write_node_status(
            session_key,
            store.run_id,
            node.id,
            # The attempt that failed never ran, so the row belongs to the agent
            # that last really did -- the submitted one when none has. The frozen
            # node names the destination, which is not it.
            _current_agent(node.id, state, submitted_agent),
            status[node.id],
            assignment=fields,
        )
        return False
    return True


async def _run_group(
    nids: list[str],
    *,
    by_id: dict[str, DagNodeSpec],
    node_backends: dict[str, Any],
    state: ReassignmentState | None,
    store: DagRunStore,
    backend: Any,
    workdir: str,
    roots: tuple[str, ...],
    sandbox_factory: Any,
    output_paths: dict[str, str],
    status: dict[str, str],
    errors: dict[str, str],
    prompt_written: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    node_activity: dict[str, dict],
    semaphore: asyncio.Semaphore,
    branch: BranchState,
    settled: asyncio.Event | None = None,
    unsettled: set[str] | None = None,
    state_for: "Callable[[str, str | None, str], Any] | None" = None,
    memory_for: "Callable[[str], MemoryScope | None] | None" = None,
    mode_for: "Callable[[str, str | None, str], str | None] | None" = None,
    model_for: "Callable[[str | None, str | None, str | None], str | None] | None" = None,
    capabilities: dict[str, AgentCapabilities] | None = None,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
    subagents_root: str | None = None,
    record_tasks: "set[asyncio.Task] | None" = None,
    attempts: dict[str, int] | None = None,
    continuations: dict[str, str] | None = None,
    desk: "AdjudicationDesk | None" = None,
    judge_node: "Callable[..., Awaitable[Any]] | None" = None,
    on_node_start: "Callable[[str], Awaitable[None]] | None" = None,
    announce_exception: ExceptionAnnouncer | None = None,
    max_continuations: int = 2,
    origin: dict | None = None,
    dependents: dict[str, list[str]] | None = None,
    adjudication_timeout_s: float = 600.0,
    control_reachable: "Callable[[], bool] | None" = None,
    control_advert: "Callable[[str], str | None] | None" = None,
    provider: Any = None,
    model: str | None = None,
) -> None:
    """Run one instance-group's nodes sequentially, in id order."""
    for nid in nids:
        # Both halves live here rather than inside `_run_node`, so the `finally`
        # covers every way it can end -- a raise from the bookkeeping that follows
        # the verdict, or the cancellation a replan and a stop both deliver.
        # Leaving a node in `unsettled` blocks its dependents for the life of the
        # run with nothing to show why, and the wake is what the next round is
        # waiting on. Per node, not per group: a stateful instance's nodes share
        # one task, and the first one's dependents must not wait for the last.
        if unsettled is not None:
            unsettled.add(nid)
        try:
            await _run_node(
                nid,
                by_id=by_id,
                node_backends=node_backends,
                state=state,
                store=store,
                backend=backend,
                workdir=workdir,
                roots=roots,
                sandbox_factory=sandbox_factory,
                output_paths=output_paths,
                status=status,
                errors=errors,
                prompt_written=prompt_written,
                node_started_at=node_started_at,
                node_ended_at=node_ended_at,
                node_activity=node_activity,
                semaphore=semaphore,
                branch=branch,
                state_for=state_for,
                memory_for=memory_for,
                mode_for=mode_for,
                model_for=model_for,
                capabilities=capabilities,
                progress_publisher=progress_publisher,
                session_key=session_key,
                subagents_root=subagents_root,
                record_tasks=record_tasks,
                attempts=attempts,
                continuations=continuations,
                desk=desk,
                judge_node=judge_node,
                on_node_start=on_node_start,
                announce_exception=announce_exception,
                max_continuations=max_continuations,
                origin=origin,
                dependents=dependents,
                adjudication_timeout_s=adjudication_timeout_s,
                control_reachable=control_reachable,
                control_advert=control_advert,
                provider=provider,
                model=model,
            )
        finally:
            if unsettled is not None:
                unsettled.discard(nid)
            if settled is not None:
                settled.set()


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


async def _write_node_closing(store: DagRunStore, node_id: str, did: Any) -> None:
    """Persist what the node said after its last step, for the answer row.

    An empty file when the lane reported none, rather than no write: the id is
    reused across attempts (``DagRunStore.closing_path``). Failing to write it
    must not fail the node, for the reason the transcript's writer gives.
    """
    closing = getattr(did, "closing", None)
    try:
        await store.write_text(store.closing_path(node_id), closing if isinstance(closing, str) else "")
    except Exception as exc:  # noqa: BLE001 - an audit trail may not break the run
        logger.warning("DAG node {} closing could not be written: {}", node_id, exc)


async def _previous_output(store: DagRunStore, node_id: str) -> str:
    """The last attempt's answer, for a stateless node's follow-up prompt."""
    try:
        return await store.read_text(store.output_path(node_id))
    except Exception:  # noqa: BLE001 - a missing prior answer is not worth failing the retry
        return "(the previous attempt left no output)"


def _stateless_retry_prompt(prompt: str, previous: str, follow_ups: list[str]) -> str:
    """A stateless node's retry: its prompt, its previous answer, and every follow-up so far.

    The previous answer shows what the last attempt did, not what it was told, so
    an earlier follow-up left out here would be gone from this attempt while its
    judge still holds the node to it. With a single follow-up this is the shape a
    retry has always had.
    """
    *earlier, latest = follow_ups
    told = (
        "Earlier follow-ups, oldest first; where a later one disagrees, the later one stands:\n"
        + "\n\n".join(earlier)
        + "\n\n"
        if earlier
        else ""
    )
    return f"{prompt}\n\nYour previous attempt returned:\n{previous}\n\n{told}Now: {latest}"


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
    node_id: str,
    *,
    by_id: dict[str, DagNodeSpec],
    node_backends: dict[str, Any],
    state: ReassignmentState | None,
    store: DagRunStore,
    backend: Any,
    workdir: str,
    roots: tuple[str, ...],
    sandbox_factory: Any,
    output_paths: dict[str, str],
    status: dict[str, str],
    errors: dict[str, str],
    prompt_written: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    node_activity: dict[str, dict],
    semaphore: asyncio.Semaphore,
    branch: BranchState,
    state_for: "Callable[[str, str | None, str], Any] | None" = None,
    memory_for: "Callable[[str], MemoryScope | None] | None" = None,
    mode_for: "Callable[[str, str | None, str], str | None] | None" = None,
    model_for: "Callable[[str | None, str | None, str | None], str | None] | None" = None,
    capabilities: dict[str, AgentCapabilities] | None = None,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
    subagents_root: str | None = None,
    record_tasks: "set[asyncio.Task] | None" = None,
    attempts: dict[str, int] | None = None,
    continuations: dict[str, str] | None = None,
    desk: "AdjudicationDesk | None" = None,
    judge_node: "Callable[..., Awaitable[Any]] | None" = None,
    on_node_start: "Callable[[str], Awaitable[None]] | None" = None,
    announce_exception: ExceptionAnnouncer | None = None,
    max_continuations: int = 2,
    origin: dict | None = None,
    dependents: dict[str, list[str]] | None = None,
    adjudication_timeout_s: float = 600.0,
    control_reachable: "Callable[[], bool] | None" = None,
    control_advert: "Callable[[str], str | None] | None" = None,
    provider: Any = None,
    model: str | None = None,
) -> None:
    """Render, dispatch to the node's backend, and record one node.

    The node and its backend are read from the run's assignment record *inside*
    the concurrency gate: a group entry that waited for a slot through a move
    has to dispatch the pair the node is on now, and an entry whose group is
    still being transferred returns to the scheduler instead, so the next pass
    can regroup it. Everything after the pair is frozen -- the judge, the
    instance log, the memory scope, the terminal record and the reason kept for
    the move report -- reads the attempt's own node, never a later mutation.
    The node's durable registry row moves to this pair at the same boundary,
    before the backend is called: it is the only record of who is running the
    node while the run is in flight.
    """
    async with semaphore:
        if state is not None:
            node = state.effective(node_id, by_id[node_id])
            agent_backend = state.backends.get(node_id, node_backends.get(node_id))
            if state.blocked(node):
                return
        else:
            node = by_id[node_id]
            agent_backend = node_backends[node_id]
        attempt = (attempts or {}).get(node.id, 0) + 1
        if attempts is not None:
            attempts[node.id] = attempt
        follow_up = (continuations or {}).pop(node.id, None)
        replaced: DagNodeSpec | None = None
        started = True
        if state is not None:
            replaced = state.last_attempt_nodes.get(node.id)
            state.last_attempt_nodes[node.id] = node
            state.attempt_agents.setdefault(node.id, []).append(node.subagent)
            state.active_attempts[node.id] = _node_pair(node)
            started = await _start_attempt_row(
                state,
                node,
                replaced,
                store=store,
                session_key=session_key,
                status=status,
                errors=errors,
                continuations=continuations,
                progress_publisher=progress_publisher,
                node_ended_at=node_ended_at,
                submitted_agent=by_id[node.id].subagent,
            )
        try:
            ended = None
            if started:
                ended = await _attempt_node(
                    node,
                    agent_backend,
                    attempt=attempt,
                    follow_up=follow_up,
                    seed=state.handover_for(node) if state is not None else None,
                    state=state,
                    by_id=by_id,
                    store=store,
                    backend=backend,
                    workdir=workdir,
                    roots=roots,
                    sandbox_factory=sandbox_factory,
                    output_paths=output_paths,
                    status=status,
                    errors=errors,
                    prompt_written=prompt_written,
                    node_started_at=node_started_at,
                    node_ended_at=node_ended_at,
                    node_activity=node_activity,
                    branch=branch,
                    state_for=state_for,
                    mode_for=mode_for,
                    model_for=model_for,
                    capabilities=capabilities,
                    progress_publisher=progress_publisher,
                    session_key=session_key,
                    subagents_root=subagents_root,
                    continuations=continuations,
                    desk=desk,
                    judge_node=judge_node,
                    on_node_start=on_node_start,
                    announce_exception=announce_exception,
                    max_continuations=max_continuations,
                    origin=origin,
                    dependents=dependents,
                    adjudication_timeout_s=adjudication_timeout_s,
                    control_reachable=control_reachable,
                    control_advert=control_advert,
                    provider=provider,
                    model=model,
                )
        finally:
            if state is not None:
                state.active_attempts.pop(node.id, None)
        if ended is not None and state is not None:
            state.attempt_reasons.setdefault(node.id, []).append(_bounded_attempt_reason(ended.reason))
    # Scheduled after the `async with semaphore` above has already exited, so a
    # node's slot is released the moment its own work is done -- not held for
    # however long the recorder's poll budget takes.
    scope = memory_for(node.subagent) if memory_for is not None else None
    if scope is not None and ended is not None:
        _schedule_node_memory(
            node, store=store, scope=scope, session_key=session_key, record_tasks=record_tasks, turn=ended.turn
        )


_NO_REASON = "This attempt recorded no reason."


@dataclass(frozen=True)
class _AttemptEnd:
    """How one attempt of a node ended.

    Attributes:
        outcome (`str`):
            ``completed``, ``failed``, ``exception`` (waiting on an adjudication) or
            ``continued`` (judged short and sent its judge's own follow-up).
        reason (`str | None`):
            Why the attempt did not complete; ``None`` exactly when it did.
        turn (`list[dict[str, Any]]`):
            The turn the attempt added to its instance's log, for the node's memory record.
    """

    outcome: str
    reason: str | None
    turn: list[dict[str, Any]]


def _bounded_attempt_reason(reason: str | None) -> str:
    """One attempt's reason as the assignment record keeps it, bounded like an error."""
    if not reason:
        return ""
    collapsed = " ".join(reason.split())
    return (
        collapsed
        if len(collapsed) <= _MAX_ATTEMPT_REASON_CHARS
        else collapsed[:_MAX_ATTEMPT_REASON_CHARS] + "... (truncated)"
    )


def _attempt_outcome(
    node_id: str, status: dict[str, str], errors: dict[str, str], shortfall: str | None
) -> tuple[str, str | None]:
    """An attempt's outcome and reason, read off the node's status once its verdict has landed.

    The reason is never empty for an attempt that did not complete: it is what a
    reader of the trace is shown in place of the work, and ``str(exc)`` of a bare
    exception is ``""``. The fallback stays here and never reaches ``errors``.
    """
    state = status[node_id]
    if state == "completed":
        return "completed", None
    if state == "pending":
        return "continued", shortfall or errors.get(node_id) or _NO_REASON
    outcome = "exception" if state == "exception" else "failed"
    return outcome, errors.get(node_id) or shortfall or _NO_REASON


def _reported_from_here(origin: dict | None) -> dict | None:
    """``origin`` with its dispatch link moved to the span running now, for a report this attempt sends.

    The run's ``origin`` names the span that dispatched the whole graph; a report
    about one node belongs to the attempt that sent it, so the turn it starts links
    there. A copy, because the run's own reports -- its result -- keep the graph's
    link. Unchanged with tracing off, and for a graph dispatched outside any span:
    its attempts root traces no turn dispatched, so a turn linked to one could be
    reached from no main-line turn, where a report with no link is one itself.
    """
    link = trace.dispatch_link()
    if origin is None or link is None or trace.dispatch_link_of(origin) is None:
        return origin
    return {**origin, **trace.dispatch_attrs(link)}


@trace.instrument("subagent.run", on_open=semconv.dag_node_open, extract=semconv.dag_node)
async def _attempt_node(
    node: DagNodeSpec,
    agent_backend: Any,
    *,
    attempt: int,
    follow_up: str | None,
    seed: "_PendingHandover | None" = None,
    state: ReassignmentState | None = None,
    by_id: dict[str, DagNodeSpec],
    store: DagRunStore,
    backend: Any,
    workdir: str,
    roots: tuple[str, ...],
    sandbox_factory: Any,
    output_paths: dict[str, str],
    status: dict[str, str],
    errors: dict[str, str],
    prompt_written: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    node_activity: dict[str, dict],
    branch: BranchState,
    state_for: "Callable[[str, str | None, str], Any] | None" = None,
    mode_for: "Callable[[str, str | None, str], str | None] | None" = None,
    model_for: "Callable[[str | None, str | None, str | None], str | None] | None" = None,
    capabilities: dict[str, AgentCapabilities] | None = None,
    progress_publisher: ProgressPublisher | None = None,
    session_key: str | None = None,
    subagents_root: str | None = None,
    continuations: dict[str, str] | None = None,
    desk: "AdjudicationDesk | None" = None,
    judge_node: "Callable[..., Awaitable[Any]] | None" = None,
    on_node_start: "Callable[[str], Awaitable[None]] | None" = None,
    announce_exception: ExceptionAnnouncer | None = None,
    max_continuations: int = 2,
    origin: dict | None = None,
    dependents: dict[str, list[str]] | None = None,
    adjudication_timeout_s: float = 600.0,
    control_reachable: "Callable[[], bool] | None" = None,
    control_advert: "Callable[[str], str | None] | None" = None,
    provider: Any = None,
    model: str | None = None,
) -> _AttemptEnd:
    """Run one attempt of a node: from the moment it holds a concurrency slot to its terminal event.

    Recorded as one ``subagent.run`` span, so the attempt's own steps -- its
    model and tool calls, its judge, the reports it sends -- nest under it, apart
    from a sibling attempt running beside it. The span closes on the outcome
    returned here (see ``semconv.dag_node``).
    """
    origin = _reported_from_here(origin)
    started_at_ms = _now_ms()
    did = None
    node_started_at[node.id] = started_at_ms
    # Set inside the gate, so a node still queued for a concurrency slot
    # stays `pending`: this is what tells a stop which nodes actually ran.
    status[node.id] = "running"
    if on_node_start is not None:
        # Inside the gate and before the first byte of work, because what a
        # caller wants from this moment is a *baseline*: where the tree stood
        # before this node touched it. Taken when the node was queued it would
        # include whatever ran while it waited, and taken afterwards there is
        # nothing left to compare against.
        try:
            await on_node_start(node.id)
        except Exception as exc:  # noqa: BLE001 - a bookkeeping hook must not fail a node
            logger.opt(exception=True).warning("DAG node {} start hook raised: {}", node.id, exc)
    assignment = _assignment_fields(node.id, state)
    await _emit(
        progress_publisher,
        "dag_node_updated",
        {
            "run_id": store.run_id,
            "node": node.id,
            "status": "running",
            "started_at": started_at_ms,
            **assignment,
        },
    )
    await _write_node_status(session_key, store.run_id, node.id, node.subagent, "running", assignment=assignment)
    seed_dispatch = seed is not None and state is not None
    if not seed_dispatch:
        await _link_node_instance(session_key, store.run_id, node.id, node.subagent, node.instance)
    node_output: str | None = None
    # A claimed seed whose dispatch never happened, and the destination of a
    # refused move: neither session may receive this attempt's failure turn.
    claimed_seed: _PendingHandover | None = None
    dispatched = False
    keep_turn_out = False
    try:
        rendered_prompt = await render_prompt(
            node,
            backend=backend,
            cwd=workdir,
            nodes_root=store.nodes_root,
            roots=roots,
            run_id=store.run_id,
            by_id=by_id,
            capabilities=capabilities,
            not_executed={nid for nid, st in status.items() if st == NOT_EXECUTED},
            may_not_execute=branch.skippable,
        )
        prompt = rendered_prompt
        # What the handover, when one is planted, introduces: the message this
        # attempt would otherwise be dispatched with. A stateless retry's wrapper
        # is not part of it -- the handover already carries the earlier answer it
        # would restate.
        next_message = rendered_prompt
        if follow_up is not None:
            # Recorded before the dispatch, since this attempt's judge reads the
            # record, and as the message itself, not `prompt`: the judge reads the
            # task from `prompt_path`, and a stateless node's prompt repeats it.
            follow_ups = await store.record_follow_up(node.id, follow_up)
            # A node with an instance is resuming a conversation whose history
            # is already on disk, so restating the task would only compete with
            # it. A stateless one has no history at all, so the whole task has
            # to travel with the follow-up or it starts from nothing -- every
            # follow-up, too, since its previous answer need not repeat them.
            previous = await _previous_output(store, node.id)
            prompt = follow_up if node.instance else _stateless_retry_prompt(prompt, previous, follow_ups)
            next_message = follow_up
        prompt_path = store.prompt_path(node.id)
        output_path = store.output_path(node.id)
        if attempt == 1:
            # `prompt_path` is the prompt as first rendered -- what
            # dag_status/read_node show, and what the judge reads ahead of the
            # follow-ups recorded above -- so it must stay fixed at the original
            # render even once a later attempt substitutes a follow-up into `prompt`.
            await store.write_text(prompt_path, rendered_prompt)
            await store.forget_follow_ups(node.id)
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
        # A third-party acp row's own model, on the same terms: an instance
        # override if the node names one and one is set, else the row's own
        # configured `model`. `optional_keyword` below is what keeps this
        # off a backend that declares no such parameter -- a builtin node's
        # `RavenLoopBackend` among them, whose own model is a pin the
        # backend pairs with its credential itself (see `manager.build_builtin_backend`).
        node_session_model = (
            model_for(session_key or "", node.subagent, node.instance) if model_for is not None else None
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
            if seed_dispatch:
                # Claimed under the destination handle's lock: exactly one
                # dispatch in this run seeds the new session, and the collision
                # check has to see a binding another lane made while this node
                # waited, before any of our text reaches that session.
                if state.claim_handover(seed):
                    claimed_seed = seed
                    try:
                        prompt = await _plant_handover(
                            seed,
                            node=node,
                            store=store,
                            attempt=attempt,
                            next_message=next_message,
                            session_key=session_key,
                            subagents_root=subagents_root,
                        )
                    except _DestinationOccupiedError:
                        state.release_handover(seed)
                        claimed_seed = None
                        keep_turn_out = True
                        raise
                    except Exception:
                        state.release_handover(seed)
                        claimed_seed = None
                        keep_turn_out = True
                        raise
                    await _announce_handover_notices(
                        seed,
                        run_id=store.run_id,
                        node=node,
                        origin=origin,
                        announce_exception=announce_exception,
                    )
                await _link_node_instance(session_key, store.run_id, node.id, node.subagent, node.instance)
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
                stall_watch = asyncio.create_task(
                    _watch_stall(
                        did,
                        run_id=store.run_id,
                        node=node,
                        origin=origin,
                        announce_exception=announce_exception,
                        progress_publisher=progress_publisher,
                    )
                )
                # One VM per node, the way a spawn holds one per sub-agent
                # (manager.py): a sibling's crashed VM takes no neighbour
                # with it, and the graph's semaphore bounds how many boot.
                # All of it under the watcher's cancel: a VM that fails to
                # boot is an expected operational failure, and a watcher
                # left behind would report a stall that is not one.
                try:
                    executor = sandbox_factory(Path(workdir)) if sandbox_factory is not None else None
                    async with executor if executor is not None else contextlib.nullcontext():
                        dispatched = True
                        result = await agent_backend.run(
                            # The graph's own dispatch route: `spawn` goes
                            # through the manager and never reaches here, so
                            # the language has to be stated on both or an acp
                            # node still narrates in English.
                            dispatch_language_line(prompt),
                            task_id=node.id,
                            workspace=Path(workdir),
                            executor=executor,
                            session_key=session_key,
                            instance=node.instance,
                            mode=node_mode,
                            # The template, not the rendering: a routing entry
                            # reads the deliverable off the words the planner
                            # wrote, not off whatever an upstream output inlined.
                            **optional_keyword(agent_backend, "authored_task", node.prompt_template),
                            # The invoking turn's binding, when the tool resolved one:
                            # a pooled ACP worker otherwise keeps whatever model it
                            # was first launched with. Only when set, because a node
                            # backend built for a test may take no such keywords.
                            **({"provider": provider} if provider is not None else {}),
                            **({"model": model} if model else {}),
                            # The row's own acp model choice, not the parent
                            # binding above: this is what the session itself
                            # answers with, pushed over `session/set_config_option`
                            # the same way a spawn's does (`manager.row_default_model`).
                            **optional_keyword(agent_backend, "session_model", node_session_model),
                            **state_kwargs,
                        )
                        if (failure := llm_error_reply(result)) is not None:
                            # The same rule as a spawn's (manager.py): a reply
                            # that is nothing but the provider's error is not
                            # this node's output, and a node that read it as
                            # one fed the error to the step downstream.
                            raise SubagentNoAnswerError(failure)
                finally:
                    stall_watch.cancel()
                    node_activity[node.id] = did.as_meta()
                    # Set aside for the reader until the manifest carries
                    # it: `collecting` drops the live entry as this block
                    # exits, and the manifest is written once the whole
                    # run is over.
                    activity.record_settled(node_live_key(store.run_id, node.id), node_activity[node.id])
        # The whole answer when the reply cap cut one: the in-context copy of
        # a terminal output is capped again on the way out (see
        # `_terminal_outputs`), and this file is what the reader, the next
        # node's placeholder, and the record all resolve to. The per-attempt
        # archive keeps the same uncapped text, for the same reason.
        persisted = activity.persisted_output(did, result) or ""
        await store.write_text(output_path, persisted)
        await store.write_text(store.attempt_output_path(node.id, attempt), persisted)
        await _write_node_closing(store, node.id, did)
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
        if claimed_seed is not None and not dispatched:
            # Nothing reached the session behind the seed, so the next mover may
            # still plant it, and this attempt's failure stays out of that log.
            state.release_handover(claimed_seed)
            claimed_seed = None
            keep_turn_out = True
        logger.opt(exception=True).warning("DAG node {} failed: {}", node.id, exc)
        status[node.id] = "failed"
        errors[node.id] = str(exc)
    # Written before the verdict, not after: the production judge reads this
    # file back through `store.transcript_path` to build its evidence, so
    # writing it later meant a first attempt was always judged with no
    # transcript, and a continuation's judge read the previous attempt's.
    await _write_node_transcript(store, node.id, did, attempt)
    shortfall = None
    if judge_node is not None:
        shortfall = await _apply_verdict(
            node,
            store=store,
            status=status,
            errors=errors,
            output_paths=output_paths,
            continuations=continuations,
            node_output=node_output,
            attempt=attempt,
            desk=desk,
            judge_node=judge_node,
            announce_exception=announce_exception,
            max_continuations=max_continuations,
            origin=origin,
            dependents=dependents or {},
            adjudication_timeout_s=adjudication_timeout_s,
            branch=branch,
            control_reachable=control_reachable,
            control_advert=control_advert,
            # Read off the dispatch's own activity rather than the transcript:
            # a turn that spent its whole budget thinking leaves a record that
            # looks merely empty, and the cut is the difference between "it
            # produced nothing" and "it was stopped before it could".
            output_limited=bool(getattr(did, "output_limited", False)),
            reassignment_state=state,
        )
    elif node.routes and status[node.id] == "completed":
        # Nobody to select a route, and none to default to. The tool refuses such a
        # graph on a host with no judge; `run_dag` called directly does not.
        _fail_undetermined_route(
            node.id,
            "this run has no judge to select a route",
            status=status,
            errors=errors,
            output_paths=output_paths,
        )
    outcome, reason = _attempt_outcome(node.id, status, errors, shortfall)
    turn: list[dict[str, Any]] = []
    if not keep_turn_out:
        turn = await _add_node_to_instance_log(subagents_root, node, did, session_key, errors.get(node.id), node_output)
    ended_at_ms = _now_ms()
    node_ended_at[node.id] = ended_at_ms
    selection = _selection_fields(node.id, branch)
    assignment = _assignment_fields(node.id, state)
    await _emit(
        progress_publisher,
        "dag_node_updated",
        {
            "run_id": store.run_id,
            "node": node.id,
            "status": status[node.id],
            "started_at": started_at_ms,
            "ended_at": ended_at_ms,
            **selection,
            **assignment,
        },
    )
    await _write_node_status(
        session_key, store.run_id, node.id, node.subagent, status[node.id], **selection, assignment=assignment
    )
    return _AttemptEnd(outcome, reason, turn)


def _memory_backend():
    """A fresh, unstarted memory backend, or ``None`` when there is not one.

    Built per record rather than held: this runs after a node has already
    answered, and a record nobody is waiting on must not keep a backend alive
    for the life of the run. Its caller owns the ``start`` / ``stop`` pair
    around the one record (``started_backend``).
    """
    from raven.config import load_config
    from raven.config.raven import load_raven_config
    from raven.core.plugin_stack import maybe_build_memory_backend

    try:
        return maybe_build_memory_backend(load_config().workspace_path, load_raven_config())
    except Exception as exc:  # noqa: BLE001 - a record must never fail a node
        logger.warning("DAG node memory record: no backend ({})", exc)
        return None


def _schedule_node_memory(
    node: DagNodeSpec,
    *,
    store: DagRunStore,
    scope: MemoryScope,
    session_key: str | None,
    record_tasks: "set[asyncio.Task] | None" = None,
    turn: list[dict[str, Any]] | None = None,
) -> None:
    """Fire off this node's Memory record without waiting on it.

    Never awaited by the dispatch path: everos extraction runs an LLM, and the
    run must not wait on the host's bookkeeping (see `record_memories`'s own
    docstring, and the same reasoning `SubagentManager._schedule_memory_record`
    already applies to spawn and a direct chat). ``turn`` is only read for a
    ``trace`` source, whose record has no other conversation to poll for.
    """
    task = asyncio.create_task(_record_node_memory(node, store=store, scope=scope, session_key=session_key, turn=turn))
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
    scope: MemoryScope,
    session_key: str | None,
    turn: list[dict[str, Any]] | None = None,
) -> None:
    """Write this node's Memory record, swallowing any failure.

    Written through the store rather than to a local path: a DAG run's files go
    to the DAG core's file backend, which may not be this filesystem.
    """
    if scope.source == "trace":
        # The host owns both the write and the read here, so it mints the join
        # key instead of resolving one the node's own run committed.
        session_id = trace_session_id(node.subagent, f"{store.run_id}:{node.id}")
        rows = turn or []

        async def _resolve() -> str | None:
            return session_id

        prime_rows, budget = rows, TRACE_BUDGET_S
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
            return f"{scope.session_prefix}{agent_id}" if agent_id else None

        prime_rows, budget = None, None

    async def _write(text: str) -> None:
        await store.write_text(store.memory_path(node.id), text)

    try:
        kwargs = {"budget_s": budget} if budget is not None else {}
        async with started_backend(_memory_backend(), label="DAG node memory record") as backend:
            if backend is None:
                return

            async def _prime(sid: str) -> bool:
                return await prime_from_turn(backend=backend, scope=scope, session_id=sid, turn=prime_rows or [])

            await record_memories(
                agent=node.subagent,
                backend=backend,
                scope=scope,
                resolve_session_id=_resolve,
                write=_write,
                instance=node.instance,
                prime=_prime if prime_rows is not None else None,
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
    *,
    branch: BranchState,
    state: ReassignmentState | None = None,
) -> DagRunResult:
    """Assemble the result, write the manifest, and update the registry.

    Also reconciles every node's registry row to its final status. This is
    the only place that unconditionally re-asserts a status regardless of
    what earlier writes landed, so it repairs the case where a node's own
    terminal write raced a concurrent registry flush and lost, or where an
    outer cancellation interrupted a write mid-flight -- either of which would
    otherwise leave a row stuck at a stale, non-terminal status forever. The
    write is idempotent, so re-asserting an already-correct row is a no-op.

    A node that moved reports the agent, instance and history of its last
    attempt, and its row is re-asserted under that agent: the reconciliation is
    a write like any other, and a row under the submitted agent would leave the
    moved one behind.
    """
    files: list[dict] = []
    manifest: dict = {}
    outcomes: dict[str, dict[str, Any]] = {}
    # The assignment record owns the minted set once it exists: a handle minted by
    # a move is as auto as one minted at submission, and the flag is about the
    # handle this node ended on.
    minted = state.auto_instances if state is not None else auto_instances
    for node in spec.nodes:
        nid = node.id
        prompt_file = store.prompt_path(nid) if nid in prompt_written else None
        output_file = output_paths.get(nid)
        error = errors.get(nid)
        if error is not None and len(error) > _MAX_ERROR_CHARS:
            error = error[:_MAX_ERROR_CHARS] + "... (truncated)"
        # Present only where a branch put them: a graph without routes or guards carries none.
        branched = {
            **({"reason": branch.reasons[nid]} if nid in branch.reasons else {}),
            **_selection_fields(nid, branch),
        }
        outcomes[nid] = branched
        assignment = _assignment_fields(nid, state)
        moved = state is not None and nid in state.reassigned
        last = state.last_attempt_nodes.get(nid) if moved and state is not None else None
        # A node that moved reports the agent and instance of the attempt that ran
        # it last; one that never ran reports what the graph submitted. Its attempt
        # history rides beside them, factual even when it is empty.
        reported_agent = last.subagent if last is not None else node.subagent
        reported_instance = last.instance if last is not None else node.instance
        history = {"attempt_agents": assignment["attempt_agents"]} if "attempt_agents" in assignment else {}
        files.append(
            {
                "node": nid,
                "subagent": reported_agent,
                "depends_on": node.depends_on,
                "instance": reported_instance,
                "instance_auto": nid in minted,
                "status": status[nid],
                "started_at": node_started_at.get(nid),
                "ended_at": node_ended_at.get(nid),
                "prompt_file": prompt_file,
                "output_file": output_file,
                "error": error,
                **branched,
                **history,
            },
        )
        manifest[nid] = {
            "status": status[nid],
            "subagent": reported_agent,
            "depends_on": node.depends_on,
            "instance": reported_instance,
            "instance_auto": nid in minted,
            "started_at": node_started_at.get(nid),
            "ended_at": node_ended_at.get(nid),
            "prompt_file": prompt_file,
            "output_file": output_file,
            "error": errors.get(nid),
            **branched,
            **history,
            # What the node did on the way, on the same terms a spawned call
            # records it: absent keys mean the transport could not say, not zero.
            **node_activity.get(nid, {}),
        }

    if session_key:
        # Concurrent, not sequential: each call already has its own bounded
        # timeout (_REGISTRY_WRITE_TIMEOUT_S), so awaiting them one at a time
        # would make teardown scale as N x that timeout under a contended
        # registry. Run them all at once so the worst case is roughly one
        # timeout window regardless of node count. Each carries the node's outcome:
        # the row is rebuilt on every write, so a bare status here would erase it.
        await asyncio.gather(
            *(
                _write_node_status(
                    session_key,
                    store.run_id,
                    node.id,
                    _current_agent(node.id, state, node.subagent),
                    status[node.id],
                    **outcomes[node.id],
                    assignment=_assignment_fields(node.id, state),
                )
                for node in spec.nodes
            )
        )

    terminal_outputs: list[dict] = []
    for nid in by_id:
        # A completed node whose dependents all stayed unexecuted is the last word on its
        # path -- a `select: many` router that selected nothing is the case. Only an
        # unexecuted dependent ends the path there: one that failed, was skipped or was
        # cancelled is a path the run did not finish, so the node it depends on stays an
        # intermediate result.
        if status[nid] != "completed":
            continue
        if any(status[dep] != NOT_EXECUTED for dep in dependents[nid]):
            continue
        text = await store.read_text(output_paths[nid])
        if len(text) > _MAX_OUTPUT_CHARS:
            text = text[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"
        terminal_outputs.append({"node": nid, "text": text})

    summary = _tally(status)
    await store.write_manifest(manifest)
    # The manifest now carries every node's account; the copies set aside for
    # the nodes that finished before it was written have done their job.
    activity.forget_settled(node_live_key(store.run_id, nid) for nid in by_id)
    await _record_outcome(store, status)
    return DagRunResult(
        run_id=store.run_id,
        dir=store.run_dir,
        terminal_outputs=terminal_outputs,
        files=files,
        summary=summary,
    )
