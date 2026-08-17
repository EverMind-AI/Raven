"""Guardrail trigger counters: how often each completion gate fired in a turn.

The gates are on by default, so the question an audit asks is no longer "were
they enabled" but "how often did they fire". The counter rides on TurnOutcome
and is rendered once per turn, so a full run can be summarised without parsing
the model transcript.
"""

from raven.agent.loop.main import TurnOutcome, format_gate_counters


def test_outcome_defaults_to_no_triggers():
    assert TurnOutcome().gate_triggers == {}


def test_counters_render_as_one_stable_line():
    line = format_gate_counters({"test_evidence": 2, "empty_diff": 1})
    assert line == "gate_triggers: empty_diff=1 test_evidence=2"


def test_zero_counts_are_omitted_but_a_quiet_turn_still_reports():
    assert format_gate_counters({"test_evidence": 0}) == "gate_triggers: none"
    assert format_gate_counters({}) == "gate_triggers: none"
