"""Event ingress for on-call campaigns: turn job completion into an early wake.

The wake table alone gives only the "periodic check" leg -- the agent guesses
an ETA and sleeps it out. This watcher supplies the other leg: *event wake for
terminal states*. A dumb resident loop (no LLM, no decisions, state read fresh
from disk every tick) polls the campaigns with in-flight jobs and, the moment
a round's jobs are all terminal -- or a running job's progress turns unhealthy
(NaN/inf) -- pulls the campaign's pending wake forward to now. The agent then
wakes on the event instead of the timer; its ETA-based wake remains as the
fallback bound.

The fork ran this loop hand-started per host; here it is the plugin's
``services`` contribution (paper: raven/contracts/services.py), and the two
paper disciplines are load-bearing: the watcher **never mutates the host's
assembly** -- it consumes its grant and its own store, nothing else -- and an
error is **stopped loudly, never silently restarted**. A host that lends no
wake scheduler gets the same loudness: an error log at start and a watcher
that stays stopped, because a watcher that is secretly dead is the exact lie
this product exists to prevent.

Probing the jobs themselves belongs to the backends, which arrive with the
part-two tool port; part one carries the seam (:class:`TaskProbe`, the fork's
``JobBackend`` poll surface) and the loop around it. Without a probe factory
the watcher ticks and touches nothing -- honest inertness, not a fake event.
"""

from __future__ import annotations

import asyncio
import math
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Callable, Protocol

from loguru import logger

from oncall_flow import wakes
from oncall_flow.state import (
    LEDGER_FILE,
    META_FILE,
    CampaignStore,
    Ledger,
    TaskHandle,
    TaskStatus,
    is_concluded,
    log_event,
    read_meta,
)

if TYPE_CHECKING:
    from raven.contracts.scheduling import WakeScheduler
    from raven.plugins.context import PluginContext

DEFAULT_POLL_INTERVAL_S = 20.0


class TaskProbe(Protocol):
    """How the watcher asks after a submitted job: the fork's backend poll
    surface, narrowed to the three verbs one tick needs. Part two's backends
    implement it; tests inject fakes."""

    async def poll(self, handle: TaskHandle) -> TaskStatus: ...

    async def fetch_result(self, handle: TaskHandle) -> dict[str, Any]: ...

    async def fetch_progress(self, handle: TaskHandle, tail: int = 1) -> list[dict[str, Any]]: ...


def _unhealthy(sample: dict[str, Any]) -> bool:
    """A progress sample with any non-finite metric value (NaN/inf) is a health
    event: the run is burning compute on a diverged state."""
    for key, value in sample.items():
        if key == "step":
            continue
        if isinstance(value, float) and not math.isfinite(value):
            return True
    return False


