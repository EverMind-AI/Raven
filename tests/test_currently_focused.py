"""Unit tests for CurrentlyFocusedProducer — deterministic 'what is the
user doing in the last N hours' summary built from SessionManager files
+ episodes.md tag distribution.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.proactive_engine.sentinel.attention_producers import (
    CurrentlyFocusedProducer,
)
from raven.session.manager import SessionManager


class Clock:
    def __init__(self, t0: datetime) -> None:
        self.t = t0

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 5, 29, 14, 0))


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> MemoryStore:
    return MemoryStore(tmp_path, now_fn=clock)


@pytest.fixture
def session_manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path)


def _seed_session(
    sessions_dir: Path, channel: str, chat_id: str,
    timestamps: list[datetime],
) -> Path:
    (sessions_dir / channel).mkdir(parents=True, exist_ok=True)
    path = sessions_dir / channel / f"{chat_id}.jsonl"
    lines: list[str] = [json.dumps({
        "_type": "metadata",
        "key": f"{channel}:{chat_id}",
        "created_at": timestamps[0].isoformat(),
    })]
    for i, ts in enumerate(timestamps):
        lines.append(json.dumps({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"msg{i}", "timestamp": ts.isoformat(),
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _seed_episodes(
    store: MemoryStore, entries: list[tuple[datetime, str, list[str]]],
) -> None:
    """``entries`` = ``[(timestamp, summary, [tag, tag, ...]), ...]``."""
    path = store.history_file
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for ts, summary, tags in entries:
        tag_str = " ".join(f"#{t}" for t in tags)
        lines.append(
            f"[{ts.strftime('%Y-%m-%d %H:%M')}] {summary} {tag_str}",
        )
    path.write_text("\n\n".join(lines) + "\n", encoding="utf-8")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================


class TestEmptyState:
    def test_no_sessions_no_episodes(self, store, session_manager, clock) -> None:
        body = _run(CurrentlyFocusedProducer(
            store, session_manager,
        ).compute_body(clock()))
        assert body == ""

    def test_old_sessions_outside_window(
        self, store, session_manager, clock,
    ) -> None:
        # All session activity > 6h ago
        old = clock() - timedelta(hours=12)
        _seed_session(
            session_manager.sessions_dir, "cli", "default",
            [old, old + timedelta(minutes=5)],
        )
        body = _run(CurrentlyFocusedProducer(
            store, session_manager,
        ).compute_body(clock()))
        assert body == ""


class TestActiveSessions:
    def test_renders_recent_session(
        self, store, session_manager, clock,
    ) -> None:
        recent_ts = [
            clock() - timedelta(hours=2),
            clock() - timedelta(hours=1),
            clock() - timedelta(minutes=15),
        ]
        _seed_session(
            session_manager.sessions_dir, "cli", "default", recent_ts,
        )
        body = _run(CurrentlyFocusedProducer(
            store, session_manager,
        ).compute_body(clock()))
        assert "cli:default" in body
        assert "3 msgs" in body
        assert "Active sessions" in body

    def test_skips_session_with_no_recent_activity(
        self, store, session_manager, clock,
    ) -> None:
        old = clock() - timedelta(hours=10)
        recent = clock() - timedelta(minutes=30)
        _seed_session(
            session_manager.sessions_dir, "cli", "default", [old, old],
        )
        _seed_session(
            session_manager.sessions_dir, "telegram", "user42", [recent],
        )
        body = _run(CurrentlyFocusedProducer(
            store, session_manager,
        ).compute_body(clock()))
        assert "telegram:user42" in body
        assert "cli:default" not in body


class TestTagDistribution:
    def test_renders_top_topics_and_projects(
        self, store, session_manager, clock,
    ) -> None:
        within = clock() - timedelta(hours=2)
        _seed_episodes(store, [
            (within, "debugged api", ["debug", "project-raven"]),
            (within - timedelta(minutes=10),
             "reviewed pr", ["review", "project-raven"]),
            (within - timedelta(hours=1),
             "wrote tests", ["test", "project-raven"]),
            (within - timedelta(hours=3),
             "planned next sprint", ["plan", "project-side-x"]),
        ])
        body = _run(CurrentlyFocusedProducer(
            store, session_manager,
        ).compute_body(clock()))
        assert "Top topics" in body
        assert "`#debug`" in body
        assert "Top projects" in body
        assert "`raven`" in body
        assert "`side-x`" in body

    def test_skips_episodes_outside_window(
        self, store, session_manager, clock,
    ) -> None:
        # All episodes > 6h ago
        old = clock() - timedelta(hours=12)
        _seed_episodes(store, [
            (old, "ancient debug", ["debug", "project-raven"]),
        ])
        body = _run(CurrentlyFocusedProducer(
            store, session_manager,
        ).compute_body(clock()))
        assert body == ""

    def test_project_tag_does_not_double_count_as_topic(
        self, store, session_manager, clock,
    ) -> None:
        within = clock() - timedelta(hours=2)
        _seed_episodes(store, [
            (within, "x", ["project-raven"]),
        ])
        body = _run(CurrentlyFocusedProducer(
            store, session_manager,
        ).compute_body(clock()))
        # The `project-raven` tag should appear in the projects
        # section but NOT also in topics.
        assert "Top projects" in body
        assert "`raven`" in body
        assert "Top topics" not in body


class TestWindowOverride:
    def test_longer_window_picks_up_older_activity(
        self, store, session_manager, clock,
    ) -> None:
        ts = clock() - timedelta(hours=10)  # outside default 6h window
        _seed_session(
            session_manager.sessions_dir, "cli", "default", [ts, ts],
        )
        # Default 6h: empty
        body = _run(CurrentlyFocusedProducer(
            store, session_manager,
        ).compute_body(clock()))
        assert body == ""
        # 12h window: visible
        body = _run(CurrentlyFocusedProducer(
            store, session_manager, window_hours=12,
        ).compute_body(clock()))
        assert "cli:default" in body


class TestNestedKeyDerivation:
    def test_metadata_less_nested_file_derives_channel_chat_key(
        self, store, session_manager, clock,
    ) -> None:
        """A metadata-less session file under the nested layout
        (sessions/{channel}/{chat_id}.jsonl) derives its key from
        parent.name + stem, not the flat path.stem.replace('_', ':', 1)
        which mis-derives a chat_id that contains an underscore."""
        recent = clock() - timedelta(minutes=30)
        sessions_dir = session_manager.sessions_dir
        (sessions_dir / "telegram").mkdir(parents=True, exist_ok=True)
        path = sessions_dir / "telegram" / "user_42.jsonl"
        path.write_text(
            json.dumps({
                "role": "user", "content": "hi",
                "timestamp": recent.isoformat(),
            }) + "\n",
            encoding="utf-8",
        )
        rows = CurrentlyFocusedProducer(
            store, session_manager,
        )._collect_active_sessions(clock())
        keys = [r["key"] for r in rows]
        assert "telegram:user_42" in keys
        assert "user:42" not in keys
