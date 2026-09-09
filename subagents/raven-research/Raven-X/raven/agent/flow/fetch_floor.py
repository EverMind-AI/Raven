"""Search-without-fetch pathology guard (DR flow).

DR search results carry titles and links (snippets are stripped by
default; an arm that restores them gets a short selection preview, still
not an answer) — an answer can only be grounded in fetched pages. A turn
that keeps searching without opening one is collecting leads it will
never use. When the streak passes the
floor, a note is appended to the newest tool result (persisted history is
the only train-serve-safe channel), escalating at most ``max_notes``
times per turn.

The streak is counted **since the last fetch**, not since the turn
started. Keying it on "has this turn ever fetched" made the observer
switch itself off for good at the first page opened, which in a
150-iteration turn is almost immediately: the pathology that actually
costs answers is opening a page early and then running twenty more
searches without opening another. Measured on one batch, that shape hit
12 and 13 questions out of 76 per arm and every one of them sat in the
dead zone, while the whole batch emitted a single note.
"""

from __future__ import annotations

import logging

from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision

logger = logging.getLogger(__name__)

_NOTE_TAIL = (
    "the answer must be grounded in fetched pages, not result listings. Pick "
    "the most promising sources and open them with web_fetch, passing "
    "info_to_extract.]"
)
# Two templates because one of them was a lie in the commonest case. The streak
# counter was corrected to "since the last fetch" without correcting the English,
# so on a turn that had never fetched -- the pathology this observer exists to
# catch -- the note asserted a page open that never happened. ``state["fetches"]``
# was already tracked and simply never consulted.
_NOTE_NEVER_FETCHED = "[note: {searches} searches and no page opened yet - " + _NOTE_TAIL
_NOTE_SINCE_FETCH = "[note: {searches} searches since the last page was opened - " + _NOTE_TAIL


class FetchFloorObserver(AgentHook):
    """Nudge a search-only turn to start opening sources."""

    def __init__(self, min_searches: int = 5, max_notes: int = 2) -> None:
        self._min_searches = min_searches
        self._max_notes = max_notes

    @property
    def name(self) -> str:
        return "FetchFloorObserver"

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if not getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        messages = ctx.messages or []
        # The watermark opens at ``ctx.turn_base``, not 0: below it sits the
        # session's persisted history, and counting the previous turns' tool
        # results would double-book them into this turn's streak on the first
        # tool iteration of every follow-up.
        state = ctx.metadata.setdefault(
            "fetch_floor",
            {"searches": 0, "fetches": 0, "notes": 0, "watermark": ctx.turn_base or 0,
             "streak": 0, "max_streak": 0},
        )
        state.setdefault("streak", 0)
        state.setdefault("max_streak", 0)
        fresh = messages[state["watermark"] :]
        state["watermark"] = len(messages)
        for m in fresh:
            if isinstance(m, dict) and m.get("role") == "tool":
                if m.get("name") == "web_search":
                    state["searches"] += 1
                    state["streak"] += 1
                    # ``streak`` is the TAIL run and a fetch zeroes it, so by the end of
                    # a turn it reports only what happened after the last fetch. Any
                    # criterion evaluated online needs the high-water mark instead: the
                    # two disagree by ~2x in selectivity at K=20 on the corpus dr@3.0
                    # batch -- 58 of 240 questions by tail against 115 of 240 by max
                    # UNDER THIS CODE'S CALIBER, which is the one written below: ANY
                    # ``web_fetch`` call zeroes the streak, successful or not. The
                    # figure 127/240 that circulates for the same batch is a DIFFERENT
                    # instrument -- it zeroes only on a *successful* fetch. Both numbers
                    # are real and both reproduce; quoting one against the other's rule
                    # is the "change one word in the trigger and the conclusion flips"
                    # trap this project has already booked once. Calibrate K against the
                    # caliber you will actually run.
                    # so a threshold calibrated on one and applied to the other is not
                    # a stricter or looser version of the same rule - it selects a
                    # different population. Recorded, not acted on: nothing reads this
                    # yet, which is what keeps adding it inert.
                    if state["streak"] > state["max_streak"]:
                        state["max_streak"] = state["streak"]
                elif m.get("name") == "web_fetch":
                    state["fetches"] += 1
                    state["streak"] = 0

        if state["notes"] >= self._max_notes:
            return HookDecision()
        if state["streak"] < self._min_searches * (state["notes"] + 1):
            return HookDecision()
        if not messages or messages[-1].get("role") != "tool":
            return HookDecision()

        state["notes"] += 1
        template = _NOTE_SINCE_FETCH if state["fetches"] else _NOTE_NEVER_FETCHED
        note = template.format(searches=state["streak"])
        messages[-1]["content"] = f"{messages[-1].get('content', '')}\n\n{note}"
        logger.warning(
            "fetch-floor: %d searches since the last fetch; note %d/%d appended",
            state["streak"],
            state["notes"],
            self._max_notes,
        )
        return HookDecision(notes=[f"fetch_floor_note {state['notes']}"])


__all__ = ["FetchFloorObserver"]
