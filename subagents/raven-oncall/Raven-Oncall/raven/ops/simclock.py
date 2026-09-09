"""Virtual clock for time-compressed on-call evaluation.

An on-call campaign's defining scale is hours to days, which no test suite can
wait out. The clock separates *campaign time* from wall time so a three-day
campaign is evaluated in seconds, deterministically:

  - ``advance()`` jumps campaign time forward by an exact amount -- the mode used
    by evals and tests, with no wall-clock coupling at all (fully reproducible);
  - ``speed_factor`` additionally lets campaign time flow from real time at a
    multiple, for driving a live agent loop under compression (1 real second =
    1 campaign hour), the way SentinelBench compresses its event scripts.

Both modes compose: a live compressed run can still be jumped forward.
"""

from __future__ import annotations

import time
from collections.abc import Callable

_MS_PER = {"seconds": 1_000, "minutes": 60_000, "hours": 3_600_000, "days": 86_400_000}


class SimClock:
    """Campaign time in milliseconds, advanced explicitly and/or scaled from real time."""

    def __init__(
        self,
        *,
        start_ms: int = 0,
        speed_factor: float = 0.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._start_ms = start_ms
        self._speed_factor = speed_factor
        self._advanced_ms = 0
        self._monotonic = monotonic
        self._origin = monotonic()

    @property
    def speed_factor(self) -> float:
        return self._speed_factor

    def now_ms(self) -> int:
        """Campaign time now: explicit advances plus, if a speed factor is set,
        real elapsed time scaled by it."""
        drift = 0
        if self._speed_factor:
            drift = int((self._monotonic() - self._origin) * 1000 * self._speed_factor)
        return self._start_ms + self._advanced_ms + drift

    def advance_ms(self, ms: int) -> int:
        if ms < 0:
            raise ValueError("campaign time cannot move backwards")
        self._advanced_ms += ms
        return self.now_ms()

    def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0, days: float = 0) -> int:
        total = sum(_MS_PER[unit] * value for unit, value in
                    (("seconds", seconds), ("minutes", minutes), ("hours", hours), ("days", days)))
        return self.advance_ms(int(total))

    def real_seconds_for(self, *, campaign_ms: int) -> float:
        """Wall seconds a compressed run needs to cover ``campaign_ms`` of campaign
        time (0.0 in pure-advance mode, where no waiting happens at all)."""
        if not self._speed_factor:
            return 0.0
        return campaign_ms / 1000 / self._speed_factor
