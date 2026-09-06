"""The outbox: a foreground DAG run's tray of reports and its result (dag_adjudication.py).

The desk carries decisions from the main agent to the run; the outbox carries
reports from the run to the main agent. These tests pin the rules the tool and
the runner rely on: a taker is registered before take() yields, reports are
handed one per take, a handed report stays owed until it is answered, a Final
sticks, and release/stop decide what is announced.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.agent.subagent.dag_adjudication import Final, Outbox, Report, Stopped


class _Announced:
    def __init__(self) -> None:
        self.reports: list[tuple[str, str]] = []
        self.verdicts: list[bool] = []
        self.finals: list[object] = []

    async def report(self, node_id: str, text: str, *, awaiting_decision: bool) -> None:
        self.reports.append((node_id, text))
        self.verdicts.append(awaiting_decision)

    async def final(self, result: object) -> None:
        self.finals.append(result)


def _outbox() -> tuple[Outbox, _Announced]:
    announced = _Announced()
    return Outbox(conversation="c1", announce_report=announced.report, announce_final=announced.final), announced


async def test_reports_buffer_in_order_and_are_handed_one_per_take() -> None:
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.put_report("b", "report b", awaiting_decision=True)

    assert await box.take() == Report("a", "report a")
    assert await box.take() == Report("b", "report b")
    assert announced.reports == [], "a bound outbox never announces"


async def test_a_report_put_while_a_taker_waits_goes_to_that_taker() -> None:
    box, _ = _outbox()
    taker = asyncio.create_task(box.take())
    await asyncio.sleep(0)

    await box.put_report("a", "report a", awaiting_decision=True)

    assert await taker == Report("a", "report a")


async def test_take_registers_its_taker_before_it_yields() -> None:
    """The hinge the whole handoff rests on: a caller resolves a node and takes the next
    event in one tick, so the run must not be able to produce into an empty tray."""
    box, _ = _outbox()
    coro = box.take()
    coro.send(None)
    assert len(box._takers) == 1
    await box.put_report("a", "x", awaiting_decision=True)
    with pytest.raises(StopIteration) as si:
        coro.send(None)
    assert si.value.value == Report("a", "x")


async def test_a_final_reaches_every_pending_taker_and_sticks() -> None:
    box, _ = _outbox()
    first = asyncio.create_task(box.take())
    second = asyncio.create_task(box.take())
    await asyncio.sleep(0)

    await box.put_final("summary")

    assert await first == Final("summary", False)
    assert await second == Final("summary", False)
    assert await box.take() == Final("summary", False), "a later take gets the final again"


async def test_release_with_flush_re_sends_the_buffer_and_switches_to_announcing() -> None:
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.put_report("b", "report b", awaiting_decision=True)
    await box.put_final("summary")

    await box.release(flush=True)

    assert not box.bound
    assert box.released.is_set()
    assert announced.reports == [("a", "report a"), ("b", "report b")]
    assert announced.finals == ["summary"]

    await box.put_report("c", "report c", awaiting_decision=True)
    assert announced.reports[-1] == ("c", "report c"), "a released outbox announces directly"


async def test_release_re_sends_a_report_that_was_taken_and_never_answered() -> None:
    """Taking a report is not deciding it.

    The node stays open on the desk, so the tray still owes this report a
    re-send: after the turn ends nothing else will raise that node again.
    """
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)

    assert isinstance(await box.take(), Report)
    await box.release(flush=True)

    assert announced.reports == [("a", "report a")]


async def test_a_handed_report_is_re_sent_to_a_taker_that_asks_again() -> None:
    """The debt this tray records is owed to whoever asks next, not only to release.

    While the node it names is open, nothing else can reach the agent: the runner
    waits for every open node and waits only once nothing else can run, so a taker
    that parked here would wait for a report that cannot be produced and a result
    that cannot arrive.
    """
    box, _ = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)
    first = await box.take()

    again = await asyncio.wait_for(box.take(), timeout=1)

    assert again == first, f"the tray still owes {first!r}, so that is what the next taker gets, got {again!r}"


async def test_an_unseen_report_outranks_a_re_send() -> None:
    """Queue first, then the re-send: what the agent has never seen goes ahead of a repeat.

    A guard on the order, not on the re-send itself: removing the re-send leaves this
    green, because the queue answers the same take either way. Moving the re-send ahead
    of the queue turns it red -- along with two older cases that read the same order
    from the other side, so what this adds is the rule under its own name.
    """
    box, _ = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.take()
    await box.put_report("b", "report b", awaiting_decision=True)

    nxt = await asyncio.wait_for(box.take(), timeout=1)

    assert isinstance(nxt, Report) and nxt.node_id == "b", f"expected the queued report, got {nxt!r}"


async def test_answered_retires_a_handed_report_so_release_leaves_it_alone() -> None:
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.take()

    box.answered("a")
    await box.release(flush=True)

    assert announced.reports == [], "a decided node must not be asked about twice"


async def test_answered_retires_only_the_node_it_names() -> None:
    """Two owed reports, one decision. The debt is per node, so the other survives."""
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.put_report("b", "report b", awaiting_decision=True)
    await box.take()
    await box.take()

    box.answered("a")
    await box.release(flush=True)

    assert announced.reports == [("b", "report b")]


async def test_a_node_that_suspends_again_after_its_answer_is_owed_again() -> None:
    """One continuation is not a settlement for the next.

    A continued node that falls short again produces a fresh report, and retiring
    the first attempt is not an answer to the second.
    """
    box, announced = _outbox()
    await box.put_report("a", "attempt 1", awaiting_decision=True)
    await box.take()
    box.answered("a")
    await box.put_report("a", "attempt 2", awaiting_decision=True)
    await box.take()

    await box.release(flush=True)

    assert announced.reports == [("a", "attempt 2")]


async def test_release_re_sends_the_owed_reports_before_the_buffered_ones() -> None:
    """Hand order, then queue order: the turn saw the taken ones first."""
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.put_report("b", "report b", awaiting_decision=True)
    await box.put_report("c", "report c", awaiting_decision=True)
    await box.take()
    await box.take()

    await box.release(flush=True)

    assert [node for node, _ in announced.reports] == ["a", "b", "c"]


async def test_answered_retires_a_buffered_report_as_well_as_a_handed_one() -> None:
    """A decision retires the node's report wherever it is sitting.

    The agent can decide a node it has not been handed -- `dag_status` lists them,
    and a report names the nodes blocked behind it. Retiring only the handed tray
    leaves that node's own report queued, so the next take returns the question it
    just answered.
    """
    box, _ = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.put_report("b", "report b", awaiting_decision=True)
    await box.put_report("c", "report c", awaiting_decision=True)
    handed = await box.take()
    assert isinstance(handed, Report) and handed.node_id == "a"

    box.answered("b")

    nxt = await box.take()
    assert isinstance(nxt, Report) and nxt.node_id == "c", (
        f"the queue should skip the decided node and offer the next real question, got {nxt!r}"
    )


async def test_a_landed_result_retires_the_handed_tray_only() -> None:
    """The asymmetry, pinned so its reason is enforced rather than merely written.

    A handed report was already put in front of the agent, so once the result has
    landed, re-sending it asks again for a decision that cannot change anything. A
    buffered one was never seen: that a node asked for something and nobody answered
    is not in the summary, and the run ending does not make it not worth knowing.
    """
    box, announced = _outbox()
    await box.put_report("handed_q", "h", awaiting_decision=True)
    await box.take()
    await box.put_report("buffered_q", "b", awaiting_decision=True)
    await box.put_final({"ok": True})

    await box.release(flush=True)

    assert [node for node, _ in announced.reports] == ["buffered_q"], (
        "the seen one is retired by the result; the unseen one is still news"
    )
    assert announced.finals == [{"ok": True}]


async def test_a_result_retires_the_handed_reports_it_arrived_past() -> None:
    """A run that has produced its result is past deciding, so release announces
    the result rather than re-opening questions the result already settles."""
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.take()
    await box.put_final("summary")

    await box.release(flush=True)

    assert announced.reports == []
    assert announced.finals == ["summary"]


def _failing_once() -> tuple[list[str], list[tuple[str, str]], object]:
    """An announcer that raises on its first call and delivers after that."""
    calls: list[str] = []
    landed: list[tuple[str, str]] = []

    async def _announce(node_id: str, text: str, *, awaiting_decision: bool) -> None:
        calls.append(node_id)
        if len(calls) == 1:
            raise RuntimeError("the submit queue was busy")
        landed.append((node_id, text))

    return calls, landed, _announce


@pytest.mark.parametrize("tray", ["handed", "buffered"])
async def test_the_release_drain_retries_a_transient_failure(monkeypatch, tray) -> None:
    """Release is a delivery path too, and it owes the same guarantee as the runner's.

    This drain is what makes good on the promise the returned report carries, and
    it starts the adjudication deadline -- so a report dropped here can only
    expire unseen, with the agent blamed for not answering. One raise used to be
    enough to drop it: the event is popped from its tray before the announce, and
    nothing re-drains afterwards.
    """
    from raven.agent.subagent import dag_adjudication as mod

    monkeypatch.setattr(mod, "REPORT_DELIVERY_BACKOFF_S", 0.0)
    calls, landed, announce = _failing_once()

    async def _final(_result: object) -> None: ...

    box = Outbox(conversation="c1", announce_report=announce, announce_final=_final)
    await box.put_report("a", "report a", awaiting_decision=True)
    if tray == "handed":
        await box.take()

    await box.release(flush=True)

    assert calls == ["a", "a"], f"the drain has to try again, got {calls}"
    assert landed == [("a", "report a")], "and the retry is what makes good on the promised re-send"


async def test_the_release_drain_is_bounded_and_still_moves_on(monkeypatch) -> None:
    """Bounded for the same reason the runner's is, and one dead event must not strand the rest."""
    from raven.agent.subagent import dag_adjudication as mod

    monkeypatch.setattr(mod, "REPORT_DELIVERY_BACKOFF_S", 0.0)
    calls: list[str] = []

    async def _announce(node_id: str, _text: str, *, awaiting_decision: bool) -> None:
        calls.append(node_id)
        if node_id == "a":
            raise RuntimeError("delivery channel is down")

    async def _final(_result: object) -> None: ...

    box = Outbox(conversation="c1", announce_report=_announce, announce_final=_final)
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.put_report("b", "report b", awaiting_decision=True)

    await box.release(flush=True)

    assert calls.count("a") == mod.REPORT_DELIVERY_ATTEMPTS, f"every attempt and no more: {calls}"
    assert calls.count("b") == 1, f"the next event is still delivered, and only once: {calls}"


