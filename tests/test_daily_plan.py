"""Unit tests for ``parse_daily_plan`` and ``DailyPlanProducer``
attention.md section."""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.proactive_engine.sentinel.attention_producers.daily_plan import (
    DailyPlanProducer,
)
from raven.proactive_engine.sentinel.trigger_policy.derive_dnd import (
    parse_daily_plan,
)

_SAMPLE = """## 今日 fire 计划
<!-- generated 2026-05-04T06:00:00 | model=qwen3.5-27b | entries=4 -->
- 07:30 routine_morning_med | priority=high | 晨起药物
- 11:30 routine_noon_med | priority=high | 午前服药
- 13:00 deadline_clawtrack | priority=medium | release 还剩 3 天
- 19:00 routine_emotion_log | priority=low | 晚间情绪记录

## Recent stance log (30d)
- something else
"""


def test_parses_four_entries_in_order():
    out = parse_daily_plan(_SAMPLE)
    assert len(out) == 4
    assert out[0] == {
        "time_hhmm": "07:30",
        "topic_tag": "routine_morning_med",
        "priority": "high",
        "user_message": "",
        "rationale": "晨起药物",
    }
    assert out[2]["topic_tag"] == "deadline_clawtrack"
    assert out[3]["priority"] == "low"


def test_parses_user_message_field():
    text = """## 今日 fire 计划
- 07:30 routine_morning_med | priority=high | msg=该吃药啦 💊 | USER.md 记录 '每天 7:00 服药'
- 09:00 routine_legacy | priority=low | 老条目无 msg
"""
    out = parse_daily_plan(text)
    assert out[0]["user_message"] == "该吃药啦 💊"
    assert out[0]["rationale"] == "USER.md 记录 '每天 7:00 服药'"
    # Legacy entries (no msg=) keep an empty user_message and the trailing
    # text as rationale.
    assert out[1]["user_message"] == ""
    assert out[1]["rationale"] == "老条目无 msg"


def test_returns_empty_on_missing_section():
    assert parse_daily_plan("") == []
    assert parse_daily_plan("## Recent stance log (30d)\n- foo") == []


def test_skips_html_comment_and_blank_lines():
    text = """## 今日 fire 计划
<!-- some metadata -->

- 08:00 routine_x | priority=high | reason

<!-- inline -->
- 09:00 routine_y | priority=medium |
"""
    out = parse_daily_plan(text)
    assert len(out) == 2
    assert out[1]["rationale"] == ""


def test_invalid_lines_dropped_quietly():
    text = """## 今日 fire 计划
- 25:99 garbage_time | priority=high | reason
- abc:def malformed | priority=high | reason
- 08:00 routine_ok | priority=high | reason
- not a bullet line
"""
    out = parse_daily_plan(text)
    assert len(out) == 1
    assert out[0]["topic_tag"] == "routine_ok"


@pytest.mark.asyncio
async def test_producer_skips_before_6am():
    p = DailyPlanProducer(
        memory_store=MagicMock(),
        provider=MagicMock(),
        now_fn=lambda: datetime(2026, 5, 4, 5, 30),
    )
    assert p.should_run(datetime(2026, 5, 4, 5, 30)) is False
    assert p.should_run(datetime(2026, 5, 4, 6, 0)) is True


@pytest.mark.asyncio
async def test_producer_respects_cooldown():
    p = DailyPlanProducer(
        memory_store=MagicMock(),
        provider=MagicMock(),
        now_fn=lambda: datetime(2026, 5, 4, 7, 0),
    )
    # First call generates.
    p._last_plan_at = datetime(2026, 5, 4, 7, 0)
    # 5 hours later — still in cooldown (20h cadence).
    assert p.should_run(datetime(2026, 5, 4, 12, 0)) is False
    # Next day 7am — outside cooldown.
    assert p.should_run(datetime(2026, 5, 5, 7, 0)) is True


@pytest.mark.asyncio
async def test_producer_renders_llm_entries(tmp_path: Path):
    attention_file = tmp_path / "attention.md"
    mem_store = MagicMock()
    mem_store.attention_file = attention_file
    mem_store.read_long_term = MagicMock(return_value=("# Long-term Memory\n## Important Notes\n- 每天 7:00 服药\n"))
    provider = MagicMock()
    fake_response = MagicMock()
    fake_response.has_tool_calls = True
    fake_tool = MagicMock()
    fake_tool.arguments = {
        "entries": [
            {
                "time_hhmm": "07:30",
                "topic_tag": "routine_morning_med",
                "priority": "high",
                "rationale": "晨起药物",
                "user_message": "该吃药啦 💊 早上这颗别忘",
            },
            {
                "time_hhmm": "19:00",
                "topic_tag": "routine_emotion_log",
                "priority": "low",
                "rationale": "晚间情绪记录",
                "user_message": "睡前记一笔今天的心情吧～",
            },
        ],
    }
    fake_response.tool_calls = [fake_tool]
    fake_response.content = ""
    provider.chat_with_retry = AsyncMock(return_value=fake_response)

    p = DailyPlanProducer(memory_store=mem_store, provider=provider, model="qwen")
    body = await p.compute_body(datetime(2026, 5, 4, 6, 30))
    assert "routine_morning_med" in body
    assert "routine_emotion_log" in body
    assert "07:30" in body and "19:00" in body
    # The user-facing message is rendered into the body and round-trips.
    assert "该吃药啦" in body
    parsed = parse_daily_plan(f"## 今日 fire 计划\n{body}")
    assert len(parsed) == 2
    assert parsed[0]["time_hhmm"] == "07:30"
    assert parsed[0]["user_message"] == "该吃药啦 💊 早上这颗别忘"
