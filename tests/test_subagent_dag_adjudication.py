"""The adjudication desk (dag_adjudication.py).

The desk carries decisions from the main agent to the run. These tests pin the
rules the tool and the runner rely on: an answer lands once, a move travels with
it, and a decision cannot reach a later adjudication of the same node.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.config.schema import SubagentsConfig


def _failing_once() -> tuple[list[str], list[tuple[str, str]], object]:
    """An announcer that raises on its first call and delivers after that."""
    calls: list[str] = []
    landed: list[tuple[str, str]] = []

    async def _announce(
        node_id: str,
        text: str,
        *,
        awaiting_decision: bool,
        output: str | None = None,
        dispatch_link: tuple[str, str] | None = None,
    ) -> None:
        calls.append(node_id)
        if len(calls) == 1:
            raise RuntimeError("the submit queue was busy")
        landed.append((node_id, text))

    return calls, landed, _announce


from raven.agent.subagent.dag_adjudication import (
    ABANDON,
    CONTINUE,
    DECISIONS,
    REASSIGN,
    REPLAN,
    Adjudication,
    AdjudicationDesk,
    ReassignPlan,
)


def _move(waiter: asyncio.Event, *, node_id: str = "fix") -> ReassignPlan:
    return ReassignPlan(
        node_id=node_id,
        source_agent="alpha",
        source_instance=None,
        target_agent="beta",
        target_stateful=False,
        target_instance=None,
        instance_auto=False,
        revision=1,
        waiter=waiter,
        nodes=(),
        backends={},
        notices=(),
    )


def test_replan_is_a_decision() -> None:
    assert REPLAN == "replan"
    assert DECISIONS == (CONTINUE, ABANDON, REPLAN, REASSIGN)


def test_reassign_is_a_decision() -> None:
    assert REASSIGN == "reassign"


def test_a_reassign_wakes_the_round_without_requesting_a_replan() -> None:
    desk = AdjudicationDesk()
    event = desk.open("fix")
    assert desk.resolve("fix", REASSIGN, "use the parser", agent="beta", expected_waiter=event)
    assert desk.continued.is_set()
    assert not desk.proposed.is_set()
    answer = desk.take("fix")
    assert (answer.decision, answer.agent, answer.message) == (REASSIGN, "beta", "use the parser")


def test_an_answer_cannot_reach_a_later_adjudication_of_the_same_node() -> None:
    desk = AdjudicationDesk()
    old = desk.open("fix")
    desk.close("fix")
    current = desk.open("fix")
    assert not desk.resolve("fix", REASSIGN, "stale", agent="beta", expected_waiter=old)
    assert not current.is_set()
    assert desk.is_open("fix")


def test_an_unprotected_reassign_keeps_the_old_call_shape_working() -> None:
    desk = AdjudicationDesk()
    desk.open("fix")

    assert desk.resolve("fix", REASSIGN, "use the parser", agent="beta")

    assert desk.continued.is_set()
    answer = desk.take("fix")
    assert (answer.decision, answer.agent) == (REASSIGN, "beta")


def test_an_adjudication_keeps_its_existing_positional_fields() -> None:
    answer = Adjudication(CONTINUE, "try again")

    assert answer.decision == CONTINUE
    assert answer.message == "try again"
    assert answer.agent is None
    assert answer.reassignment is None


def test_peek_shows_an_answer_without_consuming_it() -> None:
    desk = AdjudicationDesk()
    desk.open("fix")
    desk.resolve("fix", CONTINUE, "try again")

    answer = desk.peek("fix")
    assert answer is not None and answer.message == "try again"
    assert desk.take("fix") is answer


def test_a_prepared_move_travels_with_the_answer() -> None:
    desk = AdjudicationDesk()
    event = desk.open("fix")
    move = _move(event)

    assert desk.resolve(
        "fix",
        REASSIGN,
        "use the parser",
        agent="beta",
        expected_waiter=event,
        reassignment=move,
    )

    answer = desk.take("fix")
    assert answer.reassignment is move
    assert answer.agent == "beta"


def test_a_second_answer_cannot_replace_the_first_on_the_same_waiter() -> None:
    desk = AdjudicationDesk()
    event = desk.open("fix")
    assert desk.resolve("fix", REASSIGN, "use the parser", agent="beta", expected_waiter=event)

    assert not desk.resolve("fix", REASSIGN, "use the lexer", agent="gamma", expected_waiter=event)

    answer = desk.take("fix")
    assert (answer.agent, answer.message) == ("beta", "use the parser")


@pytest.mark.parametrize("agent", [None, "", "   "])
def test_a_reassign_without_a_target_agent_is_refused(agent: str | None) -> None:
    desk = AdjudicationDesk()
    event = desk.open("fix")

    assert not desk.resolve("fix", REASSIGN, "use the parser", agent=agent, expected_waiter=event)

    assert not event.is_set()
    assert desk.is_open("fix")
    assert desk.take("fix") is None
    assert not desk.continued.is_set()


@pytest.mark.parametrize("message", [None, "", "   "])
def test_a_reassign_without_a_message_is_refused(message: str | None) -> None:
    desk = AdjudicationDesk()
    event = desk.open("fix")

    assert not desk.resolve("fix", REASSIGN, message, agent="beta", expected_waiter=event)

    assert not event.is_set()
    assert desk.is_open("fix")
    assert desk.take("fix") is None
    assert not desk.continued.is_set()


def test_a_prepared_move_for_another_waiter_is_refused() -> None:
    desk = AdjudicationDesk()
    event = desk.open("fix")
    move = _move(asyncio.Event())

    assert not desk.resolve(
        "fix",
        REASSIGN,
        "use the parser",
        agent="beta",
        expected_waiter=event,
        reassignment=move,
    )

    assert not event.is_set()
    assert desk.take("fix") is None


def test_a_stale_prepared_move_is_refused_without_a_waiter_claim() -> None:
    desk = AdjudicationDesk()
    desk.open("fix")
    move = _move(asyncio.Event())

    assert not desk.resolve("fix", REASSIGN, "use the parser", agent="beta", reassignment=move)

    assert desk.is_open("fix")
    assert desk.take("fix") is None


def test_a_prepared_move_for_another_node_is_refused() -> None:
    desk = AdjudicationDesk()
    event = desk.open("fix")
    move = _move(event, node_id="other")

    assert not desk.resolve(
        "fix",
        REASSIGN,
        "use the parser",
        agent="beta",
        expected_waiter=event,
        reassignment=move,
    )

    assert not event.is_set()
    assert desk.take("fix") is None


def test_a_replan_is_never_answered_through_resolve() -> None:
    """A replan is posted as a proposal and committed by the runner, so an answer
    naming one records nothing, wakes nothing, and leaves the node waiting."""
    desk = AdjudicationDesk()
    event = desk.open("a")

    assert desk.resolve("a", REPLAN, "the plan was wrong") is False

    assert desk.is_open("a") and not event.is_set()
    assert desk.peek("a") is None
    assert not desk.continued.is_set() and not desk.proposed.is_set()


def test_resolving_a_continue_sets_the_continued_event() -> None:
    desk = AdjudicationDesk()
    desk.open("a")
    assert not desk.continued.is_set()

    assert desk.resolve("a", CONTINUE, "try again") is True

    assert desk.continued.is_set()


def test_a_continue_nobody_waits_for_sets_nothing() -> None:
    desk = AdjudicationDesk()

    assert desk.resolve("gone", CONTINUE, "too late") is False

    assert not desk.continued.is_set(), "an unrecorded answer must not end the round"


def test_resolving_an_abandon_sets_neither_event() -> None:
    """Abandon is deliberately outside both escapes.

    It shares the continue's wait for the round to drain, but nothing is owed the
    graph by shortening it: an abandoned node's dependents cascade to `skipped`
    and none of them could have been dispatched sooner.
    """
    desk = AdjudicationDesk()
    desk.open("a")

    assert desk.resolve("a", ABANDON, None) is True

    assert not desk.continued.is_set()
    assert not desk.proposed.is_set()


def test_a_long_run_notice_is_on_by_default_and_has_a_switch() -> None:
    """The notice interval is a host-facing switch: an hour by default, and 0
    disables the watcher. The camelCase key is what an operator's config carries."""
    assert SubagentsConfig().long_run_notice_seconds == 3600
    assert SubagentsConfig.model_validate({"longRunNoticeSeconds": 0}).long_run_notice_seconds == 0


