"""Wake-driven campaign driver: detach and reconcile on events, not a busy loop.

Instead of ``Campaign.run()`` polling a backend in a tight loop, the driver
reconciles the campaign only when woken -- by an external job-completion signal
or a heartbeat/poll tick. It reuses the proactive engine's wake primitives:

  - ``notify()`` is the producer: it enqueues an ops ``SystemEvent`` and requests
    an early wake. Call it from a job-completion callback (true detach) or from a
    periodic tick when the backend has no callback.
  - ``pump()`` is the wake handler: it runs one ``Campaign.step()`` and acks the
    events it saw, so a failed step leaves them for the next wake.
  - ``serve()`` is a standalone loop that idles on the wake event between
    reconciles (no busy polling).

In the gateway, ``notify`` rides a job-completion callback / the heartbeat
scheduler and ``pump`` is registered as a tick handler, so Ops uses the existing
always-on loop rather than spinning its own.
"""

from __future__ import annotations

from raven.ops.campaign import Campaign
from raven.proactive_engine.system_events import SystemEvent, SystemEventQueue
from raven.proactive_engine.wake import WakeScheduler


class CampaignDriver:
    def __init__(
        self,
        campaign: Campaign,
        *,
        wake: WakeScheduler | None = None,
        events: SystemEventQueue | None = None,
    ) -> None:
        self._campaign = campaign
        self._wake = wake or WakeScheduler()
        self._events = events or SystemEventQueue()

    @property
    def wake(self) -> WakeScheduler:
        return self._wake

    @property
    def events(self) -> SystemEventQueue:
        return self._events

    def notify(self, reason: str) -> None:
        self._events.enqueue(SystemEvent(text=reason, source="ops", context_key=f"ops:{self._campaign.name}"))
        self._wake.request_wake_now(reason)

    async def pump(self) -> bool:
        seen = self._events.peek_all()
        await self._campaign.step()
        self._events.ack(seen)
        return self._campaign.is_done()

    async def serve(self) -> None:
        while not self._campaign.is_done():
            await self._wake.wake_event.wait()
            self._wake.wake_event.clear()
            await self.pump()
