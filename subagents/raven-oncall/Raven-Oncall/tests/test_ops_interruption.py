"""The interruption contract: enforced, and its breaches counted.

These tests pin down two things that pull in opposite directions. The guarantee
must hold whatever the model does -- a refused ask never reaches the person. And
the refusal must still be visible, because "the loop tried to wake someone over
four minutes of machine time" is a real fact about its judgement that would
otherwise be absorbed silently.
"""

from __future__ import annotations

from raven.ops.interruption import ContractGuard, InterruptionContract
from raven.ops.scripted_human import HumanReply, ScriptedHuman
from raven.ops.simclock import SimClock

_MIN = 60_000
_HOUR = 60 * _MIN


def _human():
    clock = SimClock(speed_factor=0)
    return clock, ScriptedHuman(clock, default=HumanReply(after_ms=1_000, answer="ok"))


def test_an_interruption_over_the_bar_reaches_the_person():
    clock, human = _human()
    guard = ContractGuard(InterruptionContract(min_expected_loss_ms=2 * _HOUR))

    decision = guard.ask(human, "train-a", "diverged", at_ms=0, expected_loss_ms=6 * _HOUR)

    assert decision.allowed
    assert human.ask_count() == 1


def test_an_interruption_under_the_bar_never_reaches_the_person():
    clock, human = _human()
    guard = ContractGuard(InterruptionContract(min_expected_loss_ms=2 * _HOUR))

    decision = guard.ask(human, "train-a", "looks odd", at_ms=0, expected_loss_ms=4 * _MIN)

    assert decision.allowed is False
    assert human.ask_count() == 0, "the guarantee holds regardless of what the loop decided"
    assert "under the" in (decision.reason or "")


def test_a_refused_interruption_is_still_counted_as_a_breach_attempt():
    _, human = _human()
    guard = ContractGuard(InterruptionContract(min_expected_loss_ms=2 * _HOUR))
    guard.ask(human, "a", "?", at_ms=0, expected_loss_ms=1 * _MIN)
    guard.ask(human, "b", "?", at_ms=5_000, expected_loss_ms=3 * _HOUR)

    summary = guard.summary()
    assert summary == {
        "allowed": 1,
        "breach_attempts": 1,
        "unestimated": 0,
        "reasons": ["expected loss 1min is under the 120min bar in force at 09:00"],
    }


def test_quiet_hours_can_raise_the_bar_without_forbidding_outright():
    _, human = _human()
    contract = InterruptionContract(
        min_expected_loss_ms=30 * _MIN,
        quiet_hours=(22, 7),
        quiet_min_expected_loss_ms=6 * _HOUR,
    )
    guard = ContractGuard(contract, start_hour=21)

    daytime = guard.ask(human, "a", "?", at_ms=0, expected_loss_ms=1 * _HOUR)
    at_3am = guard.ask(human, "b", "?", at_ms=6 * _HOUR, expected_loss_ms=1 * _HOUR)
    worth_it_at_3am = guard.ask(human, "c", "?", at_ms=6 * _HOUR, expected_loss_ms=8 * _HOUR)

    assert daytime.allowed, "one hour clears the 30 minute daytime bar"
    assert at_3am.allowed is False, "the same hour does not clear the six hour night bar"
    assert worth_it_at_3am.allowed


def test_quiet_hours_wrap_midnight():
    guard = ContractGuard(
        InterruptionContract(quiet_hours=(22, 7), quiet_min_expected_loss_ms=None),
        start_hour=20,
    )
    assert guard.hour_at(0) == 20
    assert guard.hour_at(3 * _HOUR) == 23
    assert guard.hour_at(6 * _HOUR) == 2

    contract = InterruptionContract(quiet_hours=(22, 7), quiet_min_expected_loss_ms=None)
    assert contract.threshold_at_hour(20) == 0, "before quiet hours"
    assert contract.threshold_at_hour(23) is None, "after midnight boundary, still quiet"
    assert contract.threshold_at_hour(2) is None, "past midnight"
    assert contract.threshold_at_hour(8) == 0, "morning again"


def test_forbidden_hours_deny_however_costly_the_problem_is():
    _, human = _human()
    guard = ContractGuard(
        InterruptionContract(quiet_hours=(22, 7), quiet_min_expected_loss_ms=None),
        start_hour=23,
    )
    decision = guard.ask(human, "a", "?", at_ms=0, expected_loss_ms=40 * _HOUR)

    assert decision.allowed is False
    assert "forbids" in (decision.reason or "")
    assert human.ask_count() == 0


def test_an_unestimated_ask_is_allowed_and_counted_apart():
    _, human = _human()
    guard = ContractGuard(InterruptionContract(min_expected_loss_ms=6 * _HOUR))

    decision = guard.ask(human, "a", "something I do not recognise", at_ms=0, expected_loss_ms=None)

    assert decision.allowed, (
        "forbidding 'I cannot tell how bad this is' would silence the loop "
        "exactly when a person most wants to hear from it"
    )
    assert decision.estimated is False
    assert guard.summary()["unestimated"] == 1
    assert guard.summary()["breach_attempts"] == 0


def test_the_ask_budget_is_spent_only_by_asks_that_got_through():
    _, human = _human()
    guard = ContractGuard(InterruptionContract(min_expected_loss_ms=1 * _HOUR, max_asks=2))

    guard.ask(human, "a", "?", at_ms=0, expected_loss_ms=1 * _MIN)
    guard.ask(human, "b", "?", at_ms=1_000, expected_loss_ms=2 * _HOUR)
    guard.ask(human, "c", "?", at_ms=2_000, expected_loss_ms=2 * _HOUR)
    exhausted = guard.ask(human, "d", "?", at_ms=3_000, expected_loss_ms=9 * _HOUR)

    assert guard.allowed_asks() == 2
    assert exhausted.allowed is False
    assert "used" in (exhausted.reason or "")
    assert human.ask_count() == 2


def test_the_contract_is_expressible_as_an_instruction_for_the_prompt():
    contract = InterruptionContract(
        min_expected_loss_ms=2 * _HOUR,
        quiet_hours=(22, 7),
        quiet_min_expected_loss_ms=6 * _HOUR,
        max_asks=3,
    )
    text = contract.as_instruction()

    assert "120 minutes" in text
    assert "22:00" in text and "07:00" in text
    assert "360 minutes" in text
    assert "at most 3 times" in text
    assert "cannot estimate" in text, "enforcement alone would let the loop burn turns on refused asks"


def test_a_permissive_contract_says_so_rather_than_stating_a_bar():
    text = InterruptionContract().as_instruction()
    assert "when you judge it necessary" in text
    assert "minutes of machine time" not in text
