"""Unit tests for RoutineLearner.

Pin the contract: deterministic, no LLM, candidate-only routines,
date-windowed, quiet on malformed input.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from raven.proactive_engine.sentinel.predictor.routine_learner import RoutineLearner, parse_history_entries


def _now_fixed() -> datetime:
    return datetime(2026, 4, 21, 14, 0, 0)


def _learner(**kwargs) -> RoutineLearner:
    defaults = dict(min_occurrences=3, hour_slot_size=3, learning_window_days=60,
                    now_fn=_now_fixed)
    defaults.update(kwargs)
    return RoutineLearner(**defaults)


# ---------------------------------------------------------------------------
# Parsing

def test_parse_multiple_formats_ok():
    raw = """
[2026-04-21 08:00] morning check-in
[2026-04-21T08:30] inbox review
(2026/04/21 09:15) calendar open
not a timestamped line
2026-04-21 09:30 something else
"""
    entries = parse_history_entries(raw)
    # All 4 timestamped lines should parse.
    assert len(entries) == 4
    # Sorted chronologically.
    assert entries[0].ts < entries[-1].ts


def test_parse_ignores_malformed_lines():
    raw = "random text\n2026-13-50 24:99 bogus\n[2026-04-21 10:00] ok"
    entries = parse_history_entries(raw)
    assert len(entries) == 1
    assert entries[0].content == "ok"


def test_parse_empty_returns_empty():
    assert parse_history_entries("") == []
    assert parse_history_entries("\n\n\n") == []


# ---------------------------------------------------------------------------
# Learning — basic

def test_learn_no_history_returns_empty():
    assert _learner().learn("") == []


def test_learn_below_threshold_returns_empty():
    # Only 2 entries in one bin — threshold is 3.
    raw = "\n".join([
        "[2026-04-07 08:00] morning duolingo session",
        "[2026-04-14 08:15] morning duolingo session",
    ])
    assert _learner().learn(raw) == []


def test_learn_at_threshold_emits_candidate():
    raw = "\n".join([
        "[2026-04-07 08:00] morning duolingo session",
        "[2026-04-14 08:10] morning duolingo lesson",
        "[2026-04-21 08:05] morning duolingo review",
    ])
    routines = _learner().learn(raw)
    assert len(routines) == 1
    r = routines[0]
    assert r.status == "candidate"
    assert r.user_confirmed is False
    assert r.occurrence_count == 3
    # Tuesday, 6-9am bin (hour_slot_size=3 → 06..09).
    assert r.day_of_week == 1  # Tuesday
    assert r.time_slot == (6, 9)
    assert "duolingo" in r.pattern.lower()
    assert "duolingo" in r.keywords


# ---------------------------------------------------------------------------
# Window filtering

def test_learn_old_entries_outside_window_ignored():
    # entries 90 days ago + only 1 recent → below threshold.
    old = "\n".join([
        "[2026-01-15 08:00] morning duolingo",
        "[2026-01-22 08:00] morning duolingo",
        "[2026-01-29 08:00] morning duolingo",
    ])
    recent = "\n[2026-04-14 08:00] morning duolingo"
    routines = _learner(learning_window_days=60).learn(old + recent)
    # Only 1 entry in window; below threshold.
    assert routines == []


def test_learn_wide_window_catches_everything():
    old = "\n".join([
        "[2026-01-15 08:00] morning duolingo",
        "[2026-01-22 08:00] morning duolingo",
        "[2026-01-29 08:00] morning duolingo",
    ])
    routines = _learner(learning_window_days=365).learn(old)
    assert len(routines) == 1


# ---------------------------------------------------------------------------
# Multiple bins / ordering

def test_multiple_routines_sorted_by_frequency():
    # Pattern A: 3 Monday mornings
    # Pattern B: 5 Thursday evenings
    lines = []
    for d in (6, 13, 20):                                # Mondays in Apr 2026
        lines.append(f"[2026-04-{d:02d} 08:00] inbox review morning")
    for d in (2, 9, 16, 23, 30):                         # Thursdays in Apr 2026
        lines.append(f"[2026-04-{d:02d} 21:00] evening journaling reflection")
    routines = _learner().learn("\n".join(lines))
    assert len(routines) == 2
    # More frequent bin first.
    assert routines[0].occurrence_count == 5
    assert routines[0].day_of_week == 3           # Thursday
    assert routines[0].time_slot == (21, 24)
    assert routines[1].occurrence_count == 3
    assert routines[1].day_of_week == 0           # Monday


def test_no_keywords_still_emits_routine():
    raw = "\n".join([
        "[2026-04-07 08:00]",
        "[2026-04-14 08:00]",
        "[2026-04-21 08:00]",
    ])
    routines = _learner().learn(raw)
    assert len(routines) == 1
    # pattern string should not crash on empty keywords.
    assert routines[0].keywords == []
    assert "no dominant keywords" in routines[0].pattern


# ---------------------------------------------------------------------------
# Keyword extraction

def test_keyword_extraction_drops_stopwords_and_digits():
    raw = "\n".join([
        "[2026-04-07 08:00] the user did 7 pushups and opened the app",
        "[2026-04-14 08:00] the user did 10 pushups in the app",
        "[2026-04-21 08:00] the user did 15 pushups with the app",
    ])
    routines = _learner().learn(raw)
    assert len(routines) == 1
    kw = routines[0].keywords
    # Stopwords "the", "and" should not appear; "pushups" and "app" should.
    assert "the" not in kw
    assert "and" not in kw
    assert any("pushup" in k for k in kw)


def test_keyword_extraction_handles_chinese():
    # Use same day-of-week + same 3-hour slot across 3 dates.
    raw = "\n".join([
        "[2026-04-07 22:00] 晚上 小红书 发帖 打卡",
        "[2026-04-14 22:10] 晚上 小红书 发帖 打卡",
        "[2026-04-21 22:15] 晚上 小红书 发帖 打卡",
    ])
    routines = _learner().learn(raw)
    assert len(routines) == 1
    r = routines[0]
    # At least one Chinese keyword should surface.
    assert any("小红书" in k or "打卡" in k for k in r.keywords)


# ---------------------------------------------------------------------------
# Safety

def test_invalid_hour_slot_size_rejected():
    with pytest.raises(ValueError):
        _learner(hour_slot_size=5)       # 24 % 5 != 0
    with pytest.raises(ValueError):
        _learner(hour_slot_size=0)


def test_never_emits_active_or_confirmed():
    """Invariant: RoutineLearner only produces candidates, never active ones.
    Only an explicit user confirmation upgrades status."""
    raw = "\n".join([f"[2026-04-{7+7*i:02d} 08:00] morning check" for i in range(10)])
    routines = _learner(min_occurrences=2).learn(raw)
    assert routines  # should find something
    for r in routines:
        assert r.status == "candidate", f"got {r.status}"
        assert r.user_confirmed is False


# ---------------------------------------------------------------------------
# Min-history floor — prevent noisy "0 candidates" work when user has too
# little history to possibly form a routine.

def test_learn_under_min_history_short_circuits():
    """3 entries, all in the same bin, min_occurrences=3 → would normally
    emit a routine. With min_history_entries=10 we short-circuit before
    binning."""
    raw = "\n".join([
        "[2026-04-07 08:00] morning duolingo",
        "[2026-04-14 08:00] morning duolingo",
        "[2026-04-21 08:00] morning duolingo",
    ])
    assert _learner(min_history_entries=10).learn(raw) == []
    # Sanity: same raw with floor=0 (default) DOES emit.
    assert _learner().learn(raw)


def test_learn_with_decay_under_min_history_short_circuits():
    raw = "\n".join([
        "[2026-04-07 08:00] morning duolingo",
        "[2026-04-14 08:00] morning duolingo",
        "[2026-04-21 08:00] morning duolingo",
    ])
    assert _learner(min_history_entries=10).learn_with_decay(raw) == []
    assert _learner().learn_with_decay(raw)


def test_learn_from_file_missing(tmp_path):
    missing = tmp_path / "nope.md"
    assert _learner().learn_from_file(missing) == []


def test_learn_from_file_ok(tmp_path):
    f = tmp_path / "HISTORY.md"
    f.write_text("\n".join([
        "[2026-04-07 08:00] morning run",
        "[2026-04-14 08:00] morning run",
        "[2026-04-21 08:00] morning run",
    ]))
    routines = _learner().learn_from_file(f)
    assert len(routines) == 1
    assert "run" in routines[0].keywords
