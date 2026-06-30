"""Cross-process persistence tests for Sentinel + cron.

Covers the scenario where REPL and gateway run simultaneously and need to
agree on NudgePolicy quotas, NudgeInjector queues, DeferManager pending
defers, and cron job claims. Each test simulates two processes by
instantiating two independent component instances pointing at the same
on-disk state file.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from raven.config.raven import NudgePolicyConfig
from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule
from raven.proactive_engine.sentinel.executor.defer_manager import DeferManager
from raven.proactive_engine.sentinel.executor.dispatcher import NudgeDispatcher
from raven.proactive_engine.sentinel.executor.injector import NudgeInjector
from raven.proactive_engine.sentinel.feedback.persistence import JsonStateStore
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy
from raven.proactive_engine.sentinel.types import PlannerDecision


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    return tmp_path


def _policy_cfg(**overrides) -> NudgePolicyConfig:
    base = dict(
        max_nudges_per_hour=3,
        max_nudges_per_day=10,
        min_interval_seconds=0,
        quiet_hours=(0, 0),
        cooldown_on_dismiss_seconds=0,
        high_priority_bypasses_limits=False,
        dedup_window_seconds=60,
        inject_ttl_seconds=1800,
        inject_max_pending_per_session=3,
        defer_idle_threshold_seconds=60,
        defer_max_wait_seconds=3600,
    )
    base.update(overrides)
    return NudgePolicyConfig(**base)


# ---------------------------------------------------------------------------
# JsonStateStore


def test_json_state_store_roundtrip(tmp_state_dir: Path):
    store = JsonStateStore(tmp_state_dir / "s.json")
    assert store.load() == {}
    store.update(lambda s: {"k": 1, "nested": {"a": 2}})
    assert store.load() == {"k": 1, "nested": {"a": 2}}


def test_json_state_store_atomic_rename(tmp_state_dir: Path):
    """After update(), a crash between write and rename must not leave a
    partial file. Verify the data file either has old or new contents — no
    mid-write JSON parse failure."""
    store = JsonStateStore(tmp_state_dir / "s.json")
    store.update(lambda s: {"v": 1})
    store.update(lambda s: {**s, "v": 2})
    assert store.load() == {"v": 2}


def test_json_state_store_skips_corrupt(tmp_state_dir: Path):
    """A garbage file (e.g. partial write before atomic rename landed) must
    not crash — load() returns {} so the caller can rebuild from empty."""
    p = tmp_state_dir / "s.json"
    p.write_text("not json at all {", encoding="utf-8")
    store = JsonStateStore(p)
    assert store.load() == {}


# ---------------------------------------------------------------------------
# NudgePolicy cross-process


def test_policy_fires_visible_to_peer_process(tmp_state_dir: Path):
    """Fires recorded in A must be counted by B's next check."""
    path = tmp_state_dir / "policy.json"
    a = NudgePolicy(_policy_cfg(max_nudges_per_hour=3), store=JsonStateStore(path))
    b = NudgePolicy(_policy_cfg(max_nudges_per_hour=3), store=JsonStateStore(path))
    a.record_fired("nudge", "cli:x", "one")
    a.record_fired("nudge", "cli:y", "two")
    b.record_fired("nudge", "cli:z", "three")

    # From A's perspective, next check must deny on hour quota — peer's fire
    # was visible via store reload.
    r = a.check("nudge", "cli:x", "four")
    assert r.verdict == "deny"
    assert "hour_quota" in r.reason


def test_policy_snapshot_reloads_from_store(tmp_state_dir: Path):
    path = tmp_state_dir / "policy.json"
    a = NudgePolicy(_policy_cfg(), store=JsonStateStore(path))
    b = NudgePolicy(_policy_cfg(), store=JsonStateStore(path))
    a.record_fired("nudge", "cli:x", "one")
    assert b.snapshot_state()["nudges_used_this_hour"] == 1


def test_policy_dismissal_shared_across_processes(tmp_state_dir: Path):
    path = tmp_state_dir / "policy.json"
    cfg = _policy_cfg(cooldown_on_dismiss_seconds=3600)
    a = NudgePolicy(cfg, store=JsonStateStore(path))
    b = NudgePolicy(cfg, store=JsonStateStore(path))
    b.record_dismissed("cli:q")

    r = a.check("nudge", "cli:q", "please")
    assert r.verdict == "deny"
    assert "dismissed" in r.reason


# ---------------------------------------------------------------------------
# NudgeInjector cross-process


