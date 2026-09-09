"""Where a suspended node waits for a decision, and where a foreground run's reports wait for a reader.

Two trays, one per run, both in memory only. The desk carries decisions from the
main agent to the run: `resolve_dag_node` lands there and the runner's wait wakes.
The outbox carries the other way: a foreground run's exception reports and its
final result wait there for the tool call that is awaiting the run. A gateway
restart drops both, and the run's nodes read back `interrupted` -- the same
outcome an in-flight run already has when the process dies.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

from loguru import logger

from raven.agent.subagent.dag_graph import DagNodeSpec

# A failed report delivery is retried: an announcer is a transport, and an injected
# turn can lose a race with a gateway restart or a busy submit queue, which is no
# reason to discard a report. Bounded, because a bound run has no adjudication
# deadline -- an unbounded loop would spin for the life of the turn on a transport
# that is not coming back -- and the caller still decides what an outlived failure
# costs. Retrying is only safe because an announce is all-or-nothing: the injection
# is the last step that can fail, and the marker emit after it swallows its own
# failure (see ``SubagentManager._emit_event``), so a raise means nothing landed.
REPORT_DELIVERY_ATTEMPTS = 3
REPORT_DELIVERY_BACKOFF_S = 0.5


async def deliver_report(send: Callable[[], Awaitable[None]], *, what: str) -> Exception | None:
    """Run one delivery, retrying a failure. The failure that outlived the attempts, or None."""
    failure: Exception | None = None
    for attempt in range(1, REPORT_DELIVERY_ATTEMPTS + 1):
        try:
            await send()
            return None
        except Exception as exc:  # noqa: BLE001 - the caller decides what a failed delivery costs
            failure = exc
            logger.opt(exception=True).warning(
                "report delivery for {} failed on attempt {} of {}: {}",
                what,
                attempt,
                REPORT_DELIVERY_ATTEMPTS,
                exc,
            )
            if attempt < REPORT_DELIVERY_ATTEMPTS:
                await asyncio.sleep(REPORT_DELIVERY_BACKOFF_S * attempt)
    return failure


CONTINUE = "continue"
ABANDON = "abandon"
REPLAN = "replan"
DECISIONS = (CONTINUE, ABANDON, REPLAN)


@dataclass(frozen=True)
class ReplanPlan:
    """A validated replacement graph, on its way from the resolve call to the run.

    Carries its own ``run_id`` because the run winding down names it: a node reason
    saying it was superseded with no destination leaves the reader of that run
    nowhere to go, and the id cannot be minted later than the wind-down that cites
    it. ``backends`` travels here rather than being resolved by the runner because
    the dispatch map is a closure owned by ``SubAgentDagTool._run``.

    ``task_summary`` and ``confirm`` default so existing construction sites (a
    chain of replans is not the only one) do not need updating, but a real
    ``prepare_replan`` call fills both in: the successor's own submission has to
    inherit them from the old run's spec the same way ``prepare_replan`` itself
    does, or a second hop in the same chain loses its title and its confirm gate.
    """

    run_id: str
    from_node: str
    reason: str
    nodes: tuple[DagNodeSpec, ...]
    backends: dict[str, Any]
    auto_instances: frozenset[str]
    notices: tuple[str, ...]
    task_summary: str = ""
    confirm: bool = False


@dataclass(frozen=True)
class Adjudication:
    """What the main agent decided about one suspended node."""

    decision: str
    message: str | None = None
    plan: ReplanPlan | None = None


class AdjudicationDesk:
    """The pending adjudications of one run."""

    def __init__(self) -> None:
        self._waiting: dict[str, asyncio.Event] = {}
        self._answers: dict[str, Adjudication] = {}
        self.replanned = asyncio.Event()
        self.continued = asyncio.Event()
        self._plan: ReplanPlan | None = None

    def open(self, node_id: str) -> asyncio.Event:
        """Start waiting on ``node_id``. The event fires when an answer lands."""
        event = asyncio.Event()
        self._waiting[node_id] = event
        return event

    def is_open(self, node_id: str) -> bool:
        return node_id in self._waiting

    def waiter(self, node_id: str) -> asyncio.Event:
        """The event for ``node_id``, opening one if it is not already open."""
        event = self._waiting.get(node_id)
        if event is None:
            event = self.open(node_id)
        return event

    def open_nodes(self) -> set[str]:
        return set(self._waiting)

    def resolve(self, node_id: str, decision: str, message: str | None, plan: ReplanPlan | None = None) -> bool:
        """Record an answer and wake the waiter. False when nobody was waiting.

        The caller reports that False to the model rather than swallowing it: by
        the time an answer arrives the node may have timed out or the run may
        have been cancelled, and a silently discarded decision looks to the model
        exactly like one that was applied.

        A replan also fires ``replanned``, which is what lets the scheduling round
        in flight be interrupted rather than drained -- but only once the answer
        is recorded, so an answer nobody was waiting for cannot tear down a run
        it was never going to reach.

        A continue fires ``continued`` for the same reason and to the opposite
        effect on the round's other nodes: the round ends early so this node can
        be dispatched again, and whatever is still running is handed to the next
        round rather than cancelled. Without it the re-dispatch waits out every
        sibling of the round it was suspended from -- nodes it does not depend on
        and cannot be helped by, for an unbounded time.
        """
        event = self._waiting.get(node_id)
        if event is None:
            return False
        self._answers[node_id] = Adjudication(decision=decision, message=message, plan=plan)
        if decision == REPLAN:
            self._plan = plan
            self.replanned.set()
        elif decision == CONTINUE:
            self.continued.set()
        event.set()
        return True

    def take(self, node_id: str) -> Adjudication | None:
        """The answer for ``node_id``, consumed. ``None`` if none landed."""
        self._waiting.pop(node_id, None)
        return self._answers.pop(node_id, None)

    def take_plan(self) -> ReplanPlan | None:
        """The replacement graph a replan landed, consumed."""
        plan, self._plan = self._plan, None
        return plan

    def close(self, node_id: str) -> None:
        """Stop waiting on ``node_id`` without consuming an answer."""
        self._waiting.pop(node_id, None)
        self._answers.pop(node_id, None)


@dataclass(frozen=True)
class Report:
    """One node's exception report, exactly as the runner built it."""

    node_id: str
    text: str


