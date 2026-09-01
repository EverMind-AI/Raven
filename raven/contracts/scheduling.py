"""The wake-scheduling grant: keyed one-shot wakes on the host's scheduler.

A plugin that watches something needs to say "wake me about this series at
T", with replace-not-coexist semantics: one key, at most one pending wake,
and a reschedule moves it rather than stacking a second. The grant a holder
receives is namespaced: every key is prefixed with the contributing plugin's
own namespace before it reaches the store, so one plugin's verbs can neither
see nor move another plugin's wakes -- nor any plain reminder, which lives
outside the keyed partition entirely. ``fire_missed`` defaults True: a
watcher's missed look fires late instead of vanishing; a plain reminder's
drop-plus-notice startup rule is untouched by this grant.

The loop mints one instance per contributed tool at bind time
(``RuntimeHandles.wake_scheduler``); ``None`` where the host runs no
scheduler, which a binder treats as a decline.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WakeScheduler(Protocol):
    """Keyed one-shot wakes, namespaced to the holder."""

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
    ) -> Any: ...

    def advance_wake_to_now(self, key: str) -> bool: ...

    def pending_wakes(self, prefix: str = "") -> list[Any]: ...

    def cancel_wake(self, key: str) -> bool: ...


__tier__ = "contract"
__all__ = ["WakeScheduler"]
