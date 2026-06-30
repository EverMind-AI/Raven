"""Unit tests for SentinelRunner.

Covers routing (skip / nudge / inject / defer / spawn), graceful
degradation when executors are absent, error isolation, and FeedbackTracker
integration. Mocks Planner + executors — no real LLM.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.config.raven import NudgePolicyConfig
from raven.proactive_engine.sentinel.executor.defer_manager import DeferManager
from raven.proactive_engine.sentinel.executor.dispatcher import NudgeDispatcher
from raven.proactive_engine.sentinel.executor.injector import NudgeInjector
from raven.proactive_engine.sentinel.executor.runner import SentinelRunner
from raven.proactive_engine.sentinel.executor.spawn import ProactiveSpawn
from raven.proactive_engine.sentinel.feedback.tracker import NudgeFeedbackTracker
from raven.proactive_engine.sentinel.predictor.context_assembler import ContextAssembler
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy
from raven.proactive_engine.sentinel.types import PlannerDecision


def _now():
    return datetime(2026, 4, 21, 14, 0, 0)


class _Clock:
    """Mutable clock for tests that need to advance time (engagement window,
    dismissal cooldown)."""

    def __init__(self, t0: datetime):
        self.t = t0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float):
        self.t = self.t + timedelta(seconds=seconds)


def _cfg(**overrides) -> NudgePolicyConfig:
    defaults = dict(
        max_nudges_per_hour=10,
        max_nudges_per_day=50,
        min_interval_seconds=60,
        quiet_hours=(0, 0),
        cooldown_on_dismiss_seconds=1800,
        high_priority_bypasses_limits=True,
        dedup_window_seconds=3600,
        inject_ttl_seconds=1800,
        inject_max_pending_per_session=3,
        defer_idle_threshold_seconds=300,
        defer_max_wait_seconds=86400,
    )
    defaults.update(overrides)
    return NudgePolicyConfig(**defaults)


@dataclass
class FakeSession:
    updated_at: datetime


class FakeSessionStore:
    def __init__(self):
        self.sessions: dict[str, FakeSession] = {}

    def set(self, k: str, ts: datetime):
        self.sessions[k] = FakeSession(updated_at=ts)

    def __call__(self, k: str) -> FakeSession | None:
        return self.sessions.get(k)


def _planner(decision: PlannerDecision) -> MagicMock:
    p = MagicMock()
    p.decide = AsyncMock(return_value=decision)
    return p


def _build_runner(
    decision: PlannerDecision,
    *,
    tmp_path=None,
    include_spawn=True,
    include_defer=True,
    include_injector=True,
    include_dispatcher=True,
    include_feedback=True,
    clock=None,
):
    now_fn = clock if clock is not None else _now
    policy = NudgePolicy(_cfg(), now_fn=now_fn)
    assembler = ContextAssembler(nudge_policy=policy, now_fn=now_fn)
    posted: list = []
    if include_dispatcher:
        dispatcher = NudgeDispatcher(now_fn=now_fn)

        async def _post(out, _posted=posted):
            _posted.append(out)

        dispatcher.set_post(_post)
    else:
        dispatcher = None
    injector = NudgeInjector(now_fn=now_fn) if include_injector else None
    sessions = FakeSessionStore()
    defer_mgr = DeferManager(dispatcher, sessions, now_fn=now_fn) if include_defer and dispatcher else None
    subagent_mgr = MagicMock()
    subagent_mgr.spawn = AsyncMock(return_value="Subagent started (id: abc).")
    spawn = ProactiveSpawn(subagent_mgr, policy, now_fn=now_fn) if include_spawn else None
    feedback = NudgeFeedbackTracker(tmp_path / "fb.jsonl", now_fn=now_fn) if include_feedback and tmp_path else None

    runner = SentinelRunner(
        planner=_planner(decision),
        assembler=assembler,
        policy=policy,
        dispatcher=dispatcher,
        injector=injector,
        defer_manager=defer_mgr,
        spawn=spawn,
        feedback=feedback,
        now_fn=now_fn,
    )
    return runner, {
        "policy": policy,
        "dispatcher": dispatcher,
        "posted": posted,
        "injector": injector,
        "defer_mgr": defer_mgr,
        "spawn": spawn,
        "feedback": feedback,
        "sessions": sessions,
        "subagent_mgr": subagent_mgr,
    }


def _decision(action: str, **kwargs) -> PlannerDecision:
    defaults = dict(
        action=action,
        reason="test",
        priority="low",
        proactivity_score=0.7,
        target_session="cli:direct",
        nudge_message="hello" if action in ("nudge", "nudge_inject", "nudge_defer") else None,
    )
    if action == "spawn_agent":
        defaults["spawn_task"] = "do the thing"
    if action == "nudge_defer":
        defaults["defer_condition"] = "wait for settled"
    defaults.update(kwargs)
    return PlannerDecision(**defaults)


# ---------------------------------------------------------------------------
# Nudge target resolution (sentinel:direct fan-out + empty-list fallback)


def test_resolve_nudge_targets_sentinel_fans_out_to_configured(tmp_path):
    # Regression: a daily-plan / deadline nudge targets sentinel:direct and
    # must resolve to the configured proactive targets even when the
    # discovery menu is off — these are populated independently of
    # task_discovery_enabled.
    runner, _ = _build_runner(_decision("skip"), tmp_path=tmp_path)
    runner.task_discovery_targets = [("cli", "direct"), ("feishu", "ou_x")]
    assert runner._resolve_nudge_targets("sentinel:direct") == [
        ("cli", "direct"),
        ("feishu", "ou_x"),
    ]


def test_resolve_nudge_targets_real_channel_direct(tmp_path):
    runner, _ = _build_runner(_decision("skip"), tmp_path=tmp_path)
    runner.task_discovery_targets = [("cli", "direct")]
    assert runner._resolve_nudge_targets("feishu:ou_abc") == [("feishu", "ou_abc")]


def test_resolve_nudge_targets_empty_falls_back_to_single_recent(tmp_path):
    # No configured targets → deliver to the SINGLE most-recent active
    # session, not a broadcast across every enabled channel.
    runner, _ = _build_runner(_decision("skip"), tmp_path=tmp_path)
    runner.task_discovery_targets = []
    cm = MagicMock()
    cm.enabled_channels = ["feishu", "telegram"]
    runner.set_channel_manager(cm)
    sm = MagicMock()
    sm.list_sessions = MagicMock(
        return_value=[
            {"key": "telegram:tg_99", "updated_at": "2026-04-21T10:00:00"},
            {"key": "feishu:ou_1", "updated_at": "2026-04-20T10:00:00"},
        ]
    )
    runner._delivery_session_manager = sm
    assert runner._resolve_nudge_targets("sentinel:direct") == [("telegram", "tg_99")]


def test_resolve_nudge_targets_empty_skips_disabled_channel(tmp_path):
    # The most-recent session is on a channel outside enabled_channels (e.g.
    # a stale feishu session in a REPL whose only real surface is cli) — skip
    # it and pick the most-recent enabled one.
    runner, _ = _build_runner(_decision("skip"), tmp_path=tmp_path)
    runner.task_discovery_targets = []
    cm = MagicMock()
    cm.enabled_channels = ["cli"]
    runner.set_channel_manager(cm)
    sm = MagicMock()
    sm.list_sessions = MagicMock(
        return_value=[
            {"key": "feishu:ou_stale", "updated_at": "2026-04-21T10:00:00"},
            {"key": "cli:direct", "updated_at": "2026-04-20T10:00:00"},
        ]
    )
    runner._delivery_session_manager = sm
    assert runner._resolve_nudge_targets("sentinel:direct") == [("cli", "direct")]


# ---------------------------------------------------------------------------
# Routing


@pytest.mark.asyncio
async def test_tick_skip_no_side_effects(tmp_path):
    runner, ctx = _build_runner(_decision("skip"), tmp_path=tmp_path)
    outcome = await runner.tick_once()
    assert outcome.route == "skip"
    assert outcome.result is None
    assert ctx["posted"] == []
    assert ctx["injector"].size() == 0


@pytest.mark.asyncio
async def test_tick_nudge_dispatches_and_records(tmp_path):
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path)
    outcome = await runner.tick_once()
    assert outcome.route == "nudge"
    assert outcome.result is not None
    assert outcome.result.delivered is True
    assert outcome.nudge_id is not None
    assert len(ctx["posted"]) == 1
    # Feedback + policy updated.
    assert ctx["policy"].snapshot_state()["nudges_used_this_hour"] == 1
    assert ctx["feedback"].counts()["dispatched"] == 1


@pytest.mark.asyncio
async def test_tick_inject_queues(tmp_path):
    runner, ctx = _build_runner(_decision("nudge_inject"), tmp_path=tmp_path)
    outcome = await runner.tick_once()
    assert outcome.route == "inject"
    assert outcome.result.delivered is True  # queued counts as delivered
    assert ctx["injector"].size("cli:direct") == 1
    assert ctx["feedback"].counts()["dispatched"] == 1


@pytest.mark.asyncio
async def test_tick_defer_registers(tmp_path):
    runner, ctx = _build_runner(_decision("nudge_defer"), tmp_path=tmp_path)
    outcome = await runner.tick_once()
    assert outcome.route == "defer"
    assert outcome.result.delivered is False  # deferred, not yet delivered
    assert outcome.result.defer_id is not None
    assert ctx["defer_mgr"].pending_count() == 1
    # Quota NOT consumed yet — see runner docstring.
    assert ctx["policy"].snapshot_state()["nudges_used_this_hour"] == 0


@pytest.mark.asyncio
async def test_tick_spawn_dispatches(tmp_path):
    runner, ctx = _build_runner(_decision("spawn_agent"), tmp_path=tmp_path)
    outcome = await runner.tick_once()
    assert outcome.route == "spawn"
    assert outcome.result.delivered is True
    ctx["subagent_mgr"].spawn.assert_awaited_once()
    assert ctx["feedback"].counts()["dispatched"] == 1


# ---------------------------------------------------------------------------
# Policy gating


@pytest.mark.asyncio
async def test_tick_nudge_denied_by_policy(tmp_path):
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path)
    # Pre-fill quota so the nudge is rejected.
    for i in range(10):
        ctx["policy"].record_fired("nudge", f"s{i}", f"m{i}")
    outcome = await runner.tick_once()
    assert outcome.route == "nudge_denied"
    assert outcome.result.delivered is False
    assert "policy:" in outcome.result.reason
    assert ctx["posted"] == []


# ---------------------------------------------------------------------------
# Graceful degradation


@pytest.mark.asyncio
async def test_tick_inject_without_injector_degrades(tmp_path):
    runner, ctx = _build_runner(_decision("nudge_inject"), tmp_path=tmp_path, include_injector=False)
    outcome = await runner.tick_once()
    assert outcome.route == "inject_degraded"
    assert outcome.result.delivered is False


@pytest.mark.asyncio
async def test_tick_defer_without_manager_degrades(tmp_path):
    runner, ctx = _build_runner(_decision("nudge_defer"), tmp_path=tmp_path, include_defer=False)
    outcome = await runner.tick_once()
    assert outcome.route == "defer_degraded"


@pytest.mark.asyncio
async def test_tick_spawn_without_spawner_degrades(tmp_path):
    runner, ctx = _build_runner(_decision("spawn_agent"), tmp_path=tmp_path, include_spawn=False)
    outcome = await runner.tick_once()
    assert outcome.route == "spawn_degraded"


@pytest.mark.asyncio
async def test_tick_without_feedback_still_works(tmp_path):
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path, include_feedback=False)
    outcome = await runner.tick_once()
    assert outcome.result.delivered is True
    assert len(ctx["posted"]) == 1


# ---------------------------------------------------------------------------
# Error isolation


@pytest.mark.asyncio
async def test_tick_planner_error_becomes_skip(tmp_path):
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path)
    runner.planner.decide = AsyncMock(side_effect=RuntimeError("boom"))
    outcome = await runner.tick_once()
    assert outcome.route == "error"
    assert outcome.decision.action == "skip"
    assert "planner_error" in outcome.decision.reason


@pytest.mark.asyncio
async def test_tick_dispatcher_error_caught(tmp_path):
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path)
    ctx["dispatcher"].dispatch = AsyncMock(side_effect=RuntimeError("boom"))
    outcome = await runner.tick_once()
    assert outcome.route == "nudge_error"
    assert outcome.result.delivered is False


# ---------------------------------------------------------------------------
# last_decision propagation


@pytest.mark.asyncio
async def test_last_decision_flows_to_next_tick(tmp_path):
    runner, ctx = _build_runner(_decision("skip"), tmp_path=tmp_path)
    await runner.tick_once()
    # Next assemble call should carry last_decision.
    next_ctx = runner.assembler.assemble()
    assert next_ctx.last_decision is not None
    assert next_ctx.last_decision.action == "skip"


# ---------------------------------------------------------------------------
# Lifecycle


@pytest.mark.asyncio
async def test_start_and_stop_without_crashing(tmp_path):
    runner, _ = _build_runner(_decision("skip"), tmp_path=tmp_path)
    runner.interval_s = 10000  # effectively: no tick during test
    await runner.start()
    assert runner._running is True
    await runner.stop()
    assert runner._running is False


@pytest.mark.asyncio
async def test_start_with_disabled_is_noop(tmp_path):
    runner, _ = _build_runner(_decision("skip"), tmp_path=tmp_path)
    runner.enabled = False
    await runner.start()
    assert runner._tick_task is None


@pytest.mark.asyncio
async def test_trigger_loop_drains_cli_triggers_within_seconds(tmp_path):
    """discover-now's name implies "now". Trigger drain runs on a fast
    2s poll, independent of the 10-min LLM tick, so a CLI-queued
    trigger fires within seconds — not up to ``interval_s``."""
    from raven.proactive_engine.sentinel.discover_triggers import (
        DiscoverTriggerStore,
    )

    runner, _ = _build_runner(_decision("skip"), tmp_path=tmp_path)
    runner.interval_s = 10000  # prove the fix isn't relying on tick
    store = DiscoverTriggerStore(tmp_path / "discover_triggers.json")
    runner._discover_trigger_store = store
    called: list[tuple[str, str]] = []

    async def _capture(channel, to):
        called.append((channel, to))

    runner.discover_now = _capture  # type: ignore[method-assign]
    runner._TRIGGER_POLL_INTERVAL_S = 0.05  # fast for test
    await runner.start()
    try:
        store.add("feishu", "ou_xxx")
        await asyncio.sleep(0.2)
    finally:
        await runner.stop()
    assert ("feishu", "ou_xxx") in called


@pytest.mark.asyncio
async def test_trigger_loop_stops_cleanly(tmp_path):
    """stop() must cancel the trigger task so the gateway shuts down
    promptly without leaking a background coroutine."""
    from raven.proactive_engine.sentinel.discover_triggers import (
        DiscoverTriggerStore,
    )

    runner, _ = _build_runner(_decision("skip"), tmp_path=tmp_path)
    runner.interval_s = 10000
    runner._discover_trigger_store = DiscoverTriggerStore(
        tmp_path / "discover_triggers.json",
    )
    await runner.start()
    assert runner._trigger_task is not None
    await runner.stop()
    assert runner._trigger_task is None


@pytest.mark.asyncio
async def test_trigger_loop_not_started_when_store_is_none(tmp_path):
    """REPL agents pass include_discover_triggers=False to
    build_sentinel_stack — runner gets discover_trigger_store=None and
    must not spawn the trigger loop. Without this guard REPL would race
    the gateway for triggers and consume feishu menus it cannot dispatch."""
    runner, _ = _build_runner(_decision("skip"), tmp_path=tmp_path)
    runner.interval_s = 10000
    assert runner._discover_trigger_store is None  # default from _build_runner
    await runner.start()
    try:
        assert runner._trigger_task is None
    finally:
        await runner.stop()


# ---------------------------------------------------------------------------
# Engagement tracking — on_user_inbound accept/dismiss handler

from dataclasses import dataclass as _dc

from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, SentinelExtras, TurnRequest


def _FakeInbound(
    *,
    channel: str = "cli",
    chat_id: str = "direct",
    content: str = "thanks for the reminder",
    sentinel_action_origin: bool = False,
) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel=channel,
            chat_id=chat_id,
            sender_id="user",
            chat_type=ChatType.DM,
        ),
        text=content,
        sentinel=(SentinelExtras(action_origin=True) if sentinel_action_origin else None),
    )


@pytest.mark.asyncio
async def test_on_user_inbound_defers_non_dismiss_to_llm(tmp_path):
    """Non-/dismiss replies move the nudge to ``_awaiting_llm_feedback``
    for the main LLM to classify via the nudge_feedback tool — they are
    NOT immediately recorded as accepted (the legacy behavior was a
    reverse-signal bug: "不要提醒了" / "stop reminding me" got logged
    as ACCEPTED and tightened the adaptive quota in the wrong
    direction)."""
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path)
    await runner.tick_once()  # dispatch one nudge
    runner.on_user_inbound(_FakeInbound(channel="cli", chat_id="direct", content="thanks!"))
    counts = ctx["feedback"].counts()
    # Crucially: no accepted/dismissed/neutral on the inbound hook
    # itself — the LLM hasn't run yet.
    assert counts["dispatched"] == 1
    assert counts["accepted"] == 0
    assert counts["dismissed"] == 0
    assert counts["neutral"] == 0
    # Original pending is drained; nudge moved into awaiting queue.
    assert runner._pending_engagement.get("cli:direct", []) == []
    awaiting = runner._awaiting_llm_feedback.get("cli:direct", [])
    assert len(awaiting) == 1


@pytest.mark.asyncio
async def test_on_user_inbound_skips_action_origin_turn(tmp_path):
    """A menu-pick execution (sentinel_action_origin) is not a new nudge
    reaction — the accept was already recorded at /pick. on_user_inbound must
    skip it, so the pending nudge is left untouched (no double-count). On the
    spine path this turn is origin=USER (so the user-inbound chain runs), so the
    guard is what restores the legacy whole-chain-skip behavior for it."""
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path)
    await runner.tick_once()  # dispatch one nudge
    runner.on_user_inbound(
        _FakeInbound(channel="cli", chat_id="direct", content="请帮我草拟回复", sentinel_action_origin=True)
    )
    counts = ctx["feedback"].counts()
    # No engagement recorded; the pending nudge is NOT moved/dismissed.
    assert counts["accepted"] == 0
    assert counts["dismissed"] == 0
    assert counts["neutral"] == 0
    assert len(runner._pending_engagement.get("cli:direct", [])) == 1
    assert runner._awaiting_llm_feedback.get("cli:direct", []) == []


@pytest.mark.asyncio
async def test_consume_feedback_via_tool_records_accepted(tmp_path):
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path)
    await runner.tick_once()
    runner.on_user_inbound(_FakeInbound(content="great, thanks"))
    outcome = runner.consume_feedback_via_tool(
        "cli:direct",
        sentiment="accepted",
        reason="thanked us",
    )
    assert outcome["recorded"] is True
    assert outcome["signal"] == "accepted"
    counts = ctx["feedback"].counts()
    assert counts["accepted"] == 1
    assert counts["dismissed"] == 0
    assert runner._awaiting_llm_feedback.get("cli:direct", []) == []


@pytest.mark.asyncio
async def test_consume_feedback_via_tool_records_dismissed(tmp_path):
    """Natural-language dismissal classified by the main LLM: behaves
    identically to the deterministic /dismiss fast path (records
    dismissed + applies session cooldown via NudgePolicy)."""
    clock = _Clock(datetime(2026, 4, 21, 14, 0, 0))
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path, clock=clock)
    await runner.tick_once()
    runner.on_user_inbound(_FakeInbound(content="不要提醒了"))
    outcome = runner.consume_feedback_via_tool(
        "cli:direct",
        sentiment="dismissed",
        reason="不要提醒了",
    )
    assert outcome["signal"] == "dismissed"
    counts = ctx["feedback"].counts()
    assert counts["dismissed"] == 1
    assert counts["accepted"] == 0
    # Cooldown applied — match the /dismiss fast-path test's assertion.
    clock.advance(65)
    verdict = ctx["policy"].check("nudge", "cli:direct", "other msg", "low")
    assert verdict.verdict == "deny"
    assert "dismissed" in verdict.reason


@pytest.mark.asyncio
async def test_consume_feedback_via_tool_records_neutral_on_irrelevant(tmp_path):
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path)
    await runner.tick_once()
    runner.on_user_inbound(_FakeInbound(content="oh by the way, what's the weather"))
    outcome = runner.consume_feedback_via_tool(
        "cli:direct",
        sentiment="irrelevant",
        reason="user switched topic",
    )
    assert outcome["signal"] == "neutral"
    counts = ctx["feedback"].counts()
    assert counts["neutral"] == 1
    assert counts["accepted"] == 0
    assert counts["dismissed"] == 0


@pytest.mark.asyncio
async def test_consume_feedback_via_tool_no_awaiting_is_noop(tmp_path):
    runner, ctx = _build_runner(_decision("skip"), tmp_path=tmp_path)
    outcome = runner.consume_feedback_via_tool(
        "cli:direct",
        sentiment="accepted",
    )
    assert outcome["recorded"] is False
    assert outcome["reason"] == "no_awaiting_nudge"
    assert ctx["feedback"].counts()["accepted"] == 0


@pytest.mark.asyncio
async def test_finalize_pending_feedback_records_neutral(tmp_path):
    """When the turn ends without the LLM calling nudge_feedback, the
    after_send hook flushes anything still awaiting as NEUTRAL — by
    design this never falls back to ACCEPTED."""
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path)
    await runner.tick_once()
    runner.on_user_inbound(_FakeInbound(content="hmm"))
    # LLM didn't call the tool — simulate after_send.
    n = runner.finalize_pending_feedback("cli:direct")
    assert n == 1
    counts = ctx["feedback"].counts()
    assert counts["neutral"] == 1
    assert counts["accepted"] == 0
    assert runner._awaiting_llm_feedback.get("cli:direct", []) == []


@pytest.mark.asyncio
async def test_on_user_inbound_dismiss_command_marks_dismissed(tmp_path):
    clock = _Clock(datetime(2026, 4, 21, 14, 0, 0))
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path, clock=clock)
    await runner.tick_once()
    runner.on_user_inbound(_FakeInbound(content="/dismiss not helpful"))
    counts = ctx["feedback"].counts()
    assert counts["accepted"] == 0
    assert counts["dismissed"] == 1
    # Advance past session cooldown (60s) so we can verify the dismissed
    # cooldown (1800s) is what's blocking — not the session-cooldown.
    clock.advance(65)
    verdict = ctx["policy"].check("nudge", "cli:direct", "other msg", "low")
    assert verdict.verdict == "deny"
    assert "dismissed" in verdict.reason


@pytest.mark.asyncio
async def test_on_user_inbound_no_pending_is_noop(tmp_path):
    runner, ctx = _build_runner(_decision("skip"), tmp_path=tmp_path)
    runner.on_user_inbound(_FakeInbound(content="hi"))
    assert ctx["feedback"].counts()["accepted"] == 0


@pytest.mark.asyncio
async def test_on_user_inbound_outside_window_ignored(tmp_path):
    clock = _Clock(datetime(2026, 4, 21, 14, 0, 0))
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path, clock=clock)
    runner._engagement_window = 60  # 60s window
    await runner.tick_once()
    # Advance clock beyond window.
    clock.advance(120)
    runner.on_user_inbound(_FakeInbound(content="later reply"))
    # No accepted recorded — stale.
    assert ctx["feedback"].counts()["accepted"] == 0


@pytest.mark.asyncio
async def test_on_user_inbound_different_session_untouched(tmp_path):
    runner, ctx = _build_runner(_decision("nudge"), tmp_path=tmp_path)
    await runner.tick_once()  # dispatch on cli:direct
    runner.on_user_inbound(_FakeInbound(channel="telegram", chat_id="home", content="reply on different session"))
    assert ctx["feedback"].counts()["accepted"] == 0
    # Original pending is still there — different session, untouched.
    assert len(runner._pending_engagement.get("cli:direct", [])) == 1
    # And nothing got deferred to awaiting on the unrelated session.
    assert runner._awaiting_llm_feedback.get("telegram:home", []) == []


@pytest.mark.asyncio
async def test_on_user_inbound_handles_missing_session_key(tmp_path):
    runner, _ = _build_runner(_decision("skip"), tmp_path=tmp_path)

    # Object without session_key property — falls back to channel+chat_id.
    @_dc
    class MinimalMsg:
        channel: str = ""
        chat_id: str = ""
        content: str = ""
        metadata: dict | None = None

    runner.on_user_inbound(MinimalMsg())  # should not raise


# ---------------------------------------------------------------------------
# Cross-process engagement persistence (JsonStateStore-backed)
#
# These cover the longrun / gateway split where ``sentinel ticks --live``
# dispatches in one subprocess and ``agent --message`` handles user replies
# in another. Without the store the in-memory ``_pending_engagement`` /
# ``_awaiting_llm_feedback`` dicts vanish at subprocess exit, leaving
# every reply unable to correlate back to its dispatch — meaning the new
# nudge_feedback tool path can never be exercised.

from raven.proactive_engine.sentinel.feedback.persistence import JsonStateStore


def _build_runner_with_store(
    decision: PlannerDecision,
    *,
    tmp_path,
    store: JsonStateStore,
    clock=None,
):
    """Like ``_build_runner`` but threads ``store`` so engagement state
    persists across constructions."""
    now_fn = clock if clock is not None else _now
    policy = NudgePolicy(_cfg(), store=store, now_fn=now_fn)
    assembler = ContextAssembler(nudge_policy=policy, now_fn=now_fn)
    dispatcher = NudgeDispatcher(now_fn=now_fn)
    dispatcher.set_post(AsyncMock())
    injector = NudgeInjector(store=store, now_fn=now_fn)
    defer_mgr = DeferManager(
        dispatcher,
        FakeSessionStore(),
        store=store,
        now_fn=now_fn,
    )
    feedback = NudgeFeedbackTracker(tmp_path / "fb.jsonl", now_fn=now_fn)
    runner = SentinelRunner(
        planner=_planner(decision),
        assembler=assembler,
        policy=policy,
        dispatcher=dispatcher,
        injector=injector,
        defer_manager=defer_mgr,
        spawn=None,
        feedback=feedback,
        store=store,
        now_fn=now_fn,
    )
    return runner, {"policy": policy, "feedback": feedback}


@pytest.mark.asyncio
async def test_engagement_persists_across_runner_instances(tmp_path):
    """Runner A dispatches; a fresh runner B (same store) sees the
    pending nudge after construction — proves cross-subprocess
    correlation works in the longrun architecture."""
    state_path = tmp_path / "state.json"
    store_a = JsonStateStore(state_path)
    store_b = JsonStateStore(state_path)

    runner_a, _ = _build_runner_with_store(
        _decision("nudge"),
        tmp_path=tmp_path,
        store=store_a,
    )
    await runner_a.tick_once()  # dispatches one nudge → writes engagement
    assert "cli:direct" in runner_a._pending_engagement

    # Fresh runner with the same store — hydrates engagement from disk.
    runner_b, _ = _build_runner_with_store(
        _decision("skip"),
        tmp_path=tmp_path,
        store=store_b,
    )
    assert "cli:direct" in runner_b._pending_engagement, (
        "engagement state should be visible to a peer runner via the shared JsonStateStore"
    )
    assert len(runner_b._pending_engagement["cli:direct"]) == 1


@pytest.mark.asyncio
async def test_cross_instance_dismiss(tmp_path):
    """Dispatch in instance A; /dismiss inbound on instance B → instance
    B records the dismissal correctly, finds the nudge_id via the store."""
    clock = _Clock(datetime(2026, 4, 21, 14, 0, 0))
    state_path = tmp_path / "state.json"
    store_a = JsonStateStore(state_path)
    runner_a, _ = _build_runner_with_store(
        _decision("nudge"),
        tmp_path=tmp_path,
        store=store_a,
        clock=clock,
    )
    await runner_a.tick_once()

    store_b = JsonStateStore(state_path)
    runner_b, ctx_b = _build_runner_with_store(
        _decision("skip"),
        tmp_path=tmp_path,
        store=store_b,
        clock=clock,
    )
    runner_b.on_user_inbound(_FakeInbound(content="/dismiss not helpful"))
    counts = ctx_b["feedback"].counts()
    assert counts["dismissed"] == 1
    # And the pending queue is now empty in the shared store.
    runner_c, _ = _build_runner_with_store(
        _decision("skip"),
        tmp_path=tmp_path,
        store=JsonStateStore(state_path),
        clock=clock,
    )
    assert runner_c._pending_engagement.get("cli:direct", []) == []


@pytest.mark.asyncio
async def test_cross_instance_consume_feedback_via_tool(tmp_path):
    """Dispatch in A; non-/dismiss inbound on B → defers to awaiting
    queue persisted via store. Instance C calls consume_feedback_via_tool
    and finds the awaiting entry."""
    state_path = tmp_path / "state.json"
    runner_a, _ = _build_runner_with_store(
        _decision("nudge"),
        tmp_path=tmp_path,
        store=JsonStateStore(state_path),
    )
    await runner_a.tick_once()

    runner_b, _ = _build_runner_with_store(
        _decision("skip"),
        tmp_path=tmp_path,
        store=JsonStateStore(state_path),
    )
    runner_b.on_user_inbound(_FakeInbound(content="thanks!"))
    # Deferred — neither accepted nor dismissed yet.
    assert runner_b._pending_engagement.get("cli:direct", []) == []
    assert "cli:direct" in runner_b._awaiting_llm_feedback

    # New instance C: simulates the in-ReAct nudge_feedback tool call.
    runner_c, ctx_c = _build_runner_with_store(
        _decision("skip"),
        tmp_path=tmp_path,
        store=JsonStateStore(state_path),
    )
    outcome = runner_c.consume_feedback_via_tool(
        "cli:direct",
        sentiment="accepted",
        reason="thanked",
    )
    assert outcome["recorded"] is True
    assert outcome["signal"] == "accepted"
    assert ctx_c["feedback"].counts()["accepted"] == 1
