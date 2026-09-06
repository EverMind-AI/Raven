"""Fitting raven's session tier onto whatever rungs one sub-agent actually has.

The tier is raven's word; the menu is the agent's, measured from its own ACP
handshake. The two need not match, and every mismatch has a defined answer here
rather than at each call site.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Iterator

from raven.config.schema import TIER_LADDER

_TURN_TIER: ContextVar[str | None] = ContextVar("raven_turn_subagent_tier", default=None)


@contextmanager
def turn_tier(tier: str) -> Iterator[None]:
    """Freeze the session's tier for the length of one turn.

    A turn reads its operating policy once, at the start, and a switch arriving
    while it runs lands on the next one -- the contract ``raven/acp/modes.py``
    states and the iteration cap already keeps. The sub-agent tier has to be held
    the same way: a dispatch late in a turn would otherwise pick up a tier the
    turn never started under, so two sub-agents in one turn could run at
    different efforts with nothing in the transcript explaining why.

    A ContextVar rather than an argument threaded down: everything between the
    turn's start and a dispatch is the loop's own call graph, and the same
    choice is made for the run activity and the DAG's MCP scope.
    """
    token = _TURN_TIER.set(tier)
    try:
        yield
    finally:
        _TURN_TIER.reset(token)


def turn_tier_in_force() -> str | None:
    """The tier this turn started on, or ``None`` outside any turn.

    ``""`` and ``None`` are different answers: the first is a turn that began
    with no tier, the second is no turn scope at all -- a direct-chat dispatch or
    a test rig, where the live policy is the only thing there is to read.
    """
    return _TURN_TIER.get()


def clamp_tier(tier: str, offered: Iterable[str]) -> str | None:
    """The rung to ask this agent for, or ``None`` to leave it on its default.

    Nearest at or below. Where nothing is below, the cheapest rung on offer --
    but only when every rung on offer is one the ladder can rank.

    ``None`` in the three cases where a choice would be a guess: a tier outside
    the ladder (another vocabulary, where "nearest" has no meaning), an agent
    sharing no rung with it at all, and a menu carrying a rung the ladder has no
    word for when the climb is the only move left. That last one is the
    expensive direction to be wrong in -- an agent offering `low`/`max` would
    take a `medium` session to `max`, its dearest rung, picked only because the
    cheaper one is illegible from here.

    A coincidentally shared name is not a fourth case: an exact hit is honoured
    in any vocabulary that spells the rung the same way.
    """
    if tier not in TIER_LADDER:
        return None
    menu = set(offered)
    have = [rung for rung in TIER_LADDER if rung in menu]
    if not have:
        return None
    if tier in have:
        return tier
    below = [rung for rung in have if TIER_LADDER.index(rung) < TIER_LADDER.index(tier)]
    if below:
        return below[-1]
    # Nothing at or below, so the only way to honour the tier is to raise it --
    # and that is only honest when the whole menu is legible. An agent offering a
    # rung the ladder has no word for may well have something cheaper than
    # anything visible here, so picking the cheapest *visible* rung would guess in
    # the expensive direction: raven-code offering `low`/`max` would take a
    # `medium` session to `max`. Leave it on its own default instead.
    if menu - set(TIER_LADDER):
        return None
    return have[0]
