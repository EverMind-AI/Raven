"""FetchFloorObserver: search-only turns get an in-history fetch nudge."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.agent.flow import FetchFloorObserver
from raven.agent.hook import AgentHookContext


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
    await observer.after_iteration(ctx)
    assert "[note:" not in ctx.messages[-1]["content"]

    _add_tool_results(ctx, "web_search", 1)
    decision = await observer.after_iteration(ctx)

    assert "5 searches and no page opened yet" in ctx.messages[-1]["content"]
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
    await observer.after_iteration(ctx)
    assert "[note:" not in ctx.messages[-1]["content"]
    assert ctx.metadata["fetch_floor"]["streak"] == 0

    _add_tool_results(ctx, "web_search", 5)
    decision = await observer.after_iteration(ctx)

    # A page WAS opened here, so this is the branch that may say so. The
    # never-fetched branch is asserted separately above; keeping the two apart is
    # the point, since the previous single template lied in the commonest case.
    assert "5 searches since the last page was opened" in ctx.messages[-1]["content"]
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
    await observer.after_iteration(ctx)
    assert ctx.metadata["fetch_floor"]["notes"] == 2
    assert "10 searches and no page opened yet" in ctx.messages[-1]["content"]

    _add_tool_results(ctx, "web_search", 5)
    await observer.after_iteration(ctx)
    assert ctx.metadata["fetch_floor"]["notes"] == 2


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
    observer = FetchFloorObserver(min_searches=5, max_notes=0)   # record only, no notes
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
async def test_max_streak_reaches_the_persisted_trajectory():
    """Counted on the export, not inside the hook.

    A new observer key has been lost at the persistence layer before: three salvage
    counters were written by ForcedFinalizeGate, dropped by the exporter's key
    whitelist, and the downstream scorer keyed on them never fired - so the arm
    measured the previous version's behaviour under the new version's label. The
    writing side cannot see that; only the trajectory is short. So this asserts
    against ``terminal_state``, which is what AgentLoop stamps onto the turn.
    """
    from raven.agent.hook.observers import terminal_state

    observer = FetchFloorObserver(min_searches=5, max_notes=0)
    ctx = _ctx()
    _add_tool_results(ctx, "web_search", 6)
    await observer.after_iteration(ctx)
    _add_tool_results(ctx, "web_fetch", 1)
    _add_tool_results(ctx, "web_search", 1)
    await observer.after_iteration(ctx)

    exported = terminal_state(ctx.metadata)

    assert "fetch_floor" in exported
    assert exported["fetch_floor"]["max_streak"] == 6
    assert exported["fetch_floor"]["streak"] == 1
    # Both must be present: reading only one of them is how the 2x discrepancy
    # became invisible in the first place.
    assert {"streak", "max_streak"} <= set(exported["fetch_floor"])
