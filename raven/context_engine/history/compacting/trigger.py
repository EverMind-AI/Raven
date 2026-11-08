"""When to compact, and how far down.

Two numbers per turn, both measured against the **raw context window**:
``trigger_tokens``, above which this turn compacts, and ``target_tokens``, the
size it compacts to. Nothing is subtracted from the window first except the
reply headroom -- the policy every history strategy obeys -- which enters as a
cap, not as a reduced base.

That distinction is the whole point. Deducting the model's output ceiling from
the base before applying a percentage charges the fixed overhead twice and
makes the effective trigger *fall* as the window shrinks, which is backwards:
a small window is where compacting late matters most. Measuring against the
raw window is also the majority practice among comparable harnesses; the one
that deducts an output reserve is forced to set its threshold at 50-75%
because 80-90% is unreachable once the reserve is gone.

The ratio itself falls as the window grows, on ``log2``: the incompressible
floor of a prompt is roughly constant, so its share of a large window is small
and the same absolute slack can be bought with a lower percentage. Two
parameters, both readable -- the ratio at the reference window, and how many
points it loses per doubling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from raven.config.raven import ContextCompactionConfig


class CompactionTrigger(StrEnum):
    """Why compaction is being asked for.

    Two causes, not two severities: ``PRESSURE`` is the strategy's own reading
    during assembly, ``OVERFLOW`` is a provider having refused an assembled
    prompt as too long. The second is authoritative about the window in a way
    the first can never be, which is why it is worth telling apart in traces.
    """

    PRESSURE = "pressure"
    OVERFLOW = "overflow"


@dataclass(frozen=True)
class CompactionPlan:
    """One turn's verdict, with the arithmetic that produced it.

    Carried whole into segment metadata: a trigger that fired at the wrong
    size is diagnosable only if the inputs travel with the decision.
    """

    action: Literal["pass", "compact"]
    trigger: CompactionTrigger
    prompt_tokens: int
    trigger_tokens: int
    target_tokens: int
    window: int
    ratio: float
    headroom_clamped: bool
    """True when the reply headroom, not the adaptive ratio, set the trigger.

    Expected on small windows and not a fault: with the default 4096 floor and
    a 0.90 ratio the two cross at 40960, so every window at or below that is
    governed by the floor and the ratio is inert there.
    """

    reclaim_clamped: bool
    """True when the target had to be lowered so one compaction is worth its
    cost -- see ``min_reclaim_tokens``."""


class TriggerPolicy:
    """Turns a window and a prompt size into a :class:`CompactionPlan`."""

    def __init__(self, cfg: "ContextCompactionConfig") -> None:
        self.cfg = cfg

    def ratio(self, window: int) -> float:
        cfg = self.cfg
        if window <= 0 or cfg.trigger_ref_window <= 0:
            return cfg.trigger_ratio_max
        adaptive = cfg.trigger_ratio_max - cfg.trigger_slope * math.log2(window / cfg.trigger_ref_window)
        return min(cfg.trigger_ratio_max, max(cfg.trigger_ratio_min, adaptive))

    def evaluate(
        self,
        *,
        prompt_tokens: int,
        window: int,
        reply_headroom: int,
        trigger: CompactionTrigger = CompactionTrigger.PRESSURE,
    ) -> CompactionPlan:
        """Decide this turn.

        ``reply_headroom`` is passed in rather than looked up: the host already
        computed it for the turn's
        :class:`~raven.contracts.assembled.TokenBudget` (``reserved_output``),
        and one number resolved twice is one number that can disagree with
        itself. It caps the trigger -- a prompt is never allowed up to the
        adaptive line if that would leave less room for the answer than the
        policy holds back.
        """
        cfg = self.cfg
        ratio = self.ratio(window)
        adaptive = int(ratio * window)
        capped = max(1, window - max(0, reply_headroom))
        trigger_tokens = min(adaptive, capped)

        target_tokens = int(cfg.retain_ratio * window)
        reclaim_clamped = trigger_tokens - target_tokens < cfg.min_reclaim_tokens
        if reclaim_clamped:
            target_tokens = max(0, trigger_tokens - cfg.min_reclaim_tokens)

        return CompactionPlan(
            action="compact" if prompt_tokens > trigger_tokens else "pass",
            trigger=trigger,
            prompt_tokens=prompt_tokens,
            trigger_tokens=trigger_tokens,
            target_tokens=target_tokens,
            window=window,
            ratio=ratio,
            headroom_clamped=capped < adaptive,
            reclaim_clamped=reclaim_clamped,
        )


__all__ = ["CompactionPlan", "CompactionTrigger", "TriggerPolicy"]
