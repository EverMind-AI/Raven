"""Unit tests for RoutineLearner.learn_with_decay (MS2.5)."""

from __future__ import annotations

from datetime import datetime, timedelta

from raven.proactive_engine.sentinel.predictor.routine_learner import (
    RoutineLearner,
    _decay_factor,
)

_NOW = datetime(2026, 5, 8, 12, 0)


def _history_line(ts: datetime, content: str) -> str:
    return f"[{ts.strftime('%Y-%m-%d %H:%M')}] {content}"


def _history_md(*lines: str) -> str:
    """Join HISTORY.md-style entries with the same blank-line separator
    MemoryStore.append_history uses."""
    return "\n\n".join(lines)


def test_decay_factor_today_is_one():
    assert _decay_factor(now=_NOW, ts=_NOW, half_life_days=14) == 1.0


def test_decay_factor_one_half_life_is_half():
    ts = _NOW - timedelta(days=14)
    factor = _decay_factor(now=_NOW, ts=ts, half_life_days=14)
    assert abs(factor - 0.5) < 1e-9


def test_decay_factor_two_half_lives_is_quarter():
    ts = _NOW - timedelta(days=28)
    factor = _decay_factor(now=_NOW, ts=ts, half_life_days=14)
    assert abs(factor - 0.25) < 1e-9


def test_decay_factor_future_clamps_to_one():
    """Defensive: parsing artifact shouldn't make weights blow up."""
    ts = _NOW + timedelta(days=5)
    assert _decay_factor(now=_NOW, ts=ts, half_life_days=14) == 1.0


def test_decay_factor_zero_half_life_is_one():
    """Defensive against divide-by-zero / config typo."""
    ts = _NOW - timedelta(days=10)
    assert _decay_factor(now=_NOW, ts=ts, half_life_days=0) == 1.0


def test_learn_with_decay_recent_routine_outweighs_stale_one():
    """Two routines with same occurrence_count: recent fresh > stale old."""
    learner = RoutineLearner(
        min_occurrences=3,
        learning_window_days=60,
        now_fn=lambda: _NOW,
    )

    fresh_dates = [
        _NOW - timedelta(days=0, hours=0),
        _NOW - timedelta(days=7, hours=0),
        _NOW - timedelta(days=14, hours=0),
    ]
    # Stale routine — same Tuesday-9am bin but 30+ days old
    stale_dates = [
        _NOW - timedelta(days=35),
        _NOW - timedelta(days=42),
        _NOW - timedelta(days=49),
    ]
    # Use different days-of-week so they're separate bins
    # fresh: Friday 9am (weekday 4), stale: Tuesday 9am (weekday 1)
    fresh_tuesdays = [d.replace(hour=9, minute=0) for d in [_NOW - timedelta(days=0)]]
    # Build entries for two clearly-distinct bins:
    fresh_entries = []
    stale_entries = []
    # 4 Tuesdays at 9am, recent (within last 28 days)
    for week in range(4):
        d = _NOW - timedelta(days=_NOW.weekday() + 7 * week - 1)  # Tuesday
        d = d.replace(hour=9, minute=0)
        fresh_entries.append(_history_line(d, f"meeting standup week {week}"))
    # 4 Fridays at 14:00, OLD (35-56 days ago)
    for week in range(4):
        d = _NOW - timedelta(days=35 + 7 * week)
        d = d.replace(hour=14, minute=0)
        stale_entries.append(_history_line(d, f"old report week {week}"))

    md = _history_md(*(fresh_entries + stale_entries))
    routines = learner.learn_with_decay(md)

    # Both bins should produce a candidate
    assert len(routines) >= 2
    # Sort guarantees fresh-and-frequent first
    fresh_r = next(r for r in routines if r.day_of_week == 1)  # Tuesday
    stale_r = next(r for r in routines if r.day_of_week == 4)  # Friday
    assert fresh_r.weight > stale_r.weight
    # Returned order has fresh first
    fresh_idx = routines.index(fresh_r)
    stale_idx = routines.index(stale_r)
    assert fresh_idx < stale_idx


def test_learn_with_decay_skips_below_min_occurrences():
    learner = RoutineLearner(
        min_occurrences=3,
        learning_window_days=60,
        now_fn=lambda: _NOW,
    )
    # Only 2 occurrences in same bin — below threshold
    md = _history_md(
        _history_line(_NOW.replace(hour=9), "meeting"),
        _history_line(_NOW - timedelta(days=7, hours=3), "meeting"),
    )
    routines = learner.learn_with_decay(md)
    assert routines == []


def test_learn_with_decay_handles_empty_history():
    learner = RoutineLearner(now_fn=lambda: _NOW)
    assert learner.learn_with_decay("") == []
    assert learner.learn_with_decay("no parseable lines here") == []


def test_learn_with_decay_outside_learning_window_drops_entries():
    learner = RoutineLearner(
        min_occurrences=3,
        learning_window_days=14,  # short window
        now_fn=lambda: _NOW,
    )
    # 4 entries 30 days ago — outside window
    old = [_history_line(_NOW - timedelta(days=30 + i), f"old activity {i}") for i in range(4)]
    routines = learner.learn_with_decay(_history_md(*old))
    assert routines == []


def test_tfidf_keywords_downweights_common_terms():
    """Words appearing in many bins (high DF) get TF-IDF score 0; bin-
    specific terms surface."""
    learner = RoutineLearner(
        min_occurrences=3,
        learning_window_days=60,
        now_fn=lambda: _NOW,
    )
    # Three different bins, each with 'work' and one unique term.
    # Bin 1 (Mon 9): work + standup
    # Bin 2 (Wed 14): work + review
    # Bin 3 (Fri 16): work + planning
    entries = []
    for week in range(4):
        # Monday 9am — standup
        mon = _NOW - timedelta(days=_NOW.weekday() + 7 * week)
        mon = mon.replace(hour=9, minute=0)
        entries.append(_history_line(mon, "work standup meeting"))
        # Wednesday 2pm — review
        wed = _NOW - timedelta(days=(_NOW.weekday() - 2) % 7 + 7 * week)
        wed = wed.replace(hour=14, minute=0)
        entries.append(_history_line(wed, "work review session"))
        # Friday 4pm — planning
        fri = _NOW - timedelta(days=(_NOW.weekday() - 4) % 7 + 7 * week)
        fri = fri.replace(hour=16, minute=0)
        entries.append(_history_line(fri, "work planning sync"))

    routines = learner.learn_with_decay(_history_md(*entries))
    # Each bin's keywords should NOT be dominated by 'work' (which appears
    # in all 3 bins → DF=3, log(3/3)=0 score). It should be bin-specific.
    by_dow = {r.day_of_week: r for r in routines}
    # 'work' should be dropped or at least not first in any bin's keywords
    for r in by_dow.values():
        if r.keywords:
            assert r.keywords[0] != "work", f"TF-IDF should downweight common 'work' term, got {r.keywords}"


def test_learn_with_decay_sets_weight_field():
    learner = RoutineLearner(
        min_occurrences=3,
        learning_window_days=60,
        now_fn=lambda: _NOW,
    )
    # 3 fresh Tuesday 9am entries
    entries = []
    for week in range(3):
        d = _NOW - timedelta(days=_NOW.weekday() + 7 * week - 1)
        d = d.replace(hour=9, minute=0)
        entries.append(_history_line(d, "morning meeting"))
    routines = learner.learn_with_decay(_history_md(*entries))
    assert len(routines) == 1
    r = routines[0]
    assert r.weight > 0
    # 3 entries within ~21 days, half-life 14: roughly 1 + 0.5 + 0.25 = ~1.75
    # (loose bound — just check it's plausibly in range)
    assert 0.5 < r.weight < 3.0


def test_legacy_learn_still_works():
    """`learn` (without decay) shouldn't have regressed — existing
    ContextAssembler paths still call it."""
    learner = RoutineLearner(
        min_occurrences=3,
        learning_window_days=60,
        now_fn=lambda: _NOW,
    )
    entries = []
    for week in range(3):
        d = _NOW - timedelta(days=_NOW.weekday() + 7 * week - 1)
        d = d.replace(hour=9, minute=0)
        entries.append(_history_line(d, "morning meeting"))
    routines = learner.learn(_history_md(*entries))
    assert len(routines) == 1
    # weight defaults to 0.0 in legacy path (only set by learn_with_decay)
    assert routines[0].weight == 0.0