def test_injector_queue_visible_to_peer(tmp_state_dir: Path):
    path = tmp_state_dir / "inj.json"
    a = NudgeInjector(store=JsonStateStore(path))
    b = NudgeInjector(store=JsonStateStore(path))
    a.queue("cli:s", "hello", source="test")
    # Independent B instance — must reload to see A's queue.
    b._reload_from_store()
    assert b.peek("cli:s") == ["hello"]


def test_injector_pop_is_atomic_across_processes(tmp_state_dir: Path):
    """If A queued and B pops, A's in-memory view must reflect the empty
    queue after its own reload — no duplicate delivery from A."""
    path = tmp_state_dir / "inj.json"
    a = NudgeInjector(store=JsonStateStore(path))
    b = NudgeInjector(store=JsonStateStore(path))
    a.queue("cli:s", "one")
    a.queue("cli:s", "two")

    popped = b.pop_pending("cli:s")
    assert popped == ["one", "two"]

    a._reload_from_store()
    assert a.peek("cli:s") == []


# ---------------------------------------------------------------------------
# DeferManager cross-process


def test_defer_register_visible_to_peer(tmp_state_dir: Path):
    path = tmp_state_dir / "defer.json"
    dispatcher = NudgeDispatcher()
    a = DeferManager(dispatcher, lambda k: None, store=JsonStateStore(path))
    b = DeferManager(dispatcher, lambda k: None, store=JsonStateStore(path))

    decision = PlannerDecision(
        action="nudge_defer",
        reason="test",
        priority="low",
        nudge_message="follow up later",
        defer_condition="idle",
    )
    did = a.register(decision, "cli:default", max_wait_seconds=3600)
    b._reload_from_store()
    assert b.pending_count() == 1
    loaded = b._by_id[did].decision
    assert loaded.nudge_message == "follow up later"
    assert loaded.defer_condition == "idle"
    assert loaded.action == "nudge_defer"


def test_defer_cancel_propagates_to_peer(tmp_state_dir: Path):
    path = tmp_state_dir / "defer.json"
    dispatcher = NudgeDispatcher()
    a = DeferManager(dispatcher, lambda k: None, store=JsonStateStore(path))
    b = DeferManager(dispatcher, lambda k: None, store=JsonStateStore(path))

    decision = PlannerDecision(
        action="nudge_defer",
        nudge_message="x",
        defer_condition="idle",
    )
    did = a.register(decision, "cli:default", max_wait_seconds=3600)
    a.cancel(did)
    b._reload_from_store()
    assert b.pending_count() == 0


# ---------------------------------------------------------------------------
# Cron: claim prevents double-fire across processes


def test_cron_claim_prevents_concurrent_fire(tmp_state_dir: Path, monkeypatch):
    """Simulate two CronServices pointing at the same jobs.json. Schedule a
    job due now. Only one service's on_job callback should fire per tick.

    Real two-process use has distinct pids; in-test we patch os.getpid on
    the cron.service module to differ per invocation so the claim-skip
    logic engages (otherwise both services' claims look self-owned).
    """
    from raven.proactive_engine.schedulers.cron import service as cron_service

    store_path = tmp_state_dir / "jobs.json"
    fire_a: list[str] = []
    fire_b: list[str] = []

    svc_a = CronService(store_path)
    svc_b = CronService(store_path)

    async def cb_a(job):
        fire_a.append(job.id)

    async def cb_b(job):
        fire_b.append(job.id)

    svc_a.on_job = cb_a
    svc_b.on_job = cb_b

    # Schedule slightly in the future; _compute_next_run rejects past "at"
    # times by returning None.
    now_ms = int(time.time() * 1000)
    job = svc_a.add_job(
        name="t",
        schedule=CronSchedule(kind="at", at_ms=now_ms + 500),
        message="m",
        delete_after_run=True,
    )

    # Patch getpid so svc_a reports pid 1000 and svc_b reports pid 2000.
    current_pid = {"val": 1000}

    def _fake_getpid():
        return current_pid["val"]

    monkeypatch.setattr(cron_service.os, "getpid", _fake_getpid)

    async def drive():
        # Wait until the job is due, then tick both services.
        await asyncio.sleep(0.6)
        current_pid["val"] = 1000
        await svc_a._on_timer()
        current_pid["val"] = 2000
        await svc_b._on_timer()

    asyncio.run(drive())

    fired_count = len(fire_a) + len(fire_b)
    assert fired_count == 1, f"expected 1 execution total, got A={fire_a} B={fire_b}"
    assert job.id in fire_a, "svc_a claimed first so must be the one to fire"


