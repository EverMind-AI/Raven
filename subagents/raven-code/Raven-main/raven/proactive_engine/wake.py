"""Wake scheduler — coalesces wake requests into early heartbeat ticks.

Producers (cron completion, subagent completion, manual triggers) call
``request_wake_now``; the HeartbeatService loop waits on ``wake_event``
instead of a bare sleep, so a wake simply ends the current sleep early.

Design properties:
- No priority lanes: there is a single consumer that drains all pending
  events in one tick, so priorities would have no observable effect.
- No polling: when the agent is busy with user messages the wake is parked
  and re-fired from AgentLoop's turn-complete callback.
- No background task: everything rides on ``loop.call_later`` timers, so
  there is nothing to clean up on shutdown beyond cancelling the timer.
- Rate guard: ``min_interval_s`` spaces consecutive wake fires. Producers
  without their own decay (channel status flaps, delivery failures) can't
  turn the consumer into a hot loop — requests inside the guard window
  keep accumulating reasons and fire once at the window boundary.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from loguru import logger

DEFAULT_COALESCE_S = 0.25


class WakeScheduler:
    def __init__(
        self,
        *,
        is_busy: Callable[[], bool] | None = None,
        coalesce_s: float = DEFAULT_COALESCE_S,
        min_interval_s: float = 0.0,
    ):
        self._is_busy = is_busy or (lambda: False)
        self._coalesce_s = coalesce_s
        self._min_interval_s = min_interval_s
        #: The HeartbeatService loop waits on this event.
        self.wake_event = asyncio.Event()
        self._reasons: list[str] = []
        self._pending_busy = False
        self._timer: asyncio.TimerHandle | None = None
        self._last_fire_at: float | None = None

    def request_wake_now(self, reason: str) -> None:
        """Request an early heartbeat tick. Safe to call repeatedly; requests
        within the coalesce window collapse into a single wake."""
        self._reasons.append(reason)
        if self._timer is None:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(self._coalesce_s, self._fire)

    def _fire(self) -> None:
        self._timer = None
        loop = asyncio.get_running_loop()
        if self._min_interval_s > 0 and self._last_fire_at is not None:
            remaining = self._last_fire_at + self._min_interval_s - loop.time()
            if remaining > 0:
                # Inside the rate-guard window: re-arm to the boundary.
                # Reasons (and the producer's queued events) keep
                # accumulating and are consumed in one tick at the boundary.
                self._timer = loop.call_later(remaining, self._fire)
                logger.debug("wake deferred {:.1f}s: min-interval guard", remaining)
                return
        if self._is_busy():
            # Never compete with in-flight user messages; the turn-complete
            # callback re-fires the wake once the agent is idle.
            self._pending_busy = True
            logger.debug("wake deferred: agent busy")
            return
        self._last_fire_at = loop.time()
        self.wake_event.set()

    def on_turn_complete(self) -> None:
        """AgentLoop callback after each turn (including failed turns):
        re-fire a wake that was parked because the agent was busy."""
        if self._pending_busy:
            self._pending_busy = False
            self.request_wake_now("deferred-after-turn")

    def consume_reasons(self) -> list[str]:
        """Hand the accumulated wake reasons to the consumer (clears them)."""
        reasons, self._reasons = self._reasons, []
        return reasons

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
