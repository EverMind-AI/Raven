"""The budget note must quote the window the turn actually runs on.

Two DR observers divide by the context window: ``BudgetNoteObserver`` writes the
quotient into the model's own history, and ``SpinEntryBreaker`` gates on it. Both
were handed the configured default while the loop's own shrink path had been
resolving the real window per model. On the served student the two agree by
accident - its model id resolves to nothing, so both fall back to 65,536 - which
is why the defect survived the whole dr@2.x ladder with no symptom. It surfaces
only on a model the resolver knows, where the note claimed 110% of a window the
turn was nowhere near while not one tool result had been elided.

In the plugin the resolver lives with the host loop; the chain assembly receives
the resolved window and hands it to both observers. What is testable here is the
half that lives in this package: same turn, same usage, the note and the warning
follow whichever denominator the caller injected.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.budget_note import BudgetNoteObserver  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402

# What the fork's ``AgentLoop._window_for`` resolved for a model the catalog
# knows (``anthropic/claude-sonnet-4.5``); the plugin takes the resolved value
# from its caller, so the resolver itself stays with the host loop's tests.
_RESOLVED_WINDOW = 1_000_000
_CONFIGURED_DEFAULT = 65_536


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
    decision = asyncio.run(obs.after_iteration(ctx))
    assert messages[-1]["content"] == "body", "the note must travel as append_note, not a mutation"
    return decision.append_note or ""


def _pct(note: str) -> int | None:
    m = re.search(r"context ~(\d+)%", note)
    return int(m.group(1)) if m else None


def test_the_note_text_is_what_actually_changes():
    """The defect is a sentence, not an estimate, so assert on the sentence.

    72,000 prompt tokens is over the configured default and far under the real
    window. Same turn, same usage: one note tells the model it has overrun its
    context, the other that it has used a fraction of it.
    """
    used = 72_000
    wrong = _note_for(_CONFIGURED_DEFAULT, used)
    right = _note_for(_RESOLVED_WINDOW, used)
    assert _pct(wrong) == 110
    assert _pct(right) is not None and _pct(right) < 10


def test_the_converge_warning_follows_the_denominator():
    """The note is not the only consequence - the warning fires off the same ratio.

    Worth its own assertion because the warning is the part that asks the model to
    stop researching, so a wrong denominator does not merely misinform, it
    terminates work early on a turn with most of its window still free.
    """
    used = 72_000
    assert "budget warning" in _note_for(_CONFIGURED_DEFAULT, used)
    assert "budget warning" not in _note_for(_RESOLVED_WINDOW, used)


_TOOLS = [{"function": {"name": name}} for name in ("web_search", "web_fetch", "write_file")]


def _offered(obs: BudgetNoteObserver, metadata: dict) -> list[str]:
    ctx = AgentHookContext(session_key="t", iteration=1, messages=[], tools=list(_TOOLS), metadata=metadata)
    decision = asyncio.run(obs.before_iteration(ctx))
    tools = decision.modified_tools if decision.modified_tools is not None else _TOOLS
    return [t["function"]["name"] for t in tools]


def _step(obs: BudgetNoteObserver, iteration: int, metadata: dict) -> str:
    ctx = AgentHookContext(
        session_key="t",
        iteration=iteration,
        messages=[{"role": "tool", "content": "body"}],
        response=_Response(1_000),
        metadata=metadata,
    )
    return asyncio.run(obs.after_iteration(ctx)).append_note or ""


def test_research_closes_a_set_number_of_iterations_after_the_warning():
    """One run said "two more searches" from the warning at 48 to the cap at 60."""
    obs = BudgetNoteObserver(max_iterations=60, context_window_tokens=_RESOLVED_WINDOW, close_after=6)
    metadata: dict = {}
    warned = _step(obs, 48, metadata)
    assert "research closes 6 iterations from now" in warned
    assert "research is closed" not in _step(obs, 53, metadata)
    assert _offered(obs, metadata) == ["web_search", "web_fetch", "write_file"]
    assert "research is closed" in _step(obs, 54, metadata)
    assert _offered(obs, metadata) == ["write_file"]
    assert "research is closed" not in _step(obs, 55, metadata), "the closing note is said once"


def test_without_a_deadline_the_warning_stays_advisory():
    obs = BudgetNoteObserver(max_iterations=60, context_window_tokens=_RESOLVED_WINDOW)
    metadata: dict = {}
    assert "research closes" not in _step(obs, 48, metadata)
    _step(obs, 59, metadata)
    assert _offered(obs, metadata) == ["web_search", "web_fetch", "write_file"]


def test_a_reviewers_evidence_round_reopens_closed_research_while_it_lasts():
    from research_flow.support.evidence_round import EvidenceRound

    rounds = EvidenceRound(searches=1)
    obs = BudgetNoteObserver(
        max_iterations=60, context_window_tokens=_RESOLVED_WINDOW, close_after=2, evidence_round=rounds
    )
    metadata: dict = {}
    _step(obs, 48, metadata)
    _step(obs, 50, metadata)
    assert _offered(obs, metadata) == ["write_file"]
    rounds.open()
    assert _offered(obs, metadata) == ["web_search", "web_fetch", "write_file"]
    rounds.consume()
    assert _offered(obs, metadata) == ["write_file"]