async def test_a_post_release_final_does_not_escape_its_task(monkeypatch) -> None:
    """A released run's result is delivered, not raised.

    `put_final` is awaited from `_run_detached` with nothing around it, and the
    task's done callbacks only drop the run's indexes -- they never retrieve
    `task.exception()`. So an announcer that raises here loses the result and
    leaves an unretrieved-exception warning behind, where the background lane
    catches the same failure and logs it.
    """
    from raven.agent.subagent import dag_adjudication as mod

    monkeypatch.setattr(mod, "REPORT_DELIVERY_BACKOFF_S", 0.0)
    calls: list[object] = []

    async def _report(_node_id: str, _text: str, *, awaiting_decision: bool) -> None: ...

    async def _final(result: object) -> None:
        calls.append(result)
        raise RuntimeError("the submit queue was busy")

    box = Outbox(conversation="c1", announce_report=_report, announce_final=_final)
    await box.release(flush=True)

    await box.put_final("summary")

    assert len(calls) == mod.REPORT_DELIVERY_ATTEMPTS, f"every attempt and no more: {len(calls)}"


async def test_a_post_release_final_survives_a_transient(monkeypatch) -> None:
    """The same guarantee the drain's buffered final already gets."""
    from raven.agent.subagent import dag_adjudication as mod

    monkeypatch.setattr(mod, "REPORT_DELIVERY_BACKOFF_S", 0.0)
    calls: list[object] = []
    landed: list[object] = []

    async def _report(_node_id: str, _text: str, *, awaiting_decision: bool) -> None: ...

    async def _final(result: object) -> None:
        calls.append(result)
        if len(calls) == 1:
            raise RuntimeError("the submit queue was busy")
        landed.append(result)

    box = Outbox(conversation="c1", announce_report=_report, announce_final=_final)
    await box.release(flush=True)

    await box.put_final("summary")

    assert landed == ["summary"], f"the retry is what delivers the run's result, got {landed}"


