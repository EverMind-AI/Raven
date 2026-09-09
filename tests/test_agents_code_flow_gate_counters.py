"""Guardrail trigger counters: the one stable line an audit greps for.

Ported from the fork's tests/test_gate_counters.py against the plugin's
completion-gates module. The counter store itself (the fork's
``TurnOutcome.gate_triggers``, fork loop/main.py:111) rides the fork loop's
own turn accounting and boards with the completion-gates hook wave; the
rendering contract ports now so the line's shape is pinned before anything
consumes it.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow.completion_gates import format_gate_counters  # noqa: E402


def test_counters_render_as_one_stable_line():
    line = format_gate_counters({"test_evidence": 2, "empty_diff": 1})
    assert line == "gate_triggers: empty_diff=1 test_evidence=2"


def test_zero_counts_are_omitted_but_a_quiet_turn_still_reports():
    assert format_gate_counters({"test_evidence": 0}) == "gate_triggers: none"
    assert format_gate_counters({}) == "gate_triggers: none"