def test_cli_reminder_stores_request_context_verbatim(tmp_state_dir: Path):
    """New design: CronTool persists the request-time channel/chat_id as-is.
    Delivery resolution (pass-through vs ephemeral forward) is the cron
    handler's job at trigger time, not the tool's at creation time."""
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.proactive_engine.schedulers.cron.tool import CronTool

    svc = CronService(tmp_state_dir / "jobs.json")
    tool = CronTool(svc)
    tool.set_context(channel="cli", chat_id="direct")
    tool._add_job(message="drink water", every_seconds=None, cron_expr=None, tz=None, at="2099-01-01T00:00:00")
    jobs = svc.list_jobs()
    assert jobs[0].payload.channel == "cli"
    assert jobs[0].payload.to == "direct"


def test_session_manager_find_most_recent_chat_id(tmp_state_dir: Path):
    """SessionManager.find_most_recent_chat_id respects mtime and metadata."""
    import json

    from raven.session.manager import SessionManager

    workspace = tmp_state_dir / "ws"
    sessions_dir = workspace / "sessions"
    (sessions_dir / "feishu").mkdir(parents=True)
    (sessions_dir / "telegram").mkdir(parents=True)
    # Feishu — two sessions, pick newer by updated_at
    (sessions_dir / "feishu" / "ou_old.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "feishu:ou_old", "updated_at": "2026-06-10T10:00:00"}) + "\n",
        encoding="utf-8",
    )
    (sessions_dir / "feishu" / "ou_new.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "feishu:ou_new", "updated_at": "2026-06-10T11:00:00"}) + "\n",
        encoding="utf-8",
    )
    # Distractor: telegram session — must not pollute feishu lookup
    (sessions_dir / "telegram" / "12345.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "telegram:12345", "updated_at": "2026-06-10T12:00:00"}) + "\n",
        encoding="utf-8",
    )

    mgr = SessionManager(workspace)
    assert mgr.find_most_recent_chat_id("feishu") == "ou_new"
    assert mgr.find_most_recent_chat_id("telegram") == "12345"
    assert mgr.find_most_recent_chat_id("discord") is None


def test_cron_channel_filter_routes_to_right_process(tmp_state_dir: Path, monkeypatch):
    """A CronService with allowed_channels={"cli"} must NOT claim a job
    whose payload.channel is "feishu". The gateway-side service (no filter)
    claims it instead. Prevents Feishu-bound reminders leaking into REPL.
    """
    from raven.proactive_engine.schedulers.cron import service as cron_service

    store_path = tmp_state_dir / "jobs.json"
    svc_repl = CronService(store_path, allowed_channels={"cli"})
    svc_gw = CronService(store_path)  # no filter — gateway behavior
    fired_repl: list[str] = []
    fired_gw: list[str] = []

    async def cb_repl(job):
        fired_repl.append(job.id)

    async def cb_gw(job):
        fired_gw.append(job.id)

    svc_repl.on_job = cb_repl
    svc_gw.on_job = cb_gw

    now_ms = int(time.time() * 1000)
    # Create a feishu-bound job via svc_gw (doesn't matter which adds; file is shared).
    job = svc_gw.add_job(
        name="reminder",
        schedule=CronSchedule(kind="at", at_ms=now_ms + 500),
        message="drink water",
        deliver=True,
        channel="feishu",
        to="ou_xxx",
        delete_after_run=True,
    )

    current_pid = {"val": 1000}
    monkeypatch.setattr(cron_service.os, "getpid", lambda: current_pid["val"])

    async def drive():
        await asyncio.sleep(0.6)
        # REPL ticks first, must skip the job.
        current_pid["val"] = 1000
        await svc_repl._on_timer()
        # Gateway ticks, must claim it.
        current_pid["val"] = 2000
        await svc_gw._on_timer()

    asyncio.run(drive())

    assert fired_repl == [], f"REPL must not claim feishu job, got {fired_repl}"
    assert fired_gw == [job.id], f"gateway must claim feishu job, got {fired_gw}"


