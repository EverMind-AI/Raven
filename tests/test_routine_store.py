"""Unit tests for RoutineStore (MS1.5)."""

from __future__ import annotations

from pathlib import Path

from raven.proactive_engine.sentinel.predictor.routine_store import (
    DEFAULT_DISMISS_COOLDOWN_DAYS,
    RoutineStore,
)
from raven.proactive_engine.sentinel.types import LLMValidation, Routine


# ── helpers ───────────────────────────────────────────────────────────


def _routine(
    rid: str = "dow1-h09-meeting",
    *,
    pattern: str = "Tuesday 09:00-12:00 — meeting · standup",
    keywords: tuple[str, ...] = ("meeting", "standup"),
    day_of_week: int = 1,
    time_slot: tuple[int, int] = (9, 12),
    status: str = "candidate",
    occurrence_count: int = 4,
    last_triggered: str | None = None,
    weight: float = 4.0,
    description: str | None = None,
    user_confirmed_at_ms: int | None = None,
    dismissed_at_ms: int | None = None,
) -> Routine:
    return Routine(
        id=rid,
        pattern=pattern,
        keywords=list(keywords),
        day_of_week=day_of_week,
        time_slot=time_slot,
        status=status,
        occurrence_count=occurrence_count,
        last_triggered=last_triggered,
        user_confirmed=(status == "active"),
        weight=weight,
        description=description,
        user_confirmed_at_ms=user_confirmed_at_ms,
        dismissed_at_ms=dismissed_at_ms,
    )


_NOW_MS = 1_700_000_000_000


def _ms_days_ago(days: int) -> int:
    return _NOW_MS - days * 24 * 60 * 60 * 1000


# ── tests ─────────────────────────────────────────────────────────────


