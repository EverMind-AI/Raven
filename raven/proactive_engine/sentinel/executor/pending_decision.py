"""PendingDecisionStore — fcntl-locked persistence for discovery menus
awaiting a user pick.

A TaskDiscoverer run produces a ``PendingDecision`` (one menu, 3-4
options) and ``put`` s it here. When the user replies, DecisionRouter
calls ``get_recent`` to find a still-live menu on the same (channel,
to). On match, ``mark_consumed`` records the pick.

Backed by ``~/.raven/sentinel/pending_decisions.json`` via the same
``JsonStateStore`` abstraction NudgePolicy uses — fcntl-advisory lock so
REPL + gateway processes don't clobber each other.

Lifecycle:
    put              — write a new decision; supersedes any prior live
                       decision on the same (channel, to) so the user
                       only sees one menu at a time.
    get_recent       — return the most-recent NON-expired and NON-consumed
                       decision for a given (channel, to), or None.
    mark_consumed    — record which option_id the user picked.
    sweep_expired    — drop decisions past TTL; called opportunistically
                       on every put/get to keep the file from growing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from raven.proactive_engine.sentinel.feedback.persistence import JsonStateStore
from raven.proactive_engine.sentinel.types import PendingDecision, TaskOption


class PendingDecisionStore:
    """Fcntl-locked store for menu decisions awaiting user pick."""

    _STATE_KEY = "decisions"

    def __init__(self, path: Path) -> None:
        self._store = JsonStateStore(path)

    # ------------------------------------------------------------------
    # Public API

    def put(self, decision: PendingDecision) -> list[str]:
        """Persist a new pending decision. Any prior live (un-consumed,
        un-expired) decision on the same (channel, to) is dropped —
        the user only ever picks from the freshest menu.

        Returns a list of decision_ids of superseded decisions that
        were in ``awaiting_confirm`` state — the caller should notify
        the user about those to avoid silent loss of their pending
        pick. Decisions that were superseded but not yet picked
        (still showing fresh options) are not returned because there's
        nothing user-facing to recover."""

        result: dict[str, list[str]] = {"superseded_awaiting": []}

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            decisions = state.get(self._STATE_KEY, [])
            now_ms = decision.created_at_ms
            superseded = 0
            superseded_awaiting: list[str] = []
            kept: list[dict[str, Any]] = []
            for raw in decisions:
                try:
                    existing = self._raw_to_decision(raw)
                except Exception:
                    # malformed entry — drop it silently rather than
                    # propagate corruption forward
                    continue
                if existing.is_expired(now_ms):
                    continue
                same_addr = existing.channel == decision.channel and existing.to == decision.to
                if same_addr and not existing.consumed:
                    superseded += 1
                    if existing.awaiting_confirm:
                        # User had picked + was waiting for yes/no —
                        # losing this without notice is the silent-
                        # data-loss case we surface back to the caller.
                        superseded_awaiting.append(existing.decision_id)
                    # drop — superseded by the new decision
                    continue
                kept.append(self._decision_to_raw(existing))
            kept.append(self._decision_to_raw(decision))
            state[self._STATE_KEY] = kept
            if superseded:
                if superseded_awaiting:
                    logger.warning(
                        "PendingDecisionStore: superseded {} prior decision(s)"
                        " for ({}, {}) including {} in awaiting_confirm — "
                        "caller should notify user",
                        superseded,
                        decision.channel,
                        decision.to,
                        len(superseded_awaiting),
                    )
                else:
                    logger.info(
                        "PendingDecisionStore: superseded {} prior decision(s) for ({}, {})",
                        superseded,
                        decision.channel,
                        decision.to,
                    )
            result["superseded_awaiting"] = superseded_awaiting
            return state

        self._store.update(_mutate)
        return result["superseded_awaiting"]

    def get_recent(self, channel: str, to: str, *, now_ms: int) -> PendingDecision | None:
        """Return the most-recent still-live (un-consumed, un-expired)
        decision for this (channel, to), or None."""
        raw_state = self._store.load()
        candidates: list[PendingDecision] = []
        for raw in raw_state.get(self._STATE_KEY, []):
            try:
                d = self._raw_to_decision(raw)
            except Exception:
                continue
            if d.channel != channel or d.to != to:
                continue
            if d.consumed:
                continue
            if d.is_expired(now_ms):
                continue
            candidates.append(d)
        if not candidates:
            return None
        candidates.sort(key=lambda d: d.created_at_ms, reverse=True)
        return candidates[0]

    def mark_consumed(
        self,
        decision_id: str,
        picked_option_id: str | None,
        *,
        consumed_at_ms: int,
        require_pending: bool = False,
    ) -> bool:
        """Mark the decision as consumed. Returns True if the decision
        existed and was updated; False if it was missing or already
        consumed (idempotent loser case). Also clears ``awaiting_confirm``
        so the two-leg state machine ends cleanly.

        When ``require_pending=True``, refuse to consume if the decision
        is already in ``awaiting_confirm`` — this guards skip / dismiss
        handlers against a concurrent process arming awaiting_confirm
        between the router's read and the handler's write. Pick handlers
        leave the default (False) since they legitimately move
        pending → consumed without going through awaiting_confirm."""

        hit = {"updated": False}

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            decisions = state.get(self._STATE_KEY, [])
            new_decisions: list[dict[str, Any]] = []
            for raw in decisions:
                try:
                    d = self._raw_to_decision(raw)
                except Exception:
                    continue
                if d.decision_id == decision_id and not d.consumed and not (require_pending and d.awaiting_confirm):
                    d.consumed = True
                    d.picked_option_id = picked_option_id
                    d.consumed_at_ms = consumed_at_ms
                    d.awaiting_confirm = False
                    hit["updated"] = True
                new_decisions.append(self._decision_to_raw(d))
            state[self._STATE_KEY] = new_decisions
            return state

        self._store.update(_mutate)
        return hit["updated"]

    # Outcomes for mark_awaiting_confirm — a single bool collapses three
    # distinct cases (missing / already consumed / already awaiting) into
    # one False, which causes consumer code to silently misroute on
    # concurrent access. Callers can branch on the named result.
    AWAIT_OK = "ok"  # transitioned pending → awaiting_confirm
    AWAIT_NOT_FOUND = "not_found"  # decision_id not in store
    AWAIT_CONSUMED = "consumed"  # already consumed (dismissed/picked-and-done)
    AWAIT_ALREADY = "already_awaiting"  # someone else already armed it

    def mark_awaiting_confirm(
        self,
        decision_id: str,
        *,
        picked_option_id: str,
        picked_at_ms: int,
    ) -> str:
        """Park the decision in AWAITING_CONFIRM (user picked an option,
        we sent the yes/no prompt, now waiting for their second-leg
        reply).

        Returns one of ``AWAIT_*`` — callers should branch on the
        outcome instead of treating non-OK as "fall through":

          - ``AWAIT_OK``: state moved pending → awaiting_confirm
          - ``AWAIT_NOT_FOUND``: ``decision_id`` not present
          - ``AWAIT_CONSUMED``: already consumed, do not re-arm
          - ``AWAIT_ALREADY``: already in awaiting_confirm — likely a
            concurrent leg we'd race; the consumer should treat the
            existing menu as authoritative."""

        result = {"outcome": self.AWAIT_NOT_FOUND}

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            decisions = state.get(self._STATE_KEY, [])
            new_decisions: list[dict[str, Any]] = []
            for raw in decisions:
                try:
                    d = self._raw_to_decision(raw)
                except Exception:
                    continue
                if d.decision_id == decision_id:
                    if d.consumed:
                        result["outcome"] = self.AWAIT_CONSUMED
                    elif d.awaiting_confirm:
                        result["outcome"] = self.AWAIT_ALREADY
                    else:
                        d.awaiting_confirm = True
                        d.picked_option_id = picked_option_id
                        d.picked_at_ms = picked_at_ms
                        result["outcome"] = self.AWAIT_OK
                new_decisions.append(self._decision_to_raw(d))
            state[self._STATE_KEY] = new_decisions
            return state

        self._store.update(_mutate)
        return result["outcome"]

    def cancel_confirm(
        self,
        decision_id: str,
        *,
        cancelled_at_ms: int,
    ) -> bool:
        """User said 'no' on the confirm prompt — mark fully consumed
        with picked_option_id cleared so this counts as a dismissal in
        downstream signals. Returns True if the decision was found and
        was indeed in AWAITING_CONFIRM state."""

        hit = {"updated": False}

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            decisions = state.get(self._STATE_KEY, [])
            new_decisions: list[dict[str, Any]] = []
            for raw in decisions:
                try:
                    d = self._raw_to_decision(raw)
                except Exception:
                    continue
                if d.decision_id == decision_id and not d.consumed and d.awaiting_confirm:
                    d.consumed = True
                    d.picked_option_id = None
                    d.consumed_at_ms = cancelled_at_ms
                    d.awaiting_confirm = False
                    hit["updated"] = True
                new_decisions.append(self._decision_to_raw(d))
            state[self._STATE_KEY] = new_decisions
            return state

        self._store.update(_mutate)
        return hit["updated"]

    def sweep_expired(self, *, now_ms: int) -> int:
        """Drop expired decisions. Returns the count removed. Cheap to
        call opportunistically; takes the lock so do it sparingly (e.g.
        once per discovery run, not on every read)."""

        removed_count = {"n": 0}

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            decisions = state.get(self._STATE_KEY, [])
            kept: list[dict[str, Any]] = []
            for raw in decisions:
                try:
                    d = self._raw_to_decision(raw)
                except Exception:
                    # malformed — drop
                    removed_count["n"] += 1
                    continue
                if d.is_expired(now_ms) and not d.consumed:
                    removed_count["n"] += 1
                    continue
                kept.append(raw)
            state[self._STATE_KEY] = kept
            return state

        self._store.update(_mutate)
        return removed_count["n"]

    def all_active(self, *, now_ms: int) -> list[PendingDecision]:
        """Diagnostic helper — return every still-live decision (any
        channel/to). Used by ``raven sentinel decisions`` and tests."""
        raw_state = self._store.load()
        out: list[PendingDecision] = []
        for raw in raw_state.get(self._STATE_KEY, []):
            try:
                d = self._raw_to_decision(raw)
            except Exception:
                continue
            if d.consumed or d.is_expired(now_ms):
                continue
            out.append(d)
        return out

    def all_decisions(
        self,
        *,
        include_consumed: bool = False,
        now_ms: int | None = None,
    ) -> list[PendingDecision]:
        """Return every decision in the store. Stable public surface
        for inspector / admin tools that need to see consumed history
        too — distinct from ``all_active`` which only returns live
        decisions.

        - ``include_consumed=False`` (default): same as ``all_active``
          but without the expiry filter (so already-expired-but-not-
          yet-swept decisions also appear).
        - ``include_consumed=True``: returns every decision regardless
          of state.

        Malformed entries are silently dropped (same defensive behavior
        as ``all_active``)."""
        raw_state = self._store.load()
        out: list[PendingDecision] = []
        for raw in raw_state.get(self._STATE_KEY, []):
            try:
                d = self._raw_to_decision(raw)
            except Exception:
                continue
            if not include_consumed and d.consumed:
                continue
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # Serialization

    @staticmethod
    def _decision_to_raw(d: PendingDecision) -> dict[str, Any]:
        return {
            "decision_id": d.decision_id,
            "channel": d.channel,
            "to": d.to,
            "created_at_ms": d.created_at_ms,
            "ttl_min": d.ttl_min,
            "consumed": d.consumed,
            "picked_option_id": d.picked_option_id,
            "consumed_at_ms": d.consumed_at_ms,
            "awaiting_confirm": d.awaiting_confirm,
            "picked_at_ms": d.picked_at_ms,
            "options": [
                {
                    "id": o.id,
                    "title": o.title,
                    "why": o.why,
                    "type": o.type,
                    "exec_kind": o.exec_kind,
                    "exec_payload": o.exec_payload,
                    "source": o.source,
                    "priority": o.priority,
                    "created_at_ms": o.created_at_ms,
                }
                for o in d.options
            ],
        }

    @staticmethod
    def _raw_to_decision(raw: dict[str, Any]) -> PendingDecision:
        options = [
            TaskOption(
                id=o["id"],
                title=o["title"],
                why=o.get("why", ""),
                type=o.get("type", "ad_hoc"),
                exec_kind=o.get("exec_kind", "reply"),
                exec_payload=o.get("exec_payload") or {},
                source=o.get("source", "history"),
                priority=o.get("priority", "medium"),
                created_at_ms=o.get("created_at_ms", 0),
            )
            for o in raw.get("options", [])
        ]
        return PendingDecision(
            decision_id=raw["decision_id"],
            channel=raw["channel"],
            to=raw["to"],
            created_at_ms=raw.get("created_at_ms", 0),
            ttl_min=raw.get("ttl_min", 60),
            options=options,
            consumed=raw.get("consumed", False),
            picked_option_id=raw.get("picked_option_id"),
            consumed_at_ms=raw.get("consumed_at_ms"),
            awaiting_confirm=raw.get("awaiting_confirm", False),
            picked_at_ms=raw.get("picked_at_ms"),
        )


__all__ = ["PendingDecisionStore"]