def _empty_carry_plan():
    from raven.agent.subagent.dag_carry import CarryPlan

    return CarryPlan(new=(), redefined=(), carried=(), dropped=(), context=(), nodes=())


def _proposal(desk, node: str = "a", **overrides):
    from raven.agent.subagent.dag_adjudication import ReplanProposal

    fields = {
        "successor_run_id": "run-next",
        "from_node": node,
        "reason": "the plan was wrong",
        "waiter": desk.waiter(node),
        "basis": 0,
        "entries": (),
        "authored": {},
        "plan": _empty_carry_plan(),
        "dispatch_nodes": {},
        "backends": {},
        "auto_instances": frozenset(),
        "notices": (),
        "task_summary": "graph under test",
        "confirm": False,
        "charge": lambda: None,
    }
    fields.update(overrides)
    return ReplanProposal(**fields)


async def test_a_proposal_lands_only_on_the_open_unanswered_waiter_it_was_prepared_against() -> None:
    from raven.agent.subagent.dag_adjudication import CONTINUE, AdjudicationDesk

    desk = AdjudicationDesk()
    desk.open("a")
    stale = _proposal(desk, waiter=asyncio.Event())
    assert desk.propose(stale) is None, "a proposal prepared against another waiter must not land"

    desk.open("b")
    desk.resolve("b", CONTINUE, "go on")
    assert desk.propose(_proposal(desk, "b")) is None, "an answered node takes no proposal"

    closed = _proposal(desk, "c")
    desk.close("c")
    assert desk.propose(closed) is None, "a closed node takes no proposal"

    assert desk.pending_proposal is None
    assert not desk.proposed.is_set()


