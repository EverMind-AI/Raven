"""The ledger -- one stable numbering of the history, priced and cut-aware.

Everything the compacting strategy decides is decided over indices into the
session it was handed: which prefix is the head, where the tail starts, which
exchanges a hole covers. Those indices have to mean the same thing to the
policy that picks them and to the projection that applies them, so they are
computed once, here, rather than recomputed from the message list at each
step.

Tool exchanges are paired by position, through the same index
:class:`~raven.context_engine.history_trimmer.HistoryTrimmer` drops by: a
result answers the nearest earlier assistant that declared its id. Nothing
makes a tool-call id unique across a session, so pairing by id alone would let
the ledger and the trimmer disagree about which messages stand or fall
together -- and a cut that one of them thinks is clean would hand the provider
an orphan.

The prices are ``estimate_message_tokens``, and they are additive by
construction. They are not the authority on whether a prompt fits -- that is
:meth:`~raven.context_engine.history_trimmer.HistoryTrimmer.trim`, which
measures the assembled list through the provider's own counter when it has
one. The ledger's job is to choose *what* to keep; the trimmer's is to enforce
the bound on the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence

from raven.context_engine.history_trimmer import _group_index
from raven.utils.tokens import estimate_message_tokens

Boundary = Literal["tool_pair", "turn"]


@dataclass(frozen=True)
class LedgerEntry:
    """One session message: where it is, what it is, what it costs."""

    index: int
    role: str
    tokens: int


class MessageLedger:
    """Stable indices, per-message token costs, and legal cut points."""

    def __init__(self, messages: list[dict[str, Any]], tokens: "Sequence[int] | None" = None) -> None:
        """``tokens``, when given, are the per-message prices already known --
        one per message, in order -- so a caller that keeps them across turns
        does not pay the estimator again for messages that did not change."""
        self.messages = messages
        prices = list(tokens) if tokens is not None and len(tokens) == len(messages) else None
        self.entries = [
            LedgerEntry(
                index=idx,
                role=str(msg.get("role", "")),
                tokens=prices[idx] if prices is not None else estimate_message_tokens(msg),
            )
            for idx, msg in enumerate(messages)
        ]
        self.groups = _group_index(messages)
        self._balanced = _balanced_cuts(len(messages), self.groups)
        self._user_cuts = frozenset(e.index for e in self.entries if e.role == "user") | {0, len(messages)}

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def total_tokens(self) -> int:
        return sum(entry.tokens for entry in self.entries)

    def tokens_for(self, indices: "set[int] | frozenset[int]") -> int:
        return sum(self.entries[index].tokens for index in indices if 0 <= index < len(self.entries))

    def group_of(self, index: int) -> frozenset[int]:
        """The tool exchange ``index`` cannot be separated from -- itself, when
        it is not part of one."""
        return self.groups.get(index, frozenset((index,)))

    def walk_back_until(self, budget_tokens: int) -> int:
        """The earliest index whose suffix still costs at most ``budget_tokens``.

        ``len(self)`` when even the last message is over budget: the caller is
        asking for a floor, and answering 0 there would turn a token floor into
        "keep everything".
        """
        start = len(self.entries)
        spent = 0
        for entry in reversed(self.entries):
            if spent + entry.tokens > budget_tokens:
                break
            spent += entry.tokens
            start = entry.index
        return start

    def align_backward(self, index: int, boundary: Boundary) -> int:
        """The nearest legal cut at or before ``index`` -- keeps more."""
        for candidate in range(min(index, len(self.entries)), -1, -1):
            if self._is_cut(candidate, boundary):
                return candidate
        return 0

    def align_forward(self, index: int, boundary: Boundary) -> int:
        """The nearest legal cut at or after ``index`` -- keeps more.

        "More" reads the other way round here because this aligns the *end* of
        the head: a head that ends later protects more.
        """
        for candidate in range(max(index, 0), len(self.entries) + 1):
            if self._is_cut(candidate, boundary):
                return candidate
        return len(self.entries)

    def _is_cut(self, index: int, boundary: Boundary) -> bool:
        """Whether the history may be split before ``index``.

        Under ``tool_pair`` the only requirement is that no tool exchange has
        members on both sides of the cut, so neither side is handed a dangling
        half. ``turn`` additionally demands the cut fall on a user message --
        coarser, and it never lands mid-exchange.
        """
        if index not in self._balanced:
            return False
        return boundary != "turn" or index in self._user_cuts


def _balanced_cuts(count: int, groups: dict[int, frozenset[int]]) -> frozenset[int]:
    """Positions where no tool exchange straddles the cut.

    A cut before ``i`` is illegal when some group has a member below ``i`` and
    a member at or above it. Computed from the positional groups rather than
    by counting open call ids, so a session that reuses an id cuts where the
    trimmer would.
    """
    blocked: set[int] = set()
    for group in set(groups.values()):
        blocked.update(range(min(group) + 1, max(group) + 1))
    return frozenset(index for index in range(count + 1) if index not in blocked)


__all__ = ["Boundary", "LedgerEntry", "MessageLedger"]
