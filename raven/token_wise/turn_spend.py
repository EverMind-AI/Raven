"""What one turn costs: its own model calls plus the calls it delegated.

A turn's cost is what it spent, not the price of the call it happened to end
on. The loop bills each of its own iterations into the scope opened here, and
:class:`~raven.token_wise.usage_tracker.UsageTracker` bills the delegated ones
-- a sub-agent's turn runs under a session key of its own and names the
delegating turn's session as its root, and the recorder is the one place every
billed call already passes.

A delegation runs in a task of its own, so the scope is found by root session
key through the module-level table below rather than inherited through a
ContextVar, which a task started elsewhere would never see.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

# session key -> the turns of that session that are running right now. A list
# because two turns of one session may overlap (a relay re-entering the
# conversation it was delegated from); each bills what was spent while it ran.
_OPEN: dict[str, list["TurnSpend"]] = {}


class TurnSpend:
    """A turn's billed cost so far, in provider-reported dollars.

    ``cost_usd`` stays None until some call reports a price: a model with no
    published price bills an unknown amount, not zero. ``cost_missing_calls``
    counts those, so a reader can tell a cheap turn from an unpriced one.
    """

    def __init__(self, session_key: str | None) -> None:
        self.session_key = session_key or ""
        self.cost_usd: float | None = None
        self.cost_missing_calls = 0

    def note(self, cost_usd: float | None) -> None:
        """Bill one call to this turn."""
        if cost_usd is None:
            self.cost_missing_calls += 1
        else:
            self.cost_usd = (self.cost_usd or 0.0) + cost_usd

    @contextmanager
    def collecting(self) -> Iterator["TurnSpend"]:
        """Take delegated calls for the length of the turn.

        A turn with no session key is collected from too -- it still bills its
        own calls -- but nothing can name it as a root, so it is not listed.
        """
        if not self.session_key:
            yield self
            return
        live = _OPEN.setdefault(self.session_key, [])
        live.append(self)
        try:
            yield self
        finally:
            live.remove(self)
            if not live:
                _OPEN.pop(self.session_key, None)


def note_delegated(root_session_key: str | None, cost_usd: float | None) -> None:
    """Bill one delegated call to the turn that delegated it.

    Called by the recorder for a call whose own session is not its root: the
    sub-agent's turn bills it as its own spend, and the turn that delegated it
    bills it here. A root with no turn running -- a delegation that outlived
    the turn that started it -- bills nobody.
    """
    if not root_session_key:
        return
    for spend in tuple(_OPEN.get(root_session_key, ())):
        spend.note(cost_usd)
