"""Scoring the upward path: reports to the orchestrating agent.

The interesting cases are the rejections. A report that reads well but omits the
value something started at leaves the orchestrator unable to check the claim, and
against a program receiver that is not a quality argument -- it is a missing
field with a name.
"""

from __future__ import annotations

from raven.ops.handoff import (
    ABSOLUTE,
    CONDITION_MET,
    FINISHED,
    NEEDS_DECISION,
    RELATIVE,
    MockOrchestrator,
    Report,
    Suggestion,
    baseline_mismatches,
    is_self_sufficient,
    missing_fields,
    unmeasured_fields,
)


def _report(**over) -> Report:
    base = dict(
        campaign="seo-watch",
        subject="ranking:acme-landing",
        kind=CONDITION_MET,
        at_ms=120_000,
        dedupe_key="ranking:acme-landing:dropped-below-10",
        observed={"position": 14},
        condition_type=ABSOLUTE,
    )
    base.update(over)
    return Report(**base)


def test_a_complete_report_is_accepted():
    orch = MockOrchestrator()
    receipt = orch.receive(_report())
    assert receipt.accepted
    assert orch.summary()["accepted"] == 1


def test_a_relative_condition_without_a_baseline_is_not_enough_to_act_on():
    thin = _report(condition_type=RELATIVE, observed={"position": 14})
    assert missing_fields(thin) == ["baseline"]
    assert not is_self_sufficient(thin)

    orch = MockOrchestrator()
    receipt = orch.receive(thin)
    assert receipt.accepted is False
    assert receipt.missing == ["baseline"]
    assert orch.missing_field_counts() == {"baseline": 1}


def test_the_same_relative_report_with_a_baseline_is_accepted():
    orch = MockOrchestrator()
    assert orch.receive(
        _report(condition_type=RELATIVE, observed={"position": 14}, baseline={"position": 4})
    ).accepted


def test_an_absolute_condition_needs_no_baseline():
    assert missing_fields(_report(condition_type=ABSOLUTE)) == []


def test_a_report_with_no_observation_is_rejected_however_it_is_worded():
    orch = MockOrchestrator()
    receipt = orch.receive(_report(observed={}))
    assert receipt.accepted is False
    assert "observed" in receipt.missing


def test_asking_for_a_decision_without_options_or_a_suggestion_is_rejected():
    orch = MockOrchestrator()
    receipt = orch.receive(_report(kind=NEEDS_DECISION))
    assert receipt.missing == ["options"], (
        "handing back a decision with nothing to decide between makes the "
        "orchestrator redo the analysis"
    )


def test_asking_for_a_decision_with_a_suggestion_is_enough():
    orch = MockOrchestrator()
    assert orch.receive(
        _report(
            kind=NEEDS_DECISION,
            suggestion=Suggestion(agent="coding", reason="the landing page needs rebuilding"),
        )
    ).accepted


def test_the_second_report_for_the_same_signal_is_rejected():
    orch = MockOrchestrator()
    assert orch.receive(_report()).accepted
    second = orch.receive(_report(at_ms=300_000))
    assert second.accepted is False
    assert orch.duplicates() == 1
    assert len(orch.accepted()) == 1


def test_a_different_signal_on_the_same_subject_still_gets_through():
    orch = MockOrchestrator()
    assert orch.receive(_report()).accepted
    assert orch.receive(
        _report(dedupe_key="ranking:acme-landing:recovered", observed={"position": 3})
    ).accepted


def test_a_report_that_claims_to_have_dispatched_is_rejected():
    orch = MockOrchestrator()
    receipt = orch.receive(
        _report(
            kind=NEEDS_DECISION,
            suggestion=Suggestion(agent="coding", reason="rebuild the page"),
            dispatched="coding",
        )
    )
    assert receipt.accepted is False
    assert orch.dispatch_attempts() == 1
    assert "orchestrator" in (receipt.reason or "")


def test_a_suggestion_is_recorded_but_acceptance_never_depends_on_it():
    orch = MockOrchestrator()
    without = orch.receive(_report(kind=FINISHED, dedupe_key="k1"))
    with_hint = orch.receive(
        _report(
            kind=FINISHED,
            dedupe_key="k2",
            suggestion=Suggestion(agent="content", reason="a comment arrived, reply to it"),
        )
    )
    assert without.accepted and with_hint.accepted
    assert [s.agent for s in orch.suggestions()] == ["content"]


