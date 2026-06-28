"""Unit tests for ``behaviors_extractor`` — idle-triggered LLM extractor
producing append-only ``user_memory/behaviors.md``."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.config.raven import BehaviorsExtractConfig
from raven.memory_engine.consolidate.behaviors import parse_behaviors
from raven.memory_engine.consolidate.behaviors_extractor import (
    BehaviorsExtractor,
    BehaviorsOffsets,
    _SessionOffset,
)
from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.providers.base import LLMResponse, ToolCallRequest
from raven.session.manager import SessionManager


# ---------------------------------------------------------------------------
# Fixtures


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


@pytest.fixture
def config() -> BehaviorsExtractConfig:
    return BehaviorsExtractConfig(
        enabled=True,
        idle_seconds=900,
        cooldown_hours=12,
        min_segment_messages=5,
        max_messages_per_call=60,
    )


def _seed_session(
    sessions_dir: Path, channel: str, chat_id: str,
    n_messages: int, day: str = "2026-05-29",
) -> Path:
    """Write a synthetic session JSONL with metadata + N messages."""
    (sessions_dir / channel).mkdir(parents=True, exist_ok=True)
    path = sessions_dir / channel / f"{chat_id}.jsonl"
    lines: list[str] = []
    lines.append(json.dumps({
        "_type": "metadata",
        "key": f"{channel}:{chat_id}",
        "created_at": f"{day}T09:00:00",
    }))
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        ts = f"{day}T{9 + i // 4:02d}:{(i % 4) * 15:02d}:00"
        lines.append(json.dumps({
            "role": role, "content": f"message {i}", "timestamp": ts,
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_provider_with_events(events: list[dict[str, Any]]) -> MagicMock:
    """Build a mock LLM provider that returns one tool call yielding
    ``events`` as the extracted set."""
    provider = MagicMock()
    response = LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(
            id="tc_1", name="emit_behavior_events",
            arguments={"events": events},
        )],
    )
    provider.chat_with_retry = AsyncMock(return_value=response)
    return provider


def _make_provider_no_tool_calls() -> MagicMock:
    """Provider that returns a content-only response (no tool calls)."""
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="ok", tool_calls=[]),
    )
    return provider


# ---------------------------------------------------------------------------
# Offsets round trip


class TestOffsetsPersistence:
    def test_load_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        offsets = BehaviorsOffsets.load(tmp_path / ".offsets.json")
        assert offsets.offsets == {}
        assert offsets.last_run_ts == ""

    def test_save_and_reload_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / ".offsets.json"
        offsets = BehaviorsOffsets(path=path)
        offsets.set("cli:default", _SessionOffset(
            processed_until_msg_idx=42,
            processed_until_ts="2026-05-29T14:00:00",
        ))
        offsets.last_run_ts = "2026-05-29T14:30:00"
        offsets.save()

        reloaded = BehaviorsOffsets.load(path)
        assert reloaded.last_run_ts == "2026-05-29T14:30:00"
        assert reloaded.get("cli:default").processed_until_msg_idx == 42

    def test_load_tolerates_corrupt_json(self, tmp_path: Path) -> None:
        path = tmp_path / ".offsets.json"
        path.write_text("not json", encoding="utf-8")
        offsets = BehaviorsOffsets.load(path)
        assert offsets.offsets == {}


# ---------------------------------------------------------------------------
# Gate enforcement


class TestTickGates:
    async def test_skips_when_disabled(
        self, store, session_manager, config, clock,
    ) -> None:
        config.enabled = False
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=_make_provider_no_tool_calls(),
            config=config, model="test-model", now_fn=clock,
        )
        result = await extractor.tick(idle_seconds_observed=99999)
        assert result == 0

    async def test_skips_when_idle_below_threshold(
        self, store, session_manager, config, clock,
    ) -> None:
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=_make_provider_no_tool_calls(),
            config=config, model="test-model", now_fn=clock,
        )
        # 5min idle < 15min threshold
        result = await extractor.tick(idle_seconds_observed=300)
        assert result == 0

    async def test_skips_when_cooldown_active(
        self, store, session_manager, config, clock, tmp_path: Path,
    ) -> None:
        # Pre-seed offsets with a recent last_run_ts.
        offsets = BehaviorsOffsets(path=store.behaviors_offsets_path)
        offsets.last_run_ts = (clock() - timedelta(hours=1)).isoformat(
            timespec="seconds",
        )
        offsets.save()

        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=_make_provider_no_tool_calls(),
            config=config, model="test-model", now_fn=clock,
        )
        result = await extractor.tick(idle_seconds_observed=99999)
        assert result == 0


# ---------------------------------------------------------------------------
# Extraction happy paths


class TestExtractionHappyPath:
    async def test_appends_events_and_advances_offset(
        self, store, session_manager, config, clock, tmp_path: Path,
    ) -> None:
        _seed_session(session_manager.sessions_dir, "cli", "default", 10)
        provider = _make_provider_with_events([{
            "start": "09:00", "end": "09:30",
            "intent": "debug", "outcome": "resolved",
            "topic": "memory-engine", "project": "raven",
            "source": "user-asked", "owner": "user",
            "tools": ["Bash", "Edit"], "turns": 5,
            "summary": "debugged memory_engine session split",
        }])
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=provider, config=config, model="test-model",
            now_fn=clock,
        )
        added = await extractor.tick(idle_seconds_observed=99999)
        assert added == 1
        assert store.behaviors_file.exists()
        events = parse_behaviors(
            store.behaviors_file.read_text(encoding="utf-8"),
        )
        assert len(events) == 1
        assert events[0].summary == "debugged memory_engine session split"
        assert events[0].session == "cli:default"
        assert events[0].tools == ["Bash", "Edit"]

        # Offset advanced past all 10 messages
        offsets = BehaviorsOffsets.load(store.behaviors_offsets_path)
        assert offsets.get("cli:default").processed_until_msg_idx == 10

    async def test_iterates_multiple_sessions(
        self, store, session_manager, config, clock,
    ) -> None:
        _seed_session(session_manager.sessions_dir, "cli", "default", 6)
        _seed_session(session_manager.sessions_dir, "telegram", "user42", 8)
        provider = _make_provider_with_events([{
            "start": "09:00", "end": "09:15",
            "intent": "ask", "outcome": "resolved",
            "topic": "x", "project": "", "source": "user-asked",
            "owner": "user", "tools": [], "turns": 2,
            "summary": "asked about X",
        }])
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=provider, config=config, model="test-model",
            now_fn=clock,
        )
        added = await extractor.tick(idle_seconds_observed=99999)
        # One event per session
        assert added == 2
        # Both sessions have offsets
        offsets = BehaviorsOffsets.load(store.behaviors_offsets_path)
        assert offsets.get("cli:default").processed_until_msg_idx == 6
        assert offsets.get("telegram:user42").processed_until_msg_idx == 8

    async def test_skip_session_when_tail_too_short(
        self, store, session_manager, config, clock,
    ) -> None:
        _seed_session(session_manager.sessions_dir, "cli", "default", 3)
        provider = _make_provider_with_events([{
            "start": "09:00", "end": "09:15", "intent": "x",
            "outcome": "x", "topic": "x", "summary": "x",
        }])
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=provider, config=config, model="test-model",
            now_fn=clock,
        )
        added = await extractor.tick(idle_seconds_observed=99999)
        assert added == 0
        provider.chat_with_retry.assert_not_called()

    async def test_empty_events_advances_offset(
        self, store, session_manager, config, clock,
    ) -> None:
        _seed_session(session_manager.sessions_dir, "cli", "default", 10)
        provider = _make_provider_with_events([])
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=provider, config=config, model="test-model",
            now_fn=clock,
        )
        added = await extractor.tick(idle_seconds_observed=99999)
        assert added == 0
        assert not store.behaviors_file.exists()
        offsets = BehaviorsOffsets.load(store.behaviors_offsets_path)
        # Offset still advances so the chunk isn't re-extracted next tick
        assert offsets.get("cli:default").processed_until_msg_idx == 10

    async def test_resume_skips_already_processed_messages(
        self, store, session_manager, config, clock,
    ) -> None:
        _seed_session(session_manager.sessions_dir, "cli", "default", 12)
        # Pre-seed offset past message 8
        pre = BehaviorsOffsets(path=store.behaviors_offsets_path)
        pre.set("cli:default", _SessionOffset(
            processed_until_msg_idx=8,
            processed_until_ts="2026-05-29T11:00:00",
        ))
        pre.save()

        provider = _make_provider_with_events([{
            "start": "11:00", "end": "11:15", "intent": "x",
            "outcome": "x", "topic": "x", "summary": "x",
        }])
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=provider, config=config, model="test-model",
            now_fn=clock,
        )
        await extractor.tick(idle_seconds_observed=99999)
        # Verify the LLM was called with only the tail (12-8=4 messages),
        # which is below min_segment_messages=5 → call should be skipped.
        provider.chat_with_retry.assert_not_called()


class TestRunAllBypassesGates:
    async def test_run_all_ignores_cooldown(
        self, store, session_manager, config, clock,
    ) -> None:
        offsets = BehaviorsOffsets(path=store.behaviors_offsets_path)
        offsets.last_run_ts = clock().isoformat(timespec="seconds")
        offsets.save()

        _seed_session(session_manager.sessions_dir, "cli", "default", 8)
        provider = _make_provider_with_events([{
            "start": "09:00", "end": "09:15", "intent": "x",
            "outcome": "x", "topic": "x", "summary": "ran via rebuild",
        }])
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=provider, config=config, model="test-model",
            now_fn=clock,
        )
        added = await extractor.run_all()
        assert added == 1
        assert store.behaviors_file.exists()


# ---------------------------------------------------------------------------
# Robustness


class TestRobustness:
    async def test_no_tool_call_returns_empty(
        self, store, session_manager, config, clock,
    ) -> None:
        _seed_session(session_manager.sessions_dir, "cli", "default", 10)
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=_make_provider_no_tool_calls(),
            config=config, model="test-model", now_fn=clock,
        )
        added = await extractor.tick(idle_seconds_observed=99999)
        assert added == 0
        # Offset still advances so we don't re-call the LLM next tick
        offsets = BehaviorsOffsets.load(store.behaviors_offsets_path)
        assert offsets.get("cli:default").processed_until_msg_idx == 10

    async def test_malformed_event_records_dropped(
        self, store, session_manager, config, clock,
    ) -> None:
        _seed_session(session_manager.sessions_dir, "cli", "default", 10)
        provider = _make_provider_with_events([
            {"summary": "missing start/end → dropped"},
            {"start": "09:00", "end": "09:15", "summary": "kept"},
            "not even a dict — dropped",
        ])
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=provider, config=config, model="test-model",
            now_fn=clock,
        )
        added = await extractor.tick(idle_seconds_observed=99999)
        assert added == 1
        events = parse_behaviors(
            store.behaviors_file.read_text(encoding="utf-8"),
        )
        assert len(events) == 1
        assert events[0].summary == "kept"

    async def test_session_with_missing_metadata_derives_nested_key(
        self, store, session_manager, config, clock,
    ) -> None:
        # Metadata-less file under the nested layout
        # (sessions/{channel}/{chat_id}.jsonl): the key is derived from
        # parent.name + stem, preserving the channel and an
        # underscore-bearing chat_id.
        sessions_dir = session_manager.sessions_dir
        (sessions_dir / "telegram").mkdir(parents=True, exist_ok=True)
        path = sessions_dir / "telegram" / "user_42.jsonl"
        lines = [
            json.dumps({
                "role": "user", "content": f"msg{i}",
                "timestamp": f"2026-05-29T09:{i:02d}:00",
            }) for i in range(8)
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        provider = _make_provider_with_events([{
            "start": "09:00", "end": "09:15", "intent": "x",
            "outcome": "x", "topic": "x", "summary": "x",
        }])
        extractor = BehaviorsExtractor(
            memory_store=store, session_manager=session_manager,
            provider=provider, config=config, model="test-model",
            now_fn=clock,
        )
        await extractor.tick(idle_seconds_observed=99999)
        events = parse_behaviors(
            store.behaviors_file.read_text(encoding="utf-8"),
        )
        assert events[0].session == "telegram:user_42"
