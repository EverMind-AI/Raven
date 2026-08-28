"""Event ingress for on-call campaigns: turn job completion into an early wake.

The wake table alone gives only the "periodic check" leg -- the agent guesses an
ETA and sleeps it out. This watcher supplies the other leg from the locked
definition: *event wake for terminal states*. A dumb resident loop (part of the
B layer: no LLM, no decisions, state read fresh from disk every tick) polls the
backend cheaply for campaigns with in-flight jobs and, the moment a round's jobs
are all terminal -- or a running job's progress turns unhealthy (NaN/inf) --
pulls the campaign's pending wake forward to now. The agent then wakes on the
event instead of the timer; its ETA-based wake remains as the fallback bound.

Poll-to-event conversion is deliberate: a bare docker/Slurm host has no push
channel to a laptop, and requiring one (public endpoint, message broker) would
break the zero-infra open-source posture. Cheap dumb polling at the engine
layer, judgment only in the woken turn.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

from loguru import logger

from raven.ops.backends import backend_from_meta
from raven.ops.instrument import log_event
from raven.ops.ledger import Ledger

DEFAULT_POLL_INTERVAL_S = 20.0


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
    """Resident poll loop that advances a campaign's pending wake on job events."""

    def __init__(
        self,
        cron: Any,
        *,
        ops_home: str | Path | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        from raven.ops.instrument import ops_home as default_ops_home

        self._cron = cron
        # Resolved here, not as a default argument: a default is evaluated at
        # import, before ``--config`` is applied, which would pin the watcher to
        # ``~/.raven/ops`` while the rest of the instance moved.
        self._home = Path(ops_home).expanduser() if ops_home else default_ops_home()
        self._poll_interval = poll_interval
        self._warned: set[tuple[str, str]] = set()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("ops event watcher tick failed: {}: {}", type(exc).__name__, exc)
            await asyncio.sleep(self._poll_interval)

    async def tick(self) -> list[str]:
        """One pass over all campaigns; returns the campaigns whose wake was advanced."""
        advanced: list[str] = []
        if not self._home.exists():
            return advanced
        for campaign_dir in sorted(self._home.iterdir()):
            ledger_path = campaign_dir / "ledger.json"
            meta_path = campaign_dir / "meta.json"
            if not ledger_path.exists() or not meta_path.exists():
                continue
            if (campaign_dir / "concluded.json").exists():
                continue
            try:
                campaign = await self._watch_campaign(campaign_dir, ledger_path, meta_path)
            except Exception as exc:
                # Best-effort per campaign: an unreachable host must not stall the
                # others. But the first occurrence is logged at warning, because a
                # misconfigured campaign fails identically to a quiet one and only
                # the log distinguishes them; repeats drop to debug so a flapping
                # host does not flood.
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

    async def _watch_campaign(self, campaign_dir: Path, ledger_path: Path, meta_path: Path) -> str | None:
        ledger = Ledger(ledger_path)
        pending = [r for r in ledger.all() if not r.is_terminal and r.handle is not None]
        if not pending:
            return None
        campaign = pending[0].campaign or campaign_dir.name

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        # Resolve through the same seam the tools use. Hardcoding Docker here made
        # the watcher a silent no-op for every other backend: a process-backend
        # campaign polled as if it were containerised, found nothing, and the
        # failure was swallowed per-campaign -- indistinguishable from "no events
        # happened", which is the shape this file exists to remove.
        backend = backend_from_meta(meta)

        unhealthy: list[str] = []
        for rec in pending:
            status = await backend.poll(rec.handle)
            if status.is_terminal:
                ledger.set_result(rec.idem_key, await backend.fetch_result(rec.handle))
                log_event(campaign_dir, "trial_terminal_observed", trial=rec.idem_key, status=status.value)
            else:
                ledger.set_status(rec.idem_key, status)
                samples = await backend.fetch_progress(rec.handle, tail=1)
                if samples and _unhealthy(samples[-1]):
                    unhealthy.append(rec.idem_key)

        round_done = all(r.is_terminal for r in ledger.all())
        if not (round_done or unhealthy):
            return None
        reason = "round terminal" if round_done else f"unhealthy progress: {', '.join(unhealthy)}"
        if self._advance_wake(campaign):
            log_event(campaign_dir, "event_wake_advanced", reason=reason)
            logger.info("ops event watcher: advanced wake for campaign '{}' ({})", campaign, reason)
            return campaign
        return None

    def _advance_wake(self, campaign: str) -> bool:
        """Pull the campaign's earliest pending wake to now. False if none exists
        (e.g. the agent is mid-turn and has not scheduled the next wake yet --
        the next tick retries, so the event is delayed one interval, not lost)."""
        prefix = f"ops:{campaign}:"
        jobs = [j for j in self._cron.list_jobs() if j.name.startswith(prefix)]
        if not jobs:
            return False
        target = min(jobs, key=lambda j: j.state.next_run_at_ms or float("inf"))
        return bool(self._cron.advance_job_to_now(target.id))
