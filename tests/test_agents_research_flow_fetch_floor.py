"""FetchFloorObserver: search-only turns get an in-history fetch nudge."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.fetch_floor import FetchFloorObserver  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402


def _ctx():
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = 1
    ctx.messages = [{"role": "user", "content": "task"}]
    ctx.response = SimpleNamespace(has_tool_calls=True)
    return ctx


def _add_tool_results(ctx, name, n):
    for i in range(n):
        ctx.messages.append({"role": "tool", "name": name, "content": f"{name} result {i}"})


@pytest.mark.asyncio
async def test_note_appended_after_search_streak():
    observer = FetchFloorObserver(min_searches=5)
    ctx = _ctx()
    _add_tool_results(ctx, "web_search", 4)
    decision = await observer.after_iteration(ctx)
    assert decision.append_note is None

    _add_tool_results(ctx, "web_search", 1)
    decision = await observer.after_iteration(ctx)

    assert "5 searches and no page opened yet" in decision.append_note
    assert ctx.messages[-1]["content"] == "web_search result 0"
    assert decision.notes == ["fetch_floor_note 1"]


@pytest.mark.asyncio
async def test_a_fetch_resets_the_streak_but_does_not_disarm_the_floor():
    """The streak is counted since the last fetch, not since the turn started.

    Keying it on "has this turn ever fetched" switched the observer off for good at
    the first page opened, which in a 150-iteration turn is almost immediately. The
    shape that actually costs answers is the second half of this test: open a page,
    then run another streak of searches without opening anything.
    """
    observer = FetchFloorObserver(min_searches=5)
    ctx = _ctx()
    _add_tool_results(ctx, "web_search", 5)
    _add_tool_results(ctx, "web_fetch", 1)
    decision = await observer.after_iteration(ctx)
    assert decision.append_note is None
    assert ctx.metadata["fetch_floor"]["streak"] == 0

    _add_tool_results(ctx, "web_search", 5)
    decision = await observer.after_iteration(ctx)

    # A page WAS opened here, so this is the branch that may say so. The
    # never-fetched branch is asserted separately above; keeping the two apart is
    # the point, since the previous single template lied in the commonest case.
    assert "5 searches since the last page was opened" in decision.append_note
    assert decision.notes == ["fetch_floor_note 1"]
    assert ctx.metadata["fetch_floor"]["fetches"] == 1


@pytest.mark.asyncio
async def test_notes_escalate_then_stop():
    observer = FetchFloorObserver(min_searches=5, max_notes=2)
    ctx = _ctx()
    _add_tool_results(ctx, "web_search", 5)
    await observer.after_iteration(ctx)
    assert ctx.metadata["fetch_floor"]["notes"] == 1

    _add_tool_results(ctx, "web_search", 5)
    decision = await observer.after_iteration(ctx)
    assert ctx.metadata["fetch_floor"]["notes"] == 2
    assert "10 searches and no page opened yet" in decision.append_note

    _add_tool_results(ctx, "web_search", 5)
    decision = await observer.after_iteration(ctx)
    assert ctx.metadata["fetch_floor"]["notes"] == 2
    assert decision.append_note is None


@pytest.mark.asyncio
async def test_terminal_turns_ignored():
    observer = FetchFloorObserver(min_searches=1)
    ctx = _ctx()
    _add_tool_results(ctx, "web_search", 3)
    ctx.response.has_tool_calls = False

    decision = await observer.after_iteration(ctx)

    assert decision.notes == []
    assert "fetch_floor" not in ctx.metadata


# --------------------------------------------------------------------------- #
# max_streak: the high-water mark, because ``streak`` is only the tail          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_max_streak_survives_a_fetch_that_zeroes_the_tail():
    """``streak`` is the run since the last fetch, so at end of turn it reports only
    the tail. Any online criterion needs the maximum.

    On the corpus dr@3.0 batch the two disagree by 2x in selectivity at K=20: 58 of
    240 questions by tail against 115 of 240 by max UNDER THIS CODE'S CALIBER
    (any ``web_fetch`` zeroes the streak). The 127/240 figure that circulates for
    the same batch is a different instrument -- it zeroes only on a *successful*
    fetch. Both reproduce; see ``fetch_floor.py``. A threshold calibrated on one
    and applied to the other is not a stricter version of the same rule - it selects
    a different population.
    """
    observer = FetchFloorObserver(min_searches=5, max_notes=0)  # record only, no notes
    ctx = _ctx()
    _add_tool_results(ctx, "web_search", 7)
    await observer.after_iteration(ctx)
    assert ctx.metadata["fetch_floor"]["streak"] == 7
    assert ctx.metadata["fetch_floor"]["max_streak"] == 7

    _add_tool_results(ctx, "web_fetch", 1)
    _add_tool_results(ctx, "web_search", 2)
    await observer.after_iteration(ctx)

    state = ctx.metadata["fetch_floor"]
    assert state["streak"] == 2, "tail must still reset on a fetch"
    assert state["max_streak"] == 7, "the high-water mark must not be reset by a fetch"


@pytest.mark.asyncio
async def test_previous_turns_tool_results_are_not_double_booked():
    """dr@3.4. The watermark opens at ``ctx.turn_base``, never 0.

    The assembled context replays earlier turns' persisted tool messages below
    the current user message; counted into this turn's streak they fire the
    note on the first tool iteration of every follow-up.
    """
    observer = FetchFloorObserver(min_searches=5)
    history = [{"role": "tool", "name": "web_search", "content": f"old {i}"} for i in range(10)]
    ctx = AgentHookContext(session_key="cli:test", turn_base=len(history))
    ctx.iteration = 1
    ctx.messages = history + [{"role": "user", "content": "follow-up"}]
    ctx.response = SimpleNamespace(has_tool_calls=True)
    _add_tool_results(ctx, "web_search", 1)
    decision = await observer.after_iteration(ctx)
    assert not decision.notes, "prior-turn searches fired this turn's note"
    assert ctx.metadata["fetch_floor"]["searches"] == 1
