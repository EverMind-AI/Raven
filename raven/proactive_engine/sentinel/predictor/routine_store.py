"""RoutineStore — persistence for detected user routines.

RoutineLearner is stateless: every tick it re-derives candidate routines
from HISTORY.md. That works for raw detection but loses anything the user
has decided about a routine — confirmations, dismissals, cooldowns.

RoutineStore remembers those decisions across ticks and processes:

- ``merge(learned, now_ms)`` reconciles freshly-learned candidates with
  persisted state. If the user previously confirmed a routine, we keep
  ``status="active"`` and refresh stats. If the user dismissed one, we
  honor the dismissal during the cooldown window.
- ``upgrade(id, ts_ms)`` records a candidate → active confirmation
  (called by ActionExecutor when a discovery menu's
  ``exec_kind="routine_confirm"`` option is picked).
- ``dismiss(id, ts_ms)`` records "user said no thanks" — status=retired
  with a stamp so we don't re-surface it for ``cooldown_days`` (default
  60).
- ``get(id)`` / ``all_routines()`` for read access.

Backed by ``~/.raven/sentinel/routines.json`` via JsonStateStore
(fcntl + atomic rename). Sibling to pending_decisions.json + state.json
so all sentinel persistence shares the same locking model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from raven.proactive_engine.sentinel.feedback.persistence import JsonStateStore
from raven.proactive_engine.sentinel.types import LLMValidation, Routine

# How long a dismissal suppresses re-surfacing in the discovery menu.
DEFAULT_DISMISS_COOLDOWN_DAYS = 60


class RoutineStore:
    """Fcntl-locked store for detected + user-curated routines."""

    _STATE_KEY = "routines"

    def __init__(
        self,
        path: Path,
        *,
        dismiss_cooldown_days: int = DEFAULT_DISMISS_COOLDOWN_DAYS,
    ) -> None:
        self._store = JsonStateStore(path)
        self.dismiss_cooldown_days = dismiss_cooldown_days

    # ------------------------------------------------------------------
    # Reads

    def get(self, routine_id: str) -> Routine | None:
        for r in self._load_routines():
            if r.id == routine_id:
                return r
        return None

    def all_routines(self) -> list[Routine]:
        return self._load_routines()

    def active(self) -> list[Routine]:
        return [r for r in self._load_routines() if r.status == "active"]

    def candidates(self) -> list[Routine]:
        return [r for r in self._load_routines() if r.status == "candidate"]

    # ------------------------------------------------------------------
    # Writes

    def merge(self, learned: list[Routine], *, now_ms: int) -> list[Routine]:
        """Reconcile freshly-learned candidates with persisted state.

        Rules:
        - Persisted ``active``/``paused``/``retired`` state always wins;
          we just refresh stats (occurrence_count / last_triggered /
          weight / keywords / pattern) from the new derivation.
        - A persisted retired routine inside its cooldown window stays
          retired and is excluded from the returned list (Discoverer
          shouldn't surface it).
        - A persisted retired routine past its cooldown window is
          revived as ``candidate`` with fresh stats — user gets another
          chance to confirm.
        - Routines that no longer appear in ``learned`` keep their
          persisted state but don't get fresh stats. They survive in
          the file so that confirmation history isn't lost on a quiet
          period.

        Returns the post-merge view (excludes retired-within-cooldown).
        """
        existing = {r.id: r for r in self._load_routines()}
        learned_by_id = {r.id: r for r in learned}

        cooldown_ms = self.dismiss_cooldown_days * 24 * 60 * 60 * 1000
        merged: list[Routine] = []

        for rid, learner_view in learned_by_id.items():
            persisted = existing.get(rid)
            if persisted is None:
                merged.append(learner_view)
                continue

            if persisted.status == "retired":
                age = now_ms - (persisted.dismissed_at_ms or 0)
                if age < cooldown_ms:
                    # Still in cooldown — exclude from merged view but
                    # keep in store so future merges still know.
                    continue
                # Cooldown expired — revive as candidate with fresh stats.
                persisted.status = "candidate"
                persisted.dismissed_at_ms = None

            persisted.pattern = learner_view.pattern
            persisted.keywords = learner_view.keywords
            persisted.day_of_week = learner_view.day_of_week
            persisted.time_slot = learner_view.time_slot
            persisted.occurrence_count = learner_view.occurrence_count
            persisted.last_triggered = learner_view.last_triggered
            # Unconditional assign: weight=0.0 is a legitimate decay
            # outcome; ``or`` would silently keep stale weight and mask
            # real signal decay.
            persisted.weight = learner_view.weight
            # description/semantic_group come from RoutineAggregator on a
            # different cadence — preserve them across learner refreshes.
            merged.append(persisted)

        # Carry over persisted routines absent from this batch.
        # active/paused stay; retired-within-cooldown stays in store but
        # excluded from view; retired-past-cooldown is dropped — next
        # learning pass will re-derive it if the pattern is real.
        for rid, persisted in existing.items():
            if rid in learned_by_id:
                continue
            if persisted.status == "retired":
                age = now_ms - (persisted.dismissed_at_ms or 0)
                if age < cooldown_ms:
                    continue
                continue
            merged.append(persisted)

        # Persist including retired-within-cooldown so we remember the
        # dismissal even if the routine vanishes from the learner.
        self._save_full_set(merged, existing, now_ms=now_ms)
        return merged

    def upgrade(
        self,
        routine_id: str,
        *,
        confirmed_at_ms: int,
        new_status: str = "active",
    ) -> bool:
        """Record a user confirmation. Returns True on success, False if
        the routine isn't in the store."""

        hit = {"updated": False}

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            routines = state.get(self._STATE_KEY, [])
            for raw in routines:
                if raw.get("id") != routine_id:
                    continue
                raw["status"] = new_status
                raw["user_confirmed"] = True
                raw["user_confirmed_at_ms"] = confirmed_at_ms
                raw["dismissed_at_ms"] = None
                hit["updated"] = True
                break
            state[self._STATE_KEY] = routines
            return state

        self._store.update(_mutate)
        if hit["updated"]:
            logger.info(
                "RoutineStore: upgraded routine '{}' to {}",
                routine_id, new_status,
            )
        return hit["updated"]

    def dismiss(self, routine_id: str, *, dismissed_at_ms: int) -> bool:
        """Record a user dismissal — status=retired with a cooldown
        timestamp. Returns True if the routine existed."""

        hit = {"updated": False}

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            routines = state.get(self._STATE_KEY, [])
            for raw in routines:
                if raw.get("id") != routine_id:
                    continue
                raw["status"] = "retired"
                raw["user_confirmed"] = False
                raw["dismissed_at_ms"] = dismissed_at_ms
                hit["updated"] = True
                break
            state[self._STATE_KEY] = routines
            return state

        self._store.update(_mutate)
        if hit["updated"]:
            logger.info("RoutineStore: dismissed routine '{}'", routine_id)
        return hit["updated"]

    def set_llm_validation(
        self,
        routine_id: str,
        validation: LLMValidation,
    ) -> bool:
        """Persist a Stage 1 RoutineValidator verdict on the named routine.
        Returns True if the routine existed and was updated."""

        hit = {"updated": False}

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            routines = state.get(self._STATE_KEY, [])
            for raw in routines:
                if raw.get("id") != routine_id:
                    continue
                raw["llm_validation"] = _llm_validation_to_raw(validation)
                hit["updated"] = True
                break
            state[self._STATE_KEY] = routines
            return state

        self._store.update(_mutate)
        return hit["updated"]

    def upsert_description(
        self,
        routine_id: str,
        *,
        description: str,
        semantic_group: str | None = None,
    ) -> bool:
        """Set a routine's human-friendly description / semantic group
        (called by RoutineAggregator). Returns True if updated."""

        hit = {"updated": False}

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            routines = state.get(self._STATE_KEY, [])
            for raw in routines:
                if raw.get("id") != routine_id:
                    continue
                raw["description"] = description
                if semantic_group is not None:
                    raw["semantic_group"] = semantic_group
                hit["updated"] = True
                break
            state[self._STATE_KEY] = routines
            return state

        self._store.update(_mutate)
        return hit["updated"]

    # ------------------------------------------------------------------
    # Internals

    def _load_routines(self) -> list[Routine]:
        raw_state = self._store.load()
        out: list[Routine] = []
        for raw in raw_state.get(self._STATE_KEY, []):
            try:
                out.append(_raw_to_routine(raw))
            except Exception:
                continue
        return out

    def _save_full_set(
        self,
        merged: list[Routine],
        existing: dict[str, Routine],
        *,
        now_ms: int,
    ) -> None:
        """Persist `merged` plus any retired-within-cooldown routines
        that ``merge`` excluded from the returned view. Drops
        retired-past-cooldown entries (their absence from `merged`
        signals revival/expiry)."""

        cooldown_ms = self.dismiss_cooldown_days * 24 * 60 * 60 * 1000
        merged_ids = {r.id for r in merged}
        full: list[Routine] = list(merged)
        for rid, r in existing.items():
            if rid in merged_ids:
                continue
            if r.status != "retired":
                continue
            age = now_ms - (r.dismissed_at_ms or 0)
            if age >= cooldown_ms:
                continue  # cooldown expired — drop
            full.append(r)

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            state[self._STATE_KEY] = [_routine_to_raw(r) for r in full]
            return state

        self._store.update(_mutate)


# ── (de)serialization ────────────────────────────────────────────────


def _routine_to_raw(r: Routine) -> dict[str, Any]:
    return {
        "id": r.id,
        "pattern": r.pattern,
        "keywords": list(r.keywords),
        "day_of_week": r.day_of_week,
        "time_slot": list(r.time_slot) if r.time_slot is not None else None,
        "status": r.status,
        "occurrence_count": r.occurrence_count,
        "last_triggered": r.last_triggered,
        "user_confirmed": r.user_confirmed,
        "weight": r.weight,
        "description": r.description,
        "semantic_group": r.semantic_group,
        "user_confirmed_at_ms": r.user_confirmed_at_ms,
        "dismissed_at_ms": r.dismissed_at_ms,
        "llm_validation": _llm_validation_to_raw(r.llm_validation),
    }


def _raw_to_routine(raw: dict[str, Any]) -> Routine:
    time_slot_raw = raw.get("time_slot")
    if time_slot_raw is not None and len(time_slot_raw) == 2:
        time_slot: tuple[int, int] | None = (
            int(time_slot_raw[0]), int(time_slot_raw[1])
        )
    else:
        time_slot = None
    return Routine(
        id=raw["id"],
        pattern=raw.get("pattern", ""),
        keywords=list(raw.get("keywords") or []),
        day_of_week=raw.get("day_of_week"),
        time_slot=time_slot,
        status=raw.get("status", "candidate"),
        occurrence_count=raw.get("occurrence_count", 0),
        last_triggered=raw.get("last_triggered"),
        user_confirmed=raw.get("user_confirmed", False),
        weight=raw.get("weight", 0.0),
        description=raw.get("description"),
        semantic_group=raw.get("semantic_group"),
        user_confirmed_at_ms=raw.get("user_confirmed_at_ms"),
        dismissed_at_ms=raw.get("dismissed_at_ms"),
        llm_validation=_raw_to_llm_validation(raw.get("llm_validation")),
    )


def _llm_validation_to_raw(v: LLMValidation | None) -> dict[str, Any] | None:
    if v is None:
        return None
    return {
        "is_routine": v.is_routine,
        "confidence": v.confidence,
        "reason": v.reason,
        "validated_at_ms": v.validated_at_ms,
    }


def _raw_to_llm_validation(raw: Any) -> LLMValidation | None:
    if not isinstance(raw, dict):
        return None
    try:
        return LLMValidation(
            is_routine=bool(raw["is_routine"]),
            confidence=float(raw.get("confidence", 0.0)),
            reason=str(raw.get("reason", "")),
            validated_at_ms=int(raw.get("validated_at_ms", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


__all__ = ["RoutineStore", "DEFAULT_DISMISS_COOLDOWN_DAYS"]
