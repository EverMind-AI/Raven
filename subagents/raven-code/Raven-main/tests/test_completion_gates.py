"""Completion-gate guardrail (empty-diff falsification): pure decision logic.

Written test-first (2026-08-10). Evidence base: trajectory error analysis
(two independent model rounds; the evidence chain lives in the guardrail design notes).
Design constraints inherited from guardrail-design.md: opt-in via env (default OFF, behavior
byte-identical when unset), one-shot per turn, facts only, innocent-until-proven,
never inject on the last iteration.
"""

from raven.agent.loop import completion_gates as cg

# ---------------------------------------------------------------- empty-diff gate


def _nudge(**overrides):
    kwargs = dict(
        enabled=True,
        code_edits_observed=False,
        already_nudged=False,
        iteration=3,
        max_iterations=200,
    )
    kwargs.update(overrides)
    return cg.empty_diff_nudge(**kwargs)


def test_empty_diff_disabled_is_silent():
    assert _nudge(enabled=False) is None


def test_empty_diff_fires_once_when_finishing_with_no_edits():
    text = _nudge()
    assert text is not None
    # Strong form: must demand the discriminating evidence -- a symptom
    # reproduction that fails on the current, unmodified code.
    assert "unmodified" in text.lower()
    assert "reproduc" in text.lower()


def test_empty_diff_respects_one_shot():
    assert _nudge(already_nudged=True) is None


def test_empty_diff_silent_when_code_was_edited():
    assert _nudge(code_edits_observed=True) is None


def test_empty_diff_never_fires_on_last_iteration():
    assert _nudge(iteration=200, max_iterations=200) is None