def test_merge_persists_new_candidates(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    learned = [_routine(), _routine(rid="dow6-h08-running",
                                    pattern="Saturday 08:00-11:00 — running")]
    merged = store.merge(learned, now_ms=_NOW_MS)

    assert len(merged) == 2
    assert {r.id for r in merged} == {"dow1-h09-meeting", "dow6-h08-running"}
    # Persisted
    assert {r.id for r in store.all_routines()} == {"dow1-h09-meeting",
                                                     "dow6-h08-running"}


def test_merge_preserves_user_confirmed_status(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    # First merge: candidate
    store.merge([_routine()], now_ms=_NOW_MS)
    # User confirms
    assert store.upgrade("dow1-h09-meeting",
                         confirmed_at_ms=_NOW_MS + 60_000) is True
    # Second merge with refreshed stats — status should stay active
    refreshed = _routine(occurrence_count=8, weight=8.0, keywords=("meeting",))
    merged = store.merge([refreshed], now_ms=_NOW_MS + 120_000)

    persisted = store.get("dow1-h09-meeting")
    assert persisted is not None
    assert persisted.status == "active"
    assert persisted.user_confirmed is True
    assert persisted.user_confirmed_at_ms == _NOW_MS + 60_000
    # Stats refreshed though
    assert persisted.occurrence_count == 8
    assert persisted.weight == 8.0


def test_dismiss_blocks_re_surfacing_within_cooldown(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    store.merge([_routine()], now_ms=_NOW_MS)
    assert store.dismiss("dow1-h09-meeting",
                         dismissed_at_ms=_NOW_MS) is True

    # Within cooldown — re-derive the routine again, expect it excluded
    fresh = _routine(occurrence_count=10)
    merged = store.merge([fresh], now_ms=_NOW_MS + 30 * 24 * 60 * 60_000)
    assert "dow1-h09-meeting" not in {r.id for r in merged}

    # Persisted state still has it as retired
    persisted = store.get("dow1-h09-meeting")
    assert persisted is not None
    assert persisted.status == "retired"


def test_dismiss_revives_after_cooldown(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    store.merge([_routine()], now_ms=_NOW_MS)
    store.dismiss("dow1-h09-meeting", dismissed_at_ms=_NOW_MS)

    # Past cooldown (default 60 days) — re-derive should revive as candidate
    past_cooldown = _NOW_MS + (DEFAULT_DISMISS_COOLDOWN_DAYS + 1) * 24 * 60 * 60_000
    merged = store.merge([_routine(occurrence_count=10)], now_ms=past_cooldown)
    assert "dow1-h09-meeting" in {r.id for r in merged}
    revived = store.get("dow1-h09-meeting")
    assert revived is not None
    assert revived.status == "candidate"
    assert revived.dismissed_at_ms is None


def test_routine_disappears_from_learned_keeps_persisted_state(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    store.merge([_routine()], now_ms=_NOW_MS)
    store.upgrade("dow1-h09-meeting", confirmed_at_ms=_NOW_MS + 1000)

    # Next merge with empty learned set — confirmed routine should survive
    merged = store.merge([], now_ms=_NOW_MS + 60_000)
    assert {r.id for r in merged} == {"dow1-h09-meeting"}
    assert store.get("dow1-h09-meeting").status == "active"


def test_retired_past_cooldown_routine_drops_when_not_re_learned(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    store.merge([_routine()], now_ms=_NOW_MS)
    store.dismiss("dow1-h09-meeting", dismissed_at_ms=_NOW_MS)

    # Past cooldown + not re-learned → drop entirely
    past_cooldown = _NOW_MS + (DEFAULT_DISMISS_COOLDOWN_DAYS + 1) * 24 * 60 * 60_000
    merged = store.merge([], now_ms=past_cooldown)
    assert merged == []
    # And it's gone from storage too
    assert store.get("dow1-h09-meeting") is None


def test_upsert_description_updates_aggregator_fields(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    store.merge([_routine()], now_ms=_NOW_MS)
    assert store.upsert_description(
        "dow1-h09-meeting",
        description="weekly engineering team standup",
        semantic_group="standup",
    ) is True
    r = store.get("dow1-h09-meeting")
    assert r is not None
    assert r.description == "weekly engineering team standup"
    assert r.semantic_group == "standup"


def test_serialization_roundtrip_preserves_extension_fields(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    learned = [_routine(weight=12.5, description="weekly engineering standup")]
    store.merge(learned, now_ms=_NOW_MS)

    fresh_store = RoutineStore(tmp_path / "routines.json")
    persisted = fresh_store.get("dow1-h09-meeting")
    assert persisted is not None
    assert persisted.weight == 12.5
    # Description from learner is preserved (merge() doesn't strip it)
    assert persisted.description == "weekly engineering standup"
    assert persisted.time_slot == (9, 12)
    assert persisted.day_of_week == 1


def test_serialization_roundtrip_preserves_llm_validation(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    r = _routine()
    r.llm_validation = LLMValidation(
        is_routine=True,
        confidence=0.82,
        reason="3 consecutive Tuesdays mention 'meeting' between 09-12, supported by HISTORY",
        validated_at_ms=_NOW_MS,
    )
    store.merge([r], now_ms=_NOW_MS)

    fresh_store = RoutineStore(tmp_path / "routines.json")
    persisted = fresh_store.get("dow1-h09-meeting")
    assert persisted is not None
    assert persisted.llm_validation is not None
    assert persisted.llm_validation.is_routine is True
    assert persisted.llm_validation.confidence == 0.82
    assert "3 consecutive Tuesdays" in persisted.llm_validation.reason
    assert persisted.llm_validation.validated_at_ms == _NOW_MS


def test_serialization_back_compat_no_llm_validation_field(tmp_path: Path):
    """Old persisted routines (pre-PR-B) have no ``llm_validation`` key — must
    deserialize cleanly with the field defaulted to None."""
    import json
    p = tmp_path / "routines.json"
    p.write_text(json.dumps({
        "routines": [{
            "id": "dow1-h09-meeting",
            "pattern": "Tuesday 09:00-12:00 — meeting",
            "keywords": ["meeting"],
            "day_of_week": 1,
            "time_slot": [9, 12],
            "status": "candidate",
            "occurrence_count": 4,
            "weight": 4.0,
        }]
    }))
    store = RoutineStore(p)
    r = store.get("dow1-h09-meeting")
    assert r is not None
    assert r.llm_validation is None


def test_serialization_invalid_llm_validation_falls_back_to_none(tmp_path: Path):
    """Malformed llm_validation in persisted JSON shouldn't crash load."""
    import json
    p = tmp_path / "routines.json"
    p.write_text(json.dumps({
        "routines": [{
            "id": "dow1-h09-meeting",
            "pattern": "x",
            "keywords": [],
            "day_of_week": 1,
            "time_slot": [9, 12],
            "status": "candidate",
            "occurrence_count": 3,
            "weight": 3.0,
            "llm_validation": "not a dict",  # corrupt
        }]
    }))
    r = RoutineStore(p).get("dow1-h09-meeting")
    assert r is not None
    assert r.llm_validation is None


def test_upgrade_returns_false_for_missing(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    assert store.upgrade("nope", confirmed_at_ms=_NOW_MS) is False


def test_dismiss_returns_false_for_missing(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    assert store.dismiss("nope", dismissed_at_ms=_NOW_MS) is False


def test_active_and_candidates_filters(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.json")
    learned = [
        _routine(rid="r1"),
        _routine(rid="r2", pattern="Wednesday 09:00 — sync"),
        _routine(rid="r3", pattern="Friday 14:00 — review"),
    ]
    store.merge(learned, now_ms=_NOW_MS)
    store.upgrade("r1", confirmed_at_ms=_NOW_MS + 1000)

    assert {r.id for r in store.active()} == {"r1"}
    assert {r.id for r in store.candidates()} == {"r2", "r3"}


def test_cross_process_visibility(tmp_path: Path):
    path = tmp_path / "routines.json"
    a = RoutineStore(path)
    b = RoutineStore(path)

    a.merge([_routine()], now_ms=_NOW_MS)
    assert b.get("dow1-h09-meeting") is not None

    b.upgrade("dow1-h09-meeting", confirmed_at_ms=_NOW_MS + 1000)
    seen = a.get("dow1-h09-meeting")
    assert seen is not None
    assert seen.status == "active"
