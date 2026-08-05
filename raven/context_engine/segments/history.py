"""History slot — the conversation transcript, selected deterministically.

Replaces the Curator, which selected the same slot by running a bounded
internal LLM loop with its own tool registry (search / set-relevance /
archive / retrieve / build-context) and persisted a manifest, archives and
per-turn traces to disk. That machinery was built for a long-lived personal
assistant whose sessions outgrow any window. A coding agent's transcript is
bounded by one task, and the loop now compacts in-turn
(:mod:`raven.agent.loop.compaction`), so the LLM-driven selection bought
nothing and cost an LLM call plus disk writes on every turn -- and its
fast/slow decision had an edge (a zero history budget makes ``0 < 0`` false)
that spent ``max_steps`` calls planning over an empty transcript.

Both jobs the Curator actually did survive, deterministically:

*Projection.* Reduce the session messages to provider-safe keys and start at
a user message, so the provider never sees a dangling tool exchange. This is
what its fast path did, and :class:`HistoryTrimmer` already implements both
halves.

*Bounding.* Its slow path also kept a long session's history inside the
budget. Here that is oldest-exchange-first dropping until the history fits
``budget.available_history`` -- the same direction the Curator's own fallback
plan took, without the LLM call.
"""

from __future__ import annotations

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.utils.helpers import estimate_prompt_tokens


class HistorySegmentBuilder:
    """Contributes ``*history``; no system-slot text, no LLM call, no disk."""

    name = "history"
    order = 6
    needs_prefix = False

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        messages = ctx.session_messages
        ids = HistoryTrimmer.canonical_ids(messages, list(range(len(messages))))
        budget = ctx.budget.available_history
        dropped = 0
        # A non-positive budget means the fixed overhead (system prompt + tool
        # schemas + reply reservation) already fills the window, so no amount of
        # dropping makes the history fit. Trimming to nothing there would be a
        # cliff -- an unrealistically small window silently losing the whole
        # conversation -- so pass it through and let the loop's overflow
        # recovery deal with a request that cannot fit.
        while budget > 0 and ids and estimate_prompt_tokens(HistoryTrimmer.history_from_ids(messages, ids)) > budget:
            # ``canonical_ids`` re-closes tool adjacency and re-trims to the
            # next user message, so each pass drops a whole exchange and the
            # loop strictly shrinks.
            ids = HistoryTrimmer.canonical_ids(messages, ids[1:])
            dropped += 1
        history = HistoryTrimmer.history_from_ids(messages, ids)
        meta = {"path": "deterministic", "messages": len(history)}
        if dropped:
            meta["dropped_exchanges"] = dropped
        return Segment(text="", history=history, meta=meta)
