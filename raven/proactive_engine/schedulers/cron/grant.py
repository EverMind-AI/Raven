"""The wake-scheduling grant over the cron store, namespaced per holder.

Implements the ``WakeScheduler`` paper (contracts/scheduling.py): every key
is prefixed with the holder's namespace before it reaches the store, so one
plugin's verbs can neither see nor move another plugin's wakes -- nor any
plain reminder, which lives outside the keyed partition entirely. Minted at
bind time by the loop, one instance per contributed tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.proactive_engine.schedulers.cron.types import CronJob


class NamespacedWakeScheduler:
    """``WakeScheduler`` over one cron service, keys prefixed by namespace."""

    def __init__(self, service: "CronService", namespace: str) -> None:
        self._service = service
        self._namespace = namespace

    def _key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    def schedule_wake(
        self,
        key: str,
        at_ms: int,
        message: str,
        *,
        channel: str | None = None,
        to: str | None = None,
        direct_agent: str | None = None,
        direct_handle: str | None = None,
        fire_missed: bool = True,
    ) -> "CronJob":
        return self._service.schedule_wake(
            self._key(key),
            at_ms,
            message,
            channel=channel,
            to=to,
            direct_agent=direct_agent,
            direct_handle=direct_handle,
            fire_missed=fire_missed,
        )

    def advance_wake_to_now(self, key: str) -> bool:
        return self._service.advance_wake_to_now(self._key(key))

    def pending_wakes(self, prefix: str = "") -> "list[CronJob]":
        return self._service.pending_wakes(f"{self._namespace}:{prefix}")

    def cancel_wake(self, key: str) -> bool:
        return self._service.cancel_wake(self._key(key))


__all__ = ["NamespacedWakeScheduler"]
