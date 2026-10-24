"""What is kept, and in what order the rest goes.

The history is cut into three positional ranges -- ``[HEAD][COMPACT][TAIL]`` --
and compaction only ever operates inside the middle one. Positional, because a
contiguous compactable range is what lets a later phase send the compaction
request as a verbatim prefix of the real conversation and have the provider's
cache recognise it; a "keep the interesting ones" rule would scatter the
survivors and destroy that property.

HEAD is the task anchor -- the opening question and the first answer -- and it
decays to nothing after the first compaction, because by then those turns are
represented by what compaction produced and protecting them forever makes the
head grow without bound.

TAIL is dual: a message count and a share of the window, and the *union* of
the two, since each expresses a floor. A count alone leaves nothing when the
recent messages are large; a share alone can leave a single half-message when
they are enormous.

Pinned skill bodies are the exception to positional selection, so they are
modelled as **holes** in COMPACT rather than as a fourth region: the range
structure stays contiguous, and the hole simply does not participate. That
also keeps the cut points honest -- a hole is a whole tool exchange, so
removing everything around it never orphans a tool result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.context_engine.pinned import pinned_message_ids

if TYPE_CHECKING:
    from raven.config.raven import ContextCompactionConfig
    from raven.context_engine.history.compacting.ledger import MessageLedger
    from raven.context_engine.history.compacting.trigger import CompactionPlan


@dataclass(frozen=True)
class Ranges:
    """The three regions, plus the holes punched in the middle one."""

    head: range
    compact: range
    tail: range
    holes: tuple[range, ...]

    @property
    def hole_ids(self) -> frozenset[int]:
        return frozenset(index for hole in self.holes for index in hole)


@dataclass(frozen=True)
class Selection:
    """Which indices survive this turn, and what it cost to get there."""

    kept: tuple[int, ...]
    dropped: tuple[int, ...]
    kept_tokens: int
    exhausted: bool
    """True when COMPACT ran out before the target was met.

    The turn still ships -- the budget bound is enforced downstream by the
    trimmer, which drops whole exchanges from the front -- but it says the
    window cannot host this retention policy, which is worth a warning rather
    than a silent degradation.
    """


class RetentionPlanner:
    """Cuts the history into ranges, then empties COMPACT until it fits."""

    def __init__(self, cfg: "ContextCompactionConfig", pinned_skill_ids: list[str] | None = None) -> None:
        self.cfg = cfg
        self.pinned_skill_ids = list(pinned_skill_ids or [])

    def split(self, ledger: "MessageLedger", plan: "CompactionPlan", *, generation: int) -> Ranges:
        """Where HEAD ends, where TAIL starts, and which exchanges are holes."""
        cfg = self.cfg
        total = len(ledger)

        wanted_head = 0 if (cfg.head_keep_decays and generation > 0) else min(cfg.head_keep_messages, total)
        head_end = ledger.align_forward(wanted_head, cfg.boundary)

        by_count = max(0, total - cfg.tail_keep_messages)
        by_tokens = ledger.walk_back_until(int(plan.window * cfg.tail_keep_ratio))
        tail_start = ledger.align_backward(min(by_count, by_tokens), cfg.boundary)

        head_end = min(head_end, tail_start)
        compact = range(head_end, tail_start)
        return Ranges(
            head=range(0, head_end),
            compact=compact,
            tail=range(tail_start, total),
            holes=self._holes(ledger, compact),
        )

    def pinned_ids(self, ledger: "MessageLedger") -> frozenset[int]:
        """Every message a pinned skill body occupies, wherever it sits.

        Wider than :attr:`Ranges.holes`, which is this set narrowed to the
        compacted range because that is the only region removal touches. The
        pruner needs the unnarrowed answer: a pinned body inside HEAD is in no
        hole at all, and rewriting it into a ref would defeat the pin just as
        thoroughly as dropping it would.
        """
        if not self.cfg.honor_pinned or not self.pinned_skill_ids:
            return frozenset()
        return frozenset(pinned_message_ids(ledger.messages, self.pinned_skill_ids))

    def _holes(self, ledger: "MessageLedger", compact: range) -> tuple[range, ...]:
        return _as_ranges(self.pinned_ids(ledger) & frozenset(compact))

    def keep_within(
        self,
        ledger: "MessageLedger",
        ranges: Ranges,
        *,
        fixed_tokens: int,
        budget_tokens: int,
        retired: "frozenset[int] | set[int]" = frozenset(),
    ) -> Selection:
        """Drop whole exchanges from the front of COMPACT until the total fits.

        Oldest first and never a partial exchange: a tool result separated from
        its call is refused by the provider, and re-adding one half is how a
        drop loop stops converging. Holes are stepped over entirely.

        Only COMPACT is touched. Reaching ``budget_tokens`` is the goal, not the
        requirement -- the requirement is the window, enforced afterwards by
        the trimmer, which is also what performs the tail-only truncation when
        even HEAD and TAIL together do not fit.

        ``retired`` are the messages an earlier compaction already replaced with
        a checkpoint. They begin dropped rather than being selected and dropped
        again, which is what keeps a session that has compacted once from paying
        for the same removal every later turn -- and what keeps the history a
        provider cached from growing back.
        """
        kept = set(range(len(ledger))) - set(retired)
        holes = ranges.hole_ids
        compact_ids = set(ranges.compact)
        total = fixed_tokens + ledger.tokens_for(kept)
        dropped: set[int] = set(retired)

        for index in ranges.compact:
            if total <= budget_tokens:
                break
            if index in dropped or index in holes:
                continue
            group = ledger.group_of(index)
            if group & holes or not group <= compact_ids:
                continue
            dropped |= group
            kept -= group
            total -= ledger.tokens_for(group)

        kept = self._anchored(ledger, kept)
        dropped -= kept
        kept_tokens = ledger.tokens_for(kept)
        return Selection(
            kept=tuple(sorted(kept)),
            dropped=tuple(sorted(dropped)),
            kept_tokens=kept_tokens,
            exhausted=fixed_tokens + kept_tokens > budget_tokens,
        )

    @staticmethod
    def _anchored(ledger: "MessageLedger", kept: set[int]) -> set[int]:
        """Make sure the survivors start at a user message.

        Not cosmetic. ``HistoryTrimmer.canonical_ids`` -- which every path here
        runs through -- discards everything before the first user message in a
        selection, so a selection that opens on a pinned tool exchange loses
        exactly the thing that was pinned. Re-attaching the question that
        exchange was answering costs one message and is what a reader would
        expect to see above it anyway.
        """
        if not kept:
            return kept
        first = min(kept)
        if ledger.entries[first].role == "user":
            return kept
        for entry in reversed(ledger.entries[:first]):
            if entry.role == "user":
                return kept | {entry.index}
        return kept


def _as_ranges(indices: "set[int] | frozenset[int]") -> tuple[range, ...]:
    """Contiguous runs of ``indices``, in order."""
    out: list[range] = []
    for index in sorted(indices):
        if out and index == out[-1].stop:
            out[-1] = range(out[-1].start, index + 1)
        else:
            out.append(range(index, index + 1))
    return tuple(out)


def project(messages: list[dict[str, Any]], kept: "tuple[int, ...] | list[int]") -> list[dict[str, Any]]:
    """The kept indices as a provider-safe, adjacency-closed message list."""
    return HistoryTrimmer.history_from_ids(messages, HistoryTrimmer.canonical_ids(messages, list(kept)))


__all__ = ["Ranges", "RetentionPlanner", "Selection", "project"]
