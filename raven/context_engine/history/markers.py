"""Private message keys a history strategy sets for in-turn window coordination.

A history strategy hands the loop messages it wrote itself (a checkpoint
standing in for removed history) and messages it was asked never to drop (a
pinned skill exchange). The loop's in-turn window management has to recognise
both without knowing which strategy produced them, so they are marked with a
key no provider defines. The keys never reach a provider or the session log:
they are stripped before the request is sent and before a turn is saved.

Kept outside any one strategy's package so a reader can import the names
without loading a strategy.
"""

from __future__ import annotations

HISTORY_CHECKPOINT_KEY = "_history_checkpoint"
"""Set to ``True`` on the History Checkpoint message a strategy spliced in."""

HISTORY_PINNED_KEY = "_history_pinned"
"""Set to ``True`` on every message of a pinned exchange."""

HISTORY_PRIVATE_KEYS = frozenset({HISTORY_CHECKPOINT_KEY, HISTORY_PINNED_KEY})


__all__ = ["HISTORY_CHECKPOINT_KEY", "HISTORY_PINNED_KEY", "HISTORY_PRIVATE_KEYS"]
