"""Wall-clock budget awareness for the agent loop.

An agent running under an external deadline (benchmark harness, cron slot) is
otherwise blind to it: it cannot decide to start a long build now instead of
deliberating, because nothing tells it time is passing. When a deadline is
provided, this emits a short reminder line the first time consumption crosses
each threshold; the loop appends it to the latest tool result.

Pure decision logic, no I/O, mirroring recovery.py.
"""

from __future__ import annotations

import os

DEADLINE_ENV = "RAVEN_TASK_DEADLINE_EPOCH"

_DEFAULT_THRESHOLDS = (0.5, 0.75, 0.9)


class TimeBudgetReminder:
    """Emits one reminder per crossed budget threshold."""

    def __init__(self, deadline_epoch: float, start_epoch: float):
        self.deadline = deadline_epoch
        self.start = start_epoch
        self._pending = [t for t in _DEFAULT_THRESHOLDS if deadline_epoch > start_epoch]

    @classmethod
    def from_env(cls, now: float) -> "TimeBudgetReminder | None":
        raw = os.environ.get(DEADLINE_ENV)
        if not raw:
            return None
        try:
            deadline = float(raw)
        except ValueError:
            return None
        if deadline <= now:
            return None
        return cls(deadline, now)

    def poll(self, now: float) -> str | None:
        """Return a reminder line when a new threshold was crossed, else None."""
        if not self._pending:
            return None
        total = self.deadline - self.start
        if total <= 0:
            return None
        frac = (now - self.start) / total
        crossed = False
        while self._pending and frac >= self._pending[0]:
            self._pending.pop(0)
            crossed = True
        if not crossed:
            return None
        remaining_min = max(0, int((self.deadline - now) / 60))
        return (
            f"[time budget] About {remaining_min} minutes of wall-clock remain for "
            "this task. If long-running work (builds, training, downloads, servers) "
            "is still needed, start it now and verify while it runs; keep further "
            "deliberation short."
        )