async def test_a_stopped_post_release_final_is_still_never_announced(monkeypatch) -> None:
    """Retrying must not turn a cancelled run into an announcement it never made.

    A guard on the risk this change introduces rather than on the change itself:
    routing through the shared step is only correct while that step honours
    `stopped`, so it goes red when the step stops honouring it and stays green when
    the routing is reverted.
    """
    from raven.agent.subagent import dag_adjudication as mod

    monkeypatch.setattr(mod, "REPORT_DELIVERY_BACKOFF_S", 0.0)
    calls: list[object] = []

    async def _report(_node_id: str, _text: str, *, awaiting_decision: bool) -> None: ...

    async def _final(result: object) -> None:
        calls.append(result)

    box = Outbox(conversation="c1", announce_report=_report, announce_final=_final)
    await box.release(flush=True)

    await box.put_final("summary", stopped=True)

    assert calls == [], "a stopped run stays silent, retry or not"


async def test_release_without_flush_drops_a_handed_report_too() -> None:
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.take()

    await box.release(flush=False)

    assert announced.reports == []


async def test_a_released_outbox_announces_a_notification_as_one() -> None:
    """Released, both kinds announce, and the verdict travels with them.

    There is no blocking call left for a summary to reach, so a notification dropped
    here reaches nobody until the graph finishes -- and the announcer needs the
    verdict, because a notification framed as a question asks for a decision the
    node is already past.
    """
    box, announced = _outbox()
    await box.release(flush=True)

    await box.put_report("a", "terminal report", awaiting_decision=False)

    assert announced.reports == [("a", "terminal report")]
    assert announced.verdicts == [False]


