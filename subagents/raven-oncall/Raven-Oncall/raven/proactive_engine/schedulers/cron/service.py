"""Cron service for scheduling agent tasks."""

import asyncio
import json
import os
import sys
import time

try:
    import fcntl
except ImportError:
    fcntl = None
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Iterator

from loguru import logger

from raven.proactive_engine.schedulers.cron.types import CronJob, CronJobState, CronPayload, CronSchedule, CronStore

# Stale-claim TTL — if a claim is older than this, another process may steal
# it (the original process likely crashed mid-job).
_CLAIM_TTL_MS = 30 * 60 * 1000
# An on-call wake is one-shot: a turn that neither acts on its campaign nor
# schedules the next look would end the watch. Re-arm a bounded number of
# times -- unbounded retry is its own silent failure.
_MAX_OPS_WAKE_REARMS = 3
_OPS_REARM_DELAY_MS = 600_000


def _pid_alive(pid: int) -> bool:
    """Whether a claiming process is still running on this host.

    The cron store is per-instance and local, so every claimer is a local pid.
    Signal 0 checks existence without delivering anything; EPERM means it exists
    and belongs to someone else.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# Cap the sleep-until-next-wake so _on_timer runs at least this often. This
# is how we pick up jobs written to jobs.json by a peer process — _on_timer
# reloads the store on mtime change. Without this cap, a gateway armed for
# a far-future wake would miss a sooner job added by REPL.
_MAX_WAKE_INTERVAL_S = 30.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter

            # Use caller-provided reference time for deterministic scheduling
            base_time = now_ms / 1000
            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


def _is_system_wake(job: "CronJob") -> bool:
    """A campaign's own wake, not a user reminder.

    Such a wake is added with ``dedup=False``, which keeps it from being merged
    INTO an existing job -- but nothing kept it from being returned as the
    "existing duplicate" of a later reminder. Measured 2026-08-06 on the CFD line:
    a turn asked for a check 28 seconds away from its campaign's wake, got that
    wake back, read the unfamiliar name as a duplicate of its own request, and
    removed it. The run finished that night with nothing scheduled to look at it.

    A reminder and a campaign wake are different things even when they fire in the
    same minute, so one must never stand in for the other.
    """
    payload = getattr(job, "payload", None)
    if payload is not None and getattr(payload, "campaign", None):
        return True
    # Name prefix stays as a fallback for jobs written before the field existed.
    return str(getattr(job, "name", "")).startswith("ops:")


def _validate_schedule_for_add(schedule: CronSchedule, now_ms: int) -> None:
    """Validate schedule fields that would otherwise create non-runnable jobs."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")

    if schedule.kind == "cron" and schedule.tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(schedule.tz)
        except Exception:
            raise ValueError(f"unknown timezone '{schedule.tz}'") from None

    # A schedule with no next run would be stored as a job that silently never
    # fires (a false success to the caller). _compute_next_run is the single
    # source of truth for "runnable", so reject any kind it maps to None here.
    if _compute_next_run(schedule, now_ms) is None:
        if schedule.kind == "at":
            raise ValueError("at time is in the past")
        if schedule.kind == "every":
            raise ValueError("every_seconds must be positive")
        if schedule.kind == "cron":
            raise ValueError(f"invalid cron expression '{schedule.expr}'")
        raise ValueError(f"schedule kind '{schedule.kind}' is not runnable")


