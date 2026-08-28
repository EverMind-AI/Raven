"""MemoryStore.recent_project_tags().

Seeds episodes.md with a mix of project / non-project / out-of-window
events and checks the helper returns the right (slug, count) pairs.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from raven.memory_engine.consolidate.consolidator import MemoryStore

_NOW = datetime(2026, 5, 20, 21, 0)


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path, now_fn=lambda: _NOW)


def test_empty_episodes_returns_empty(store: MemoryStore):
    assert store.recent_project_tags() == []


def test_returns_only_project_slugs_sorted_by_count(store: MemoryStore):
    # Within the default 14-day window (2026-05-06 onward).
    store.append_history("[2026-05-15 09:00] event A #project-foo #bug")
    store.append_history("[2026-05-16 09:00] event B #project-foo #decision")
    store.append_history("[2026-05-17 09:00] event C #project-bar #pr")
    # A non-project tag should be filtered out.
    store.append_history("[2026-05-18 09:00] event D #habit")

    out = store.recent_project_tags()
    assert out == [("project-foo", 2), ("project-bar", 1)]


def test_excludes_events_older_than_window(store: MemoryStore):
    # Older than 14 days from 2026-05-20 → before 2026-05-06.
    store.append_history("[2026-04-01 09:00] ancient #project-old")
    store.append_history("[2026-05-15 09:00] recent #project-new")

    out = store.recent_project_tags(days=14)
    assert out == [("project-new", 1)]


def test_respects_limit_parameter(store: MemoryStore):
    for i in range(20):
        store.append_history(f"[2026-05-15 09:{i:02d}] event {i} #project-p{i:02d}")
    out = store.recent_project_tags(days=14, limit=5)
    assert len(out) == 5
    # All have count 1; stable sort preserves insertion order on ties.
    assert all(n == 1 for _, n in out)


def test_handles_t_separated_timestamps(store: MemoryStore):
    # `[YYYY-MM-DDTHH:MM]` (ISO-T) is also a valid line format.
    store.append_history("[2026-05-15T09:00] iso-style #project-iso #bug")
    out = store.recent_project_tags()
    assert out == [("project-iso", 1)]


def test_handles_malformed_timestamp(store: MemoryStore):
    # Bad timestamp → silently skipped (helper returns what it can).
    store.append_history("[not-a-date] bad #project-ignored")
    store.append_history("[2026-05-15 09:00] good #project-kept")
    out = store.recent_project_tags()
    assert out == [("project-kept", 1)]


def test_multiple_project_tags_on_same_line_each_count(store: MemoryStore):
    store.append_history("[2026-05-15 09:00] cross-project ep #project-foo #project-bar")
    out = store.recent_project_tags()
    assert ("project-foo", 1) in out
    assert ("project-bar", 1) in out