def test_cron_arm_timer_caps_sleep_for_peer_writes(tmp_state_dir: Path, monkeypatch):
    """_arm_timer must cap sleep at _MAX_WAKE_INTERVAL_S so a peer process's
    write to jobs.json gets picked up within that window — otherwise a
    gateway armed for a far-future wake misses a sooner job added by REPL.
    """
    from raven.proactive_engine.schedulers.cron import service as cron_service

    captured: list[float] = []

    async def _recording_sleep(delay):
        captured.append(delay)
        raise asyncio.CancelledError  # abort the tick coroutine

    async def _drive(scenario: str):
        svc = CronService(tmp_state_dir / f"{scenario}.json")
        svc._running = True
        if scenario == "far_future":
            now_ms = int(time.time() * 1000)
            svc.add_job(
                name="f",
                schedule=CronSchedule(kind="at", at_ms=now_ms + 3600_000),
                message="x",
                delete_after_run=True,
            )
        monkeypatch.setattr(cron_service.asyncio, "sleep", _recording_sleep)
        svc._arm_timer()
        if svc._timer_task:
            try:
                await svc._timer_task
            except asyncio.CancelledError:
                pass
        monkeypatch.undo()

    asyncio.run(_drive("no_jobs"))
    asyncio.run(_drive("far_future"))

    assert len(captured) == 2, captured
    for delay in captured:
        assert delay == cron_service._MAX_WAKE_INTERVAL_S, (
            f"expected cap {cron_service._MAX_WAKE_INTERVAL_S}s, got {delay}"
        )


def test_adaptive_tuning_reduces_quota_on_low_acceptance(tmp_state_dir: Path):
    """acceptance_rate < 0.3 → multiplier 0.2 (L1 tier floor).

    High-priority bypass still respects overall day quota but the hour layer
    uses the effective (tightened) cap. Pre-L1 floor was 0.25; L1 made tiers
    stricter (≥0.3→0.5, <0.3→0.2) because moderate rejection (~30% accept)
    never crossed the old <0.1 threshold to tighten."""
    cfg = _policy_cfg(
        max_nudges_per_hour=10,
        max_nudges_per_day=100,
        min_interval_seconds=0,
        dedup_window_seconds=1,
        high_priority_bypasses_limits=False,
    )
    policy = NudgePolicy(cfg, store=JsonStateStore(tmp_state_dir / "p.json"))

    # Data sufficient but acceptance ~5% → clamp to 0.2 × 10 = 2
    policy.apply_adaptive_tuning(acceptance_rate=0.05, dispatched_count=20)
    assert policy._hour_quota_multiplier == 0.2
    assert policy._effective_hour_quota() == 2

    # Fire twice within an hour → next check denies
    policy.record_fired("nudge", "cli:x", "a")
    policy.record_fired("nudge", "cli:y", "b")
    r = policy.check("nudge", "cli:z", "c")
    assert r.verdict == "deny" and "hour_quota" in r.reason


def test_adaptive_tuning_keeps_base_on_high_acceptance(tmp_state_dir: Path):
    """acceptance_rate >= 0.7 → multiplier 1.0 (no tightening)."""
    cfg = _policy_cfg(max_nudges_per_hour=5)
    policy = NudgePolicy(cfg, store=JsonStateStore(tmp_state_dir / "p.json"))
    policy.apply_adaptive_tuning(acceptance_rate=0.85, dispatched_count=20)
    assert policy._hour_quota_multiplier == 1.0
    assert policy._effective_hour_quota() == 5


def test_adaptive_tuning_loosens_on_very_high_acceptance(tmp_state_dir: Path):
    """acceptance_rate >= 0.9 with enough volume → multiplier 1.5; quota lifts above base.

    The loosen direction was added so high-engagement users get more
    proactive nudges instead of being capped at the default budget.
    """
    cfg = _policy_cfg(max_nudges_per_hour=4)
    policy = NudgePolicy(cfg, store=JsonStateStore(tmp_state_dir / "p.json"))
    # 18/20 accepted (90%), volume meets the loosen gate (default 2 * min_volume = 10)
    policy.apply_adaptive_tuning(acceptance_rate=0.9, dispatched_count=20)
    assert policy._hour_quota_multiplier == 1.5
    # Effective quota lifts above the configured base: 4 × 1.5 = 6
    assert policy._effective_hour_quota() == 6


def test_adaptive_tuning_loosen_requires_volume_gate(tmp_state_dir: Path):
    """≥0.9 acceptance with insufficient volume must NOT lift the multiplier —
    a 5-of-5 streak is statistical noise, not a license to spam."""
    cfg = _policy_cfg(max_nudges_per_hour=5)
    policy = NudgePolicy(cfg, store=JsonStateStore(tmp_state_dir / "p.json"))
    # 5/5 accepted (100%) but volume below the loosen gate (default 10)
    policy.apply_adaptive_tuning(acceptance_rate=1.0, dispatched_count=5)
    assert policy._hour_quota_multiplier == 1.0, "must NOT loosen on small N — falls back to neutral tier"


