"""Event ingress for on-call campaigns: turn job events into the next look.

The wake table alone gives only the "periodic check" leg -- the agent guesses
an ETA and sleeps it out. This watcher supplies the other leg: *event wake for
terminal states*. A dumb resident loop (no LLM, no judgement calls, state read
fresh from disk every tick) polls the campaigns with in-flight jobs through
the same backend seam the tools use, and acts through the wake grant:

- a round gone all-terminal -- or a running job's progress turned unhealthy
  (NaN/inf) -- pulls the campaign's pending wake forward to now; when none is
  pending (the agent crashed mid-turn, or never scheduled) and the campaign
  declared a wake route, the watcher schedules the look itself, so a due
  round is not stranded on an agent that never came back;
- a failed trial whose retry budget is exhausted escalates, at most once per
  trial across crashes (the ledger's ``escalated`` flag, the fork's
  Campaign order): the escalation is itself a wake carrying the failure
  context -- the agent decides, and a person is only ever reached through
  the ask tool's contract guard (D4);
- a concluded campaign's pending wake is cancelled: a watch that was closed
  must not come back.

Everything it decides is mechanical (terminal? unhealthy? retries left?);
what to DO about a result stays in the woken turn. The fork ran this loop
hand-started per host; here it is the plugin's ``services`` contribution
(paper: raven/contracts/services.py), and the two paper disciplines are
load-bearing: the watcher **never mutates the host's assembly** -- it
consumes its grant and its own store, nothing else -- and an error is
**stopped loudly, never silently restarted**. A host that lends no wake
scheduler gets the same loudness: an error log at start and a watcher that
stays stopped, because a watcher that is secretly dead is the exact lie this
product exists to prevent.
"""

from __future__ import annotations

import asyncio
import math
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from loguru import logger

from oncall_flow import wakes
from oncall_flow.backend import JobHandle, JobResult, JobStatus
from oncall_flow.instrument import (
    LEDGER_FILE,
    META_FILE,
    CampaignStore,
    is_concluded,
    log_event,
    read_meta,
)
from oncall_flow.ledger import Ledger
from oncall_flow.policy import attempt_key, attempt_no, base_trial
from oncall_flow.policy import from_meta as policy_from_meta

if TYPE_CHECKING:
    from raven.contracts.scheduling import WakeScheduler
    from raven.plugins.context import PluginContext

DEFAULT_POLL_INTERVAL_S = 20.0


class TaskProbe(Protocol):
    """How the watcher asks after a submitted job: the fork's ``JobBackend``
    poll surface, narrowed to the three verbs one tick needs. Every concrete
    backend (docker/process/openfoam/mock) satisfies it; tests inject fakes."""

    async def poll(self, handle: JobHandle) -> JobStatus: ...

    async def fetch_result(self, handle: JobHandle) -> JobResult: ...

    async def fetch_progress(self, handle: JobHandle, tail: int = 1) -> list[dict[str, Any]]: ...


def _unhealthy(sample: dict[str, Any]) -> bool:
    """A progress sample with any non-finite metric value (NaN/inf) is a health
    event: the run is burning compute on a diverged state."""
    for key, value in sample.items():
        if key == "step":
            continue
        if isinstance(value, float) and not math.isfinite(value):
            return True
    return False


def _campaign_name(campaign_dir: Path, ledger: Ledger, meta: dict[str, Any]) -> str:
    """The wake key for this campaign: what its records call it, else what its
    declaration calls it, else the directory (already the slug)."""
    for rec in ledger.all():
        if rec.campaign:
            return rec.campaign
    declared = meta.get("campaign")
    return str(declared) if declared else campaign_dir.name


