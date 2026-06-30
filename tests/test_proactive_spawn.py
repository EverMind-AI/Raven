"""Unit tests for ProactiveSpawn.

Mocks SubagentManager (real spawn has heavy deps — LLM provider, tool
registry, real workspace). Verifies the gating + wiring contract.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.config.raven import NudgePolicyConfig
from raven.proactive_engine.sentinel.executor.spawn import ProactiveSpawn
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy
from raven.proactive_engine.sentinel.types import PlannerDecision


def _now():
    return datetime(2026, 4, 21, 14, 0, 0)


def _cfg(**overrides) -> NudgePolicyConfig:
    defaults = dict(
        max_nudges_per_hour=5,
        max_nudges_per_day=20,
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


def _decision(**overrides) -> PlannerDecision:
    defaults = dict(
        action="spawn_agent",
        reason="test spawn",
        priority="low",
        proactivity_score=0.8,
        target_session="cli:direct",
        spawn_task="run a health check and summarize findings",
    )
    defaults.update(overrides)
    return PlannerDecision(**defaults)


@pytest.fixture
def policy():
    return NudgePolicy(_cfg(), now_fn=_now)


@pytest.fixture
def mock_subagent_mgr():
    mgr = MagicMock()
    mgr.spawn = AsyncMock(return_value="Subagent [foo] started (id: abc123).")
    return mgr


@pytest.fixture
def spawner(mock_subagent_mgr, policy):
    return ProactiveSpawn(mock_subagent_mgr, policy, now_fn=_now)


# ---------------------------------------------------------------------------
# Happy path


@pytest.mark.asyncio
async def test_dispatch_happy_path(spawner, mock_subagent_mgr):
    result = await spawner.dispatch(_decision())
    assert result.delivered is True
    assert result.reason == "spawned"
    assert "task_id" in result.details
    mock_subagent_mgr.spawn.assert_awaited_once()
    kwargs = mock_subagent_mgr.spawn.await_args.kwargs
    assert kwargs["task"] == "run a health check and summarize findings"
    assert kwargs["origin_channel"] == "cli"
    assert kwargs["origin_chat_id"] == "direct"
    assert kwargs["session_key"] == "cli:direct"
    assert "sentinel" in kwargs["label"]


@pytest.mark.asyncio
async def test_dispatch_records_in_policy(spawner, policy):
    # Before: no history.
    assert policy.snapshot_state()["nudges_used_this_hour"] == 0
    await spawner.dispatch(_decision())
    assert policy.snapshot_state()["nudges_used_this_hour"] == 1


# ---------------------------------------------------------------------------
# Rejection paths


@pytest.mark.asyncio
async def test_dispatch_rejects_wrong_action(spawner, mock_subagent_mgr):
    for act in ("skip", "nudge", "nudge_inject", "nudge_defer"):
        r = await spawner.dispatch(_decision(action=act))
        assert r.delivered is False
        assert act in r.reason
    mock_subagent_mgr.spawn.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_rejects_empty_task(spawner):
    r = await spawner.dispatch(_decision(spawn_task=None))
    assert r.delivered is False
    assert r.reason == "empty_spawn_task"


@pytest.mark.asyncio
async def test_dispatch_blocked_by_policy(spawner, policy, mock_subagent_mgr):
    # Fill the hour quota on the policy so check() denies.
    for i in range(5):  # max_nudges_per_hour=5
        policy.record_fired("nudge", f"other{i}", f"msg{i}")
    r = await spawner.dispatch(_decision(target_session="cli:new"))
    assert r.delivered is False
    assert "policy" in r.reason
    mock_subagent_mgr.spawn.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling


@pytest.mark.asyncio
async def test_dispatch_handles_spawn_exception(policy):
    failing_mgr = MagicMock()
    failing_mgr.spawn = AsyncMock(side_effect=RuntimeError("boom"))
    spawner = ProactiveSpawn(failing_mgr, policy, now_fn=_now)
    r = await spawner.dispatch(_decision())
    assert r.delivered is False
    assert r.reason.startswith("spawn_error:RuntimeError")
    # Policy should NOT have recorded — the spawn failed.
    assert policy.snapshot_state()["nudges_used_this_hour"] == 0


# ---------------------------------------------------------------------------
# Session parsing


@pytest.mark.asyncio
async def test_dispatch_parses_session_key(spawner, mock_subagent_mgr):
    await spawner.dispatch(_decision(target_session="telegram:home:12345"))
    kwargs = mock_subagent_mgr.spawn.await_args.kwargs
    # split_session_key keeps extra colons in chat_id.
    assert kwargs["origin_channel"] == "telegram"
    assert kwargs["origin_chat_id"] == "home:12345"


@pytest.mark.asyncio
async def test_dispatch_defaults_missing_target(spawner, mock_subagent_mgr):
    await spawner.dispatch(_decision(target_session=None))
    kwargs = mock_subagent_mgr.spawn.await_args.kwargs
    assert kwargs["origin_channel"] == "sentinel"


# ---------------------------------------------------------------------------
# Details carry task_id


@pytest.mark.asyncio
async def test_result_details_contains_task_id(spawner, mock_subagent_mgr):
    mock_subagent_mgr.spawn.return_value = "Subagent [foo] started (id: zzz999)."
    r = await spawner.dispatch(_decision())
    assert r.details is not None
    assert "task_id" in r.details
    # Exact content is the full spawn() return message; just ensure non-empty.
    assert r.details["task_id"]
