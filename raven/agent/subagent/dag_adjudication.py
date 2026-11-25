"""Where a suspended node waits for a decision.

One tray, in memory only: decisions from the main agent to the run.
`resolve_dag_node` lands there and the runner's wait wakes. A gateway restart
drops it, and the run's nodes read back `interrupted` -- the same outcome an
in-flight run already has when the process dies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from raven.agent.subagent.dag_carry import CarryPlan
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
REASSIGN = "reassign"
DECISIONS = (CONTINUE, ABANDON, REPLAN, REASSIGN)


@dataclass(frozen=True)
class ReplanProposal:
    """A prepared replan, on its way from the resolve call to the runner that commits it.

    Posting one answers nothing. The node stays suspended until the runner, at the
    top of a scheduling pass, plans ``entries`` again over the run as it stands
    then and either applies the result or rejects it -- so a proposal that the run
    has overtaken while the caller was validating or asking the user is refused
    whole rather than half applied.

    ``waiter`` and ``basis`` are what it was prepared against: the node's desk
    waiter and the run's change counter. ``plan`` is the plan as it stood at
    preparation; the runner's fresh plan must add and redefine the same nodes.
    ``dispatch_nodes`` and ``backends`` cover only the new and redefined nodes,
    which are all a replan builds. ``charge`` spends the hourly dispatch budget,
    and is called only at commit. ``submission`` is the successor's admission
    record, handed to the tool when the run switches to the successor.
    """

    successor_run_id: str
    from_node: str
    reason: str
    waiter: asyncio.Event
    basis: int
    entries: tuple[DagNodeSpec, ...]
    authored: dict[str, DagNodeSpec]
    plan: CarryPlan
    dispatch_nodes: dict[str, DagNodeSpec]
    backends: dict[str, Any]
    auto_instances: frozenset[str]
    notices: tuple[str, ...]
    task_summary: str
    confirm: bool
    charge: Callable[[], str | None]
    submission: Any = None


@dataclass(frozen=True)
class ReplanOutcome:
    """What the runner did with one replan proposal.

    ``applied`` false means nothing changed; ``still_waiting`` then says whether the
    answered node can still be answered. ``stopped_suspended`` names the suspended
    nodes the replan stopped, whose reports a bound caller must retire.
    """

    applied: bool
    successor_run_id: str | None = None
    carried: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    redefined: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()
    stopped_suspended: tuple[str, ...] = ()
    reason: str | None = None
    still_waiting: bool = False


@dataclass(frozen=True)
class ReassignPlan:
    """A checked move of one node to another agent, on its way to the run.

    Run-local, and never serialized as a tool argument: ``backends`` holds live
    backend objects and ``waiter`` is the adjudication the move answers. The
    runner applies the move only after that answer is recorded, and only for
    the attempt after it -- earlier attempts keep the agent that made them.
    ``nodes`` is every possible mover the move was approved for, including
    siblings that were still running when it was decided.
    """

    node_id: str
    source_agent: str
    source_instance: str | None
    target_agent: str
    target_stateful: bool
    target_instance: str | None
    instance_auto: bool
    revision: int
    waiter: asyncio.Event
    nodes: tuple[DagNodeSpec, ...]
    backends: dict[str, Any]
    notices: tuple[str, ...]


@dataclass(frozen=True)
class Adjudication:
    """What the main agent decided about one suspended node."""

    decision: str
    message: str | None = None
    agent: str | None = None
    reassignment: ReassignPlan | None = None


class AdjudicationDesk:
    """The pending adjudications of one run."""

    def __init__(self) -> None:
        self._waiting: dict[str, asyncio.Event] = {}
        self._answers: dict[str, Adjudication] = {}
        self.continued = asyncio.Event()
        self.proposed = asyncio.Event()
        self._proposal: tuple[ReplanProposal, asyncio.Future] | None = None

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

    def resolve(
        self,
        node_id: str,
        decision: str,
        message: str | None,
        *,
        agent: str | None = None,
        reassignment: ReassignPlan | None = None,
        expected_waiter: asyncio.Event | None = None,
    ) -> bool:
        """Record an answer and wake the waiter. False when nobody was waiting.

        The caller reports that False to the model rather than swallowing it: by
        the time an answer arrives the node may have timed out or the run may
        have been cancelled, and a silently discarded decision looks to the model
        exactly like one that was applied.

        A replan is never recorded here: it is posted with :meth:`propose`, and the
        runner commits it, so ``resolve`` refuses one.

        A continue fires ``continued`` for the same reason and to the opposite
        effect on the round's other nodes: the round ends early so this node can
        be dispatched again, and whatever is still running is handed to the next
        round rather than cancelled. Without it the re-dispatch waits out every
        sibling of the round it was suspended from -- nodes it does not depend on
        and cannot be helped by, for an unbounded time. A reassign fires it too:
        the node goes back to the scheduler, on the prepared target, and never
        replaces the graph.

        ``expected_waiter`` is for a caller that read the waiter before doing
        asynchronous work. It must be the node's current waiter, and that waiter
        must be unanswered: a decision prepared against a closed, replaced or
        already-answered adjudication is refused instead of landing on whatever
        now waits under the same node id.

        A reassign is refused before anything is recorded when it carries no
        target agent or no message, or while a replan proposal is pending -- the
        replan was prepared first and the move would race it. A prepared
        move must also name this node and the waiter it was prepared against, so
        a stale plan cannot be parked under a newer adjudication.
        """
        event = self._waiting.get(node_id)
        if event is None or decision == REPLAN:
            return False
        if expected_waiter is not None and (event is not expected_waiter or node_id in self._answers):
            return False
        if decision == REASSIGN:
            if not (agent and agent.strip()) or not (message and message.strip()):
                return False
            if self._proposal is not None:
                return False
        if reassignment is not None and (reassignment.node_id != node_id or reassignment.waiter is not event):
            return False
        self._answers[node_id] = Adjudication(
            decision=decision,
            message=message,
            agent=agent,
            reassignment=reassignment,
        )
        if decision in (CONTINUE, REASSIGN):
            self.continued.set()
        event.set()
        return True

    @property
    def pending_proposal(self) -> ReplanProposal | None:
        """The replan proposal waiting for the runner, if one is."""
        return self._proposal[0] if self._proposal is not None else None

    def propose(self, proposal: ReplanProposal) -> asyncio.Future | None:
        """Hold a replan proposal for the runner, and wake it. ``None`` when refused.

        Refused, recording nothing, when its node is not open, when the node's
        waiter is not the one the proposal was prepared against, when the node
        already has an answer, or while another proposal of this run is pending.
        Accepted, it wakes a round in flight the way a continue does -- the round
        ends early and every node still running is handed on, not cancelled -- and
        wakes the adjudication wait through ``proposed``. The returned future is
        resolved by the runner, with a :class:`ReplanOutcome`, exactly once.
        """
        event = self._waiting.get(proposal.from_node)
        if event is None or event is not proposal.waiter or proposal.from_node in self._answers:
            return None
        if self._proposal is not None:
            return None
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._proposal = (proposal, future)
        self.proposed.set()
        self.continued.set()
        return future

    def take_proposal(self) -> tuple[ReplanProposal, asyncio.Future] | None:
        """The pending proposal and its future, consumed by the runner that commits it."""
        taken, self._proposal = self._proposal, None
        return taken

    def withdraw(self, proposal: ReplanProposal) -> bool:
        """Take back a proposal the runner has not taken yet. False when it is gone already."""
        if self._proposal is None or self._proposal[0] is not proposal:
            return False
        _, future = self._proposal
        self._proposal = None
        if not future.done():
            future.set_result(
                ReplanOutcome(
                    applied=False, reason="the replan was withdrawn before it was applied", still_waiting=True
                )
            )
        return True

    def reject_pending(self, reason: str, *, still_waiting: bool) -> None:
        """Refuse a pending proposal, resolving its future, when the run can no longer apply it."""
        taken, self._proposal = self._proposal, None
        if taken is not None and not taken[1].done():
            taken[1].set_result(ReplanOutcome(applied=False, reason=reason, still_waiting=still_waiting))

    def peek(self, node_id: str) -> Adjudication | None:
        """The answer that has landed for ``node_id``, without consuming it."""
        return self._answers.get(node_id)

    def take(self, node_id: str) -> Adjudication | None:
        """The answer for ``node_id``, consumed. ``None`` if none landed."""
        self._waiting.pop(node_id, None)
        return self._answers.pop(node_id, None)

    def close(self, node_id: str) -> None:
        """Stop waiting on ``node_id`` without consuming an answer."""
        self._waiting.pop(node_id, None)
        self._answers.pop(node_id, None)
