"""End-to-end integration of Sentinel's 4 nudge action paths.

Wires NudgePolicy + NudgeDispatcher + NudgeInjector + DeferManager together
without involving any real LLM or AgentLoop infra. Verifies:

- action=skip → nothing happens
- action=nudge → policy check → dispatcher posts a reply tagged
  _sentinel_origin=True
- action=nudge_inject → injector queues; callable returns appended content
- action=nudge_defer → manager registers; tick fires once session settled

This test is the minimum contract between Planner output and execution —
should stay green even as SentinelRunner lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from raven.config.raven import NudgePolicyConfig
from raven.proactive_engine.sentinel.executor.defer_manager import DeferManager
from raven.proactive_engine.sentinel.executor.dispatcher import (
    NudgeDispatcher,
    split_session_key,
)
from raven.proactive_engine.sentinel.executor.injector import NudgeInjector
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy
from raven.proactive_engine.sentinel.types import PlannerDecision


class Clock:
    def __init__(self, t0: datetime):
        self.t = t0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float):
        self.t = self.t + timedelta(seconds=seconds)


@dataclass
class FakeSession:
    updated_at: datetime


class FakeSessionStore:
    def __init__(self):
        self.sessions: dict[str, FakeSession] = {}

    def set(self, key: str, updated_at: datetime):
        self.sessions[key] = FakeSession(updated_at=updated_at)

    def __call__(self, key: str) -> FakeSession | None:
        return self.sessions.get(key)


def _cfg(**overrides) -> NudgePolicyConfig:
    defaults = dict(
        max_nudges_per_hour=10,
        max_nudges_per_day=50,
        min_interval_seconds=60,
        quiet_hours=(0, 0),  # disabled
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
class SentinelStack:
    """All Sentinel executors wired together for testing."""
    policy: NudgePolicy
    dispatcher: NudgeDispatcher
    injector: NudgeInjector
    defer: DeferManager
    clock: Clock
    sessions: FakeSessionStore
    posted: list

    async def execute(self, decision: PlannerDecision) -> dict:
        """Route one decision through the appropriate executor.

        Mirrors what SentinelRunner.dispatch() will do. For now,
        live here so we can test the executors in isolation.
        """
        action = decision.action
        content = decision.nudge_message or decision.spawn_task or ""
        session_key = decision.target_session or "sentinel:direct"

        if action == "skip":
            return {"action": "skip", "delivered": False, "reason": "skip_action"}

        check = self.policy.check(action, session_key, content, decision.priority)
        if check.verdict == "deny":
            return {"action": action, "delivered": False, "reason": f"policy:{check.reason}"}

        if action == "nudge":
            result = await self.dispatcher.dispatch(
                decision, [split_session_key(session_key)],
            )
            if result.delivered:
                self.policy.record_fired(action, session_key, content)
            return {
                "action": "nudge",
                "delivered": result.delivered,
                "reason": result.reason,
            }

        if action == "nudge_inject":
            self.injector.queue(session_key, content, source="e2e_test")
            self.policy.record_fired(action, session_key, content)
            return {"action": "nudge_inject", "delivered": True, "reason": "queued"}

        if action == "nudge_defer":
            defer_id = self.defer.register(decision, target_session=session_key)
            # NB: don't record_fired until the defer actually dispatches —
            # otherwise policy thinks the quota was consumed for a
            # not-yet-delivered nudge. The real SentinelRunner will use
            # DeferManager's on_dispatch callback for this.
            return {
                "action": "nudge_defer",
                "delivered": False,
                "reason": "deferred",
                "defer_id": defer_id,
            }

        return {"action": action, "delivered": False, "reason": "unknown_action"}


@pytest.fixture
def stack():
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    sessions = FakeSessionStore()
    policy = NudgePolicy(_cfg(), now_fn=clock)
    posted: list = []

    async def _post(msg):
        posted.append(msg)

    dispatcher = NudgeDispatcher(now_fn=clock)
    dispatcher.set_post(_post)
    injector = NudgeInjector(ttl_seconds=1800, now_fn=clock)
    defer = DeferManager(
        dispatcher, sessions,
        idle_threshold_seconds=300,
        max_wait_seconds=86400,
        now_fn=clock,
    )
    # Standalone (no SentinelRunner to auto-wire it); defer targets here are
    # real sessions, so a direct split is the equivalent resolution.
    defer.set_target_resolver(lambda ts: [split_session_key(ts)])
    return SentinelStack(
        policy=policy, dispatcher=dispatcher, injector=injector, defer=defer,
        clock=clock, sessions=sessions, posted=posted,
    )


def _decision(action: str, **overrides) -> PlannerDecision:
    defaults = dict(
        action=action,
        reason="e2e test",
        priority="low",
        proactivity_score=0.7,
        target_session="cli:direct",
        nudge_message="hello from sentinel",
    )
    if action == "spawn_agent":
        defaults["spawn_task"] = "do a thing"
    if action == "nudge_defer":
        defaults["defer_condition"] = "wait for settled"
    defaults.update(overrides)
    return PlannerDecision(**defaults)


# ---------------------------------------------------------------------------
# Skip

@pytest.mark.asyncio
async def test_skip_action_no_side_effects(stack):
    result = await stack.execute(_decision("skip"))
    assert result["action"] == "skip"
    assert result["delivered"] is False
    assert stack.posted == []
    assert stack.injector.size() == 0
    assert stack.defer.pending_count() == 0


# ---------------------------------------------------------------------------
# Plain nudge

@pytest.mark.asyncio
async def test_plain_nudge_publishes_outbound(stack):
    result = await stack.execute(_decision("nudge"))
    assert result["delivered"] is True
    # A nudge is a standalone proactive message — OUTBOUND, never re-entering
    # the agent loop (so the agent can't "act on" the reminder).
    assert len(stack.posted) == 1
    msg = stack.posted.pop(0)
    assert msg.source.channel == "cli"
    assert msg.source.chat_id == "direct"
    assert msg.source.extras["_sentinel_origin"] is True
    assert msg.source.extras["_sentinel_action"] == "nudge"


@pytest.mark.asyncio
async def test_plain_nudge_respects_policy(stack):
    # Fire once, then immediately retry → policy blocks on session cooldown.
    r1 = await stack.execute(_decision("nudge"))
    r2 = await stack.execute(_decision("nudge"))
    assert r1["delivered"] is True
    assert r2["delivered"] is False
    assert "session_cooldown" in r2["reason"]


# ---------------------------------------------------------------------------
# nudge_inject

@pytest.mark.asyncio
async def test_inject_queues_to_session(stack):
    result = await stack.execute(_decision("nudge_inject"))
    assert result["action"] == "nudge_inject"
    assert result["delivered"] is True
    assert stack.injector.size("cli:direct") == 1
    # The response_modifier callable protocol: next agent reply gets appended.
    modified = stack.injector("cli:direct", "agent original reply")
    assert "agent original reply" in modified
    assert "hello from sentinel" in modified


@pytest.mark.asyncio
async def test_inject_bypassed_when_not_target_session(stack):
    await stack.execute(_decision("nudge_inject", target_session="cli:direct"))
    # Agent reply on a different session — no injection.
    modified = stack.injector("telegram:home", "unrelated reply")
    assert modified == "unrelated reply"


# ---------------------------------------------------------------------------
# nudge_defer

@pytest.mark.asyncio
async def test_defer_register_then_fire_on_idle(stack):
    # Session has been idle long enough at registration-check time.
    result = await stack.execute(_decision("nudge_defer"))
    assert result["defer_id"] is not None
    assert result["delivered"] is False
    # Before idle threshold — no session means it fires on first check.
    stack.clock.advance(301)
    tick_results = await stack.defer.tick()
    assert len(tick_results) == 1
    assert tick_results[0].delivered is True
    # Verify the actual nudge landed on the bus with correct metadata.
    msg = stack.posted.pop(0)
    assert msg.content == "hello from sentinel"
    assert msg.source.extras["_sentinel_action"] == "nudge"
    assert msg.source.extras["_sentinel_origin"] is True


@pytest.mark.asyncio
async def test_defer_does_not_fire_while_session_active(stack):
    # Register + keep session active → not_settled until idle.
    stack.sessions.set("cli:direct", stack.clock())
    await stack.execute(_decision("nudge_defer"))
    stack.clock.advance(300)
    stack.sessions.set("cli:direct", stack.clock())  # user just did something
    tick_results = await stack.defer.tick()
    # Still pending, not delivered.
    assert all(not r.delivered for r in tick_results)
    assert stack.defer.pending_count() == 1


# ---------------------------------------------------------------------------
# Action coverage — all 4 non-skip actions exercised

@pytest.mark.asyncio
async def test_coverage_all_action_paths(stack):
    # One of each non-skip action, each on a different session to avoid
    # session cooldown blocking.
    actions = [
        ("nudge", "cli:one", "plain nudge content"),
        ("nudge_inject", "cli:two", "inject content"),
        ("nudge_defer", "cli:three", "defer content"),
    ]
    results = []
    for act, target, msg in actions:
        d = _decision(act, target_session=target, nudge_message=msg)
        results.append(await stack.execute(d))

    by_action = {r["action"]: r for r in results}
    assert by_action["nudge"]["delivered"] is True
    assert by_action["nudge_inject"]["delivered"] is True  # queued
    assert by_action["nudge_defer"]["defer_id"] is not None

    # Posted has plain nudge only (OUTBOUND); inject stays in injector; defer in mgr.
    assert len(stack.posted) == 1
    assert stack.injector.size("cli:two") == 1
    assert stack.defer.pending_count() == 1
