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
                stale_ms = now - (job.schedule.at_ms or 0)
                dropped.append(f"{job.name!r} ({stale_ms // 1000}s late)")
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
        """
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
            if self._running:
                await self._on_timer()

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
                # Skip if a live peer already has it.
                cb = j.state.claimed_by_pid
                ca = j.state.claimed_at_ms
                if cb is not None and cb != my_pid and ca is not None and (now - ca) < _CLAIM_TTL_MS:
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
                await self.on_job(job)

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

    # ========== Public API ==========

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
    ) -> CronJob:
        """Add a new job, or update an existing job with the same
        (schedule, channel, to) triple — agents often re-register the
        same recurring reminder with slightly different wording across
        conversations; without dedup the user gets N near-identical
        fires per scheduled tick.

        A cross-kind dedup layer also applies: if any existing enabled job for the
           same (channel, to) has a *byte-identical* ``payload.message``,
           return it. Catches the case where the LLM creates the same
           reminder N times across the simulation horizon (e.g. a
           medication-reminder string appearing as both an ``at`` shot
           today and a ``cron_expr`` recurring tomorrow — identical
           text, different fire times).
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
            if topic_tag:
                for j in store.jobs:
                    if not j.enabled:
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
            for j in store.jobs:
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

            # Dedup: same recurring schedule + same channel + same recipient
            # → update message in place rather than create a duplicate.
            existing = self._find_duplicate_schedule(store.jobs, schedule, channel, to)
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
