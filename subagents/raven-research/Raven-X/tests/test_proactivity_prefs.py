"""Unit tests for ProactivityPreferencesReader.

Validates parsing of the ``## Proactivity Preferences`` section and the
tighten-only merge rule in NudgePolicy._effective_quiet_hours.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from raven.config.raven import NudgePolicyConfig
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy, _hours_covered
from raven.proactive_engine.sentinel.trigger_policy.prefs import (
    PersonalizedOverrides,
    ProactivityPreferencesReader,
)

# ── Reader parsing ──────────────────────────────────────────────────────────


def _mk_reader(text: str) -> ProactivityPreferencesReader:
    return ProactivityPreferencesReader(read_fn=lambda: text)


def test_empty_memory_returns_empty_overrides():
    assert _mk_reader("").read().is_empty()


def test_missing_section_returns_empty():
    memory = "## Preferences\n- User prefers Python\n"
    assert _mk_reader(memory).read().is_empty()


def test_parse_quiet_hours_simple_range():
    memory = "## Proactivity Preferences\n- User quiet hours: 22-07\n"
    ov = _mk_reader(memory).read()
    assert ov.quiet_hours == (22, 7)


def test_parse_quiet_hours_hhmm_format():
    memory = "## Proactivity Preferences\n- User quiet hours: 22:00-07:30 (no notifications)\n"
    ov = _mk_reader(memory).read()
    assert ov.quiet_hours == (22, 7)


def test_parse_quiet_hours_chinese_keyword():
    memory = "## Proactivity Preferences\n- 安静时段: 20-09\n"
    ov = _mk_reader(memory).read()
    assert ov.quiet_hours == (20, 9)


def test_parse_quiet_hours_dnd_english():
    memory = "## Proactivity Preferences\n- User DND: 21-08\n"
    ov = _mk_reader(memory).read()
    assert ov.quiet_hours == (21, 8)


def test_facts_without_range_are_ignored():
    memory = "## Proactivity Preferences\n- User quiet hours: never\n- User only wants high priority notifications\n"
    ov = _mk_reader(memory).read()
    # First fact has no numeric range; no quiet hour is inferred.
    assert ov.quiet_hours is None


def test_section_stops_at_next_header():
    memory = (
        "## Preferences\n"
        "- User prefers Python\n"
        "\n"
        "## Proactivity Preferences\n"
        "- User quiet hours: 22-07\n"
        "\n"
        "## Other Section\n"
        "- User quiet hours: 01-02\n"
    )
    # The second "quiet hours" lives in a different section — should be ignored.
    ov = _mk_reader(memory).read()
    assert ov.quiet_hours == (22, 7)


def test_read_from_file(tmp_path: Path):
    f = tmp_path / "memory.md"
    f.write_text("## Proactivity Preferences\n- User quiet hours: 22-06\n")
    reader = ProactivityPreferencesReader(memory_file=f)
    assert reader.read().quiet_hours == (22, 6)


def test_read_from_missing_file_returns_empty(tmp_path: Path):
    reader = ProactivityPreferencesReader(memory_file=tmp_path / "missing.md")
    assert reader.read().is_empty()


def test_read_fn_exception_returns_empty():
    def boom():
        raise OSError("disk gone")

    reader = ProactivityPreferencesReader(read_fn=boom)
    assert reader.read().is_empty()


# ── _hours_covered math ──────────────────────────────────────────────────────


def test_hours_covered_simple():
    assert _hours_covered((9, 17)) == 8


def test_hours_covered_wraps_midnight():
    assert _hours_covered((22, 7)) == 9


def test_hours_covered_equal_means_none():
    assert _hours_covered((8, 8)) == 0


# ── NudgePolicy effective_quiet_hours merge rule ─────────────────────────────


def _mk_policy(config_quiet: tuple[int, int], override: tuple[int, int] | None) -> NudgePolicy:
    config = NudgePolicyConfig(
        max_nudges_per_hour=10,
        max_nudges_per_day=100,
        min_interval_seconds=0,
        quiet_hours=config_quiet,
        cooldown_on_dismiss_seconds=0,
        high_priority_bypasses_limits=True,
        dedup_window_seconds=0,
        inject_ttl_seconds=1800,
        inject_max_pending_per_session=3,
        defer_idle_threshold_seconds=300,
        defer_max_wait_seconds=86400,
    )
    ov = PersonalizedOverrides(quiet_hours=override) if override else None
    return NudgePolicy(
        config,
        now_fn=lambda: datetime(2026, 4, 22, 23, 0),
        overrides_fn=(lambda ov=ov: ov) if ov is not None else None,
    )


def test_override_widens_quiet_hours():
    # config: 22-07 (9 hours), override: 20-09 (13 hours) — override wins.
    policy = _mk_policy(config_quiet=(22, 7), override=(20, 9))
    assert policy._effective_quiet_hours() == (20, 9)


def test_override_narrows_is_rejected():
    # config: 22-07 (9 hours), override: 23-06 (7 hours) — override rejected.
    policy = _mk_policy(config_quiet=(22, 7), override=(23, 6))
    assert policy._effective_quiet_hours() == (22, 7)


def test_override_none_uses_config():
    policy = _mk_policy(config_quiet=(22, 7), override=None)
    assert policy._effective_quiet_hours() == (22, 7)


def test_override_errors_fall_back_to_config():
    config = NudgePolicyConfig(
        max_nudges_per_hour=10,
        max_nudges_per_day=100,
        min_interval_seconds=0,
        quiet_hours=(22, 7),
        cooldown_on_dismiss_seconds=0,
        high_priority_bypasses_limits=True,
        dedup_window_seconds=0,
        inject_ttl_seconds=1800,
        inject_max_pending_per_session=3,
        defer_idle_threshold_seconds=300,
        defer_max_wait_seconds=86400,
    )

    def bad():
        raise RuntimeError("reader blew up")

    policy = NudgePolicy(config, overrides_fn=bad)
    assert policy._effective_quiet_hours() == (22, 7)


def test_in_quiet_hours_uses_effective_window():
    # config: 22-07, override: 20-09 → 08:00 should now be "quiet"
    policy = _mk_policy(config_quiet=(22, 7), override=(20, 9))
    assert policy._in_quiet_hours(datetime(2026, 4, 22, 8, 0)) is True
    # 10:00 is outside even the widened window.
    assert policy._in_quiet_hours(datetime(2026, 4, 22, 10, 0)) is False
