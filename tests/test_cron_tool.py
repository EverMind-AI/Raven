"""Unit tests for CronTool argument handling (raven/proactive_engine/schedulers/cron/tool.py).

Focus: ``tz`` is only meaningful for a cron-expression schedule. For an
every/at schedule the tool ignores it (rather than erroring) so the agent does
not have to retry just to drop a no-op arg.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from raven.proactive_engine.schedulers.cron.tool import CronTool
from raven.proactive_engine.schedulers.cron.types import AddJobOutcome, CronSchedule


def _tool(schedule: CronSchedule | None = None) -> tuple[CronTool, MagicMock]:
    cron = MagicMock()
    job = SimpleNamespace(
        id="j1", name="reminder", schedule=schedule or CronSchedule(kind="cron", expr="0 9 * * *", tz=None)
    )
    cron.add_job_with_outcome.return_value = AddJobOutcome(job=job, outcome="created", schedule_replaced=True)
    tool = CronTool(cron)
    tool.set_context("tui", "default")
    return tool, cron


async def test_the_reply_names_the_zone_the_job_will_keep() -> None:
    """The job's zone is decided at creation, and the agent's clock is the
    machine's, so the reply is the only place either side learns that a
    configured zone moved the reminder.

    Driven through the real ``_zone_note`` rather than a mocked schedule: the
    note is the change, and a stub would assert its own text back.
    """
    for schedule, expected in (
        (CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Tokyo"), ", fires on Asia/Tokyo"),
        (CronSchedule(kind="cron", expr="0 9 * * *"), ", fires on this machine's local time"),
        (CronSchedule(kind="at", at_ms=1_800_000_000_000), ""),
        (CronSchedule(kind="every", every_ms=60_000), ""),
    ):
        tool, _ = _tool(schedule)
        reply = await tool.execute(action="add", message="daily", cron_expr="0 9 * * *")
        assert reply == f"Created job 'reminder' (id: j1){expected}", reply


async def test_the_tz_description_states_what_an_omitted_zone_means() -> None:
    """Seen in review: the schema named the parameter without saying which zone
    an omitted one resolves to, which is the one thing the agent has to reason
    about to answer "when will it fire".
    """
    tool, _ = _tool()
    described = tool.parameters["properties"]["tz"]["description"]
    assert "configured cron timezone" in described
    assert "machine's local time" in described
    assert "`at` stays on the machine's local time" in described


@pytest.mark.parametrize("explicit", [None, "UTC"])
async def test_tool_uses_the_runtime_default_timezone(tmp_path, monkeypatch, explicit):
    from raven.core.cron_stack import build_cron_service

    config = tmp_path / "config.json"
    config.write_text('{"cron": {"defaultTimezone": "Asia/Tokyo"}}', encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", config)
    cron = build_cron_service(allowed_channels=None)
    tool = CronTool(cron)
    tool.set_context("tui", "default")
    result = await tool.execute(action="add", message="daily", cron_expr="0 9 * * *", tz=explicit)
    assert result.startswith("Created job"), result
    assert result.endswith(f", fires on {explicit or 'Asia/Tokyo'}"), result
    assert cron.list_jobs()[0].schedule.tz == (explicit or "Asia/Tokyo")


async def test_add_every_with_tz_is_tolerated_and_dropped() -> None:
    tool, cron = _tool()
    result = await tool.execute(action="add", message="drink water", every_seconds=60, tz="Asia/Shanghai")
    assert result.startswith("Created job")  # not an error
    schedule = cron.add_job_with_outcome.call_args.kwargs["schedule"]
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
    schedule = cron.add_job_with_outcome.call_args.kwargs["schedule"]
    assert schedule.kind == "at"
    expected_ms = int(datetime(2026, 6, 24, 15, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    assert schedule.at_ms == expected_ms


async def test_add_offset_aware_at_ignores_tz_param() -> None:
    # An offset-aware string already carries its zone; the tz param is ignored.
    from datetime import datetime

    tool, cron = _tool()
    await tool.execute(action="add", message="ping", at="2026-06-24T15:00:00+09:00", tz="Asia/Shanghai")
    schedule = cron.add_job_with_outcome.call_args.kwargs["schedule"]
    expected_ms = int(datetime.fromisoformat("2026-06-24T15:00:00+09:00").timestamp() * 1000)
    assert schedule.at_ms == expected_ms


async def test_non_runnable_schedule_surfaces_service_error() -> None:
    # The service rejects a non-runnable schedule with ValueError; the tool
    # translates it to an "Error: ..." string so the agent can retry.
    tool, cron = _tool()
    cron.add_job_with_outcome.side_effect = ValueError("at time is in the past")
    result = await tool.execute(action="add", message="late", at="2020-01-01T00:00:00")
    assert result == "Error: at time is in the past"


async def test_add_cron_expr_with_valid_tz_uses_it() -> None:
    tool, cron = _tool()
    result = await tool.execute(action="add", message="daily standup", cron_expr="0 9 * * *", tz="Asia/Shanghai")
    assert result.startswith("Created job")
    schedule = cron.add_job_with_outcome.call_args.kwargs["schedule"]
    assert schedule.kind == "cron" and schedule.tz == "Asia/Shanghai"


async def test_add_cron_expr_with_bad_tz_still_errors() -> None:
    tool, cron = _tool()
    result = await tool.execute(action="add", message="daily", cron_expr="0 9 * * *", tz="Not/AZone")
    assert "unknown timezone" in result
    cron.add_job_with_outcome.assert_not_called()


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


async def test_an_add_without_a_message_says_what_to_put_there() -> None:
    """Seen live: asked for a daily gold-price push, the model sent add eight
    times without `message`, reading it as "reminder text" when the job was a
    task; the bare "message is required" never said what belongs there."""
    tool, cron = _tool()
    described = tool.parameters["properties"]["message"]["description"]
    assert "Required for add" in described and "task" in described and "instruction" in described
    reply = await tool.execute(action="add", cron_expr="0 12 * * *", tz="Asia/Shanghai")
    assert "nothing was scheduled" in reply and "the instruction Raven runs" in reply
    cron.add_job_with_outcome.assert_not_called()


async def test_the_reply_says_what_happened_to_the_existing_job() -> None:
    """The service can answer an add with an update or a skip instead of a
    creation (issue #1060); the reply has to follow the outcome, or the agent
    believes a schedule was stored that the service threw away."""
    tool, cron = _tool()
    job = cron.add_job_with_outcome.return_value.job

    cron.add_job_with_outcome.return_value.outcome = "updated"
    cron.add_job_with_outcome.return_value.schedule_replaced = True
    reply = await tool.execute(action="add", message="daily", cron_expr="0 10 * * *")
    assert reply == "Updated job 'reminder' (id: j1) with the new schedule, fires on this machine's local time", reply

    cron.add_job_with_outcome.return_value.schedule_replaced = False
    reply = await tool.execute(action="add", message="daily", cron_expr="0 10 * * *")
    assert reply == "Updated job 'reminder' (id: j1); kept its schedule", reply

    cron.add_job_with_outcome.return_value.outcome = "skipped"
    cron.add_job_with_outcome.return_value.schedule_replaced = False
    cron.add_job_with_outcome.return_value.schedule_matches = False
    reply = await tool.execute(action="add", message="daily", cron_expr="0 10 * * *")
    assert reply == (
        "No new job created: 'reminder' (id: j1) already carries this message; the requested schedule was not applied"
    ), reply

    cron.add_job_with_outcome.return_value.schedule_matches = True
    reply = await tool.execute(action="add", message="daily", cron_expr="0 10 * * *")
    assert reply == (
        "No new job created: 'reminder' (id: j1) already carries this message; "
        "the requested schedule is already in effect, fires on this machine's local time"
    ), reply
