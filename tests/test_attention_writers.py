"""Tests for the 7 Tier-A AttentionProducer subclasses + AttentionUpdater.

Each producer is exercised in isolation (calling ``compute_body`` directly
with a now-arg) plus an end-to-end pass via ``AttentionUpdater`` to
verify the splice + lock + compare-and-skip path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from raven.memory_engine.consolidate.attention import parse_attention
from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.proactive_engine.sentinel.attention_producers import (
    ActiveThreadsProducer,
    ArchivedPatternsProducer,
    PendingProposalsProducer,
    ProjectRhythmProducer,
    RecentProactiveDecisionsProducer,
    RecentlyAbandonedProducer,
    RejectedCooldownProducer,
)
from raven.proactive_engine.sentinel.attention_updater import AttentionUpdater
from raven.proactive_engine.sentinel.executor.pending_decision import (
    PendingDecisionStore,
)
from raven.proactive_engine.sentinel.feedback.tracker import NudgeFeedbackTracker
from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore
from raven.proactive_engine.sentinel.types import (
    PendingDecision, Routine, TaskOption,
)


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
def feedback(tmp_path: Path, clock: Clock) -> NudgeFeedbackTracker:
    return NudgeFeedbackTracker(
        log_path=tmp_path / "feedback.jsonl", now_fn=clock,
    )


@pytest.fixture
def pending_store(tmp_path: Path) -> PendingDecisionStore:
    return PendingDecisionStore(tmp_path / "pending.json")


@pytest.fixture
def routine_store(tmp_path: Path) -> RoutineStore:
    return RoutineStore(tmp_path / "routines.json")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# RecentProactiveDecisionsProducer
# ===========================================================================


class TestRecentDecisions:
    def test_empty(self, feedback, clock) -> None:
        p = RecentProactiveDecisionsProducer(feedback)
        assert _run(p.compute_body(clock())) == ""

    def test_dispatched_with_topic_score_and_outcome(
        self, feedback, clock,
    ) -> None:
        feedback.record_dispatched(
            "n1", action="nudge", session_key="cli:default",
            priority="high", proactivity_score=0.8,
            details={"topic_tag": "deadline_x"},
        )
        feedback.record_accepted("n1")
        body = _run(
            RecentProactiveDecisionsProducer(feedback).compute_body(clock()),
        )
        assert "deadline_x" in body
        assert "prio=high" in body
        assert "score=0.80" in body
        assert "→ accepted" in body

    def test_skips_outside_window(self, feedback, clock) -> None:
        old_ts = (clock() - timedelta(days=30)).isoformat()
        feedback._recent.append({
            "ts": old_ts, "id": "old", "signal": "dispatched",
            "priority": "low", "proactivity_score": 0.1,
        })
        feedback.record_dispatched(
            "fresh", action="nudge", session_key="cli:default",
            priority="medium", proactivity_score=0.5,
        )
        body = _run(RecentProactiveDecisionsProducer(
            feedback, since_days=14,
        ).compute_body(clock()))
        assert "score=0.50" in body
        assert "score=0.10" not in body


# ===========================================================================
# PendingProposalsProducer + RejectedCooldownProducer
# ===========================================================================


def _put_decision(
    store: PendingDecisionStore, decision_id: str, *,
    created_at_ms: int, channel: str = "cli", to: str = "default",
    title: str = "Sample option",
) -> PendingDecision:
    decision = PendingDecision(
        decision_id=decision_id,
        channel=channel, to=to,
        created_at_ms=created_at_ms,
        ttl_min=60,
        options=[TaskOption(
            id="opt_1", title=title, why="why",
            type="ad_hoc", exec_kind="message",
        )],
    )
    store.put(decision)
    return decision


class TestPendingProposalsProducer:
    def test_empty(self, pending_store, clock) -> None:
        body = _run(PendingProposalsProducer(pending_store).compute_body(clock()))
        assert body == ""

    def test_renders_active(self, pending_store, clock) -> None:
        now_ms = int(clock().timestamp() * 1000)
        _put_decision(
            pending_store, "dec_a1b2",
            created_at_ms=now_ms - 5 * 60_000,
            title="Try planning",
        )
        body = _run(PendingProposalsProducer(pending_store).compute_body(clock()))
        assert "dec_a1b2" in body
        assert "Try planning" in body
        assert "[open]" in body
        assert "cli:default" in body

    def test_skips_consumed_and_expired(self, pending_store, clock) -> None:
        now_ms = int(clock().timestamp() * 1000)
        _put_decision(
            pending_store, "dec_active",
            created_at_ms=now_ms - 5 * 60_000,
            channel="cli", to="alice",
        )
        _put_decision(
            pending_store, "dec_old",
            created_at_ms=now_ms - 24 * 3_600_000,
            channel="telegram", to="bob",
        )
        body = _run(PendingProposalsProducer(pending_store).compute_body(clock()))
        assert "dec_active" in body
        assert "dec_old" not in body


class TestRejectedCooldownProducer:
    def test_empty(self, pending_store, clock) -> None:
        body = _run(RejectedCooldownProducer(pending_store).compute_body(clock()))
        assert body == ""

    def test_renders_dismissed_within_window(
        self, pending_store, clock,
    ) -> None:
        now_ms = int(clock().timestamp() * 1000)
        _put_decision(
            pending_store, "dec_rejected",
            created_at_ms=now_ms - 2 * 3_600_000,
            title="Try a new routine",
        )
        pending_store.mark_consumed(
            decision_id="dec_rejected",
            picked_option_id=None,
            consumed_at_ms=now_ms - 1 * 3_600_000,
        )
        body = _run(RejectedCooldownProducer(
            pending_store, cooldown_hours=24,
        ).compute_body(clock()))
        assert "dec_rejected" in body
        assert "Try a new routine" in body
        assert "cooldown until" in body

    def test_skips_picked(self, pending_store, clock) -> None:
        now_ms = int(clock().timestamp() * 1000)
        _put_decision(
            pending_store, "dec_picked",
            created_at_ms=now_ms - 2 * 3_600_000,
        )
        pending_store.mark_consumed(
            decision_id="dec_picked",
            picked_option_id="opt_1",
            consumed_at_ms=now_ms - 1 * 3_600_000,
        )
        body = _run(RejectedCooldownProducer(pending_store).compute_body(clock()))
        assert "dec_picked" not in body

    def test_skips_outside_cooldown(self, pending_store, clock) -> None:
        now_ms = int(clock().timestamp() * 1000)
        _put_decision(
            pending_store, "dec_old",
            created_at_ms=now_ms - 48 * 3_600_000,
        )
        pending_store.mark_consumed(
            decision_id="dec_old",
            picked_option_id=None,
            consumed_at_ms=now_ms - 30 * 3_600_000,
        )
        body = _run(RejectedCooldownProducer(
            pending_store, cooldown_hours=24,
        ).compute_body(clock()))
        assert "dec_old" not in body


# ===========================================================================
# RoutineStore-backed producers
# ===========================================================================


def _add_routine(
    store: RoutineStore, **overrides: Any,
) -> None:
    defaults: dict[str, Any] = dict(
        id="r_xyz", pattern="weekly_planning", keywords=[],
        day_of_week=6, time_slot=(19, 21),
        status="candidate", occurrence_count=4,
        last_triggered="2026-05-29T19:00:00",
        user_confirmed=False, weight=2.5,
    )
    defaults.update(overrides)
    r = Routine(**defaults)

    def _mutate(state: dict[str, Any]) -> dict[str, Any]:
        routines_list = state.setdefault("routines", [])
        routines_list.append({
            "id": r.id, "pattern": r.pattern, "keywords": r.keywords,
            "day_of_week": r.day_of_week,
            "time_slot": list(r.time_slot) if r.time_slot else None,
            "status": r.status, "occurrence_count": r.occurrence_count,
            "last_triggered": r.last_triggered,
            "user_confirmed": r.user_confirmed,
            "weight": r.weight, "dismissed_at_ms": r.dismissed_at_ms,
        })
        return state

    store._store.update(_mutate)


class TestActiveThreadsProducer:
    def test_empty(self, routine_store, clock) -> None:
        body = _run(ActiveThreadsProducer(routine_store).compute_body(clock()))
        assert body == ""

    def test_renders_only_active_confirmed(self, routine_store, clock) -> None:
        _add_routine(routine_store, id="r_active",
                     status="active", user_confirmed=True,
                     pattern="morning_standup")
        _add_routine(routine_store, id="r_candidate",
                     status="candidate", user_confirmed=False)
        _add_routine(routine_store, id="r_retired",
                     status="retired", user_confirmed=True)
        body = _run(ActiveThreadsProducer(routine_store).compute_body(clock()))
        assert "r_active" in body
        assert "morning_standup" in body
        assert "r_candidate" not in body
        assert "r_retired" not in body


class TestRecentlyAbandonedProducer:
    def test_empty(self, routine_store, clock) -> None:
        body = _run(RecentlyAbandonedProducer(routine_store).compute_body(clock()))
        assert body == ""

    def test_silence_7_to_30d(self, routine_store, clock) -> None:
        now = clock()
        _add_routine(routine_store, id="r_abandon", status="active",
                     last_triggered=(now - timedelta(days=8)).isoformat(),
                     pattern="abandoned 8d ago")
        _add_routine(routine_store, id="r_archived", status="active",
                     last_triggered=(now - timedelta(days=45)).isoformat(),
                     pattern="too old")
        _add_routine(routine_store, id="r_fresh", status="active",
                     last_triggered=(now - timedelta(days=3)).isoformat(),
                     pattern="still fresh")
        body = _run(RecentlyAbandonedProducer(routine_store).compute_body(now))
        assert "r_abandon" in body
        assert "r_archived" not in body
        assert "r_fresh" not in body


class TestArchivedPatternsProducer:
    def test_empty(self, routine_store, clock) -> None:
        body = _run(ArchivedPatternsProducer(routine_store).compute_body(clock()))
        assert body == ""

    def test_renders_only_retired(self, routine_store, clock) -> None:
        now_ms = int(clock().timestamp() * 1000)
        _add_routine(routine_store, id="r_retired", status="retired",
                     user_confirmed=True, pattern="user said no",
                     dismissed_at_ms=now_ms)
        _add_routine(routine_store, id="r_active", status="active",
                     user_confirmed=True, pattern="alive")
        body = _run(ArchivedPatternsProducer(routine_store).compute_body(clock()))
        assert "r_retired" in body
        assert "user said no" in body
        assert "r_active" not in body


# ===========================================================================
# ProjectRhythmProducer
# ===========================================================================


class TestProjectRhythmProducer:
    def test_empty(self, store, clock) -> None:
        body = _run(ProjectRhythmProducer(store).compute_body(clock()))
        assert body == ""

    def test_renders_top_projects(self, store, clock) -> None:
        ws = store.history_file
        ws.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        base = clock() - timedelta(days=3)
        for i, hour in enumerate((9, 10, 9, 10, 11)):
            ts = (base + timedelta(days=i)).replace(hour=hour, minute=0)
            lines.append(
                f"[{ts.strftime('%Y-%m-%d %H:%M')}] worked on api "
                f"#project-raven #design",
            )
        sat = clock() - timedelta(days=1)
        for hour in (14, 15):
            ts = sat.replace(hour=hour, minute=0)
            lines.append(
                f"[{ts.strftime('%Y-%m-%d %H:%M')}] tinkered "
                f"#project-side-x #infra",
            )
        ws.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
        body = _run(ProjectRhythmProducer(store).compute_body(clock()))
        assert "**raven** (5 ep)" in body
        assert "**side-x** (2 ep)" in body
        assert "hours" in body


# ===========================================================================
# AttentionUpdater orchestrator
# ===========================================================================


def _build_updater(
    store, feedback, pending_store, routine_store, clock,
) -> AttentionUpdater:
    return AttentionUpdater(
        memory_store=store,
        producers=[
            PendingProposalsProducer(pending_store),
            RejectedCooldownProducer(pending_store),
            RecentProactiveDecisionsProducer(feedback),
            ActiveThreadsProducer(routine_store),
            RecentlyAbandonedProducer(routine_store),
            ArchivedPatternsProducer(routine_store),
            ProjectRhythmProducer(store),
        ],
        now_fn=clock,
    )


class TestAttentionUpdater:
    def test_all_empty_no_write(
        self, store, feedback, pending_store, routine_store, clock,
    ) -> None:
        updater = _build_updater(
            store, feedback, pending_store, routine_store, clock,
        )
        changed = _run(updater.update())
        # All bodies empty → splice produces same text as on disk (which
        # is empty) → no write happened.
        assert all(v is False for v in changed.values())
        assert not store.attention_file.exists()

    def test_writes_when_one_producer_has_data(
        self, store, feedback, pending_store, routine_store, clock,
    ) -> None:
        feedback.record_dispatched(
            "n1", action="nudge", session_key="cli:default",
            priority="medium", proactivity_score=0.5,
            details={"topic_tag": "x"},
        )
        updater = _build_updater(
            store, feedback, pending_store, routine_store, clock,
        )
        changed = _run(updater.update())
        assert changed["## Recent proactive decisions (14d)"] is True
        assert store.attention_file.exists()
        sections = parse_attention(
            store.attention_file.read_text(encoding="utf-8"),
        )
        assert "## Recent proactive decisions (14d)" in sections

    def test_idempotent_second_run(
        self, store, feedback, pending_store, routine_store, clock,
    ) -> None:
        feedback.record_dispatched(
            "n1", action="nudge", session_key="cli:default",
            priority="medium", proactivity_score=0.5,
        )
        updater = _build_updater(
            store, feedback, pending_store, routine_store, clock,
        )
        _run(updater.update())
        first = store.attention_file.read_text(encoding="utf-8")
        changed = _run(updater.update())
        assert all(v is False for v in changed.values())
        assert store.attention_file.read_text(encoding="utf-8") == first

    def test_preserves_external_section(
        self, store, feedback, pending_store, routine_store, clock,
    ) -> None:
        store.attention_file.parent.mkdir(parents=True, exist_ok=True)
        store.attention_file.write_text(
            "## Sentinel Observations (auto)\n\n"
            "<!-- last_updated=2026-05-29T13:00 -->\n\nbody\n",
            encoding="utf-8",
        )
        feedback.record_dispatched(
            "n1", action="nudge", session_key="cli:default",
            priority="medium", proactivity_score=0.5,
        )
        updater = _build_updater(
            store, feedback, pending_store, routine_store, clock,
        )
        _run(updater.update())
        body = store.attention_file.read_text(encoding="utf-8")
        assert "## Sentinel Observations (auto)" in body
        assert "## Recent proactive decisions (14d)" in body