async def test_one_replan_proposal_per_run_at_a_time() -> None:
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk

    desk = AdjudicationDesk()
    desk.open("a")
    desk.open("b")
    first = _proposal(desk, "a")
    assert desk.propose(first) is not None
    assert desk.propose(_proposal(desk, "b")) is None
    assert desk.pending_proposal is first


async def test_proposing_wakes_the_scheduler_without_answering_anything() -> None:
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk

    desk = AdjudicationDesk()
    desk.open("a")
    assert desk.propose(_proposal(desk)) is not None

    assert desk.continued.is_set(), "a round in flight must end early and hand its running nodes on"
    assert desk.proposed.is_set(), "the adjudication wait must return"
    assert desk.peek("a") is None, "posting a proposal answers nothing"
    assert desk.is_open("a")


async def test_take_proposal_hands_the_runner_the_proposal_and_its_future() -> None:
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk

    desk = AdjudicationDesk()
    desk.open("a")
    proposal = _proposal(desk)
    future = desk.propose(proposal)

    taken = desk.take_proposal()
    assert taken is not None
    assert taken[0] is proposal
    assert taken[1] is future
    assert desk.pending_proposal is None
    assert desk.take_proposal() is None


async def test_a_withdrawn_proposal_resolves_its_future_as_rejected() -> None:
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk

    desk = AdjudicationDesk()
    desk.open("a")
    proposal = _proposal(desk)
    future = desk.propose(proposal)

    assert desk.withdraw(proposal) is True
    outcome = future.result()
    assert outcome.applied is False
    assert outcome.still_waiting is True
    assert desk.pending_proposal is None
    assert desk.withdraw(proposal) is False


async def test_reject_pending_resolves_the_future_once() -> None:
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk

    desk = AdjudicationDesk()
    desk.open("a")
    future = desk.propose(_proposal(desk))

    desk.reject_pending("the run ended before the replan was applied", still_waiting=False)
    desk.reject_pending("again", still_waiting=False)

    outcome = future.result()
    assert outcome.applied is False
    assert outcome.reason == "the run ended before the replan was applied"
    assert outcome.still_waiting is False
    assert desk.pending_proposal is None


async def test_a_reassign_is_refused_while_a_replan_proposal_is_pending() -> None:
    from raven.agent.subagent.dag_adjudication import REASSIGN, AdjudicationDesk

    desk = AdjudicationDesk()
    desk.open("plan")
    event = desk.open("fix")
    assert desk.propose(_proposal(desk, "plan")) is not None

    assert not desk.resolve("fix", REASSIGN, "use the parser", agent="beta", expected_waiter=event)
    assert desk.is_open("fix")
    assert desk.peek("fix") is None
