"""Claims about actions, checked against the actions that happened.

The case these exist for: a loop read "you are right, cancel it", wrote "the
owner confirmed the issue and authorized cancellation" into its conclusion, and
never called cancel. Judged on its text it passed.

Two layers, and the split matters. A structured claim the log does not back is
refused, because accepting it hands a misleading report to whoever acts next. A
prose assertion the log does not back is only flagged, because prose is for
people and a heuristic that refuses reports over phrasing would be measuring
itself.
"""

from __future__ import annotations

from raven.ops.action_log import (
    CANCEL,
    ESCALATE,
    ActionLog,
    Claim,
    action_for_verb,
    prose_findings,
)
from raven.ops.handoff import MockOrchestrator, Report


def _log(*calls) -> ActionLog:
    log = ActionLog()
    for kind, target in calls:
        log.record(kind, target, at_ms=1_000)
    return log


def _report(**over) -> Report:
    base = dict(
        campaign="watch",
        subject="train-c",
        kind="finished",
        at_ms=40_000,
        dedupe_key="train-c:diverged",
        observed={"loss": 6.45},
    )
    base.update(over)
    return Report(**base)


def test_the_log_is_written_by_the_tool_layer_and_records_what_happened():
    log = _log((CANCEL, "train-c"))
    assert log.has(CANCEL, "train-c")
    assert not log.has(CANCEL, "train-d")
    assert not log.has(ESCALATE, "train-c")


def test_a_failed_call_does_not_back_a_claim():
    log = ActionLog()
    log.record(CANCEL, "train-c", at_ms=1, outcome="error: not permitted")
    ok, why = log.verify(Claim("cancelled", "train-c"))
    assert not ok
    assert "no cancel of it was called" in why


def test_verbs_map_to_actions_and_unknown_ones_are_reported_not_guessed():
    assert action_for_verb("Killed") == CANCEL
    assert action_for_verb("contacted") == ESCALATE
    assert action_for_verb("rebooted") is None

    ok, why = ActionLog().verify(Claim("rebooted", "train-c"))
    assert not ok
    assert "rebooted" in why, "the refusal must name the verb -- the likely cause is a missing row"


def test_a_report_claiming_a_cancel_that_never_happened_is_refused():
    orch = MockOrchestrator(ActionLog())
    receipt = orch.receive(_report(claims=[Claim("cancelled", "train-c")]))

    assert receipt.accepted is False
    assert "unsubstantiated claim" in (receipt.reason or "")
    assert orch.unsubstantiated_claims() == 1


def test_the_same_report_is_accepted_once_the_cancel_actually_happened():
    orch = MockOrchestrator(_log((CANCEL, "train-c")))
    assert orch.receive(_report(claims=[Claim("cancelled", "train-c")])).accepted


def test_claiming_the_right_action_on_the_wrong_target_is_refused():
    orch = MockOrchestrator(_log((CANCEL, "train-d")))
    receipt = orch.receive(_report(claims=[Claim("cancelled", "train-c")]))
    assert receipt.accepted is False


def test_a_report_that_claims_nothing_is_unaffected():
    orch = MockOrchestrator(ActionLog())
    assert orch.receive(_report()).accepted, "verification must not become a tax on reports that make no claims"


def test_with_no_log_attached_claims_are_not_checked_at_all():
    orch = MockOrchestrator()
    assert orch.receive(_report(claims=[Claim("cancelled", "train-c")])).accepted, (
        "the check is opt-in, so existing callers keep working"
    )


def test_prose_asserting_an_action_that_never_happened_is_flagged_not_refused():
    orch = MockOrchestrator(ActionLog())
    receipt = orch.receive(_report(narrative="I contacted the owner who authorized it, and I cancelled train-c."))

    assert receipt.accepted, "prose must never be grounds for refusal"
    ((key, verbs),) = orch.prose_flags()
    assert key == "train-c:diverged"
    assert sorted(verbs) == ["cancelled", "contacted"]
    assert orch.summary()["prose_flagged"] == 1


def test_prose_matching_what_actually_happened_is_not_flagged():
    orch = MockOrchestrator(_log((CANCEL, "train-c"), (ESCALATE, "train-c")))
    orch.receive(_report(narrative="I contacted the owner and then I cancelled train-c."))
    assert orch.prose_flags() == []


def test_an_instruction_or_a_quote_is_not_read_as_a_claim_of_having_acted():
    log = ActionLog()
    for text in (
        "I was told to cancel it if it diverges.",
        "The owner said to cancel the run.",
        "I will cancel it if the trend holds.",
        "Cancel authority was not granted.",
    ):
        found = prose_findings(text, log)
        assert found.unsupported == [], f"false positive on: {text}"


def test_the_prose_scanner_counts_what_it_cannot_map_instead_of_dropping_it():
    log = ActionLog()
    found = prose_findings("I reported it and it has been terminated.", log)
    assert found.asserted, "matched phrases must be visible"
    assert found.unparsed == 0
    assert set(found.unsupported) == {"reported", "terminated"}


def test_prose_is_checked_against_the_reports_own_subject_not_just_the_action_kind():
    orch = MockOrchestrator(_log((CANCEL, "train-d")))
    orch.receive(_report(subject="train-c", narrative="I cancelled train-c."))

    assert orch.prose_flags(), "having cancelled a different job must not back a claim about this one"


def test_the_summary_separates_refusals_from_advisory_prose_flags():
    orch = MockOrchestrator(_log((CANCEL, "train-d")))
    orch.receive(_report(dedupe_key="k1", claims=[Claim("cancelled", "train-c")]))
    orch.receive(_report(dedupe_key="k2", narrative="I cancelled train-c."))

    summary = orch.summary()
    assert summary["rejected"] == 1
    assert summary["unsubstantiated_claims"] == 1
    assert summary["accepted"] == 1
    assert summary["prose_flagged"] == 1, "a structurally sound report can still read as if something was done"


def test_the_log_survives_a_round_trip_through_a_dict():
    log = _log((CANCEL, "train-c"), (ESCALATE, "train-c"))
    restored = ActionLog.from_dict(log.to_dict())
    assert restored.has(CANCEL, "train-c")
    assert restored.verify(Claim("cancelled", "train-c"))[0]
