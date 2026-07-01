"""Unit tests for TaskDiscoverer's Stage 1 LLM-validation wiring.

Covers two surfaces:

- ``_validate_uncached_routines`` — runs the validator on candidates that
  don't yet have a cached llm_validation, persists verdicts via
  ``RoutineStore.set_llm_validation``, and never blows up when the
  validator returns None or raises.
- ``_passes_llm_gate`` / ``_format_candidate_routines`` — drops candidates
  whose validation says not-a-routine or low-confidence; lets unvalidated
  ones pass through (feature-off / first-tick parity)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.proactive_engine.sentinel.predictor.task_discoverer import (
    TaskDiscoverer,
    _passes_llm_gate,
)
from raven.proactive_engine.sentinel.types import LLMValidation, Routine, TaskOption

_NOW = datetime(2026, 5, 14, 8, 0)
_NOW_MS = int(_NOW.timestamp() * 1000)


def _routine(rid: str, *, count: int = 4, validation: LLMValidation | None = None) -> Routine:
    return Routine(
        id=rid,
        pattern=f"pattern {rid}",
        keywords=["k"],
        occurrence_count=count,
        weight=float(count),
        status="candidate",
        llm_validation=validation,
    )


def _disco_with_validator(
    *,
    candidates: list[Routine],
    validator: AsyncMock | None,
    confidence_floor: float = 0.6,
) -> tuple[TaskDiscoverer, MagicMock]:
    """Wire a TaskDiscoverer where routine_store.candidates() returns ``candidates``
    and ``set_llm_validation`` is a MagicMock to assert on."""
    routine_store = MagicMock()
    routine_store.candidates.return_value = candidates
    memory_store = MagicMock()
    memory_store.read_history_since.return_value = "[2026-05-01 08:00] x"
    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=MagicMock(),
        dispatcher=MagicMock(),
        provider=MagicMock(),
        model="x",
        routine_store=routine_store,
        routine_validator=validator,
        validator_confidence_floor=confidence_floor,
        now_fn=lambda: _NOW,
    )
    return disco, routine_store


# ── _passes_llm_gate ─────────────────────────────────────────────────


def test_gate_unvalidated_passes_through():
    """No llm_validation attached → must pass (validator-off / first-tick parity)."""
    r = _routine("a", validation=None)
    assert _passes_llm_gate(r, confidence_floor=0.6) is True


def test_gate_not_routine_verdict_filtered():
    """Validator said 'this is keyword coincidence' → drop."""
    r = _routine(
        "a",
        validation=LLMValidation(
            is_routine=False,
            confidence=0.9,
            reason="3 different topics",
            validated_at_ms=1,
        ),
    )
    assert _passes_llm_gate(r, confidence_floor=0.6) is False


def test_gate_low_confidence_filtered_even_when_is_routine_true():
    """Validator unsure (confidence < floor) → drop."""
    r = _routine(
        "a",
        validation=LLMValidation(
            is_routine=True,
            confidence=0.4,
            reason="weak",
            validated_at_ms=1,
        ),
    )
    assert _passes_llm_gate(r, confidence_floor=0.6) is False


def test_gate_high_confidence_passes():
    r = _routine(
        "a",
        validation=LLMValidation(
            is_routine=True,
            confidence=0.85,
            reason="3 Tuesdays",
            validated_at_ms=1,
        ),
    )
    assert _passes_llm_gate(r, confidence_floor=0.6) is True


def test_gate_at_floor_passes():
    """Boundary: confidence == floor must pass (>=, not >)."""
    r = _routine(
        "a",
        validation=LLMValidation(
            is_routine=True,
            confidence=0.6,
            reason="boundary",
            validated_at_ms=1,
        ),
    )
    assert _passes_llm_gate(r, confidence_floor=0.6) is True


# ── _format_candidate_routines applies the gate ─────────────────────


def test_format_drops_not_routine_verdicts():
    """A routine the validator rejected must NOT appear in the formatted summary."""
    candidates = [
        _routine(
            "real",
            count=5,
            validation=LLMValidation(
                is_routine=True,
                confidence=0.9,
                reason="clear",
                validated_at_ms=1,
            ),
        ),
        _routine(
            "noise",
            count=5,
            validation=LLMValidation(
                is_routine=False,
                confidence=0.95,
                reason="3 different topics",
                validated_at_ms=1,
            ),
        ),
    ]
    disco, _ = _disco_with_validator(candidates=candidates, validator=None)
    out = disco._format_candidate_routines(now_ms=_NOW_MS)
    assert "real:" in out
    assert "noise:" not in out


def test_format_keeps_unvalidated_candidates():
    """Mix of unvalidated + low-confidence-rejected — unvalidated still surfaces."""
    candidates = [
        _routine("untouched", count=5),  # no validation
        _routine(
            "rejected",
            count=5,
            validation=LLMValidation(
                is_routine=True,
                confidence=0.3,
                reason="weak",
                validated_at_ms=1,
            ),
        ),
    ]
    disco, _ = _disco_with_validator(candidates=candidates, validator=None)
    out = disco._format_candidate_routines(now_ms=_NOW_MS)
    assert "untouched:" in out
    assert "rejected:" not in out


# ── _validate_uncached_routines ─────────────────────────────────────


@pytest.mark.asyncio
async def test_validator_called_only_for_uncached_candidates():
    cached = _routine(
        "cached",
        validation=LLMValidation(
            is_routine=True,
            confidence=0.8,
            reason="prior",
            validated_at_ms=42,
        ),
    )
    fresh = _routine("fresh", validation=None)
    validator = AsyncMock()
    validator.validate.return_value = LLMValidation(
        is_routine=True,
        confidence=0.7,
        reason="new",
        validated_at_ms=_NOW_MS,
    )
    disco, store = _disco_with_validator(
        candidates=[cached, fresh],
        validator=validator,
    )
    await disco._validate_uncached_routines(now_ms=_NOW_MS)

    # Only the uncached one triggers an LLM call.
    assert validator.validate.await_count == 1
    called_routine_id = validator.validate.await_args.args[0].id
    assert called_routine_id == "fresh"
    # And the verdict is persisted.
    assert store.set_llm_validation.call_count == 1
    assert store.set_llm_validation.call_args.args[0] == "fresh"


@pytest.mark.asyncio
async def test_validator_none_returns_silent_no_call_no_persist():
    """validator returning None (soft failure) must NOT mark the routine
    as rejected; it stays unvalidated for a retry next pass."""
    validator = AsyncMock()
    validator.validate.return_value = None
    fresh = _routine("fresh", validation=None)
    disco, store = _disco_with_validator(
        candidates=[fresh],
        validator=validator,
    )
    await disco._validate_uncached_routines(now_ms=_NOW_MS)
    # Validator was called but returned None → no persistence.
    assert validator.validate.await_count == 1
    assert store.set_llm_validation.call_count == 0


@pytest.mark.asyncio
async def test_validator_exception_is_caught_other_candidates_still_processed():
    """A validator raise on one candidate must NOT abort processing the others."""
    validator = AsyncMock()

    async def _flaky(routine, history_md, *, now_ms):
        if routine.id == "boom":
            raise RuntimeError("simulated LLM crash")
        return LLMValidation(
            is_routine=True,
            confidence=0.8,
            reason="ok",
            validated_at_ms=now_ms,
        )

    validator.validate.side_effect = _flaky

    disco, store = _disco_with_validator(
        candidates=[_routine("boom"), _routine("good")],
        validator=validator,
    )
    await disco._validate_uncached_routines(now_ms=_NOW_MS)
    # Despite the raise, "good" got validated and persisted.
    assert store.set_llm_validation.call_count == 1
    assert store.set_llm_validation.call_args.args[0] == "good"


@pytest.mark.asyncio
async def test_validate_noop_when_validator_not_wired():
    """validator=None: method short-circuits, no side effects."""
    disco, store = _disco_with_validator(
        candidates=[_routine("x")],
        validator=None,
    )
    await disco._validate_uncached_routines(now_ms=_NOW_MS)
    assert store.set_llm_validation.call_count == 0


@pytest.mark.asyncio
async def test_validator_skips_sub_threshold_candidates():
    """Don't waste LLM calls on candidates that the surfacing layer will
    filter out by min_occurrences_to_surface (default 3) anyway."""
    validator = AsyncMock()
    validator.validate.return_value = LLMValidation(
        is_routine=True,
        confidence=0.7,
        reason="ok",
        validated_at_ms=_NOW_MS,
    )
    candidates = [
        _routine("one", count=1),  # below default floor of 3
        _routine("two", count=2),  # below floor
        _routine("three", count=3),  # at floor
        _routine("five", count=5),  # above floor
    ]
    disco, store = _disco_with_validator(
        candidates=candidates,
        validator=validator,
    )
    # disco's default min_occurrences_to_surface = 3
    await disco._validate_uncached_routines(now_ms=_NOW_MS)

    # Only "three" and "five" should be validated.
    validated_ids = [call.args[0].id for call in validator.validate.await_args_list]
    assert sorted(validated_ids) == ["five", "three"]
    assert validator.validate.await_count == 2
    assert store.set_llm_validation.call_count == 2


@pytest.mark.asyncio
async def test_validate_uses_bounded_history_since():
    """Validator should call memory_store.read_history_since(60d) — not read
    the full HISTORY.md file (could be MBs on long-lived users)."""
    validator = AsyncMock()
    validator.validate.return_value = LLMValidation(
        is_routine=True,
        confidence=0.8,
        reason="ok",
        validated_at_ms=_NOW_MS,
    )
    disco, _ = _disco_with_validator(
        candidates=[_routine("a", count=5)],
        validator=validator,
    )
    await disco._validate_uncached_routines(now_ms=_NOW_MS)
    # memory_store.read_history_since must have been called with a 60-day window
    call = disco.memory_store.read_history_since.call_args
    assert call is not None, "read_history_since was never called"
    since_ms = call.args[0]
    sixty_days_ms = 60 * 86400 * 1000
    assert since_ms == _NOW_MS - sixty_days_ms
    # And the legacy full-file read is NOT used
    assert not disco.memory_store.history_file.read_text.called


@pytest.mark.asyncio
async def test_validate_summary_log_records_outcome_counts(caplog):
    """After a validation pass, an INFO summary log line is emitted with
    accepted / rejected / error counts."""
    import logging

    from loguru import logger

    # Bridge loguru → stdlib caplog (loguru doesn't write to logging by default)
    handler_id = logger.add(
        lambda msg: logging.getLogger("loguru.bridge").info(msg),
        level="INFO",
    )
    try:
        validator = AsyncMock()
        outcomes = [
            LLMValidation(is_routine=True, confidence=0.9, reason="r", validated_at_ms=_NOW_MS),
            LLMValidation(is_routine=False, confidence=0.8, reason="r", validated_at_ms=_NOW_MS),
            None,  # soft fail → errors
        ]

        async def _seq(routine, history, *, now_ms):
            return outcomes.pop(0)

        validator.validate.side_effect = _seq

        candidates = [_routine("a", count=4), _routine("b", count=4), _routine("c", count=4)]
        disco, _ = _disco_with_validator(
            candidates=candidates,
            validator=validator,
        )
        with caplog.at_level(logging.INFO, logger="loguru.bridge"):
            await disco._validate_uncached_routines(now_ms=_NOW_MS)
        text = "\n".join(rec.message for rec in caplog.records)
        assert "validated 3 candidates" in text
        assert "1 accepted" in text
        assert "1 rejected" in text
        assert "1 errors" in text
    finally:
        logger.remove(handler_id)


@pytest.mark.asyncio
async def test_validator_respects_custom_min_occurrences():
    """The pre-filter follows whatever min_occurrences_to_surface is set."""
    routine_store = MagicMock()
    candidates = [_routine(f"r{n}", count=n) for n in (3, 4, 5, 6, 7)]
    routine_store.candidates.return_value = candidates
    memory_store = MagicMock()
    memory_store.read_history_since.return_value = ""
    validator = AsyncMock()
    validator.validate.return_value = LLMValidation(
        is_routine=True,
        confidence=0.8,
        reason="ok",
        validated_at_ms=_NOW_MS,
    )
    disco = TaskDiscoverer(
        memory_store=memory_store,
        pending_store=MagicMock(),
        dispatcher=MagicMock(),
        provider=MagicMock(),
        model="x",
        routine_store=routine_store,
        routine_validator=validator,
        min_occurrences_to_surface=5,  # custom threshold
        now_fn=lambda: _NOW,
    )
    await disco._validate_uncached_routines(now_ms=_NOW_MS)
    validated_ids = sorted(call.args[0].id for call in validator.validate.await_args_list)
    assert validated_ids == ["r5", "r6", "r7"]


# ── set_llm_validation persistence roundtrip ─────────────────────────


def test_routine_store_set_llm_validation_persists(tmp_path):
    from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore

    store = RoutineStore(tmp_path / "routines.json")
    r = _routine("x")
    store.merge([r], now_ms=_NOW_MS)
    verdict = LLMValidation(
        is_routine=True,
        confidence=0.78,
        reason="3 hits",
        validated_at_ms=_NOW_MS,
    )
    assert store.set_llm_validation("x", verdict) is True
    # Reload from disk
    fresh = RoutineStore(tmp_path / "routines.json").get("x")
    assert fresh is not None
    assert fresh.llm_validation == verdict


def test_routine_store_set_llm_validation_returns_false_for_missing(tmp_path):
    from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore

    store = RoutineStore(tmp_path / "routines.json")
    verdict = LLMValidation(
        is_routine=True,
        confidence=0.5,
        reason="r",
        validated_at_ms=_NOW_MS,
    )
    assert store.set_llm_validation("never-existed", verdict) is False


# ── deadline parsing + overdue annotation ────────────────────────────────


def _raw_option(**overrides) -> dict:
    base = {
        "title": "T",
        "why": "w",
        "type": "ad_hoc",
        "exec_kind": "reply",
        "exec_payload": {"prompt": "p"},
    }
    base.update(overrides)
    return base


def _opt(title: str, deadline: str = "") -> TaskOption:
    return TaskOption(
        id="opt_x",
        title=title,
        why="w",
        type="ad_hoc",
        exec_kind="reply",
        deadline=deadline,
    )


def test_raw_to_option_keeps_valid_iso_deadline():
    opt = TaskDiscoverer._raw_to_option(_raw_option(deadline="2026-06-03"), now_ms=_NOW_MS, seen_ids=set())
    assert opt is not None
    assert opt.deadline == "2026-06-03"


@pytest.mark.parametrize("bad", ["下周", "6/3", "2026-13-40", "soon", "2026/06/03"])
def test_raw_to_option_drops_invalid_deadline(bad):
    opt = TaskDiscoverer._raw_to_option(_raw_option(deadline=bad), now_ms=_NOW_MS, seen_ids=set())
    assert opt is not None
    assert opt.deadline == ""


def test_raw_to_option_missing_deadline_defaults_empty():
    opt = TaskDiscoverer._raw_to_option(_raw_option(), now_ms=_NOW_MS, seen_ids=set())
    assert opt is not None
    assert opt.deadline == ""


def test_annotate_overdue_prefixes_and_floats_front():
    # _NOW = 2026-05-14
    out = TaskDiscoverer._annotate_overdue([_opt("on time", ""), _opt("late", "2026-05-01")], _NOW)
    assert out[0].title == "⚠️ 逾期 5/1 late"
    assert out[1].title == "on time"


def test_annotate_overdue_sorts_overdue_earliest_first():
    out = TaskDiscoverer._annotate_overdue([_opt("a", "2026-05-10"), _opt("b", "2026-05-02")], _NOW)
    assert [o.title for o in out] == ["⚠️ 逾期 5/2 b", "⚠️ 逾期 5/10 a"]


def test_annotate_overdue_leaves_future_and_undated_untouched():
    out = TaskDiscoverer._annotate_overdue([_opt("future", "2026-06-01"), _opt("none", "")], _NOW)
    assert [o.title for o in out] == ["future", "none"]
    assert all(not o.title.startswith("⚠️") for o in out)


def test_annotate_overdue_today_is_not_overdue():
    out = TaskDiscoverer._annotate_overdue([_opt("due today", "2026-05-14")], _NOW)
    assert out[0].title == "due today"