def test_adaptive_tuning_noop_on_insufficient_data(tmp_state_dir: Path):
    """dispatched_count < min_volume → multiplier stays at cold-start floor (0.7).

    L4 lowered the cold-start floor from 1.0 to 0.7 — asymmetric trust:
    tighten by default until enough feedback accumulates. (0.5 was tried
    first but starved the feedback loop entirely; 0.7 keeps the posture
    while letting signal accumulate.)"""
    cfg = _policy_cfg(max_nudges_per_hour=5)
    policy = NudgePolicy(cfg, store=JsonStateStore(tmp_state_dir / "p.json"))
    policy.apply_adaptive_tuning(acceptance_rate=0.0, dispatched_count=2)
    assert policy._hour_quota_multiplier == 0.7

    policy.apply_adaptive_tuning(acceptance_rate=None, dispatched_count=50)
    assert policy._hour_quota_multiplier == 0.7


def test_adaptive_tuning_hysteresis_ignores_tiny_changes(tmp_state_dir: Path):
    """< 5% swing in multiplier should not be applied (avoid flap).

    Use the ≥0.5 band (acceptance 0.55 → 0.65) to stay in the same tier
    (0.7) for both calls — gives a clean hysteresis check independent of
    tier boundaries."""
    cfg = _policy_cfg()
    policy = NudgePolicy(cfg, store=JsonStateStore(tmp_state_dir / "p.json"))
    # Tier-locked at 0.7 (acceptance ≥ 0.5 with sufficient volume)
    policy.apply_adaptive_tuning(acceptance_rate=0.55, dispatched_count=10)
    assert policy._hour_quota_multiplier == 0.7
    # Tiny acceptance change still in the ≥0.5 band → no write
    policy.apply_adaptive_tuning(acceptance_rate=0.65, dispatched_count=10)
    assert policy._hour_quota_multiplier == 0.7


def test_adaptive_multiplier_reaches_planner_prompt(tmp_state_dir: Path):
    """PlannerContext surfaces hour_quota_multiplier, and the prompt renders
    an adaptive-tightening note only when multiplier < 1.0."""
    from datetime import datetime

    from raven.proactive_engine.sentinel.trigger_policy.prompts import build_context_prompt
    from raven.proactive_engine.sentinel.types import NudgePolicyState, PlannerContext

    # Case 1: multiplier == 1.0 → no extra line
    ctx = PlannerContext(
        now=datetime(2026, 4, 24, 18, 0),
        nudge_policy_state=NudgePolicyState(
            nudges_used_this_hour=1,
            remaining_today=8,
            in_quiet_hours=False,
            hour_quota_multiplier=1.0,
        ),
    )
    prompt = build_context_prompt(ctx)
    assert "自适应收紧" not in prompt

    # Case 2: multiplier < 1.0 → note appears
    ctx2 = PlannerContext(
        now=datetime(2026, 4, 24, 18, 0),
        nudge_policy_state=NudgePolicyState(
            nudges_used_this_hour=1,
            remaining_today=8,
            in_quiet_hours=False,
            hour_quota_multiplier=0.5,
        ),
    )
    prompt2 = build_context_prompt(ctx2)
    assert "自适应收紧" in prompt2
    assert "× 0.50" in prompt2


def test_adaptive_multiplier_persists_across_instances(tmp_state_dir: Path):
    """Multiplier survives into peer process via JsonStateStore."""
    cfg = _policy_cfg()
    path = tmp_state_dir / "p.json"
    a = NudgePolicy(cfg, store=JsonStateStore(path))
    a.apply_adaptive_tuning(acceptance_rate=0.05, dispatched_count=30)
    assert a._hour_quota_multiplier == 0.2

    b = NudgePolicy(cfg, store=JsonStateStore(path))
    assert b._hour_quota_multiplier == 0.2