class OpsEventWatcher:
    """Resident poll loop that turns job events into wake-grant acts.

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
        """One pass over all campaigns; returns the campaigns acted on (a wake
        advanced, scheduled, or cancelled)."""
        acted: list[str] = []
        if self._scheduler is None:
            return acted
        for campaign_dir in self.store.campaign_dirs():
            if not (campaign_dir / LEDGER_FILE).exists() or not (campaign_dir / META_FILE).exists():
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
                acted.append(campaign)
        return acted

    async def _watch_campaign(self, campaign_dir: Path) -> str | None:
        ledger = Ledger(campaign_dir / LEDGER_FILE)
        meta = read_meta(campaign_dir)
        campaign = _campaign_name(campaign_dir, ledger, meta)

        if is_concluded(campaign_dir):
            # A concluded campaign must not come back: stand its one pending
            # wake down (the ops scheduling tools refuse to schedule new ones) and
            # probe nothing.
            if wakes.cancel_look(self._scheduler, campaign):
                log_event(campaign_dir, "wake_cancelled", reason="campaign concluded")
                logger.info("ops event watcher: cancelled wake for concluded campaign '{}'", campaign)
                return campaign
            return None

        pending = [r for r in ledger.all() if not r.is_terminal and r.handle is not None]
        if not pending:
            return None
        if self._probe_from_meta is None:
            return None

        # Resolve through the same seam the tools use (the fork's
        # backend_from_meta): hardcoding one backend here made the watcher a
        # silent no-op for every other kind of campaign.
        probe = self._probe_from_meta(meta)

        unhealthy: list[str] = []
        for rec in pending:
            status = await probe.poll(rec.handle)
            if status.is_terminal:
                ledger.set_result(rec.idem_key, await probe.fetch_result(rec.handle))
                log_event(campaign_dir, "trial_terminal_observed", trial=rec.idem_key, status=status.value)
            else:
                ledger.set_status(rec.idem_key, status)
                samples = await probe.fetch_progress(rec.handle, tail=1)
                if samples and _unhealthy(samples[-1]):
                    unhealthy.append(rec.idem_key)

        acted = self._escalate_exhausted(campaign, campaign_dir, meta, ledger)

        round_done = all(r.is_terminal for r in ledger.all())
        if not acted and (round_done or unhealthy):
            # The escalation wake, when one was raised, already is the look.
            reason = "round terminal" if round_done else f"unhealthy progress: {', '.join(unhealthy)}"
            message = wakes.recheck_message(campaign=campaign, ledger=str(campaign_dir / LEDGER_FILE))
            acted = self._raise_look(campaign, campaign_dir, meta, message=message, reason=reason)
        return campaign if acted else None

    def _escalate_exhausted(self, campaign: str, campaign_dir: Path, meta: dict[str, Any], ledger: Ledger) -> bool:
        """Escalate-once for failed trials whose retry budget is spent.

        The fork's ``Campaign._maybe_escalate`` said in watcher acts: the flag
        is persisted first (at-most-once across crashes, the fork's order),
        the trail records it, and the escalation itself is a wake carrying the
        failure context -- the agent decides, and a person is only reached
        through ops_ask_owner's contract guard (D4). A failed attempt whose
        policy still allows a retry is not escalated: the round-terminal wake
        hands the resubmit decision to the agent instead.
        """
        policy = policy_from_meta(meta)
        raised = False
        for rec in ledger.all():
            if rec.status is not JobStatus.FAILED or rec.escalated:
                continue
            attempt = attempt_no(rec.idem_key)
            if policy.should_retry(attempt):
                continue
            if ledger.has(attempt_key(base_trial(rec.idem_key), attempt + 1)):
                # The chain already moved on; the live attempt answers for it.
                continue
            ledger.mark_escalated(rec.idem_key)
            error = rec.result.error if rec.result else None
            log_event(campaign_dir, "escalated", trial=rec.idem_key, error=error)
            message = wakes.escalation_message(
                campaign=campaign,
                trial=rec.idem_key,
                error=error or "",
                attempt=attempt,
                ledger=str(campaign_dir / LEDGER_FILE),
            )
            if self._raise_look(
                campaign,
                campaign_dir,
                meta,
                message=message,
                reason=f"escalation: {rec.idem_key}",
                replace=True,
            ):
                raised = True
        return raised

    def _raise_look(
        self,
        campaign: str,
        campaign_dir: Path,
        meta: dict[str, Any],
        *,
        message: str,
        reason: str,
        replace: bool = False,
    ) -> bool:
        """Bring the campaign's next look to now, through the grant.

        ``replace=False`` advances the agent's own pending wake and only
        schedules when none is pending -- the event path: the agent's message
        (usually the round-due one, with the round numbers only it knows) is
        the better cold-start context. ``replace=True`` schedules first so
        the wake carries this event's context -- the escalation path, the
        fork's last-request-wins discipline. Scheduling needs the campaign's
        declared route (``wakes.wake_route``); without one the event waits
        for the pending wake or the next tick -- delayed, not lost.
        """
        order = ("schedule", "advance") if replace else ("advance", "schedule")
        for act in order:
            if act == "advance":
                if wakes.advance_look(self._scheduler, campaign):
                    log_event(campaign_dir, "event_wake_advanced", reason=reason)
                    logger.info("ops event watcher: advanced wake for campaign '{}' ({})", campaign, reason)
                    return True
            else:
                route = wakes.wake_route(meta)
                if not route:
                    continue
                note = wakes.schedule_next_look(
                    self._scheduler, campaign=campaign, eta_seconds=1, message=message, **route
                )
                if note.startswith("Scheduled"):
                    log_event(campaign_dir, "wake_scheduled", reason=reason)
                    logger.info("ops event watcher: scheduled a look for campaign '{}' ({})", campaign, reason)
                    return True
        return False


def make_event_watcher(ctx: "PluginContext") -> OpsEventWatcher | None:
    """Factory for the ``oncall_event_watcher`` service contribution.

    Declines (returns None) when the slice leaves the flow off: no surface is
    cast at all, the fork's gate shape. The probe seam is wired to the real
    backend registry here -- the same resolution the part-2b tools use.
    """
    from oncall_flow.backends import backend_from_meta
    from oncall_flow.config import FlowConfig, state_root

    raw = dict(ctx.config or {})
    cfg = FlowConfig.from_slice(raw)
    if not cfg.enabled:
        return None
    store = CampaignStore(state_root(raw, ctx.services.workspace))
    return OpsEventWatcher(
        store,
        poll_interval=cfg.watcher.poll_interval_seconds,
        probe_from_meta=backend_from_meta,
    )


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "OpsEventWatcher",
    "TaskProbe",
    "make_event_watcher",
]