class CronService:
    """Service for managing and executing scheduled jobs."""

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
        *,
        allowed_channels: set[str] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        """``allowed_channels`` restricts which jobs this service will claim.

        Set to e.g. ``{"cli"}`` in REPL mode so REPL doesn't steal Feishu /
        Telegram reminders that gateway should deliver. ``None`` (default)
        means any channel — use that in gateway where ChannelManager can
        route replies to any configured channel.

        Jobs with empty/None ``payload.channel`` are always claimable —
        they predate the channel attribution field.
        """
        self.store_path = store_path
        # Sibling file for fcntl advisory locking (survives atomic rename of
        # the data file, lets concurrent processes coordinate _on_timer).
        self.lock_path = store_path.with_suffix(store_path.suffix + ".lock")
        self.on_job = on_job
        self.allowed_channels = allowed_channels
        self._store: CronStore | None = None
        # Nanosecond precision: float st_mtime collapses writes ~238ns apart
        # into one value, serving a stale cache after an external rewrite.
        self._last_mtime: int = 0
        self._timer_task: asyncio.Task | None = None
        self._tick_executing = False
        self._running = False
        # Remember whether fcntl is usable — degrade to lock-less on Windows.
        self._can_lock = sys.platform != "win32"
        # Optional fake-clock injection for benchmark harnesses (longrun).
        # When provided, all internal time reads route through this callable
        # so newly created jobs' next_run_at_ms aligns with simulated time
        # rather than real wall-clock.
        self._now_fn = now_fn

    def _now_ms(self) -> int:
        """Return current time in ms, honouring fake-clock injection."""
        if self._now_fn is not None:
            return int(self._now_fn().timestamp() * 1000)
        return int(time.time() * 1000)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Exclusive advisory lock on the jobs-file sibling. No-op on Windows."""
        if not self._can_lock:
            yield
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock_fd:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)

    def _load_store(self) -> CronStore:
        """Load jobs from disk. Reloads automatically if file was modified externally."""
        if self._store and self.store_path.exists():
            mtime = self.store_path.stat().st_mtime_ns
            if mtime != self._last_mtime:
                logger.info("Cron: jobs.json modified externally, reloading")
                self._store = None
        if self._store:
            return self._store

        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                jobs = []
                for j in data.get("jobs", []):
                    jobs.append(
                        CronJob(
                            id=j["id"],
                            name=j["name"],
                            enabled=j.get("enabled", True),
                            schedule=CronSchedule(
                                kind=j["schedule"]["kind"],
                                at_ms=j["schedule"].get("atMs"),
                                every_ms=j["schedule"].get("everyMs"),
                                expr=j["schedule"].get("expr"),
                                tz=j["schedule"].get("tz"),
                            ),
                            payload=CronPayload(
                                kind=j["payload"].get("kind", "agent_turn"),
                                message=j["payload"].get("message", ""),
                                deliver=j["payload"].get("deliver", False),
                                channel=j["payload"].get("channel"),
                                to=j["payload"].get("to"),
                                topic_tag=j["payload"].get("topicTag"),
                                campaign=j["payload"].get("campaign"),
                                owner=j["payload"].get("owner"),
                                owner_pid=j["payload"].get("ownerPid"),
                            ),
                            state=CronJobState(
                                next_run_at_ms=j.get("state", {}).get("nextRunAtMs"),
                                last_run_at_ms=j.get("state", {}).get("lastRunAtMs"),
                                last_status=j.get("state", {}).get("lastStatus"),
                                last_error=j.get("state", {}).get("lastError"),
                                claimed_by_pid=j.get("state", {}).get("claimedByPid"),
                                claimed_at_ms=j.get("state", {}).get("claimedAtMs"),
                                silent_fire_count=j.get("state", {}).get("silentFireCount", 0),
                            ),
                            created_at_ms=j.get("createdAtMs", 0),
                            updated_at_ms=j.get("updatedAtMs", 0),
                            delete_after_run=j.get("deleteAfterRun", False),
                            silent_fire_limit=j.get("silentFireLimit", 12),
                        )
                    )
                self._store = CronStore(jobs=jobs)
            except Exception as e:
                logger.warning("Failed to load cron store: {}", e)
                self._store = CronStore()
        else:
            self._store = CronStore()

        return self._store

    def _save_store(self) -> None:
        """Save jobs to disk."""
        if not self._store:
            return

        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": self._store.version,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule": {
                        "kind": j.schedule.kind,
                        "atMs": j.schedule.at_ms,
                        "everyMs": j.schedule.every_ms,
                        "expr": j.schedule.expr,
                        "tz": j.schedule.tz,
                    },
                    "payload": {
                        "kind": j.payload.kind,
                        "message": j.payload.message,
                        "deliver": j.payload.deliver,
                        "channel": j.payload.channel,
                        "to": j.payload.to,
                        "topicTag": j.payload.topic_tag,
                        # Round-trips, or the invariant it keys silently stops
                        # holding: add_job reloads the store first, so a field that
                        # does not survive a write/read cycle is gone by the time
                        # the next producer is checked against it.
                        "campaign": j.payload.campaign,
                        "owner": j.payload.owner,
                        "ownerPid": j.payload.owner_pid,
                    },
                    "state": {
                        "nextRunAtMs": j.state.next_run_at_ms,
                        "lastRunAtMs": j.state.last_run_at_ms,
                        "lastStatus": j.state.last_status,
                        "lastError": j.state.last_error,
                        "claimedByPid": j.state.claimed_by_pid,
                        "claimedAtMs": j.state.claimed_at_ms,
                        "silentFireCount": j.state.silent_fire_count,
                    },
                    "createdAtMs": j.created_at_ms,
                    "updatedAtMs": j.updated_at_ms,
                    "deleteAfterRun": j.delete_after_run,
                    "silentFireLimit": j.silent_fire_limit,
                }
                for j in self._store.jobs
            ],
        }

        # Atomic write (temp + rename) so concurrent readers never see a
        # partially-flushed file.
        tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.store_path)
        self._last_mtime = self.store_path.stat().st_mtime_ns

    async def start(self) -> None:
        """Start the cron service."""
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        logger.info("Cron service started with {} jobs", len(self._store.jobs if self._store else []))

    def stop(self) -> None:
        """Stop the cron service."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs.

        Past-due one-shot 'at' reminders are dropped — we don't re-deliver
        reminders missed while the service was down (matches iOS /
        Slack / Google Calendar behavior). A warning log records each
        drop so users can audit via gateway logs.

        Recurring ('every', 'cron') jobs just advance to the next future
        run — missed intervals are skipped, not backfilled.

        A past-due job still held by a live peer claim is NOT dropped: the
        peer may be mid-execution, and this recompute runs on *every*
        process start, so any incidental raven invocation would otherwise
        delete work another process is running. Liveness uses the same
        claim TTL as _on_timer, and erring toward keeping is deliberate --
        a stale claim only delays the drop by one TTL, whereas an
        erroneous drop destroys an in-flight turn.
        """
        if not self._store:
            return
        now = self._now_ms()
        dropped: list[str] = []
        kept = []
        for job in self._store.jobs:
            if not job.enabled:
                kept.append(job)
                continue
            next_run = _compute_next_run(job.schedule, now)
            if job.schedule.kind == "at" and next_run is None:
                if self._claim_is_live(job, now):
                    logger.info(
                        "Cron: keeping past-due one-shot {!r} (id={}) held by live claim pid={}",
                        job.name,
                        job.id,
                        job.state.claimed_by_pid,
                    )
                    kept.append(job)
                    continue
                stale_ms = now - (job.schedule.at_ms or 0)
                dropped.append(f"{job.name!r} (id={job.id}, {stale_ms // 1000}s late)")
                continue
            job.state.next_run_at_ms = next_run
            kept.append(job)
        self._store.jobs = kept
        if dropped:
            logger.warning(
                "Cron: dropped {} past-due one-shot reminder(s) on startup: {}",
                len(dropped),
                "; ".join(dropped),
            )

    @staticmethod
    def _owner_is_elsewhere(job: CronJob, my_pid: int) -> bool:
        """Whether this wake belongs to a window that is not this process.

        A wake with an owner is that owner's alone, open or closed. When the
        window is closed the wake stays pending until a window takes the
        campaign over by name, which rewrites the owner; it is never handed to
        whoever happens to be open. Measured 2026-08-14: two closed windows'
        wakes ran inside a third window, read a different experiment's ledger
        and submitted jobs, and removing them did not stop it because each turn
        armed the next one.

        An absent owner is every job written before the field existed, and those
        must keep running.

        The campaign's current window outranks the owner recorded when the wake
        was written: that is how a takeover works. A window says which campaign
        it is watching by naming it, which writes the binding, and the pending
        wake follows without anyone rewriting the job. When no window is bound,
        the recorded owner decides -- and if that window is closed, nobody runs
        it.
        """
        campaign = getattr(job.payload, "campaign", None)
        if campaign:
            try:
                from raven.config.paths import get_ops_home
                from raven.ops.window import window_for_campaign

                bound = window_for_campaign(get_ops_home(), str(campaign))
            except Exception:  # noqa: BLE001 -- an unreadable index costs the takeover, not the tick
                bound = None
            if bound and bound[1]:
                return bound[1] != my_pid
        owner_pid = getattr(job.payload, "owner_pid", None)
        return bool(owner_pid) and owner_pid != my_pid

    @staticmethod
    def _claim_is_live(job: CronJob, now_ms: int) -> bool:
        """Whether another process currently holds an unexpired claim on job."""
        pid = job.state.claimed_by_pid
        claimed_at = job.state.claimed_at_ms
        if pid is not None and not _pid_alive(pid):
            # A claim whose holder is gone is not a claim. Without this, closing
            # the window that held one froze its campaign for the whole TTL --
            # and the point of keeping the campaign's state on disk is that
            # another window can take the wake over, which "in half an hour" is
            # not. A reused pid reads as alive, which errs toward waiting out the
            # TTL rather than running a turn twice.
            return False
        if pid is None or claimed_at is None:
            return False
        if pid == os.getpid():
            return False
        return (now_ms - claimed_at) < _CLAIM_TTL_MS

    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        if not self._store:
            return None
        times = [j.state.next_run_at_ms for j in self._store.jobs if j.enabled and j.state.next_run_at_ms]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        """Schedule the next timer tick.

        Always sleeps at most ``_MAX_WAKE_INTERVAL_S`` so a peer process's
        write to jobs.json (e.g. a new reminder from REPL while gateway is
        running) gets picked up within that window — _on_timer reloads on
        mtime change.

        While a tick is EXECUTING jobs, re-arming is a no-op: the executing
        tick re-arms itself when it finishes (execution is serial anyway, so
        the new job cannot run sooner regardless). Cancelling it here — which
        add_job does when a job's own turn schedules a follow-up (the Ops
        campaign wake path) — would kill the in-flight ``on_job`` mid-await:
        its delivery fan-out and post-run cleanup silently never run.
        """
        if self._tick_executing:
            return
        if self._timer_task:
            self._timer_task.cancel()

        if not self._running:
            return

        next_wake = self._get_next_wake_ms()
        if next_wake:
            delay_s = max(0.0, (next_wake - _now_ms()) / 1000)
        else:
            # No pending job — still poll for new writes.
            delay_s = _MAX_WAKE_INTERVAL_S
        delay_s = min(delay_s, _MAX_WAKE_INTERVAL_S)

        async def tick():
            await asyncio.sleep(delay_s)
            if not self._running:
                return
            self._tick_executing = True
            try:
                await self._on_timer()
            finally:
                self._tick_executing = False
                if self._running:
                    self._arm_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        """Handle timer tick - run due jobs.

        Claim phase (under exclusive lock): reload from disk, pick due jobs
        not already claimed by a live peer, stamp them with this pid+now,
        save. Execution phase (lock released): run each claimed job; then
        reacquire the lock to write post-run state and clear the claim.
        """
        my_pid = os.getpid()
        with self._locked():
            # Force reread — peer process may have mutated in the meantime.
            self._store = None
            self._load_store()
            if not self._store:
                return

            now = self._now_ms()
            my_jobs: list[CronJob] = []
            for j in self._store.jobs:
                if not (j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms):
                    continue
                # Channel routing: if the caller set an allow-list, only
                # claim jobs whose channel is in it. Jobs created before
                # channel attribution existed (empty/None channel) remain
                # claimable by any process for backwards compat.
                if self.allowed_channels is not None and j.payload.channel:
                    if j.payload.channel not in self.allowed_channels:
                        continue
                # Window routing, one step finer than channel. Every TUI window is
                # channel "tui" to "default", so channel alone cannot say which of
                # them a campaign's wake belongs to -- and running it in the wrong
                # one puts the turn in front of a different experiment's
                # conversation, while looking entirely normal.
                #
                # A wake belongs to one window and to no other. When that window
                # is closed the wake waits rather than moving: it stays pending,
                # and a window that wants it says so by naming the campaign,
                # which rebinds it here. Handing it to whoever is open instead
                # was measured on 2026-08-14 -- two closed windows' wakes ran in
                # the windows of a different experiment, read its ledger and
                # submitted jobs, and deleting them did not stop it because each
                # turn armed the next one.
                # Asked by pid, not by session key: "is that window still there"
                # is "is the process that owns that session still running", and a
                # pid is the one handle every process can check about another. The
                # session key rides along as the label the trail reads by. An
                # empty owner is every job written before this field.
                if self._owner_is_elsewhere(j, my_pid):
                    continue
                # Skip if a live peer already has it.
                cb = j.state.claimed_by_pid
                ca = j.state.claimed_at_ms
                if cb is not None and cb != my_pid and _pid_alive(cb) and ca is not None and (now - ca) < _CLAIM_TTL_MS:
                    continue
                j.state.claimed_by_pid = my_pid
                j.state.claimed_at_ms = now
                my_jobs.append(j)
            if my_jobs:
                self._save_store()

        for job in my_jobs:
            await self._execute_job(job)
            # Post-run flush + clear claim, under lock so concurrent reader
            # observes the complete updated job record.
            with self._locked():
                # Reload + patch our job in case peer wrote intervening state.
                self._store = None
                self._load_store()
                if self._store is None:
                    continue
                for j in self._store.jobs:
                    if j.id == job.id and j.state.claimed_by_pid == my_pid:
                        j.state.claimed_by_pid = None
                        j.state.claimed_at_ms = None
                        j.state.last_run_at_ms = job.state.last_run_at_ms
                        j.state.last_status = job.state.last_status
                        j.state.last_error = job.state.last_error
                        j.state.next_run_at_ms = job.state.next_run_at_ms
                        j.enabled = job.enabled
                        j.updated_at_ms = job.updated_at_ms
                        break
                # Handle "at"-kind delete_after_run (_execute_job removed from
                # our local store; reflect on the reloaded store).
                if job.schedule.kind == "at" and job.delete_after_run:
                    self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
                self._save_store()

        self._arm_timer()

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job."""
        start_ms = self._now_ms()
        logger.info("Cron: executing job '{}' ({})", job.name, job.id)

        try:
            if self.on_job:
                await self.on_job(self._with_campaign_handles(job))

            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info("Cron: job '{}' completed", job.name)

        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error("Cron: job '{}' failed: {}", job.name, e)

        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = self._now_ms()

        # Handle one-shot jobs
        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        elif job.enabled:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, self._now_ms())
        else:
            # Recurring job that was force-fired while disabled (CLI
            # `cron run --force`). Don't advance next_run_at_ms — the
            # job is still disabled, and a future-dated next-run combined
            # with enabled=False would mislead `cron list` output.
            job.state.next_run_at_ms = None

        if getattr(job.payload, "campaign", None) or job.name.startswith("ops:"):
            self._keep_ops_campaign_watched(job, start_ms)

    def _with_campaign_handles(self, job: CronJob) -> CronJob:
        """Append the on-call handles to a wake message that lacks them.

        A wake turn starts from an empty history, so the message is the whole of
        what it has. Measured 2026-08-06, one arrived as "check the embedding
        fine-tuning progress - round one should be done": it names no campaign and
        no ledger, and the turn went hunting for the host over ssh instead of
        reading the campaign it was woken for.

        Two sources, both read at fire time rather than trusted from creation: the
        job name when a producer followed the ops:<campaign>: convention, and the
        one campaign with a trial still in flight otherwise. With two such
        campaigns nothing is appended -- naming the wrong one reads exactly like
        naming the right one, and the turn would drive somebody else's experiment.

        The enrichment is on the copy handed to the callback, not on the stored
        job: a recurring job would otherwise accumulate one block per fire.
        """
        try:
            from dataclasses import replace

            from raven.ops.instrument import campaign_dir, ops_home
            from raven.ops.ledger import Ledger

            campaign = None
            parts = job.name.split(":")
            if len(parts) >= 3 and parts[0] == "ops" and parts[1]:
                campaign = parts[1] if campaign_dir(parts[1]).exists() else None
            else:
                home = ops_home()
                in_flight = []
                for cdir in sorted(d for d in home.iterdir() if d.is_dir()):
                    if (cdir / "concluded.json").exists() or not (cdir / "meta.json").is_file():
                        continue
                    ledger = cdir / "ledger.json"
                    if ledger.is_file() and Ledger(ledger).pending():
                        in_flight.append(cdir.name)
                campaign = in_flight[0] if len(in_flight) == 1 else None
            if campaign is None:
                return job

            ledger_path = str(campaign_dir(campaign) / "ledger.json")
            message = job.payload.message or ""
            if ledger_path in message:
                return job
            handles = (
                f"\n\nOn-call campaign: {campaign}\n"
                f"Ledger: {ledger_path}\n"
                f"Call ops_tune_status(ledger='{ledger_path}') before deciding anything."
            )
            return replace(job, payload=replace(job.payload, message=message + handles))
        except Exception:
            return job

    def _keep_ops_campaign_watched(self, job: CronJob, turn_start_ms: int = 0) -> None:
        """Re-arm the wake when an on-call turn ended without advancing anything.

        Holding the schedule is the on-call loop's first obligation, and one turn
        of inattention is enough to drop it for good: a wake is one-shot, so a
        turn that neither acts on the campaign nor schedules the next look leaves
        the store empty while the job keeps running. Observed 2026-08-06 -- the
        turn invented a mood-state file, wrote it, and answered "initialization
        complete", and nothing was watching from then on.

        This decides nothing. It does not submit, kill, wait or report, and the
        re-armed wake carries the same message, channel and target as the one
        that just ran, because that message is the whole context a wake turn gets
        (there is no history). Only the chance to decide is guaranteed.

        Bounded on purpose: a loop that ignores every wake must not be re-armed
        forever, and giving up has to appear on the trail rather than looking
        like a watch that never started.
        """
        try:
            from raven.ops.instrument import campaign_dir, log_event, read_events

            campaign = getattr(job.payload, "campaign", None) or ""
            if not campaign:
                parts = job.name.split(":")
                if len(parts) < 2 or not parts[1]:
                    return
                campaign = parts[1]
            cdir = campaign_dir(campaign)
            if not cdir.exists() or (cdir / "concluded.json").exists():
                return

            events = read_events(cdir)
            if any(
                e.get("kind") == "report_accepted" and e.get("report_kind") == "finished" for e in events
            ):
                return

            prefix = f"ops:{campaign}:"
            # Three ways to have a next look already, and the third is why this is
            # not just a campaign check. A turn that scheduled through the generic
            # cron tool tags nothing and follows no naming rule, so both keyed
            # checks miss it and the re-arm lands on top -- two pending wakes for
            # one campaign, produced by the mechanism meant to prevent that state.
            # When it was scheduled is the one thing no producer can fail to carry.
            if any(
                j.enabled
                and (
                    (getattr(j.payload, "campaign", None) or "") == campaign
                    or j.name.startswith(prefix)
                    or (turn_start_ms and j.created_at_ms >= turn_start_ms)
                )
                for j in self._store.jobs
            ):
                return  # the turn scheduled its own next look

            # Count only the re-arms since the last time the loop moved the
            # campaign itself, so a campaign that recovers gets its full budget
            # of retries back.
            advanced = {"submit", "check_later", "kill", "cancel", "report_accepted"}
            consecutive = 0
            for event in reversed(events):
                kind = event.get("kind")
                if kind == "wake_rearmed":
                    consecutive += 1
                elif kind in advanced:
                    break
            if consecutive >= _MAX_OPS_WAKE_REARMS:
                if not any(e.get("kind") == "wake_rearm_capped" for e in events):
                    log_event(cdir, "wake_rearm_capped", wake=job.name, rearms=consecutive)
                return

            at_ms = self._now_ms() + _OPS_REARM_DELAY_MS
            try:
                rearmed = self.add_job(
                    campaign=campaign,
                    name=f"{prefix}rearm{consecutive + 1}",
                    schedule=CronSchedule(kind="at", at_ms=at_ms),
                    message=job.payload.message,
                    deliver=job.payload.deliver,
                    channel=job.payload.channel or "",
                    to=job.payload.to or "",
                    owner=getattr(job.payload, "owner", None),
                    owner_pid=getattr(job.payload, "owner_pid", None),
                    delete_after_run=True,
                    dedup=False,
                )
            except ValueError:
                return
            log_event(
                cdir,
                "wake_rearmed",
                wake=job.name,
                rearmed_as=rearmed.name,
                at_ms=at_ms,
                reason="turn ended with no campaign action and no next wake",
            )
        except Exception:
            pass

    # ========== Public API ==========

    def advance_job_to_now(self, job_id: str) -> bool:
        """Pull a scheduled job's next fire forward to now (event-triggered wake).

        The ops event watcher calls this when it observes a job-completion
        signal, so the agent wakes on the EVENT instead of waiting out its
        ETA-guessed timer. The job itself is unchanged -- same message, same
        one-shot semantics -- only its fire time moves. Returns False if the
        job does not exist or is disabled.
        """
        with self._locked():
            self._store = None
            store = self._load_store()
            for j in store.jobs:
                if j.id != job_id:
                    continue
                if not j.enabled:
                    return False
                j.state.next_run_at_ms = self._now_ms()
                j.updated_at_ms = self._now_ms()
                self._save_store()
                break
            else:
                return False
        self._arm_timer()
        return True

    def record_silent_fire(self, job_id: str) -> bool:
        """Increment silent_fire_count for a job; auto-disable when it
        crosses silent_fire_limit. Called by harness/dispatch path right
        after a cron fire is delivered. Returns True if the job was
        auto-disabled this call."""
        with self._locked():
            self._store = None
            store = self._load_store()
            for j in store.jobs:
                if j.id != job_id:
                    continue
                j.state.silent_fire_count += 1
                limit = j.silent_fire_limit
                disabled = False
                if limit is not None and limit > 0 and j.state.silent_fire_count >= limit:
                    j.enabled = False
                    j.state.next_run_at_ms = None
                    disabled = True
                    logger.warning(
                        "Cron: auto-disabled job '{}' ({}) — {} silent fires without user activity (limit={})",
                        j.name,
                        j.id,
                        j.state.silent_fire_count,
                        limit,
                    )
                self._save_store()
                return disabled
            return False

    def notify_user_active(self, channel: str | None = None, to: str | None = None) -> int:
        """Reset silent_fire_count for jobs matching (channel, to) — call
        whenever a genuine user-originated message arrives so recently-
        firing crons don't decay toward auto-disable. None matches all.
        Returns count of jobs whose state was reset."""
        reset = 0
        with self._locked():
            self._store = None
            store = self._load_store()
            for j in store.jobs:
                if not j.enabled or j.state.silent_fire_count == 0:
                    continue
                if channel is not None and j.payload.channel != channel:
                    continue
                if to is not None and j.payload.to != to:
                    continue
                j.state.silent_fire_count = 0
                reset += 1
            if reset > 0:
                self._save_store()
        return reset

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs."""
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float("inf"))

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
        topic_tag: str | None = None,
        dedup: bool = True,
        campaign: str | None = None,
        owner: str | None = None,
        owner_pid: int | None = None,
    ) -> CronJob:
        """Add a new job, or update an existing job with the same
        (schedule, channel, to) triple — agents often re-register the
        same recurring reminder with slightly different wording across
        conversations; without dedup the user gets N near-identical
        fires per scheduled tick.

        Two cross-kind dedup layers also apply (in order):

        1. **Message-equal dedup**: if any existing enabled job for the
           same (channel, to) has a *byte-identical* ``payload.message``,
           return it. Catches the case where the LLM creates the same
           reminder N times across the simulation horizon (e.g. a
           medication-reminder string appearing as both an ``at`` shot
           today and a ``cron_expr`` recurring tomorrow — identical
           text, different fire times).

        2. **Time-window dedup**: if any existing enabled job for the
           same (channel, to) is scheduled to fire within 15 minutes of
           this new schedule's next fire (regardless of schedule kind),
           return it. Catches the case where the LLM creates both a
           recurring ``cron_expr`` AND a same-day ``at`` shot for the
           same intent (e.g. "daily 8:00 take meds" + "today 8:00 take
           meds").
        """
        # One ``now`` snapshot for both validation and storage: the validate
        # predicate and the stored next_run must agree on "now", or a boundary
        # ``at`` (at ~ now) could pass validation yet store next_run=None. Taken
        # before the lock so an invalid schedule fails fast without contending it.
        now = self._now_ms()
        _validate_schedule_for_add(schedule, now)
        with self._locked():
            # Reload under lock so we don't clobber a concurrent writer's add.
            self._store = None
            store = self._load_store()

            # One pending wake per campaign, enforced at the single door every
            # producer walks through rather than by each producer's naming. Five
            # of them create jobs here, and the two that do not follow the
            # ops:<campaign>: naming rule are the ones that broke the invariant
            # while it was keyed on the name. What the caller asked for last is
            # what it wants, so a campaign-tagged add replaces that campaign's
            # other pending jobs.
            if campaign:
                for j in list(store.jobs):
                    if j.payload.campaign == campaign:
                        store.jobs.remove(j)
                        logger.info(
                            "Cron: campaign '{}' — replaced pending job '{}' ({})",
                            campaign,
                            j.name,
                            j.id,
                        )

            # L7: topic_tag dedup — strictest, runs first. If the new
            # request carries a topic_tag, any existing enabled job for the
            # same (channel, to, topic_tag) is treated as a duplicate. This
            # catches the caregiver-style failure mode where the LLM
            # creates near-identical med-reminder crons with subtly
            # different schedule offsets (11:20 + 11:30) or message
            # wording — message-equal dedup and 15min window dedup both
            # miss them. The topic_tag IS the identity for "what topic
            # is this reminder about", so two crons with the same
            # topic_tag are by definition the same logical reminder.
            # Update the existing job's message/schedule in-place rather
            # than spawn a parallel one.
            # ``dedup=False`` (system-scheduled timers, e.g. Ops campaign wakes,
            # which legitimately fire in quick succession to the same channel/to)
            # skips all reminder-dedup layers below.
            if dedup and topic_tag:
                for j in store.jobs:
                    if not j.enabled or _is_system_wake(j):
                        continue
                    if j.payload.channel != channel or j.payload.to != to:
                        continue
                    if j.payload.topic_tag != topic_tag:
                        continue
                    logger.info(
                        "Cron: topic_tag dedup — existing job '{}' ({}) "
                        "has topic_tag='{}'; updating message + schedule "
                        "in place (kinds={}/{})",
                        j.name,
                        j.id,
                        topic_tag,
                        j.schedule.kind,
                        schedule.kind,
                    )
                    j.payload.message = message
                    j.payload.deliver = deliver
                    j.name = name
                    j.schedule = schedule
                    j.state.next_run_at_ms = _compute_next_run(schedule, now)
                    j.updated_at_ms = now
                    self._save_store()
                    self._arm_timer()
                    return j

            # Message-equal dedup (covers same-intent reminders the LLM
            # re-asks for across days, possibly with different schedule
            # kinds). Stricter than time-window: byte-equality on full
            # message text → false-positive rate ~0.
            for j in (j for j in store.jobs if not _is_system_wake(j)) if dedup else ():
                if not j.enabled:
                    continue
                if j.payload.channel != channel or j.payload.to != to:
                    continue
                if j.payload.message != message:
                    continue
                if j.state.next_run_at_ms is None or j.state.next_run_at_ms <= now:
                    continue
                logger.info(
                    "Cron: skipped duplicate add — existing job '{}' "
                    "({}) has identical message (same channel/to, "
                    "kinds={}/{})",
                    j.name,
                    j.id,
                    j.schedule.kind,
                    schedule.kind,
                )
                self._arm_timer()
                return j

            # Cross-kind time-window dedup (covers caregiver-style
            # "expr + at for the same intent" double-add). Window is
            # generous (15min) because two genuinely-distinct reminders
            # less than 15min apart are almost always an LLM mistake;
            # the rare legitimate case (two distinct meds at 8:00 and
            # 8:10) loses one fire — acceptable trade-off given the
            # spam alternative.
            new_next = _compute_next_run(schedule, now)
            if dedup and new_next is not None:
                for j in (j for j in store.jobs if not _is_system_wake(j)):
                    if not j.enabled:
                        continue
                    if j.payload.channel != channel or j.payload.to != to:
                        continue
                    existing_next = j.state.next_run_at_ms
                    if existing_next is None:
                        continue
                    if abs(existing_next - new_next) <= 15 * 60 * 1000:
                        logger.info(
                            "Cron: skipped duplicate add — existing job '{}' "
                            "({}) fires within 15min of new request "
                            "(same channel/to, kinds={}/{})",
                            j.name,
                            j.id,
                            j.schedule.kind,
                            schedule.kind,
                        )
                        self._arm_timer()
                        return j

            # Dedup: same recurring schedule + same channel + same recipient
            # → update message in place rather than create a duplicate.
            existing = (
                self._find_duplicate_schedule(
                    [j for j in store.jobs if not _is_system_wake(j)], schedule, channel, to
                )
                if dedup
                else None
            )
            if existing is not None:
                existing.payload.message = message
                existing.payload.deliver = deliver
                existing.name = name
                existing.updated_at_ms = now
                # Recompute next_run_at_ms only if the existing job already
                # fired or was disabled — otherwise keep its scheduled fire.
                if not existing.enabled or existing.state.next_run_at_ms is None:
                    existing.enabled = True
                    existing.state.next_run_at_ms = _compute_next_run(schedule, now)
                self._save_store()
                logger.info(
                    "Cron: updated existing job '{}' ({}) with new message (dedup on schedule+channel+to)",
                    existing.name,
                    existing.id,
                )
                self._arm_timer()
                return existing

            job = CronJob(
                id=str(uuid.uuid4())[:8],
                name=name,
                enabled=True,
                schedule=schedule,
                payload=CronPayload(
                    kind="agent_turn",
                    message=message,
                    deliver=deliver,
                    channel=channel,
                    to=to,
                    topic_tag=topic_tag,
                    campaign=campaign,
                    owner=owner,
                    owner_pid=owner_pid,
                ),
                state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
                created_at_ms=now,
                updated_at_ms=now,
                delete_after_run=delete_after_run,
            )
            store.jobs.append(job)
            self._save_store()
        self._arm_timer()
        logger.info("Cron: added job '{}' ({})", name, job.id)
        return job

    @staticmethod
    def _find_duplicate_schedule(
        jobs: list[CronJob],
        schedule: CronSchedule,
        channel: str | None,
        to: str | None,
    ) -> CronJob | None:
        """Return an existing enabled job whose (schedule, channel, to)
        matches — used by add_job for dedup. ``at`` jobs (one-shot) are
        only deduped if their at_ms is identical (same instant)."""
        for j in jobs:
            if not j.enabled:
                continue
            if j.payload.channel != channel or j.payload.to != to:
                continue
            s = j.schedule
            if s.kind != schedule.kind:
                continue
            if schedule.kind == "cron" and s.expr == schedule.expr and s.tz == schedule.tz:
                return j
            if schedule.kind == "every" and s.every_ms == schedule.every_ms:
                return j
            if schedule.kind == "at" and s.at_ms == schedule.at_ms:
                return j
        return None

    def sole_pending_wake_for(self, job_id: str) -> str | None:
        """The campaign left unwatched if this job goes, or None.

        Answered, not enforced. A loop that removes a campaign's only wake stops
        the campaign being looked at at all (a wake is one-shot), and on
        2026-08-06 one did -- but every step it took was right given what the tool
        had told it, so the fix is to say what removing this one costs and let it
        decide. Refusing would be worse: it would go on believing the job was gone
        and reason from there. Shape from the parallel CFD line.
        """
        with self._locked():
            self._store = None
            store = self._load_store()
            target = next((j for j in store.jobs if j.id == job_id), None)
            if target is None:
                return None
            campaign = getattr(target.payload, "campaign", None) or ""
            if not campaign and target.name.startswith("ops:"):
                parts = target.name.split(":")
                campaign = parts[1] if len(parts) > 1 and parts[1] else ""
            if not campaign:
                return None
            prefix = f"ops:{campaign}:"
            for j in store.jobs:
                if j.id == job_id or not j.enabled:
                    continue
                if (getattr(j.payload, "campaign", None) or "") == campaign or j.name.startswith(prefix):
                    return None
            return campaign

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        with self._locked():
            self._store = None
            store = self._load_store()
            before = len(store.jobs)
            store.jobs = [j for j in store.jobs if j.id != job_id]
            removed = len(store.jobs) < before
            if removed:
                self._save_store()
        if removed:
            self._arm_timer()
            logger.info("Cron: removed job {}", job_id)
        return removed

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        with self._locked():
            self._store = None
            store = self._load_store()
            for job in store.jobs:
                if job.id == job_id:
                    job.enabled = enabled
                    job.updated_at_ms = self._now_ms()
                    if enabled:
                        job.state.next_run_at_ms = _compute_next_run(job.schedule, self._now_ms())
                    else:
                        job.state.next_run_at_ms = None
                    self._save_store()
                    self._arm_timer()
                    return job
        return None

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job."""
        # Pick the target job under lock, then run it lock-free so we don't
        # block concurrent cron activity during a slow agent turn.
        with self._locked():
            self._store = None
            store = self._load_store()
            target = next((j for j in store.jobs if j.id == job_id), None)
            if target is None or (not force and not target.enabled):
                return False
            target.state.claimed_by_pid = os.getpid()
            target.state.claimed_at_ms = self._now_ms()
            self._save_store()

        await self._execute_job(target)

        with self._locked():
            self._store = None
            self._load_store()
            if self._store is not None:
                for j in self._store.jobs:
                    if j.id == target.id and j.state.claimed_by_pid == os.getpid():
                        j.state.claimed_by_pid = None
                        j.state.claimed_at_ms = None
                        j.state.last_run_at_ms = target.state.last_run_at_ms
                        j.state.last_status = target.state.last_status
                        j.state.last_error = target.state.last_error
                        j.state.next_run_at_ms = target.state.next_run_at_ms
                        j.enabled = target.enabled
                        j.updated_at_ms = target.updated_at_ms
                        break
                if target.schedule.kind == "at" and target.delete_after_run:
                    self._store.jobs = [j for j in self._store.jobs if j.id != target.id]
                self._save_store()
        self._arm_timer()
        return True

    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }
