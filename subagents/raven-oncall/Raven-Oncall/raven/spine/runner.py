"""The behavioural seam between spine and the agent: how one turn runs.

spine defines the ``TurnRunner`` protocol and the agent loop implements it;
spine never imports the agent (dependency inversion). ``emit`` is narrowed to
``RunnerEvent`` so a runner cannot emit lifecycle events — the worker owns
those. With no static checker in this repo that narrowing is intent only; the
enforcing guard lives at the scheduler's emit boundary.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from raven.spine.events import RunnerEvent, Usage
from raven.spine.turn import TurnRequest

Emit = Callable[[RunnerEvent], Awaitable[None]]
# Read-and-remove this lane's pending injects, to merge at a tool-loop gap. The
# runner may ignore it (the minimal-legal implementation never drains, so every
# inject falls back to an APPEND turn). Synchronous: draining is a deque read.
Drain = Callable[[], list[TurnRequest]]


@dataclass(frozen=True)
class UsageTotals:
    """Every LLM call the turn made, summed at the provider boundary.

    Separate from ``TurnOutcome.usage`` rather than replacing it: ``usage`` has
    always carried the turn's *final* call, and numbers published from it exist.
    Redefining it in place would put "final call" and "turn total" in the same
    column when old and new runs are read together, so the old field keeps its
    old meaning and the sum arrives under a new name.

    ``calls`` is the denominator: a total is only as trustworthy as the number of
    calls it was summed over, and a call whose response carried no usage at all
    is counted in ``calls_without_usage`` rather than passed over -- otherwise a
    provider that stops reporting usage looks like a cheap turn.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    calls_without_usage: int = 0


@dataclass(frozen=True)
class TurnOutcome:
    """What a finished run hands back; the worker fills TurnEnded from it."""

    usage: Usage
    explicit_reply: bool
    # The turn's summed usage. Optional so every existing construction site keeps
    # working; a runner that does not fill it reports zero calls, which reads as
    # "not measured" rather than as "no tokens spent".
    usage_totals: UsageTotals = UsageTotals()


@runtime_checkable
class TurnRunner(Protocol):
    async def run(self, req: TurnRequest, emit: Emit, drain: Drain) -> TurnOutcome: ...
