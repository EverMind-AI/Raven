"""Cron types."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CronSchedule:
    """Schedule definition for a cron job."""
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class CronPayload:
    """What to do when the job runs."""
    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    # Deliver response to channel
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number
    # Sentinel-coordination tag (F-G): when set, this cron fire updates
    # the shared NudgePolicy ledger (topic_fired_at + record_dispatched)
    # so the L3 Sentinel's later proactive ticks skip the same topic
    # instead of double-nudging. Set by the LLM when creating crons
    # whose subject overlaps a likely-Sentinel topic (e.g. birthday,
    # anniversary, recurring medication).
    topic_tag: str | None = None


@dataclass
class CronJobState:
    """Runtime state of a job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    # Claim fields — set by whichever process grabs the job in _on_timer.
    # Cleared post-run. Stale claims (older than CLAIM_TTL_MS) are stolen.
    claimed_by_pid: int | None = None
    claimed_at_ms: int | None = None
    # Auto-decay tracking: count of consecutive fires without intervening
    # user activity (any user-originated message in the same channel/to
    # resets this to 0). Used by silent-fires guard to disable runaway
    # recurring jobs the LLM created (e.g. every_seconds=3000 forever).
    silent_fire_count: int = 0


@dataclass
class CronJob:
    """A scheduled job."""
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False
    # Auto-decay limit: when state.silent_fire_count reaches this value,
    # the job is auto-disabled. None = no limit (runs forever until
    # explicit removal). Default 12 strikes a balance: gives ~1 day of
    # hourly fires before declaring "user not engaging".
    silent_fire_limit: int | None = 12


@dataclass
class CronStore:
    """Persistent store for cron jobs."""
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
