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

from raven.proactive_engine.schedulers.cron.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronStartupDrop,
    CronStore,
)

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

# Cap the sleep-until-next-wake so the wake loop runs at least this often.
# This is how we pick up jobs written to jobs.json by a peer process — the
# tick reloads the store on mtime change. Without this cap, a gateway parked
# on a far-future wake would miss a sooner job added by a peer.
_MAX_WAKE_INTERVAL_S = 30.0

# Backoff after a failed tick so a persistent error cannot spin the loop.
_ERROR_BACKOFF_S = 5.0

# Grace before a past-due foreign one-shot counts as "missed" — its owning
# session may just be slow (a long agent turn) rather than closed.
_MISSED_GRACE_MS = 5 * 60 * 1000


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


def _pid_alive(pid: int) -> bool:
    """Whether a local pid exists. EPERM still means alive; only ESRCH is gone."""
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001 -- an unreadable pid is treated as alive: never adopt on doubt
        return True


class _HandedOff:
    """Returned by an ``on_job`` callback that queued the wake elsewhere.

    A sentinel rather than a bool because the callback's return value already
    means something to another caller -- the gateway's handler returns the turn's
    reply text -- and "" is a reply, not a hand-off.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "HANDED_OFF"


HANDED_OFF = _HandedOff()


class CronService:
    """Service for managing and executing scheduled jobs."""

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
        *,
        allowed_channels: set[str] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        adopt_orphans: bool = False,
    ):
        """``allowed_channels`` restricts which jobs this service will claim.

        Set to e.g. ``{"tui"}`` in the TUI so it doesn't steal Feishu /
        Telegram reminders that gateway should deliver. ``None`` (default)
        means any channel — use that in gateway where ChannelManager can
        route replies to any configured channel.

        Jobs with empty/None ``payload.channel`` are always claimable —
        they predate the channel attribution field.
        """
        self.store_path = store_path
        # Whether a wake whose owning window is DEAD may be adopted. Off for
        # every runner that hosts conversations (TUI, gateway): the 2026-08-14
        # rule stands there -- an owned wake is that owner's alone, open or
        # closed, because running it inside another window's conversation drove
        # somebody else's experiment. On only for a headless runner
        # (raven.ops.wake_shell) that holds no conversation and exists precisely
        # to run the wakes of one-shot turns, whose owning process is dead by
        # design the moment the wake comes due. A live owner is never preempted
        # either way.
        self._adopt_orphans = bool(adopt_orphans)
        # Sibling file for fcntl advisory locking (survives atomic rename of
        # the data file, lets concurrent processes coordinate claim ticks).
        self.lock_path = store_path.with_suffix(store_path.suffix + ".lock")
        self.on_job = on_job
        self.allowed_channels = allowed_channels
        # Missed-reminder observer (gateway wires this): called with the
        # past-due foreign one-shots on start and once per wake-loop pass.
        # Read-only towards the store — the callback must not mutate jobs.
        self.on_missed_foreign: Callable[[list[CronJob]], None] | None = None
        self._store: CronStore | None = None
        # Nanosecond precision: float st_mtime collapses writes ~238ns apart
        # into one value, serving a stale cache after an external rewrite.
        self._last_mtime: int = 0
        self._loop_task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        # Job ids whose claim-skip was already logged (one INFO line per job,
        # not one per tick). Cleared per job on successful claim.
        self._skip_logged: set[str] = set()
        # Same rule for the other branch that logs. _may_claim is a predicate,
        # and _compute_wake_delay calls it for every job on every tick purely to
        # decide how long to sleep -- so an adoption that has not happened yet
        # announced itself every _MAX_WAKE_INTERVAL_S until the wake came due.
        # Read back on 2026-08-25 that was 16 identical "adopting orphaned job"
        # lines for one wake still eight minutes out, which reads as a claim
        # looping rather than a delay being computed.
        self._adopt_logged: set[str] = set()
        self._running = False
        # Remember whether fcntl is usable — degrade to lock-less on Windows.
        self._can_lock = sys.platform != "win32"
        # Optional fake-clock injection for benchmark harnesses (longrun).
        # When provided, all internal time reads route through this callable
        # so newly created jobs' next_run_at_ms aligns with simulated time
        # rather than real wall-clock.
        self._now_fn = now_fn
        # Past-due one-shot reminders dropped by the last start() recompute,
        # kept so the embedding process can surface them to the user (the
        # drop itself only leaves a warning log).
        self.last_startup_drops: list[CronStartupDrop] = []

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
                    channel = j["payload"].get("channel")
                    if channel == "cli":
                        # The "cli" delivery channel is retired (the REPL was
                        # removed); the TUI is the interactive surface now.
                        # Persisted on the next save.
                        logger.info("migrated legacy cli-bound job {} to tui", j["id"])
                        channel = "tui"
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
                                message=j["payload"].get("message", ""),
                                channel=channel,
                                to=j["payload"].get("to"),
                                topic_tag=j["payload"].get("topicTag"),
                                campaign=j["payload"].get("campaign"),
                                owner=j["payload"].get("owner"),
                                owner_pid=j["payload"].get("ownerPid"),
                                direct_agent=j["payload"].get("directAgent"),
                                direct_handle=j["payload"].get("directHandle"),
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
                        "message": j.payload.message,
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
                        "directAgent": j.payload.direct_agent,
                        "directHandle": j.payload.direct_handle,
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
        # partially-flushed file. The temp name is pid-unique: a shared name
        # lets two processes steal each other's temp between write and rename
        # (unlocked start() saves, and every save on Windows where the flock
        # degrades to a no-op) — the loser crashes on FileNotFoundError.
        tmp = self.store_path.with_name(f"{self.store_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.store_path)
        self._last_mtime = self.store_path.stat().st_mtime_ns

    async def start(self) -> None:
        """Start the cron service."""
        self._running = True
        # Under the store lock: two runners starting at once both rewrite the
        # shared file here, and an unlocked load/recompute/save pair can lose
        # the other runner's update.
        with self._locked():
            self._load_store()
            self._recompute_next_runs()
            self._save_store()
        self._check_missed_foreign()
        self._loop_task = asyncio.create_task(self._run_loop())
        logger.info("Cron service started with {} jobs", len(self._store.jobs if self._store else []))

    def stop(self) -> None:
        """Stop the cron service."""
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None

    def _owns_channel(self, channel: str | None) -> bool:
        """Whether this runner's partition covers ``channel``.

        Falsy channel (legacy, pre-attribution) and ``allowed_channels is
        None`` both mean yes; otherwise membership decides.
        """
        return not channel or self.allowed_channels is None or channel in self.allowed_channels

    def _recompute_next_runs(self) -> None:
        """Recompute next run times for enabled jobs this runner may own.

        Past-due one-shot 'at' reminders are dropped — we don't re-deliver
        reminders missed while the service was down (matches iOS /
        Slack / Google Calendar behavior). A warning log records each
        drop so users can audit via gateway logs, and the drops are kept
        on ``last_startup_drops`` so the embedding process can surface a
        missed-reminders notice to the user.

        Recurring ('every', 'cron') jobs just advance to the next future
        run — missed intervals are skipped, not backfilled.

        Both the drop and the recompute are scoped to this runner's
        partition: another runner's jobs pass through exactly as loaded, so
        e.g. a gateway restart never drops a past-due TUI reminder that the
        TUI process may still handle.
        """
        if not self._store:
            return
        now = self._now_ms()
        dropped: list[CronStartupDrop] = []
        kept = []
        for job in self._store.jobs:
            if not job.enabled or not self._owns_channel(job.payload.channel):
                kept.append(job)
                continue
            next_run = _compute_next_run(job.schedule, now)
            if job.schedule.kind == "at" and next_run is None:
                # A past-due job a live peer still holds is not dropped: the peer
                # may be mid-execution, and this runs on EVERY process start, so
                # any incidental raven invocation would otherwise delete work
                # another process is running. Liveness reuses the claim TTL that
                # _may_claim reads, and erring toward keeping is deliberate -- a
                # stale claim only delays the drop by one TTL, whereas a wrong
                # drop destroys an in-flight turn.
                if self._claim_is_live(job, now):
                    logger.info(
                        "Cron: keeping past-due one-shot '{}' ({}) held by live claim pid={}",
                        job.name,
                        job.id,
                        job.state.claimed_by_pid,
                    )
                    kept.append(job)
                    continue
                dropped.append(
                    CronStartupDrop(
                        name=job.name,
                        message=job.payload.message,
                        at_ms=job.schedule.at_ms or 0,
                    )
                )
                continue
            job.state.next_run_at_ms = next_run
            kept.append(job)
        self._store.jobs = kept
        self.last_startup_drops = dropped
        if dropped:
            logger.warning(
                "Cron: dropped {} past-due one-shot reminder(s) on startup: {}",
                len(dropped),
                "; ".join(f"{d.name!r} ({(now - d.at_ms) // 1000}s late)" for d in dropped),
            )

    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        if not self._store:
            return None
        times = [j.state.next_run_at_ms for j in self._store.jobs if j.enabled and j.state.next_run_at_ms]
        return min(times) if times else None

    def _signal_wake(self) -> None:
        """Wake the run loop after a job mutation.

        Safe without a running loop: CLI-process services never start the
        loop, and setting an un-awaited Event is just a flag.
        """
        self._wake_event.set()

    async def _run_loop(self) -> None:
        """Persistent wake loop: process due jobs, then wait for the next
        wake (earliest claimable run, capped) or a mutation signal.

        Process-then-wait order means an event set during processing stays
        set and is consumed on the next iteration — a wake is never lost.
        The loop task is never cancelled by job mutations (the old
        cancel-and-rearm timer cancelled in-flight executions, skipping the
        post-run writeback and double-firing one-shot jobs); only stop()
        cancels it.
        """
        while self._running:
            try:
                await self._process_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Cron: wake tick failed; continuing in {}s", _ERROR_BACKOFF_S)
                await asyncio.sleep(_ERROR_BACKOFF_S)
            self._check_missed_foreign()
            delay = self._compute_wake_delay()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            self._wake_event.clear()

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

    def _may_claim(self, job: CronJob, now: int) -> tuple[bool, str | None]:
        """Whether this runner may claim ``job`` right now.

        Not claimable when a live peer holds the claim (fresh within
        _CLAIM_TTL_MS) or when the job's channel falls outside this runner's
        ``allowed_channels`` partition. Jobs with an empty/None channel
        predate channel attribution and stay claimable by any process.
        Each skip is logged once per job id (reset on successful claim).
        """
        reason: str | None = None
        cb = job.state.claimed_by_pid
        if self._claim_is_live(job, now):
            reason = f"claimed by live peer pid {cb}"
        elif not self._owns_channel(job.payload.channel):
            reason = f"channel '{job.payload.channel}' is outside this runner's partition"
        elif (owning := self._owning_pid(job, os.getpid())) is not None:
            if self._adopt_orphans and not _pid_alive(owning):
                if job.id not in self._adopt_logged:
                    logger.info(
                        "Cron: adopting orphaned job '{}' ({}): owner {!r} (pid {}) is gone",
                        job.name, job.id, job.payload.owner, owning,
                    )
                    self._adopt_logged.add(job.id)
            else:
                reason = f"owned by window {job.payload.owner!r} (pid {owning})"
        if reason is None:
            return True, None
        if job.id not in self._skip_logged:
            logger.info("Cron: not claiming job '{}' ({}): {}", job.name, job.id, reason)
            self._skip_logged.add(job.id)
        return False, reason

    def _compute_wake_delay(self) -> float:
        """Seconds until the earliest pending run this runner may claim.

        Capped at _MAX_WAKE_INTERVAL_S (peer-write poll), floored at 0.
        Jobs this runner cannot claim (foreign partition, live peer claim)
        are excluded so a due-but-unclaimable job cannot busy-loop us.
        """
        store = self._load_store()
        now = self._now_ms()
        times = [
            j.state.next_run_at_ms
            for j in store.jobs
            if j.enabled and j.state.next_run_at_ms and self._may_claim(j, now)[0]
        ]
        if not times:
            return _MAX_WAKE_INTERVAL_S
        delay_s = (min(times) - now) / 1000
        return min(max(delay_s, 0.0), _MAX_WAKE_INTERVAL_S)

    async def _process_due(self) -> None:
        """Run due jobs once.

        Claim phase (under exclusive lock): reload from disk, pick due jobs
        this runner may claim, stamp them with this pid+now, save. Execution
        phase (lock released): run each claimed job; then reacquire the lock
        to write post-run state and clear the claim.
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
                if not self._may_claim(j, now)[0]:
                    continue
                j.state.claimed_by_pid = my_pid
                j.state.claimed_at_ms = now
                self._skip_logged.discard(j.id)
                self._adopt_logged.discard(j.id)
                my_jobs.append(j)
            if my_jobs:
                self._save_store()

        for job in my_jobs:
            await self._execute_job(job)
            # Shielded so a stop() mid-writeback cannot skip the post-run
            # flush (claim would leak and one-shots could re-fire).
            await asyncio.shield(self._writeback_after_run(job, my_pid))

    async def _writeback_after_run(self, job: CronJob, my_pid: int) -> None:
        """Post-run flush + clear claim, under lock so a concurrent reader
        observes the complete updated job record.

        Callers await this through asyncio.shield: when the outer awaiter is
        already cancelled (stop() mid-writeback), a failure here has no one
        left to observe it, so log before letting it propagate.
        """
        try:
            self._writeback_locked(job, my_pid)
        except Exception:
            logger.exception("Cron: post-run writeback failed for job '{}' ({})", job.name, job.id)
            raise

    def _writeback_locked(self, job: CronJob, my_pid: int) -> None:
        with self._locked():
            # Reload + patch our job in case peer wrote intervening state.
            self._store = None
            self._load_store()
            if self._store is None:
                return
            for j in self._store.jobs:
                if j.id == job.id and j.state.claimed_by_pid == my_pid:
                    j.state.claimed_by_pid = None
                    j.state.claimed_at_ms = None
                    j.state.last_run_at_ms = job.state.last_run_at_ms
                    j.state.last_status = job.state.last_status
                    j.state.last_error = job.state.last_error
                    # A store-side disable is sticky: record_fire's auto-disable
                    # (or a user disabling mid-run) lands between claim and this
                    # writeback, and the in-memory copy must not resurrect it.
                    j.enabled = j.enabled and job.enabled
                    j.state.next_run_at_ms = job.state.next_run_at_ms if j.enabled else None
                    j.updated_at_ms = job.updated_at_ms
                    break
            # Handle "at"-kind delete_after_run (_execute_job removed from
            # our local store; reflect on the reloaded store).
            if job.schedule.kind == "at" and job.delete_after_run:
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            self._save_store()

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job."""
        start_ms = self._now_ms()
        logger.info("Cron: executing job '{}' ({})", job.name, job.id)

        try:
            if self.on_job:
                handled = await self.on_job(self._with_campaign_handles(job))

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

        # Not when the callback handed the wake somewhere else. The re-arm asks
        # "did the turn schedule its next look?", and a handed-off wake has no
        # turn yet -- it is queued for another process to run, and the answer at
        # this instant is necessarily no. Measured 2026-08-26 on the first live
        # dispatch: the hand-off at 10:41:59 logged
        # "turn ended with no campaign action and no next wake" onto the trail,
        # and the wake it armed was only removed because the real turn's own
        # wake, 90 seconds later, replaced it under the one-pending-wake rule.
        # A slower turn and the campaign would have had two drivers.
        #
        # Whether the turn advances anything is then the running process's to
        # judge, and it holds the schedule the same way any other turn does.
        if handled is not HANDED_OFF and (
            getattr(job.payload, "campaign", None) or job.name.startswith("ops:")
        ):
            self._keep_ops_campaign_watched(job, start_ms)

    @staticmethod
    def _owning_pid(job: CronJob, my_pid: int) -> int | None:
        """The pid of the window this wake belongs to, when that is not us.

        None means claimable on ownership grounds: no owner recorded, or the
        owner is this very process. A pid means the wake is somebody else's --
        what the caller does with a dead somebody is its policy (see
        ``adopt_orphans``), not this function's.

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
                return None if bound[1] == my_pid else int(bound[1])
        owner_pid = getattr(job.payload, "owner_pid", None)
        if owner_pid and owner_pid != my_pid:
            return int(owner_pid)
        return None

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

            # Asked of the file, not of this process's copy. Under wake_shell the
            # turn is a child process, so the wake it schedules is written to
            # jobs.json by somebody else, while ``self._store`` is the snapshot
            # loaded when this job was claimed -- a whole turn ago, necessarily
            # before the turn could schedule anything. Every check below then
            # reads a store that cannot contain the answer.
            #
            # Measured 2026-08-25 on cantilever-limit-load, twice: the turn
            # submitted round 2 at 17:26:01 and scheduled 'r3'; this check read
            # the 17:25:10 snapshot, saw no next look, and re-armed. ``add_job``
            # does reload, so it found r3 and deleted it as the campaign's stale
            # pending job ("replaced pending job 'ops:...:r3'"). The loop replaced
            # its own next look with one _OPS_REARM_DELAY_MS out, losing the ETA
            # the turn had chosen, and wrote "turn ended with no campaign action"
            # onto a trail whose preceding line is that turn's submit.
            #
            # Separate lock scope: add_job takes the same lock, and _locked opens
            # a fresh descriptor per entry, so holding it across that call would
            # deadlock. Left loaded rather than restored -- the two writers after
            # this point (_writeback_after_run, add_job) each force their own
            # reload, and _last_mtime must not be left describing a store this
            # process no longer holds.
            # The wake that just ran is excluded: its removal (or disabling) is
            # _writeback_after_run's job and has not been flushed yet, so on disk
            # it is still an enabled job for this campaign -- and counting it as
            # "a next look already exists" would suppress every re-arm this
            # function exists to make. ``self._store`` had it dropped in memory by
            # _execute_job, which is the semantics being restored here, not
            # widened.
            with self._locked():
                self._store = None
                pending = [j for j in self._load_store().jobs if j.id != job.id]

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
                for j in pending
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

    def ensure_campaign_watched(self, job: CronJob) -> None:
        """Re-arm this campaign's wake if nothing came of the turn ``job`` was for.

        The same check ``_execute_job`` runs, exposed because it is not always
        this process that runs the turn. A wake handed to another process (see
        ``adopt_orphans`` and the on-call shell's dispatch mode) has no turn yet
        at the moment it is handed over, so judging it there would arm a second
        driver -- and judging it nowhere leaves the campaign with none.

        So the caller owns the timing, and owns getting it right: call this only
        once the turn has had time to run and either schedule its next look or
        not. Called too early it re-arms over a turn still working; never called,
        one turn's inattention drops the schedule for good, which is the 2026-08-06
        incident ``_keep_ops_campaign_watched`` exists for.

        Bounded and idempotent like the in-process path: an existing pending wake
        for the campaign stops it, and the re-arm budget is counted off the
        campaign's own trail.
        """
        self._keep_ops_campaign_watched(job, 0)

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
        # The new model has one loop waiting on an event with a timeout, so
        # pulling a fire forward means signalling that event rather than
        # re-arming a per-job timer.
        self._signal_wake()
        return True

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

    def record_fire(self, job_id: str) -> bool:
        """Increment silent_fire_count for a recurring job; auto-disable when
        it crosses silent_fire_limit. Called by the delivery handler
        (make_on_cron_job) right after a successful cron fire. Returns True
        if the job was auto-disabled this call.

        One-shot 'at' jobs are ignored (they cannot run away). The store
        write persists the count, but the caller executing the job must
        also flip its in-memory job.enabled on a True return —
        _writeback_after_run patches enabled/next_run from the in-memory
        job and would otherwise clobber the disable written here.
        """
        with self._locked():
            self._store = None
            store = self._load_store()
            for j in store.jobs:
                if j.id != job_id:
                    continue
                if j.schedule.kind not in ("every", "cron"):
                    return False
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
        firing crons don't decay toward auto-disable. ``None`` on the call
        side matches every job; a falsy channel/to on the JOB side is a
        wildcard too — legacy pre-attribution jobs are claimable by any
        runner, so symmetrically any user activity resets them (otherwise
        their counter could only ever climb and unfairly auto-disable).
        Returns count of jobs whose state was reset."""
        reset = 0
        with self._locked():
            self._store = None
            store = self._load_store()
            for j in store.jobs:
                if not j.enabled or j.state.silent_fire_count == 0:
                    continue
                if channel is not None and j.payload.channel and j.payload.channel != channel:
                    continue
                if to is not None and j.payload.to and j.payload.to != to:
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

    def list_missed_foreign_oneshots(self, grace_ms: int = _MISSED_GRACE_MS) -> list[CronJob]:
        """Read-only: one-shot 'at' jobs owned by another partition whose
        fire time is more than ``grace_ms`` in the past — their owning
        session (tui / cli) was closed before they could fire.

        Foreign jobs are exactly the ones this runner may never claim or
        clean up (partition rules), so observing is the only way the
        gateway can tell the user a reminder was stranded. A job with a
        fresh peer claim is excluded: the owning process is delivering it
        right now. Falsy channels (legacy, pre-attribution) are never
        foreign.
        """
        store = self._load_store()
        now = self._now_ms()
        missed: list[CronJob] = []
        for j in store.jobs:
            if j.schedule.kind != "at" or not j.enabled:
                continue
            if self._owns_channel(j.payload.channel):
                continue
            at_ms = j.schedule.at_ms
            if at_ms is None or now - at_ms < grace_ms:
                continue
            cb, ca = j.state.claimed_by_pid, j.state.claimed_at_ms
            if cb is not None and ca is not None and (now - ca) < _CLAIM_TTL_MS:
                continue
            missed.append(j)
        return missed

    def _check_missed_foreign(self) -> None:
        """Feed past-due foreign one-shots to the observer, best-effort.
        A callback failure must never break start() or the wake loop."""
        if self.on_missed_foreign is None:
            return
        try:
            missed = self.list_missed_foreign_oneshots()
            if missed:
                self.on_missed_foreign(missed)
        except Exception:
            logger.exception("Cron: missed-foreign observer failed; continuing")

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
        topic_tag: str | None = None,
        dedup: bool = True,
        campaign: str | None = None,
        owner: str | None = None,
        owner_pid: int | None = None,
        direct_agent: str | None = None,
        direct_handle: str | None = None,
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
                            "Cron: campaign '{}' -- replaced pending job '{}' ({})",
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
            # An on-call wake never takes part in reminder dedup, in either
            # direction: it is not merged into (dedup=False at the call site) and
            # it is never RETURNED as the existing duplicate of a reminder.
            # Measured 2026-08-06 on the CFD line -- a turn asked for a check 28
            # seconds from its campaign's wake, got that wake back, read the
            # unfamiliar name as a duplicate of its own request, and removed it.
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
                    j.name = name
                    j.schedule = schedule
                    j.state.next_run_at_ms = _compute_next_run(schedule, now)
                    j.updated_at_ms = now
                    self._save_store()
                    self._signal_wake()
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
                self._signal_wake()
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
                        self._signal_wake()
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
                self._signal_wake()
                return existing

            job = CronJob(
                id=str(uuid.uuid4())[:8],
                name=name,
                enabled=True,
                schedule=schedule,
                payload=CronPayload(
                    message=message,
                    channel=channel,
                    to=to,
                    topic_tag=topic_tag,
                    campaign=campaign,
                    owner=owner,
                    owner_pid=owner_pid,
                    direct_agent=direct_agent,
                    direct_handle=direct_handle,
                ),
                state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
                created_at_ms=now,
                updated_at_ms=now,
                delete_after_run=delete_after_run,
            )
            store.jobs.append(job)
            self._save_store()
        self._signal_wake()
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
            self._signal_wake()
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
                        # Re-enabling is deliberate user engagement with this
                        # job: without a counter reset, one auto-disabled at
                        # the limit would re-disable on its very next fire.
                        job.state.silent_fire_count = 0
                    else:
                        job.state.next_run_at_ms = None
                    self._save_store()
                    self._signal_wake()
                    return job
        return None

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job through the same execute/writeback path the
        wake loop uses, so a test-fire cannot diverge from real scheduling."""
        # Pick the target job under lock, then run it lock-free so we don't
        # block concurrent cron activity during a slow agent turn.
        my_pid = os.getpid()
        with self._locked():
            self._store = None
            store = self._load_store()
            target = next((j for j in store.jobs if j.id == job_id), None)
            if target is None or (not force and not target.enabled):
                return False
            target.state.claimed_by_pid = my_pid
            target.state.claimed_at_ms = self._now_ms()
            self._save_store()

        await self._execute_job(target)
        await asyncio.shield(self._writeback_after_run(target, my_pid))
        self._signal_wake()
        return True

    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }
