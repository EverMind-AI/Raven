"""Unit tests for CronTool argument handling (raven/proactive_engine/schedulers/cron/tool.py).

Focus: ``tz`` is only meaningful for a cron-expression schedule. For an
every/at schedule the tool ignores it (rather than erroring) so the agent does
not have to retry just to drop a no-op arg.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from raven.proactive_engine.schedulers.cron.tool import CronTool


def _tool() -> tuple[CronTool, MagicMock]:
    cron = MagicMock()
    cron.add_job.return_value = SimpleNamespace(id="j1", name="reminder")
    tool = CronTool(cron)
    tool.set_context("tui", "default")
    return tool, cron


async def test_add_every_with_tz_is_tolerated_and_dropped() -> None:
    tool, cron = _tool()
    result = await tool.execute(action="add", message="drink water", every_seconds=60, tz="Asia/Shanghai")
    assert result.startswith("Created job")  # not an error
    schedule = cron.add_job.call_args.kwargs["schedule"]
    assert schedule.kind == "every"
    assert getattr(schedule, "tz", None) is None  # tz dropped — meaningless for every


async def test_add_naive_at_with_tz_anchors_to_that_zone() -> None:
    # A1: a naive `at` + tz means "that wall-clock time in tz" — the stored at_ms
    # must match the tz, not the host's local zone.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tool, cron = _tool()
    result = await tool.execute(action="add", message="ping", at="2026-06-24T15:00:00", tz="Asia/Shanghai")
    assert result.startswith("Created job")
    schedule = cron.add_job.call_args.kwargs["schedule"]
    assert schedule.kind == "at"
    expected_ms = int(datetime(2026, 6, 24, 15, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    assert schedule.at_ms == expected_ms


async def test_add_offset_aware_at_ignores_tz_param() -> None:
    # An offset-aware string already carries its zone; the tz param is ignored.
    from datetime import datetime

    tool, cron = _tool()
    await tool.execute(action="add", message="ping", at="2026-06-24T15:00:00+09:00", tz="Asia/Shanghai")
    schedule = cron.add_job.call_args.kwargs["schedule"]
    expected_ms = int(datetime.fromisoformat("2026-06-24T15:00:00+09:00").timestamp() * 1000)
    assert schedule.at_ms == expected_ms


async def test_non_runnable_schedule_surfaces_service_error() -> None:
    # The service rejects a non-runnable schedule with ValueError; the tool
    # translates it to an "Error: ..." string so the agent can retry.
    tool, cron = _tool()
    cron.add_job.side_effect = ValueError("at time is in the past")
    result = await tool.execute(action="add", message="late", at="2020-01-01T00:00:00")
    assert result == "Error: at time is in the past"


async def test_add_cron_expr_with_valid_tz_uses_it() -> None:
    tool, cron = _tool()
    result = await tool.execute(action="add", message="daily standup", cron_expr="0 9 * * *", tz="Asia/Shanghai")
    assert result.startswith("Created job")
    schedule = cron.add_job.call_args.kwargs["schedule"]
    assert schedule.kind == "cron" and schedule.tz == "Asia/Shanghai"


async def test_add_cron_expr_with_bad_tz_still_errors() -> None:
    tool, cron = _tool()
    result = await tool.execute(action="add", message="daily", cron_expr="0 9 * * *", tz="Not/AZone")
    assert "unknown timezone" in result
    cron.add_job.assert_not_called()

async def test_cron_tool_context_isolation() -> None:
    """Two concurrent turns share one CronTool instance (the gateway's user
    pool is 4). The route must be turn-local: with plain attributes, turn B's
    set_context redirected turn A's reminder to B's conversation."""
    import asyncio

    tool, cron = _tool()
    seen: dict[str, tuple[str, str]] = {}

    async def turn(name: str, channel: str, chat_id: str) -> None:
        tool.set_context(channel, chat_id)
        await asyncio.sleep(0)  # yield so the other turn's set_context runs
        seen[name] = (tool._channel, tool._chat_id)

    await asyncio.gather(turn("a", "telegram", "chat-a"), turn("b", "discord", "chat-b"))

    assert seen["a"] == ("telegram", "chat-a")
    assert seen["b"] == ("discord", "chat-b")
