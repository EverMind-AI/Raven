"""Unit tests for TaskDiscoverer.min_occurrences_to_surface — the
confidence floor that prevents seen-once "routines" from popping up to
the user as confirmable suggestions.

Without the threshold, a single 2-event coincidence (e.g. user happened
to open the same app twice in a window) gets surfaced as a "routine"
the LLM may dress up as actionable. The threshold filters those out
before the discovery prompt is built."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from raven.proactive_engine.sentinel.predictor.task_discoverer import TaskDiscoverer
from raven.proactive_engine.sentinel.types import Routine


_NOW = datetime(2026, 5, 14, 8, 0)


def _stub_discoverer(routines: list[Routine], *, threshold: int = 3) -> TaskDiscoverer:
    """Build a TaskDiscoverer with stubbed deps — only routine_store is
    realistic enough to drive _format_candidate_routines()."""
    routine_store = MagicMock()
    routine_store.candidates.return_value = routines
    return TaskDiscoverer(
        memory_store=MagicMock(),
        pending_store=MagicMock(),
        dispatcher=MagicMock(),
        provider=MagicMock(),
        model="x",
        routine_store=routine_store,
        min_occurrences_to_surface=threshold,
        now_fn=lambda: _NOW,
    )


def _routine(rid: str, count: int, weight: float = None) -> Routine:
    return Routine(
        id=rid,
        pattern=f"pattern {rid}",
        keywords=[],
        occurrence_count=count,
        weight=weight if weight is not None else float(count),
        status="candidate",
    )


def test_below_threshold_routines_filtered_out():
    """occurrence_count < threshold → returned format string is empty."""
    disco = _stub_discoverer([
        _routine("a", count=1),
        _routine("b", count=2),
    ], threshold=3)
    assert disco._format_candidate_routines(now_ms=0) == ""


def test_above_threshold_routines_kept():
    """occurrence_count ≥ threshold → all formatted, sorted by weight desc."""
    disco = _stub_discoverer([
        _routine("low", count=3, weight=3.0),
        _routine("high", count=10, weight=10.0),
    ], threshold=3)
    out = disco._format_candidate_routines(now_ms=0)
    assert "high:" in out
    assert "low:" in out
    # Sorted by weight desc — high listed before low
    assert out.index("high:") < out.index("low:")


def test_mixed_threshold_only_qualified_pass():
    """Mix of below/above the floor — only the qualifying ones surface."""
    disco = _stub_discoverer([
        _routine("once", count=1),
        _routine("twice", count=2),
        _routine("thrice", count=3),
        _routine("often", count=8),
    ], threshold=3)
    out = disco._format_candidate_routines(now_ms=0)
    assert "thrice:" in out and "often:" in out
    assert "once:" not in out and "twice:" not in out


def test_custom_threshold_higher():
    """Threshold=5 → only 5+ occurrence routines surface."""
    disco = _stub_discoverer([
        _routine("three", count=3),
        _routine("four", count=4),
        _routine("five", count=5),
        _routine("ten", count=10),
    ], threshold=5)
    out = disco._format_candidate_routines(now_ms=0)
    assert "five:" in out and "ten:" in out
    assert "three:" not in out and "four:" not in out


def test_threshold_floored_at_one():
    """Pathological min_occurrences_to_surface=0 floors to 1 (a non-
    occurrent "routine" is meaningless)."""
    disco = _stub_discoverer([_routine("rare", count=1)], threshold=0)
    # Floored to 1, so count=1 routines pass
    assert disco.min_occurrences_to_surface == 1
    assert "rare:" in disco._format_candidate_routines(now_ms=0)


def test_default_threshold_is_three():
    """The default is 3 — chosen as the minimum-evidence floor below
    which a "routine" is more likely a coincidence than a pattern."""
    disco = TaskDiscoverer(
        memory_store=MagicMock(),
        pending_store=MagicMock(),
        dispatcher=MagicMock(),
        provider=MagicMock(),
        model="x",
        now_fn=lambda: _NOW,
    )
    assert disco.min_occurrences_to_surface == 3