async def test_a_bound_outbox_drops_a_notification_and_keeps_a_question() -> None:
    """Bound, the waiting call is handed the run's summary, so announcing the same
    news again would put a question nobody can answer beside it."""
    box, announced = _outbox()

    await box.put_report("a", "terminal report", awaiting_decision=False)
    await box.put_report("b", "still waiting", awaiting_decision=True)

    assert announced.reports == [], "a bound outbox announces nothing"
    event = await box.take()
    assert isinstance(event, Report) and event.node_id == "b", "the question is what a taker gets"
    await box.release(flush=True)
    assert [node for node, _ in announced.reports] == ["b"], "the dropped notification is not owed"


async def test_the_replay_of_a_buffered_report_states_that_it_is_a_question() -> None:
    """Only questions are ever buffered, so the replay is entitled to say so."""
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)

    await box.release(flush=True)

    assert announced.verdicts == [True]


async def test_release_without_flush_drops_the_buffer_but_later_events_still_announce() -> None:
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)

    await box.release(flush=False)

    assert announced.reports == []
    await box.put_final("summary")
    assert announced.finals == ["summary"]


async def test_release_survives_an_announcer_that_raises() -> None:
    """One undeliverable event must not strand the rest: release drains the buffer
    item by item and guards each announce, so a raise on the first event still
    lets the later ones through."""
    announced = _Announced()

    async def _raising_report(node_id: str, text: str, *, awaiting_decision: bool) -> None:
        if node_id == "a":
            raise RuntimeError("boom")
        await announced.report(node_id, text, awaiting_decision=awaiting_decision)

    box = Outbox(conversation="c1", announce_report=_raising_report, announce_final=announced.final)
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.put_report("b", "report b", awaiting_decision=True)
    await box.put_final("summary")

    await box.release(flush=True)

    assert announced.reports == [("b", "report b")], "the report behind the raise still reaches its announcer"
    assert announced.finals == ["summary"], "the final behind the raise still reaches its announcer"


