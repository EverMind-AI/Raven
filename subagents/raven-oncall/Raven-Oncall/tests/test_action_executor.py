"""Unit tests for ActionExecutor."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.sentinel.executor.action_executor import ActionExecutor
from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore
from raven.proactive_engine.sentinel.types import (
    PendingDecision,
    Routine,
    TaskOption,
)

_NOW = datetime(2026, 5, 8, 9, 0)
_NOW_MS = int(_NOW.timestamp() * 1000)


def _option(
    *,
    exec_kind: str = "reply",
    exec_payload: dict | None = None,
    type: str = "ad_hoc",
    title: str = "草拟回复 X",
) -> TaskOption:
    if exec_payload is None:
        exec_payload = {"prompt": "请帮我草拟回复 X"} if exec_kind == "reply" else {}
    return TaskOption(
        id="opt_test",
        title=title,
        why="why",
        type=type,
        exec_kind=exec_kind,
        exec_payload=exec_payload,
        created_at_ms=_NOW_MS,
    )


def _decision(channel: str = "feishu", to: str = "ou_xxx") -> PendingDecision:
    return PendingDecision(
        decision_id="dec_x",
        channel=channel,
        to=to,
        created_at_ms=_NOW_MS,
        ttl_min=60,
        options=[],
    )


# ── exec_kind=reply ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_submits_user_origin_turn_when_wired():
    # Spine path: with submit wired, the pick executes as a USER-origin turn
    # carrying sentinel.action_origin (its reply IS the user's intent, but
    # Sentinel must not re-count it as engagement / re-consume it).
    from raven.spine import Origin

    captured = {}

    class _Handle:
        def __init__(self):
            self.result_awaited = False

        async def result(self):
            self.result_awaited = True
            return None

    handle = _Handle()

    def _submit(req):
        captured["req"] = req
        return handle

    executor = ActionExecutor(now_fn=lambda: _NOW)
    executor.set_submit(_submit)

    result = await executor.execute(_option(), decision=_decision())

    assert result.status == "ok"
    req = captured["req"]
    assert req.origin is Origin.USER
    assert req.source.sender_id == "user"
    assert req.source.channel == "feishu" and req.source.chat_id == "ou_xxx"
    assert req.conversation == "feishu:ou_xxx"
    assert req.sentinel is not None and req.sentinel.action_origin is True
    # Fire-and-forget: the turn is enqueued but NOT awaited — awaiting result()
    # here would self-deadlock on the user lane once channels are on the spine
    # (this runs inside the /pick turn's hook on that same conversation).
    assert handle.result_awaited is False


@pytest.mark.asyncio
async def test_reply_missing_prompt_returns_error():
    executor = ActionExecutor(now_fn=lambda: _NOW)
    option = _option(exec_payload={})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "missing" in result.error.lower()


@pytest.mark.asyncio
async def test_reply_whitespace_only_prompt_returns_error():
    executor = ActionExecutor(now_fn=lambda: _NOW)
    option = _option(exec_payload={"prompt": "   "})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"


# ── exec_kind=routine_confirm ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_routine_confirm_upgrades_existing_routine(tmp_path: Path):
    routine_store = RoutineStore(tmp_path / "routines.json")
    routine_store.merge(
        [
            Routine(
                id="dow1-h09-meeting",
                pattern="Tuesday 09:00 — meeting",
                day_of_week=1,
                time_slot=(9, 12),
                occurrence_count=4,
            ),
        ],
        now_ms=_NOW_MS - 1000,
    )

    executor = ActionExecutor(
        routine_store=routine_store,
        now_fn=lambda: _NOW,
    )
    option = _option(
        type="routine_confirm",
        exec_kind="routine_confirm",
        exec_payload={"routine_id": "dow1-h09-meeting"},
        title="周二早会",
    )

    result = await executor.execute(option, decision=_decision())
    assert result.status == "ok"
    assert any("upgraded routine" in s for s in result.side_effects)
    upgraded = routine_store.get("dow1-h09-meeting")
    assert upgraded is not None
    assert upgraded.status == "active"
    assert upgraded.user_confirmed is True


@pytest.mark.asyncio
async def test_routine_confirm_no_routine_store_errors(tmp_path: Path):
    executor = ActionExecutor(now_fn=lambda: _NOW)
    option = _option(
        type="routine_confirm",
        exec_kind="routine_confirm",
        exec_payload={"routine_id": "dow1-h09-meeting"},
    )
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "routine_store" in result.error.lower()


@pytest.mark.asyncio
async def test_routine_confirm_unknown_routine_errors(tmp_path: Path):
    routine_store = RoutineStore(tmp_path / "routines.json")
    executor = ActionExecutor(
        routine_store=routine_store,
        now_fn=lambda: _NOW,
    )
    option = _option(
        type="routine_confirm",
        exec_kind="routine_confirm",
        exec_payload={"routine_id": "does-not-exist"},
    )
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_routine_confirm_with_make_cron_creates_job(tmp_path: Path):
    routine_store = RoutineStore(tmp_path / "routines.json")
    routine_store.merge(
        [
            Routine(
                id="dow1-h09-meeting",
                pattern="Tuesday 09:00 — meeting",
                day_of_week=1,
                time_slot=(9, 12),
                occurrence_count=4,
            ),
        ],
        now_ms=_NOW_MS - 1000,
    )

    cron_service = CronService(tmp_path / "jobs.json")

    executor = ActionExecutor(
        routine_store=routine_store,
        cron_service=cron_service,
        now_fn=lambda: _NOW,
    )
    option = _option(
        type="routine_confirm",
        exec_kind="routine_confirm",
        exec_payload={
            "routine_id": "dow1-h09-meeting",
            "make_cron": True,
            "cron_expr": "0 9 * * 1",
            "cron_message": "周二早会前 review PR",
        },
        title="周二早会",
    )

    result = await executor.execute(option, decision=_decision())
    assert result.status == "ok"
    side = "\n".join(result.side_effects)
    assert "upgraded routine" in side
    assert "created cron job" in side
    # And the job is in fact registered
    jobs = cron_service.list_jobs()
    assert any("周二早会前" in j.payload.message for j in jobs)


@pytest.mark.asyncio
async def test_routine_confirm_with_make_cron_but_missing_expr(tmp_path: Path):
    routine_store = RoutineStore(tmp_path / "routines.json")
    routine_store.merge(
        [
            Routine(id="dow1-h09-meeting", pattern="x", occurrence_count=4),
        ],
        now_ms=_NOW_MS - 1000,
    )

    executor = ActionExecutor(
        routine_store=routine_store,
        cron_service=CronService(tmp_path / "jobs.json"),
        now_fn=lambda: _NOW,
    )
    option = _option(
        type="routine_confirm",
        exec_kind="routine_confirm",
        exec_payload={
            "routine_id": "dow1-h09-meeting",
            "make_cron": True,
            # cron_expr deliberately missing
        },
    )
    result = await executor.execute(option, decision=_decision())
    # The routine is still upgraded; only the cron sub-step is skipped
    assert result.status == "ok"
    assert any("cron requested but" in s for s in result.side_effects)


# ── deferred exec_kinds ────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_exec_kind_unconfigured_errors():
    """When ActionExecutor has no tool_registry wired, exec_kind=tool
    returns an error explaining the missing dependency rather than
    silently no-op'ing. (Full happy-path coverage lives in
    test_action_executor_tool_spawn.py.)"""
    executor = ActionExecutor(now_fn=lambda: _NOW)
    option = _option(exec_kind="tool", exec_payload={"tool": "write_file", "args": {}})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "tool_registry" in result.error.lower()


@pytest.mark.asyncio
async def test_spawn_exec_kind_unconfigured_errors():
    """When ActionExecutor has no subagent_manager wired,
    exec_kind=spawn errors clearly. Full happy-path lives in
    test_action_executor_tool_spawn.py."""
    executor = ActionExecutor(now_fn=lambda: _NOW)
    option = _option(exec_kind="spawn", exec_payload={"task_description": "research X"})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "subagent_manager" in result.error.lower()


@pytest.mark.asyncio
async def test_unknown_exec_kind_returns_error():
    executor = ActionExecutor(now_fn=lambda: _NOW)
    option = _option(exec_kind="weird")
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "unknown" in result.error.lower()


@pytest.mark.asyncio
async def test_elapsed_ms_is_recorded():
    executor = ActionExecutor(now_fn=lambda: _NOW)
    option = _option()
    result = await executor.execute(option, decision=_decision())
    assert isinstance(result.elapsed_ms, int)
    assert result.elapsed_ms >= 0