@dataclass(frozen=True)
class Final:
    """The run's rendered result. ``stopped`` is the run's cancel event at the end."""

    result: Any
    stopped: bool = False


@dataclass(frozen=True)
class Stopped:
    """The run task was hard-cancelled before it produced a result."""


Event = Report | Final | Stopped


class AnnounceReport(Protocol):
    """How a released run's report reaches the main agent as its own turn.

    Carries ``awaiting_decision`` for the same reason the runner's announcer does:
    a released run announces notifications as readily as questions, and only the
    caller knows which this is.
    """

    async def __call__(
        self, node_id: str, text: str, *, awaiting_decision: bool, informational: bool = False
    ) -> None: ...


AnnounceFinal = Callable[[Any], Awaitable[None]]


def _describe(event: Event) -> str:
    return f"node {event.node_id}" if isinstance(event, Report) else "the run result"


class Outbox:
    """A foreground run's tray of events, waiting for the tool call that will take them.

    *Bound* while the turn that started the run is alive: events are handed to a
    waiting taker or buffered, and nothing is announced, because between the
    agent's tool calls there is no safe moment to inject a turn -- an injected
    report would either duplicate what the next ``take()`` returns or queue a
    turn behind the one about to wait on it. *Released* once that turn has
    ended: what is still unanswered is re-sent through the announcers (or
    dropped, when the turn was cancelled) and later events announce directly,
    which is what turns a released foreground run into an ordinary backgrounded
    one.

    *Unanswered* is the whole tray, not just the untaken part of it. A report
    handed to a taker is not thereby decided: its node is still open on the desk,
    the returned text promises the holder a re-send, and once the turn is over no
    other route will ever raise that node again. So a handed report stays here
    until ``answered()`` retires it, and a turn that ends holding one leaves it
    to be re-sent exactly like one nobody took.

    ``take()`` registers its taker before its first await. That is what lets a
    caller run ``desk.resolve(...)`` and then ``take()`` in one tick without the
    runner producing the next event into an empty tray in between.
    """

    def __init__(self, *, conversation: str, announce_report: AnnounceReport, announce_final: AnnounceFinal) -> None:
        self.conversation = conversation
        self.released = asyncio.Event()
        self._announce_report = announce_report
        self._announce_final = announce_final
        self._buffer: deque[Report | Final] = deque()
        self._handed: dict[str, Report] = {}
        self._takers: deque[asyncio.Future] = deque()
        self._final: Final | None = None
        self._stopped = False

    @property
    def bound(self) -> bool:
        return not self.released.is_set() and not self._stopped

    async def take(self) -> Event:
        """The next event: a queued report to one taker, an undecided handed one re-sent, a final to every taker."""
        if self._stopped:
            return Stopped()
        if self._buffer:
            return self._record(self._buffer.popleft())
        if self._final is not None:
            return self._final
        if self._handed:
            # The re-send this tray owes is owed to whoever asks next, not only to the
            # announcers at release. The runner waits for every other open node before
            # it produces anything, and it waits only once nothing else can run -- so a
            # taker parking here while a node it was already shown is undecided waits
            # for a report that cannot be produced and a final that cannot arrive.
            return self._record(next(iter(self._handed.values())))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._takers.append(fut)
        try:
            return self._record(await fut)
        except asyncio.CancelledError:
            if fut in self._takers:
                self._takers.remove(fut)
            raise

    def _record(self, event: Event) -> Event:
        """Remember a handed report as still-unanswered, keyed by its node."""
        if isinstance(event, Report):
            self._handed[event.node_id] = event
        return event

    def answered(self, node_id: str) -> None:
        """A decision reached this node, so its report is owed nowhere any more.

        Both trays, because the agent can decide a node it was never handed: the
        report it did get names what is blocked behind it, and ``dag_status`` lists
        the rest. Retiring only the handed tray leaves that node's own queued report
        to come back as the next question it is asked -- one it has just answered.
        """
        self._handed.pop(node_id, None)
        self._buffer = deque(e for e in self._buffer if not (isinstance(e, Report) and e.node_id == node_id))

    def _hand(self, event: Event) -> bool:
        """Give ``event`` to the oldest live taker. False when nobody is waiting."""
        while self._takers:
            fut = self._takers.popleft()
            if not fut.done():
                fut.set_result(event)
                return True
        return False

    async def put_report(
        self, node_id: str, text: str, *, awaiting_decision: bool, informational: bool = False
    ) -> None:
        """Take one report. Where it goes depends on the lane, which only this object knows.

        Released, every kind announces: there is no blocking call left for a summary to
        reach, so dropping a notification here would leave the node's outcome nowhere
        until the whole graph finishes -- which is the one thing a released run is not
        supposed to do differently from a backgrounded one. ``informational`` travels
        with it, so the announcer heads a still-running node's notice as a notice.

        Bound, a notification is dropped on purpose. The call is still waiting and the
        run's summary is what it will be handed; announcing would put the same news in
        two places, one of them a question nobody can answer. A stall notice is dropped
        here too: the model inside the bound call cannot act on it before the call
        returns, and the progress event the watcher publishes beside it is what the
        panel shows meanwhile.
        """
        if self._stopped:
            return
        if self.released.is_set():
            await self._announce_report(node_id, text, awaiting_decision=awaiting_decision, informational=informational)
            return
        if not awaiting_decision:
            return
        event = Report(node_id, text)
        if not self._hand(event):
            self._buffer.append(event)

    async def put_final(self, result: Any, *, stopped: bool = False) -> None:
        if self._stopped:
            return
        event = Final(result, stopped)
        self._final = event
        if self.released.is_set():
            # The same step the drain's buffered final takes, for the same reason:
            # this is awaited from the run's detached task with nothing around it,
            # and its done callbacks drop the run's indexes without retrieving
            # `task.exception()` -- so a raise here loses the result and leaves an
            # unretrieved-exception warning, where the backgrounded lane logs it.
            await self._announce_owed(event)
            return
        handed = False
        while self._takers:
            fut = self._takers.popleft()
            if not fut.done():
                fut.set_result(event)
                handed = True
        if not handed:
            self._buffer.append(event)

    async def release(self, *, flush: bool) -> None:
        """The owning turn ended. ``flush`` re-sends what it left unanswered; a cancelled turn passes False."""
        if self._stopped or self.released.is_set():
            return
        self.released.set()
        # No taker is waiting here: a take() lives inside a tool call, and every
        # tool call of the turn that owned this run has returned by the time the
        # turn ends. Nothing is done to the taker queue on purpose -- cancelling a
        # parked take() would read, in the tool, as the user stopping the agent
        # and abort the run.
        if not flush:
            self._handed.clear()
            self._buffer.clear()
            return
        # Handed first: the turn saw these before whatever queued behind them.
        #
        # The clause below governs the handed tray only, and deliberately. A handed
        # report was already put in front of the agent, so once the result has landed
        # re-sending it asks again for a decision that can no longer change anything.
        # A buffered one was never seen at all: that a node asked for something and
        # nobody answered is not in the summary, and the run being over does not make
        # it not worth knowing. A report retired by an actual decision leaves both
        # trays -- see `answered`, which is what keeps a settled node out of this.
        if self._final is not None:
            self._handed.clear()
        while self._handed and not self._stopped:
            node_id = next(iter(self._handed))
            event = self._handed.pop(node_id)
            await self._announce_owed(event)
        while self._buffer and not self._stopped:
            event = self._buffer.popleft()
            await self._announce_owed(event)

    async def _announce_owed(self, event: Event) -> None:
        """One event of the release drain, retried like any other delivery.

        This drain is what makes good on the re-send the returned report promised,
        and releasing starts the adjudication deadline -- so an event dropped here
        can only expire unseen, with the agent blamed for not answering a question
        it never received. The event has already left its tray and nothing re-drains,
        so the attempts happen now or not at all. An outlived failure is logged and
        the drain moves on: one undeliverable event must not strand the rest.
        """
        if isinstance(event, Report):
            send = partial(self._announce_report, event.node_id, event.text, awaiting_decision=True)
        elif event.stopped:
            return
        else:
            send = partial(self._announce_final, event.result)
        failure = await deliver_report(send, what=_describe(event))
        if failure is not None:
            logger.warning("outbox event could not be announced: {}", event)

    def stop(self) -> None:
        """The run task is being hard-cancelled: nothing buffered is worth re-sending."""
        if self._stopped:
            return
        self._stopped = True
        self._handed.clear()
        self._buffer.clear()
        while self._takers:
            fut = self._takers.popleft()
            if not fut.done():
                fut.set_result(Stopped())
