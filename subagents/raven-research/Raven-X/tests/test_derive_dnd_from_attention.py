"""Unit tests for ``parse_user_overrides_dnd`` and the
``NudgePolicy.set_user_override_dnd`` runtime hook."""

from datetime import datetime

from raven.config.raven import DndWindow, NudgePolicyConfig
from raven.proactive_engine.sentinel.trigger_policy.derive_dnd import (
    parse_user_overrides_dnd,
)
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy

_SAMPLE = """## User overrides
- dnd: 22:30-06:00 reason=nighttime
- dnd: 11:00-15:00 weekdays=Mon-Fri reason=translation_focus
- dnd: 00:00-09:00 weekdays=Sat-Sun reason=weekend_sleep_in
- quiet_hours: 23:00-07:00
- 这条是中文备注，不该解析

## Recent stance log (30d)
- 用户拒绝过"吃药提醒" 2 次
"""


def test_parses_three_dnd_windows_plus_legacy_quiet_hours():
    out = parse_user_overrides_dnd(_SAMPLE)
    assert len(out) == 4
    nightly, weekday_focus, weekend, legacy = out
    assert (nightly.start_hour, nightly.start_minute) == (22, 30)
    assert (nightly.end_hour, nightly.end_minute) == (6, 0)
    assert nightly.weekdays is None
    assert nightly.why == "nighttime"
    assert weekday_focus.weekdays == [0, 1, 2, 3, 4]
    assert weekday_focus.why == "translation_focus"
    assert weekend.weekdays == [5, 6]
    assert legacy.start_hour == 23 and legacy.end_hour == 7


def test_returns_empty_when_section_missing():
    assert parse_user_overrides_dnd("") == []
    assert parse_user_overrides_dnd("## Recent stance log (30d)\n- something") == []


def test_skips_unparseable_lines_silently():
    text = """## User overrides
- 这是自然语言不打算结构化
- dnd: 09:00-12:00 reason=morning_block
- dnd: 99:99-00:00 reason=garbage
- dnd: malformed
"""
    out = parse_user_overrides_dnd(text)
    assert len(out) == 1
    assert out[0].why == "morning_block"


def test_weekday_specs_accepted_forms():
    text = """## User overrides
- dnd: 12:00-13:00 weekdays=Mon,Wed,Fri reason=mwf
- dnd: 14:00-15:00 weekdays=0,2,4 reason=numeric
- dnd: 18:00-20:00 weekdays=Sat-Sun reason=weekend
"""
    out = parse_user_overrides_dnd(text)
    assert out[0].weekdays == [0, 2, 4]
    assert out[1].weekdays == [0, 2, 4]
    assert out[2].weekdays == [5, 6]


def test_chinese_alias_user_overrides_also_parsed():
    text = """## 用户指令
- dnd: 22:00-06:00 reason=cn_alias
"""
    out = parse_user_overrides_dnd(text)
    assert len(out) == 1
    assert out[0].why == "cn_alias"


def test_nudge_policy_uses_user_overrides_after_set():
    cfg = NudgePolicyConfig()
    policy = NudgePolicy(cfg, now_fn=lambda: datetime(2026, 5, 4, 12, 30))

    # 12:30 Monday — outside default quiet_hours, no DND on persona side.
    assert not policy._in_quiet_hours(datetime(2026, 5, 4, 12, 30))

    # Inject runtime override: 12:00-13:00 weekday lunch DND.
    policy.set_user_override_dnd(
        [
            DndWindow(
                start_hour=12,
                end_hour=13,
                start_minute=0,
                end_minute=0,
                weekdays=[0, 1, 2, 3, 4],
                why="lunch",
            ),
        ]
    )
    assert policy._in_quiet_hours(datetime(2026, 5, 4, 12, 30))
    # Same time but a Saturday — weekday filter excludes us, allow.
    assert not policy._in_quiet_hours(datetime(2026, 5, 2, 12, 30))


def test_nudge_policy_clears_when_set_empty():
    cfg = NudgePolicyConfig()
    policy = NudgePolicy(cfg, now_fn=lambda: datetime(2026, 5, 4, 12, 30))
    policy.set_user_override_dnd(
        [
            DndWindow(start_hour=12, end_hour=13, weekdays=[0, 1, 2, 3, 4], why="x"),
        ]
    )
    assert policy._in_quiet_hours(datetime(2026, 5, 4, 12, 30))
    policy.set_user_override_dnd([])
    assert not policy._in_quiet_hours(datetime(2026, 5, 4, 12, 30))