class OpsEventWatcher:
    """Resident poll loop that advances a campaign's pending wake on job events.

    Satisfies the :class:`~raven.contracts.services.PluginService` paper:
    ``start(handles)`` holds the host's wake grant and spawns the loop,
    ``stop()`` cancels it and is idempotent. ``tick()`` stays separately
    drivable -- the loop is one caller of it, a test another.
    """

    def __init__(
        self,
        store: CampaignStore,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        probe_from_meta: "Callable[[dict[str, Any]], TaskProbe] | None" = None,
    ) -> None:
        self.store = store
        self.poll_interval = poll_interval
        self._probe_from_meta = probe_from_meta
        self._scheduler: "WakeScheduler | None" = None
        self._task: asyncio.Task | None = None
        self._warned: set[tuple[str, str]] = set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, handles: Any) -> None:
        """Hold the wake grant and spawn the loop; without a grant, stay stopped.

        A missing ``wake_scheduler`` means this host runs no scheduler (the
        grant's own None contract), so there is nothing an event could
        advance: say so at error level and decline to run, rather than tick
        forever with nowhere to deliver.
        """
        if self._task is not None:
            return
        scheduler = getattr(handles, "wake_scheduler", None)
        if scheduler is None:
            logger.error(
                "oncall-flow event watcher: the host lent no wake scheduler "
                "(RuntimeHandles.wake_scheduler is None), so event wakes have "
                "nowhere to land; the watcher stays stopped"
            )
            return
        self._scheduler = scheduler
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the loop; idempotent. The grant held at start stays readable
        so a driver can still call ``tick()`` after the loop is down."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("ops event watcher tick failed: {}: {}", type(exc).__name__, exc)
            await asyncio.sleep(self.poll_interval)

    async def tick(self) -> list[str]:
        """One pass over all campaigns; returns the campaigns whose wake was advanced."""
        advanced: list[str] = []
        if self._scheduler is None:
            return advanced
        for campaign_dir in self.store.campaign_dirs():
            if not (campaign_dir / LEDGER_FILE).exists() or not (campaign_dir / META_FILE).exists():
                continue
            if is_concluded(campaign_dir):
                continue
            try:
                campaign = await self._watch_campaign(campaign_dir)
            except Exception as exc:
                # Best-effort per campaign: an unreachable host must not stall
                # the others. But the first occurrence is logged at warning,
                # because a misconfigured campaign fails identically to a quiet
                # one and only the log distinguishes them; repeats drop to
                # debug so a flapping host does not flood.
                signature = (campaign_dir.name, type(exc).__name__)
                if signature in self._warned:
                    logger.debug("ops watcher skipped {}: {}: {}", campaign_dir.name, type(exc).__name__, exc)
                else:
                    self._warned.add(signature)
                    logger.warning("ops watcher skipped {}: {}: {}", campaign_dir.name, type(exc).__name__, exc)
                continue
            if campaign:
                advanced.append(campaign)
        return advanced

    async def _watch_campaign(self, campaign_dir) -> str | None:
        ledger = Ledger(campaign_dir / LEDGER_FILE)
        pending = [r for r in ledger.all() if not r.is_terminal and r.handle is not None]
        if not pending:
            return None
        campaign = pending[0].campaign or campaign_dir.name
        if self._probe_from_meta is None:
            return None

        # Resolve through the same seam the tools use (the fork's
        # backend_from_meta): hardcoding one backend here made the watcher a
        # silent no-op for every other kind of campaign.
        probe = self._probe_from_meta(read_meta(campaign_dir))

        unhealthy: list[str] = []
        for rec in pending:
            status = await probe.poll(rec.handle)
            if status.is_terminal:
                ledger.set_result(rec.idem_key, status, await probe.fetch_result(rec.handle))
                log_event(campaign_dir, "trial_terminal_observed", trial=rec.idem_key, status=status.value)
            else:
                ledger.set_status(rec.idem_key, status)
                samples = await probe.fetch_progress(rec.handle, tail=1)
                if samples and _unhealthy(samples[-1]):
                    unhealthy.append(rec.idem_key)

        round_done = all(r.is_terminal for r in ledger.all())
        if not (round_done or unhealthy):
            return None
        reason = "round terminal" if round_done else f"unhealthy progress: {', '.join(unhealthy)}"
        if wakes.advance_look(self._scheduler, campaign):
            log_event(campaign_dir, "event_wake_advanced", reason=reason)
            logger.info("ops event watcher: advanced wake for campaign '{}' ({})", campaign, reason)
            return campaign
        # No wake pending (the agent is mid-turn and has not scheduled the
        # next one yet): the next tick retries, so the event is delayed one
        # interval, not lost.
        return None


def make_event_watcher(ctx: "PluginContext") -> OpsEventWatcher | None:
    """Factory for the ``oncall_event_watcher`` service contribution.

    Declines (returns None) when the slice leaves the flow off: no surface is
    cast at all, the fork's gate shape.
    """
    from oncall_flow.config import FlowConfig, state_root

    raw = dict(ctx.config or {})
    cfg = FlowConfig.from_slice(raw)
    if not cfg.enabled:
        return None
    store = CampaignStore(state_root(raw, ctx.services.workspace))
    return OpsEventWatcher(store, poll_interval=cfg.watcher.poll_interval_seconds)


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "OpsEventWatcher",
    "TaskProbe",
    "make_event_watcher",
]
