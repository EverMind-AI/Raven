"""The compacting history strategy -- deterministic retention first, a summary
as the fallback, and never a message the log cannot still produce verbatim.

It reads the whole assembled prompt against a trigger measured on the raw
context window, cuts the history into ``[HEAD][COMPACT][TAIL]`` with pinned
skill bodies held out as holes, shortens what tool results it may -- leaving a
record of the call and a ref back to the verbatim output -- and empties the
middle only if that was not enough. What it empties leaves a checkpoint in its
place: eight sections written by the turn's own model from a request that is a
verbatim prefix of the conversation, or, when no model answers, a fold saying
how much went and where to ask for it.

Self-contained by construction. It depends downward on shared parts of
:mod:`raven.context_engine` -- the reply-headroom policy in ``headroom``, the
positional tool pairing in ``history_trimmer``, the pinned lookup in
``pinned``, the window markers in ``history.markers`` -- plus two conventions
that are nobody's strategy: the ref grammar in :mod:`raven.utils.refs` and the
session store's side of it in :mod:`raven.session.refs`. The one thing it
writes is the per-session state and checkpoint chain under
``memory/.context/checkpoints``, and it reads that back every turn.

Only the builder is exported; everything else is internal to the strategy.
"""

from raven.context_engine.history.compacting.runtime import CompactingHistoryBuilder

__all__ = ["CompactingHistoryBuilder"]