def test_the_summary_separates_thin_reports_from_duplicates_and_dispatches():
    orch = MockOrchestrator()
    orch.receive(_report())
    orch.receive(_report(at_ms=200_000))
    orch.receive(_report(dedupe_key="k2", condition_type=RELATIVE))
    orch.receive(_report(dedupe_key="k3", observed={}))
    orch.receive(_report(dedupe_key="k4", dispatched="coding"))

    summary = orch.summary()
    assert summary == {
        "reports": 5,
        "accepted": 1,
        "rejected": 4,
        "duplicates": 1,
        "dispatch_attempts": 1,
        "missing_fields": {"baseline": 1, "observed": 1},
        "with_suggestion": 0,
        "unsubstantiated_claims": 0,
        "prose_flagged": 0,
        "prose_unparsed": 0,
    }


def test_a_loop_that_reports_nothing_scores_nothing_rather_than_perfectly():
    orch = MockOrchestrator()
    summary = orch.summary()
    assert summary["reports"] == 0
    assert summary["accepted"] == 0, (
        "silence must not read as a clean run; recall is measured against the "
        "signals the world actually produced, not against what was sent"
    )


# ---- a required field that is filled in with a placeholder ----------------
# Round 2 of the real-task experiment: round 1 had omitted the baseline, so the
# field was made mandatory; round 2 then supplied {"ndcg": 0} against a true
# 0.3674 and the gate passed it, because a non-empty dict satisfies a presence
# test. The schema turned an omission into a fabrication, and only the omission
# had been visible to a reader.


def test_a_baseline_of_zero_is_refused_as_not_a_measurement():
    orch = MockOrchestrator()
    receipt = orch.receive(_report(condition_type=RELATIVE, baseline={"ndcg": 0}))

    assert not receipt.accepted
    assert receipt.missing == ["baseline"]
    assert "not a measurement" in (receipt.reason or "")


def test_the_refusal_does_not_name_what_the_value_should_have_been():
    """The loop is scored on making the comparison itself, so the refusal may say
    which field carries no reading and nothing about the value it expected."""
    orch = MockOrchestrator()
    receipt = orch.receive(_report(condition_type=RELATIVE, baseline={"ndcg": 0.0}))

    assert "0.3674" not in (receipt.reason or "")
    assert not any(ch.isdigit() for ch in (receipt.reason or ""))


def test_placeholder_shapes_other_than_zero_are_also_caught():
    for placeholder in ({"ndcg": None}, {"ndcg": ""}, {"ndcg": "0"}, {"ndcg": 0, "mrr": 0.0}):
        assert unmeasured_fields(_report(condition_type=RELATIVE, baseline=placeholder)) == ["baseline"]


def test_one_genuine_reading_is_enough_even_beside_a_zero():
    """A metric can legitimately be zero, so the net only fires when the whole
    value carries no reading. A wrong-but-plausible number still gets through --
    that needs the true value, which is what expected_baseline is for."""
    assert unmeasured_fields(_report(condition_type=RELATIVE, baseline={"ndcg": 0, "mrr": 0.31})) == []
    assert unmeasured_fields(_report(condition_type=RELATIVE, baseline={"ndcg": 0.30})) == []


def test_an_absolute_condition_is_not_subject_to_the_check():
    assert unmeasured_fields(_report(condition_type=ABSOLUTE, baseline={"position": 0})) == []


def test_is_self_sufficient_agrees_with_the_receiver():
    assert not is_self_sufficient(_report(condition_type=RELATIVE, baseline={"ndcg": 0}))
    assert is_self_sufficient(_report(condition_type=RELATIVE, baseline={"ndcg": 0.3674}))


# ---- transcription check against a value the loop already holds -----------


def test_a_baseline_that_disagrees_with_the_recorded_value_is_refused():
    orch = MockOrchestrator(expected_baseline={"ndcg": 0.3674})
    receipt = orch.receive(_report(condition_type=RELATIVE, baseline={"ndcg": 0.30}))

    assert not receipt.accepted
    assert "0.3674" in (receipt.reason or "")
    assert "0.3" in (receipt.reason or "")


def test_a_correctly_copied_baseline_is_accepted():
    orch = MockOrchestrator(expected_baseline={"ndcg": 0.3674})
    assert orch.receive(_report(condition_type=RELATIVE, baseline={"ndcg": 0.3674})).accepted


def test_rounding_to_the_published_precision_still_matches():
    orch = MockOrchestrator(expected_baseline={"ndcg": 0.3674})
    assert orch.receive(_report(condition_type=RELATIVE, baseline={"ndcg": 0.367})).accepted


def test_a_missing_expected_key_is_named_in_the_refusal():
    problems = baseline_mismatches(_report(baseline={"mrr": 0.5}), {"ndcg": 0.3674})
    assert problems == ["baseline is missing ndcg; the recorded starting value is 0.3674"]


def test_no_expected_baseline_configured_means_no_transcription_check():
    orch = MockOrchestrator()
    assert orch.receive(_report(condition_type=RELATIVE, baseline={"ndcg": 0.30})).accepted
