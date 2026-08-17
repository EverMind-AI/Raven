"""Tests for DailyAnalysisService + the 3 producers it feeds:
StanceLogProducer / Predicted3DProducer / BehaviorPatternsProducer.

Verifies: cooldown cache hit (one LLM call per N producer reads),
prefix fallback when LLM returns empty / fails, individual producer
rendering, schema parsing tolerance.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.config.raven import DailyAnalysisConfig
from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.proactive_engine.sentinel.attention_producers import (
    BehaviorPatternsProducer,
    Predicted3DProducer,
    StanceLogProducer,
)
from raven.proactive_engine.sentinel.predictor.daily_analysis import (
    DailyAnalysisService,
    _parse_result,
)
from raven.proactive_engine.sentinel.predictor.routine_store import (
    RoutineStore,
)
from raven.providers.base import LLMResponse, ToolCallRequest
from raven.session.manager import SessionManager


class Clock:
    def __init__(self, t0: datetime) -> None:
        self.t = t0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, hours: float) -> None:
        self.t = self.t + timedelta(hours=hours)


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
def routine_store(tmp_path: Path) -> RoutineStore:
    return RoutineStore(tmp_path / "routines.json")


@pytest.fixture
def config() -> DailyAnalysisConfig:
    return DailyAnalysisConfig(
        enabled=True,
        cooldown_hours=24,
        max_episodes=200,
        max_inbound_messages=80,
        stance_max_keep=30,
        enable_prefix_fallback=True,
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _provider_with_result(payload: dict) -> MagicMock:
    provider = MagicMock()
    response = LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id="tc_1",
                name="emit_daily_analysis",
                arguments=payload,
            )
        ],
    )
    provider.chat_with_retry = AsyncMock(return_value=response)
    return provider


def _provider_failing() -> MagicMock:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("nope"))
    return provider


def _seed_episodes(store: MemoryStore, entries: list[tuple[datetime, str]]):
    path = store.history_file
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"[{ts.strftime('%Y-%m-%d %H:%M')}] {summary}" for ts, summary in entries]
    path.write_text("\n\n".join(lines) + "\n", encoding="utf-8")


def _seed_inbound(
    session_manager: SessionManager,
    channel: str,
    chat_id: str,
    messages: list[tuple[datetime, str]],
):
    sessions_dir = session_manager.sessions_dir
    (sessions_dir / channel).mkdir(parents=True, exist_ok=True)
    path = sessions_dir / channel / f"{chat_id}.jsonl"
    lines = [
        json.dumps(
            {
                "_type": "metadata",
                "key": f"{channel}:{chat_id}",
                "created_at": messages[0][0].isoformat(),
            }
        )
    ]
    for ts, content in messages:
        lines.append(
            json.dumps(
                {
                    "role": "user",
                    "content": content,
                    "timestamp": ts.isoformat(),
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ===========================================================================
# _parse_result (input sanitization)
# ===========================================================================


class TestParseResult:
    def test_full_payload_parses(self) -> None:
        now = datetime(2026, 5, 29)
        result = _parse_result(
            {
                "stance_entries": [
                    {"text": "I prefer dark mode", "source_ts": "2026-05-29T10:00:00"},
                ],
                "predictions": [
                    {"date": "2026-05-30", "text": "Sunday planning", "confidence": "high", "basis": "weekly pattern"},
                ],
                "patterns": [
                    {
                        "kind": "temporal",
                        "text": "Morning person",
                        "confidence": "medium",
                        "supporting_projects": ["raven"],
                    },
                ],
            },
            now,
        )
        assert len(result.stance_entries) == 1
        assert result.stance_entries[0].text == "I prefer dark mode"
        assert len(result.predictions) == 1
        assert result.predictions[0].confidence == "high"
        assert len(result.patterns) == 1
        assert result.patterns[0].supporting_projects == ["raven"]

    def test_drops_invalid_confidence(self) -> None:
        now = datetime(2026, 5, 29)
        result = _parse_result(
            {
                "predictions": [
                    {"date": "2026-05-30", "text": "x", "confidence": "strong", "basis": "y"},
                    {"date": "2026-05-30", "text": "y", "confidence": "low", "basis": "z"},
                ],
            },
            now,
        )
        # The 'strong' one is dropped, the 'low' one kept
        assert len(result.predictions) == 1
        assert result.predictions[0].text == "y"

    def test_drops_invalid_kind(self) -> None:
        now = datetime(2026, 5, 29)
        result = _parse_result(
            {
                "patterns": [
                    {"kind": "garbage", "text": "x", "confidence": "high"},
                    {"kind": "workflow", "text": "y", "confidence": "low"},
                ],
            },
            now,
        )
        assert len(result.patterns) == 1
        assert result.patterns[0].kind == "workflow"

    def test_tolerates_missing_keys(self) -> None:
        now = datetime(2026, 5, 29)
        result = _parse_result({}, now)
        assert result.stance_entries == []
        assert result.predictions == []
        assert result.patterns == []


# ===========================================================================
# DailyAnalysisService — cache + LLM call + fallback
# ===========================================================================


class TestServiceCache:
    def test_caches_within_cooldown(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        _seed_episodes(store, [(clock() - timedelta(days=2), "ep one")])
        provider = _provider_with_result(
            {
                "stance_entries": [],
                "predictions": [],
                "patterns": [],
            }
        )
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        _run(svc.get(clock()))
        _run(svc.get(clock()))  # second call within cooldown
        # Only one LLM call total
        assert provider.chat_with_retry.call_count == 1

    def test_recomputes_after_cooldown(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        _seed_episodes(store, [(clock() - timedelta(days=2), "ep one")])
        provider = _provider_with_result(
            {
                "stance_entries": [],
                "predictions": [],
                "patterns": [],
            }
        )
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        _run(svc.get(clock()))
        clock.advance(25)  # past cooldown
        _run(svc.get(clock()))
        assert provider.chat_with_retry.call_count == 2

    def test_force_bypasses_cooldown(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        _seed_episodes(store, [(clock() - timedelta(days=2), "ep one")])
        provider = _provider_with_result(
            {
                "stance_entries": [],
                "predictions": [],
                "patterns": [],
            }
        )
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        _run(svc.get(clock()))
        _run(svc.get(clock(), force=True))
        assert provider.chat_with_retry.call_count == 2

    def test_returns_none_when_disabled(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        config.enabled = False
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=_provider_with_result({}),
            config=config,
            model="m",
            now_fn=clock,
        )
        result = _run(svc.get(clock()))
        assert result is None


class TestPrefixFallback:
    def test_falls_back_on_llm_failure(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        _seed_inbound(
            session_manager,
            "cli",
            "default",
            [
                (clock() - timedelta(hours=2), "I prefer terse output"),
                (clock() - timedelta(hours=1), "random small talk"),
            ],
        )
        provider = _provider_failing()
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        result = _run(svc.get(clock()))
        assert result is not None
        # Only the "I prefer" message hits the prefix heuristic
        texts = [e.text for e in result.stance_entries]
        assert "I prefer terse output" in texts
        assert "random small talk" not in texts

    def test_disable_fallback_caches_empty_on_failure(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        # Failure path caches an empty result so the cooldown gate
        # throttles retries; second call within cooldown must not hit
        # the LLM again.
        config.enable_prefix_fallback = False
        _seed_inbound(
            session_manager,
            "cli",
            "default",
            [(clock() - timedelta(hours=2), "I prefer X")],
        )
        provider = _provider_failing()
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        result = _run(svc.get(clock()))
        assert result is not None
        assert result.stance_entries == []
        assert result.predictions == []
        assert result.patterns == []

        _run(svc.get(clock()))
        assert provider.chat_with_retry.call_count == 1

    def test_empty_cache_retries_after_1h(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        # Empty cache uses the shorter failure cooldown — still throttled
        # within 1h, but retries once 1h elapses (vs the 24h success path).
        config.enable_prefix_fallback = False
        _seed_inbound(
            session_manager,
            "cli",
            "default",
            [(clock() - timedelta(hours=2), "any user msg")],
        )
        provider = _provider_failing()
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        _run(svc.get(clock()))
        assert provider.chat_with_retry.call_count == 1

        clock.advance(0.5)  # +30min, within failure cooldown
        _run(svc.get(clock()))
        assert provider.chat_with_retry.call_count == 1

        clock.advance(0.6)  # total 1h6min, past failure cooldown
        _run(svc.get(clock()))
        assert provider.chat_with_retry.call_count == 2


# ===========================================================================
# Per-producer rendering
# ===========================================================================


class TestStanceLogProducer:
    def test_renders_entries_sorted_desc(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        provider = _provider_with_result(
            {
                "stance_entries": [
                    {"text": "I prefer dark mode", "source_ts": "2026-05-29T10:00:00"},
                    {"text": "Stop nudging me at night", "source_ts": "2026-05-29T22:30:00"},
                ],
                "predictions": [],
                "patterns": [],
            }
        )
        _seed_episodes(store, [(clock() - timedelta(days=2), "ctx")])
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        producer = StanceLogProducer(
            analysis=svc,
            memory_store=store,
            config=config,
        )
        body = _run(producer.compute_body(clock()))
        assert "Stop nudging me at night" in body
        assert "I prefer dark mode" in body
        # Sorted descending by timestamp — newest first
        assert body.index("22:30") < body.index("10:00")

    def test_merges_with_existing_section(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        import json as _json

        store.stance_log_path.parent.mkdir(parents=True, exist_ok=True)
        store.stance_log_path.write_text(
            _json.dumps(
                {
                    "entries": [
                        {"ts": "2026-05-28T09:00:00", "text": "always run tests before committing"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        provider = _provider_with_result(
            {
                "stance_entries": [
                    {"text": "I prefer dark mode", "source_ts": "2026-05-29T10:00:00"},
                ],
                "predictions": [],
                "patterns": [],
            }
        )
        _seed_episodes(store, [(clock() - timedelta(days=2), "ctx")])
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        producer = StanceLogProducer(
            analysis=svc,
            memory_store=store,
            config=config,
        )
        body = _run(producer.compute_body(clock()))
        assert "always run tests before committing" in body
        assert "I prefer dark mode" in body

    def test_prunes_entries_older_than_30d(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        import json as _json

        store.stance_log_path.parent.mkdir(parents=True, exist_ok=True)
        old_ts = (clock() - timedelta(days=45)).isoformat()
        store.stance_log_path.write_text(
            _json.dumps(
                {
                    "entries": [
                        {"ts": old_ts, "text": "ancient stance"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        provider = _provider_with_result(
            {
                "stance_entries": [
                    {"text": "fresh stance", "source_ts": clock().isoformat()},
                ],
                "predictions": [],
                "patterns": [],
            }
        )
        _seed_episodes(store, [(clock() - timedelta(days=2), "ctx")])
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        producer = StanceLogProducer(
            analysis=svc,
            memory_store=store,
            config=config,
        )
        body = _run(producer.compute_body(clock()))
        assert "fresh stance" in body
        assert "ancient stance" not in body

    def test_bootstrap_from_attention_when_sidecar_missing(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        """First run on a pre-sidecar workspace must pull legacy
        bullets from attention.md so 30d of history isn't lost."""
        store.attention_file.parent.mkdir(parents=True, exist_ok=True)
        store.attention_file.write_text(
            "## Recent stance log (30d)\n"
            "- [2026-05-28T09:00:00] migrated bullet alpha\n"
            "- [2026-05-29T11:00:00] migrated bullet beta\n",
            encoding="utf-8",
        )
        assert not store.stance_log_path.exists()
        provider = _provider_with_result(
            {
                "stance_entries": [],
                "predictions": [],
                "patterns": [],
            }
        )
        _seed_episodes(store, [(clock() - timedelta(days=2), "ctx")])
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        producer = StanceLogProducer(
            analysis=svc,
            memory_store=store,
            config=config,
        )
        body = _run(producer.compute_body(clock()))
        assert "migrated bullet alpha" in body
        assert "migrated bullet beta" in body
        # Bootstrap is one-shot: sidecar now exists with the migrated entries.
        assert store.stance_log_path.exists()

    def test_bootstrap_no_attention_returns_empty(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        """No attention.md and no sidecar — fresh workspace, just
        renders whatever DailyAnalysisService returns."""
        assert not store.attention_file.exists()
        assert not store.stance_log_path.exists()
        provider = _provider_with_result(
            {
                "stance_entries": [
                    {"text": "fresh-only", "source_ts": clock().isoformat()},
                ],
                "predictions": [],
                "patterns": [],
            }
        )
        _seed_episodes(store, [(clock() - timedelta(days=2), "ctx")])
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        producer = StanceLogProducer(
            analysis=svc,
            memory_store=store,
            config=config,
        )
        body = _run(producer.compute_body(clock()))
        assert "fresh-only" in body

    def test_bootstrap_runs_once_only(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        """After the sidecar exists, attention.md is no longer the
        source of truth: editing it directly must not affect output."""
        store.attention_file.parent.mkdir(parents=True, exist_ok=True)
        store.attention_file.write_text(
            "## Recent stance log (30d)\n- [2026-05-28T09:00:00] from attention\n",
            encoding="utf-8",
        )
        provider = _provider_with_result(
            {
                "stance_entries": [],
                "predictions": [],
                "patterns": [],
            }
        )
        _seed_episodes(store, [(clock() - timedelta(days=2), "ctx")])
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        producer = StanceLogProducer(
            analysis=svc,
            memory_store=store,
            config=config,
        )
        # First call bootstraps from attention.md.
        first_body = _run(producer.compute_body(clock()))
        assert "from attention" in first_body
        # Now edit attention.md to drop the bullet; sidecar is the
        # source of truth from here on, so output is unchanged.
        store.attention_file.write_text(
            "## Recent stance log (30d)\n",
            encoding="utf-8",
        )
        second_body = _run(producer.compute_body(clock()))
        assert "from attention" in second_body


class TestPredicted3DProducer:
    def test_renders_by_day(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        provider = _provider_with_result(
            {
                "stance_entries": [],
                "predictions": [
                    {
                        "date": "2026-05-30",
                        "text": "Sunday planning",
                        "confidence": "high",
                        "basis": "weekly pattern, 4/5 Sundays",
                    },
                    {
                        "date": "2026-05-31",
                        "text": "PR review backlog",
                        "confidence": "medium",
                        "basis": "user mentioned EOD",
                    },
                ],
                "patterns": [],
            }
        )
        _seed_episodes(store, [(clock() - timedelta(days=2), "ctx")])
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        producer = Predicted3DProducer(analysis=svc)
        body = _run(producer.compute_body(clock()))
        assert "### 2026-05-30" in body
        assert "Sunday planning" in body
        assert "high" in body
        assert "weekly pattern" in body
        assert "### 2026-05-31" in body

    def test_drops_past_dated_predictions(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        provider = _provider_with_result(
            {
                "stance_entries": [],
                "predictions": [
                    {"date": "2026-05-20", "text": "past event", "confidence": "high", "basis": "should be dropped"},
                    {"date": "2026-05-30", "text": "future event", "confidence": "high", "basis": "should be kept"},
                ],
                "patterns": [],
            }
        )
        _seed_episodes(store, [(clock() - timedelta(days=2), "ctx")])
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        producer = Predicted3DProducer(analysis=svc)
        body = _run(producer.compute_body(clock()))
        assert "past event" not in body
        assert "future event" in body


class TestBehaviorPatternsProducer:
    def test_groups_by_kind(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        provider = _provider_with_result(
            {
                "stance_entries": [],
                "predictions": [],
                "patterns": [
                    {
                        "kind": "temporal",
                        "text": "Morning person",
                        "confidence": "high",
                        "supporting_projects": ["raven"],
                    },
                    {
                        "kind": "workflow",
                        "text": "PR-first then commit",
                        "confidence": "medium",
                        "supporting_projects": [],
                    },
                    {
                        "kind": "topical",
                        "text": "Recurring infra theme",
                        "confidence": "low",
                        "supporting_projects": ["raven", "side-x"],
                    },
                ],
            }
        )
        _seed_episodes(store, [(clock() - timedelta(days=2), "ctx")])
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=config,
            model="m",
            now_fn=clock,
        )
        producer = BehaviorPatternsProducer(analysis=svc)
        body = _run(producer.compute_body(clock()))
        assert "### Temporal patterns" in body
        assert "### Workflow patterns" in body
        assert "### Topical patterns" in body
        # Ordering: temporal first
        assert body.index("Temporal") < body.index("Workflow") < body.index("Topical")
        assert "`raven`" in body
        assert "side-x" in body


class TestNestedKeyDerivation:
    def test_metadata_less_inbound_file_derives_channel_chat_key(
        self,
        store,
        routine_store,
        session_manager,
        config,
        clock,
    ) -> None:
        """A metadata-less inbound file under the nested layout
        (sessions/{channel}/{chat_id}.jsonl) derives its key from
        parent.name + stem, not the flat path.stem.replace('_', ':', 1)
        which mis-derives a chat_id that contains an underscore."""
        recent = clock() - timedelta(hours=1)
        sessions_dir = session_manager.sessions_dir
        (sessions_dir / "telegram").mkdir(parents=True, exist_ok=True)
        path = sessions_dir / "telegram" / "user_42.jsonl"
        path.write_text(
            json.dumps(
                {
                    "role": "user",
                    "content": "I prefer terse output",
                    "timestamp": recent.isoformat(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        svc = DailyAnalysisService(
            memory_store=store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=_provider_with_result({}),
            config=config,
            model="m",
            now_fn=clock,
        )
        keys = [k for k, _, _ in svc._assemble_inbound(clock())]
        assert "telegram:user_42" in keys
        assert "user:42" not in keys
