"""The budget note must quote the window the turn actually runs on.

Two DR observers divide by the context window: ``BudgetNoteObserver`` writes the
quotient into the model's own history, and ``SpinEntryBreaker`` gates on it. Both
were handed the configured default while the loop's own shrink path had been
resolving the real window per model. On the served student the two agree by
accident - its model id resolves to nothing, so both fall back to 65,536 - which
is why the defect survived the whole dr@2.x ladder with no symptom. It surfaces
only on a model the resolver knows, where the note claimed 110% of a window the
turn was nowhere near while not one tool result had been elided.

So this file asserts both branches. The accidental-agreement branch is the one
that matters most: every published reading was taken on it, and a fix that
changed it would have invalidated the ladder rather than repaired it.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from raven.agent.flow.budget_note import BudgetNoteObserver
from raven.agent.hook.base import AgentHookContext
from raven.agent.loop.main import AgentLoop

_UNKNOWN = "student"
_KNOWN = "anthropic/claude-sonnet-4.5"


def _window_for(model: str, configured: int = 65_536) -> int:
    loop = AgentLoop.__new__(AgentLoop)
    loop.model = model
    loop.context_window_tokens = configured
    return loop._window_for()


class _Response:
    has_tool_calls = True

    def __init__(self, prompt_tokens: int) -> None:
        self.usage = {"prompt_tokens": prompt_tokens, "completion_tokens": 0}


def _note_for(window: int, used_tokens: int) -> str:
    obs = BudgetNoteObserver(max_iterations=40, context_window_tokens=window)
    messages = [{"role": "tool", "content": "body"}]
    ctx = AgentHookContext(
        session_key="t",
        iteration=3,
        messages=messages,
        response=_Response(used_tokens),
        metadata={},
    )
    asyncio.run(obs.after_iteration(ctx))
    return messages[-1]["content"]


def _pct(note: str) -> int | None:
    m = re.search(r"context ~(\d+)%", note)
    return int(m.group(1)) if m else None


def test_the_served_student_is_byte_identical_to_the_published_ladder():
    """The branch every published dr@2.x reading was taken on.

    A repair that moved this number would not be a repair - it would silently
    redefine the note that trained models condition on, on the one axis where
    the whole version ladder was measured.
    """
    assert _window_for(_UNKNOWN) == 65_536


def test_a_resolvable_model_stops_using_the_configured_default():
    resolved = _window_for(_KNOWN)
    assert resolved != 65_536
    assert resolved > 65_536


def test_the_note_text_is_what_actually_changes():
    """The defect is a sentence, not an estimate, so assert on the sentence.

    72,000 prompt tokens is over the configured default and far under the real
    window. Same turn, same usage: one note tells the model it has overrun its
    context, the other that it has used a fraction of it.
    """
    used = 72_000
    wrong = _note_for(65_536, used)
    right = _note_for(_window_for(_KNOWN), used)
    assert _pct(wrong) == 110
    assert _pct(right) is not None and _pct(right) < 10


def test_the_converge_warning_follows_the_denominator():
    """The note is not the only consequence - the warning fires off the same ratio.

    Worth its own assertion because the warning is the part that asks the model to
    stop researching, so a wrong denominator does not merely misinform, it
    terminates work early on a turn with most of its window still free.
    """
    used = 72_000
    assert "budget warning" in _note_for(65_536, used)
    assert "budget warning" not in _note_for(_window_for(_KNOWN), used)


@pytest.mark.parametrize("configured", [32_768, 65_536, 200_000])
def test_an_unresolvable_model_keeps_whatever_was_configured(configured):
    """The fallback must stay the caller's number, not a constant of its own.

    A resolver that substituted its own default here would quietly overwrite the
    per-arm context setting on exactly the arms it cannot identify - which is
    every arm this programme has published.
    """
    assert _window_for(_UNKNOWN, configured) == configured