def test_cron_start_drops_past_due_at_jobs(tmp_state_dir: Path):
    """Past-due 'at' jobs must be deleted on CronService.start (α policy:
    don't re-deliver missed reminders). Recurring jobs must be preserved
    with their next_run advanced to the next future fire.
    """
    import json

    store_path = tmp_state_dir / "jobs.json"
    now_ms = int(time.time() * 1000)
    seed = {
        "version": 1,
        "jobs": [
            # Past-due at — should be dropped
            {
                "id": "past_at",
                "name": "drink water",
                "enabled": True,
                "schedule": {"kind": "at", "atMs": now_ms - 3600_000, "everyMs": None, "expr": None, "tz": None},
                "payload": {"kind": "agent_turn", "message": "m", "deliver": True, "channel": "cli", "to": "direct"},
                "state": {
                    "nextRunAtMs": None,
                    "lastRunAtMs": None,
                    "lastStatus": None,
                    "lastError": None,
                    "claimedByPid": None,
                    "claimedAtMs": None,
                },
                "createdAtMs": now_ms - 7200_000,
                "updatedAtMs": now_ms - 7200_000,
                "deleteAfterRun": True,
            },
            # Future at — should be kept
            {
                "id": "future_at",
                "name": "later",
                "enabled": True,
                "schedule": {"kind": "at", "atMs": now_ms + 600_000, "everyMs": None, "expr": None, "tz": None},
                "payload": {"kind": "agent_turn", "message": "m", "deliver": True, "channel": "cli", "to": "direct"},
                "state": {
                    "nextRunAtMs": now_ms + 600_000,
                    "lastRunAtMs": None,
                    "lastStatus": None,
                    "lastError": None,
                    "claimedByPid": None,
                    "claimedAtMs": None,
                },
                "createdAtMs": now_ms,
                "updatedAtMs": now_ms,
                "deleteAfterRun": True,
            },
            # Recurring 'every' — should be kept with next run in the future
            {
                "id": "every",
                "name": "heartbeat",
                "enabled": True,
                "schedule": {"kind": "every", "atMs": None, "everyMs": 300_000, "expr": None, "tz": None},
                "payload": {"kind": "agent_turn", "message": "m", "deliver": True, "channel": "cli", "to": "direct"},
                "state": {
                    "nextRunAtMs": now_ms - 100,
                    "lastRunAtMs": None,
                    "lastStatus": None,
                    "lastError": None,
                    "claimedByPid": None,
                    "claimedAtMs": None,
                },
                "createdAtMs": now_ms,
                "updatedAtMs": now_ms,
                "deleteAfterRun": False,
            },
        ],
    }
    store_path.write_text(json.dumps(seed), encoding="utf-8")

    svc = CronService(store_path)

    async def _drive():
        await svc.start()
        svc.stop()

    asyncio.run(_drive())

    # Reload from disk — simulate a fresh reader
    svc2 = CronService(store_path)
    jobs = svc2.list_jobs(include_disabled=True)
    ids = sorted(j.id for j in jobs)
    assert "past_at" not in ids, "past-due at job must be dropped"
    assert "future_at" in ids, "future at job must survive"
    assert "every" in ids, "recurring job must survive"

    every_job = next(j for j in jobs if j.id == "every")
    assert every_job.state.next_run_at_ms and every_job.state.next_run_at_ms > now_ms, (
        "recurring job must have next_run advanced to the future"
    )


def test_cron_stale_claim_is_stolen(tmp_state_dir: Path):
    """If a peer's claim is older than CLAIM_TTL_MS, another process can
    take over — otherwise crashed peers would freeze their jobs forever."""
    import json

    from raven.proactive_engine.schedulers.cron.service import _CLAIM_TTL_MS

    store_path = tmp_state_dir / "jobs.json"
    svc = CronService(store_path)

    # Hand-build a store where a different pid claimed long ago.
    now_ms = int(time.time() * 1000)
    stale_data = {
        "version": 1,
        "jobs": [
            {
                "id": "j1",
                "name": "t",
                "enabled": True,
                "schedule": {"kind": "at", "atMs": now_ms - 60000, "everyMs": None, "expr": None, "tz": None},
                "payload": {"kind": "agent_turn", "message": "m", "deliver": False, "channel": None, "to": None},
                "state": {
                    "nextRunAtMs": now_ms - 60000,
                    "lastRunAtMs": None,
                    "lastStatus": None,
                    "lastError": None,
                    "claimedByPid": 999999,  # no such process
                    "claimedAtMs": now_ms - _CLAIM_TTL_MS - 1000,
                },
                "createdAtMs": now_ms - 60000,
                "updatedAtMs": now_ms - 60000,
                "deleteAfterRun": True,
            }
        ],
    }
    store_path.write_text(json.dumps(stale_data), encoding="utf-8")

    captured: list[str] = []

    async def cb(job):
        captured.append(job.id)

    svc.on_job = cb
    asyncio.run(svc._on_timer())
    assert captured == ["j1"], f"expected stale claim stolen, got {captured}"