async def test_stop_from_inside_an_announcer_halts_the_remaining_announces() -> None:
    """A stop() landing mid-flush (a shutdown racing the flush, say) must not let
    the drain push out further events that are no longer worth announcing."""
    announced = _Announced()

    async def _stopping_report(node_id: str, text: str, *, awaiting_decision: bool) -> None:
        await announced.report(node_id, text, awaiting_decision=awaiting_decision)
        box.stop()

    box = Outbox(conversation="c1", announce_report=_stopping_report, announce_final=announced.final)
    await box.put_report("a", "report a", awaiting_decision=True)
    await box.put_report("b", "report b", awaiting_decision=True)

    await box.release(flush=True)

    assert announced.reports == [("a", "report a")], "stop() mid-flush halts the remaining announces"


async def test_a_stopped_final_is_never_announced() -> None:
    box, announced = _outbox()
    await box.put_final("cancelled summary", stopped=True)
    await box.release(flush=True)
    assert announced.finals == []

    released, announced_late = _outbox()
    await released.release(flush=True)
    await released.put_final("cancelled summary", stopped=True)
    assert announced_late.finals == []


async def test_stop_wakes_a_parked_taker() -> None:
    box, _ = _outbox()
    waiting = asyncio.create_task(box.take())
    await asyncio.sleep(0)

    box.stop()

    assert await waiting == Stopped()
    assert not box.bound


async def test_stop_drops_the_buffer_and_silences_release() -> None:
    box, announced = _outbox()
    await box.put_report("a", "report a", awaiting_decision=True)

    box.stop()

    assert await box.take() == Stopped(), "the buffer is dropped, not handed over"
    await box.release(flush=True)
    assert announced.reports == [], "release after stop announces nothing"


async def test_a_cancelled_taker_is_forgotten() -> None:
    box, _ = _outbox()
    taker = asyncio.create_task(box.take())
    await asyncio.sleep(0)
    taker.cancel()
    await asyncio.gather(taker, return_exceptions=True)

    await box.put_report("a", "report a", awaiting_decision=True)
    assert await box.take() == Report("a", "report a"), "the report was buffered, not lost on a dead taker"


from raven.agent.subagent.dag_adjudication import (
    ABANDON,
    CONTINUE,
    DECISIONS,
    REPLAN,
    AdjudicationDesk,
    ReplanPlan,
)


def _plan(run_id: str = "run-new", from_node: str = "a") -> ReplanPlan:
    return ReplanPlan(
        run_id=run_id,
        from_node=from_node,
        reason="the plan was wrong",
        nodes=(),
        backends={},
        auto_instances=frozenset(),
        notices=(),
    )


def test_replan_is_a_decision() -> None:
    assert REPLAN == "replan"
    assert DECISIONS == (CONTINUE, ABANDON, REPLAN)


def test_resolving_a_replan_sets_the_replanned_event() -> None:
    desk = AdjudicationDesk()
    desk.open("a")
    assert not desk.replanned.is_set()

    assert desk.resolve("a", REPLAN, "the plan was wrong", plan=_plan()) is True

    assert desk.replanned.is_set()
    assert desk.take_plan() == _plan()


def test_resolving_a_continue_leaves_the_replanned_event_alone() -> None:
    desk = AdjudicationDesk()
    desk.open("a")

    assert desk.resolve("a", CONTINUE, "try again") is True

    assert not desk.replanned.is_set()
    assert desk.take_plan() is None


def test_a_replan_nobody_waits_for_sets_nothing() -> None:
    desk = AdjudicationDesk()

    assert desk.resolve("gone", REPLAN, "too late", plan=_plan()) is False

    assert not desk.replanned.is_set(), "an unrecorded answer must not interrupt the run"
    assert desk.take_plan() is None


def test_take_plan_consumes() -> None:
    desk = AdjudicationDesk()
    desk.open("a")
    desk.resolve("a", REPLAN, "the plan was wrong", plan=_plan())

    assert desk.take_plan() is not None
    assert desk.take_plan() is None


def test_the_replanned_answer_is_still_taken_per_node() -> None:
    desk = AdjudicationDesk()
    desk.open("a")
    desk.resolve("a", REPLAN, "the plan was wrong", plan=_plan())

    answer = desk.take("a")
    assert answer is not None
    assert answer.decision == REPLAN
    assert answer.plan is not None
