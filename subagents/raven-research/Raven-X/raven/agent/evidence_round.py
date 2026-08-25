"""Sanctioned retrieval round after a verify rejection (DR flow, dr@2.8).

A rejection names decisive claims the gathered evidence does not support. Until
dr@2.8 the only action the flow offered in response was to rewrite the draft
from that same evidence -- ``_REVISION_PROMPT`` said so in as many words -- and
the spin breaker's forced-report note said it a second time. Whenever the reason
a claim is unsupported is that the supporting document was never retrieved, the
recoverable share of that instruction is exactly zero.

This object is the channel between the gate that opens such a round and the two
components that have to behave differently inside it: the search tool, which may
go deeper than the per-call default, and the spin breaker, whose entire purpose
is to stop a turn from re-opening research.

Shared mutable state is deliberate. Hooks have no handle on tool instances --
``AgentHookContext`` carries tool *schemas*, not the registry -- so a flag set on
the context cannot reach the tool that must honour it. The alternative, reading
config at the tool seam, would apply to the flow-off anchor too.

It lives beside ``ledger.py`` rather than under ``flow/`` for the reason given
there: both a flow hook and a tool need it, and a tool module importing a flow
module is the wrong direction.

The counters are not bookkeeping. A round that never opens, a depth that is
never taken, and a spin pass that never fires are each indistinguishable from
the feature being absent, and the batch that would reveal it costs questions we
cannot get back.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EvidenceRound:
    """Budget and state for post-rejection retrieval, shared across components."""

    depth: int = 50
    searches: int = 6
    remaining: int = 0
    opened: int = 0
    deep_searches: int = 0
    spin_passes: int = 0

    @property
    def active(self) -> bool:
        return self.remaining > 0

    def open(self) -> None:
        """Grant one round's worth of deeper searches."""
        self.opened += 1
        self.remaining = self.searches

    def consume(self) -> None:
        """Spend one deep search. Called only when a request is actually issued.

        Kept separate from reading ``depth`` because the depth participates in the
        repeat-search cache key: a round has to be able to ask what the key would
        be without paying for a call that then turns out to be a cache replay.
        """
        if self.remaining > 0:
            self.remaining -= 1
            self.deep_searches += 1

    def note_spin_pass(self) -> None:
        self.spin_passes += 1

    def reset(self) -> None:
        """Close any open round at a turn boundary; keep the counters."""
        self.remaining = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "rounds_opened": self.opened,
            "deep_searches": self.deep_searches,
            "spin_passes": self.spin_passes,
        }


__all__ = ["EvidenceRound"]
