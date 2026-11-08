"""The ``[[ref ...]]`` provenance-token grammar, one spelling for every store that mints one.

A ref token is a short, greppable pointer that sits in a prompt where the
verbatim text it names would otherwise be::

    [[ref session/tg_42#118]]               one entry
    [[ref session/tg_42#7-9]]               a span, both ends inclusive

Context compaction mints them: it replaces a large tool result with a one-line
call record plus a ref, so the full output stays one retrieval away instead of
occupying the window, and ``context_retrieve`` reads them back.

The grammar lives here, on neutral ground, rather than inside the context
engine: a token format is mechanism rather than the domain of whoever happens to
mint one first, and a second store that wants refs should not have to import an
engine to spell them.

**This module imports nothing but the standard library, deliberately.** No
backend, no config, no engine, nothing that pulls a subsystem along behind it.

Four conventions callers may rely on:

1. **ASCII and greppable.** Doubled brackets do not occur in ordinary prose, so
   a ref is findable by plain substring search and survives any transport that
   is byte-clean for ASCII. No Unicode brackets, for the same reason.
2. **``store`` is a namespace.** Each subsystem registers its own name and owns
   what ``scope`` means inside it -- the session log keys ``session`` by the
   log's revision -- so no two stores have to coordinate to add a scope.
3. **Ordinals are monotone and stable within their store.** ``lo`` and ``hi``
   index whatever append-only sequence that store keeps, so a token minted once
   keeps naming the same text. A store whose sequence can restart must fold the
   generation into ``scope``, so a stale ref resolves to nothing rather than to
   the wrong entry.
4. **A rendered ref is retrievable.** Putting a token somewhere the model can
   read it promises that the model can ask for what it names and get the
   verbatim text back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REF_PATTERN = re.compile(r"\[\[ref (?P<store>[\w.-]+)/(?P<scope>[^#\]]+)#(?P<lo>\d+)(?:-(?P<hi>\d+))?\]\]")
"""The grammar, in one place. ``store`` is restricted to identifier characters
so a store name stays greppable; ``scope`` is permissive up to ``#`` and ``]``
so a store may key on whatever it already has."""


@dataclass(frozen=True)
class SourceRef:
    """One resolved ``[[ref ...]]``: which store, which scope, which entries."""

    store: str
    scope: str
    lo: int
    hi: int
    """Inclusive, and equal to ``lo`` for a single entry rather than ``None``, so
    every caller reads the same pair without a single-versus-span special case."""

    def render(self) -> str:
        """The token form; a one-entry span collapses to a single ordinal."""
        span = f"{self.lo}" if self.lo == self.hi else f"{self.lo}-{self.hi}"
        return f"[[ref {self.store}/{self.scope}#{span}]]"

    @classmethod
    def parse(cls, text: str) -> SourceRef | None:
        """The first ref anywhere in ``text``, or ``None`` if it holds none.

        A search rather than an anchored match: tokens are read back out of
        prose a model wrote, where they trail a sentence or stand on their own
        line.
        """
        match = REF_PATTERN.search(text)
        return None if match is None else cls._from_match(match)

    @classmethod
    def find_all(cls, text: str) -> list[SourceRef]:
        """Every ref in ``text``, in the order they appear."""
        return [cls._from_match(match) for match in REF_PATTERN.finditer(text)]

    @classmethod
    def _from_match(cls, match: re.Match[str]) -> SourceRef:
        lo = int(match["lo"])
        return cls(store=match["store"], scope=match["scope"], lo=lo, hi=int(match["hi"]) if match["hi"] else lo)


__all__ = ["REF_PATTERN", "SourceRef"]
