"""ContextAssembler — builds PlannerContext for each Sentinel tick.

Pulls from existing Raven infrastructure (MemoryStore, SessionManager,
RoutineLearner, NudgePolicy) and packages the state into the shape
ProactivePlanner.decide() expects. No LLM calls here — pure aggregation.

In production the SentinelRunner instantiates one ContextAssembler and
calls ``assemble()`` each tick.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Memory smart-loading helpers.
#
# Section parsing is line-anchored on ``^## `` so code-fenced ``## ``
# mid-line stays in the prior section body. ALWAYS_KEEP sections survive
# even if the user blocklists them — the Planner depends on them.
import re as _re  # local alias to avoid leaking into module exports
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from raven.config.raven import DEFAULT_PLANNER_ATTENTION_SECTIONS
from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.proactive_engine.sentinel.predictor.routine_learner import RoutineLearner
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy
from raven.proactive_engine.sentinel.trigger_policy.prefs import (
    PersonalizedOverrides,
    ProactivityPreferencesReader,
)
from raven.proactive_engine.sentinel.types import (
    ActiveSession,
    NudgePolicyState,
    PlannerContext,
    Routine,
)

_H2_RE = _re.compile(r"^##\s+(.+?)\s*$", _re.MULTILINE)

ALWAYS_KEEP_SECTIONS = frozenset(
    {
        "Sentinel Observations (auto)",
        "Proactivity Preferences",
        "User Information",
        "Important Notes",
    }
)


def _parse_h2_sections(raw: str) -> list[tuple[str, str]]:
    """Split MEMORY.md into (title, body_with_header) pairs in document order.

    The first entry has title ``""`` and contains everything before the first
    ``## `` header (lead-in: top-level title, free text, etc.). ``body``
    includes the ``## title`` line so the renderer can re-emit unchanged.

    Empty input → ``[("", "")]``.
    """
    if not raw:
        return [("", "")]
    matches = list(_H2_RE.finditer(raw))
    if not matches:
        return [("", raw)]
    sections: list[tuple[str, str]] = []
    # Lead-in: from start to first match
    if matches[0].start() > 0:
        sections.append(("", raw[: matches[0].start()]))
    else:
        sections.append(("", ""))
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        body_start = m.start()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections.append((title, raw[body_start:body_end]))
    return sections


def _filter_memory_md(raw: str, cfg) -> str:
    """Apply section-level allowlist + blocklist + size cap.

    ``cfg`` is a ``NudgePolicyConfig`` (memory_* fields). Default config
    (allowlist=None, blocklist=[], max_chars=0) is byte-for-byte
    passthrough.
    """
    allow = getattr(cfg, "memory_section_allowlist", None)
    block = list(getattr(cfg, "memory_section_blocklist", []) or [])
    max_chars = int(getattr(cfg, "memory_max_chars", 0) or 0)
    no_filter = allow is None and not block and max_chars <= 0
    if no_filter:
        return raw

    sections = _parse_h2_sections(raw)
    kept: list[tuple[str, str, str]] = []  # (title, body, kind)
    for title, body in sections:
        if title == "":
            kept.append((title, body, "leadin"))
            continue
        if title in ALWAYS_KEEP_SECTIONS:
            kept.append((title, body, "priority"))
            continue
        if allow is not None and title not in allow:
            continue
        if title in block:
            continue
        kept.append((title, body, "normal"))

    out = "".join(b for _, b, _ in kept)
    if max_chars > 0 and len(out) > max_chars:
        # Drop "normal" sections tail-first until we fit.
        drop_indices: list[int] = [i for i, (_, _, kind) in enumerate(kept) if kind == "normal"]
        for idx in reversed(drop_indices):
            kept.pop(idx)
            out = "".join(b for _, b, _ in kept)
            if len(out) <= max_chars:
                break
    return out


class ContextAssembler:
    """Bundles Planner inputs. Cheap enough to invoke per tick."""

    def __init__(
        self,
        *,
        memory_store: MemoryStore | None = None,
        session_manager: Any | None = None,
        routine_learner: RoutineLearner | None = None,
        nudge_policy: NudgePolicy | None = None,
        now_fn: Callable[[], datetime] | None = None,
        history_tail_lines: int = 60,
        active_session_window_seconds: int = 3600,
        user_profile: str = "",
        calendar_fn: Callable[[], list[str]] | None = None,
        prefs_reader: ProactivityPreferencesReader | None = None,
        attention_planner_sections: list[str] | None = None,
        behaviors_planner_window_days: int = 14,
        behaviors_planner_max_events: int = 100,
    ) -> None:
        self.memory_store = memory_store
        self.session_manager = session_manager
        self.routine_learner = routine_learner
        self.nudge_policy = nudge_policy
        self._now_fn = now_fn or datetime.now
        self.history_tail_lines = history_tail_lines
        self.active_session_window = active_session_window_seconds
        self.user_profile = user_profile
        self._calendar_fn = calendar_fn
        self._last_decision: Any | None = None  # populated by SentinelRunner
        # Default 6-section selection matches SentinelConfig defaults;
        # callers override per-deploy.
        self._attention_sections: list[str] = list(
            attention_planner_sections if attention_planner_sections is not None else DEFAULT_PLANNER_ATTENTION_SECTIONS
        )
        self._behaviors_window_days = behaviors_planner_window_days
        self._behaviors_max_events = behaviors_planner_max_events

        # If a memory_store is supplied without a prefs_reader, build a
        # default reader pointing at its long-term file.
        if prefs_reader is None and memory_store is not None:
            prefs_reader = ProactivityPreferencesReader(
                read_fn=memory_store.read_long_term,
            )
        self._prefs_reader = prefs_reader
        self._cached_overrides: PersonalizedOverrides = PersonalizedOverrides()

        # Wire the policy's override lookup to our per-tick cache.
        if nudge_policy is not None and nudge_policy._overrides_fn is None:
            nudge_policy._overrides_fn = lambda: self._cached_overrides

    # ------------------------------------------------------------------
    # Mutation — SentinelRunner hands us the previous tick's decision.

    def remember_last_decision(self, decision: Any | None) -> None:
        self._last_decision = decision

    # ------------------------------------------------------------------
    # Build

    def assemble(self) -> PlannerContext:
        now = self._now_fn()
        # Refresh overrides BEFORE snapshot_state() so it sees the
        # user-tightened quiet hours.
        self._refresh_prefs()
        memory_md = self._memory_md()
        history_md = self._history_tail()
        routines = self._learn_routines(history_md)
        active_sessions = self._active_sessions(now)
        nudge_state = self._nudge_state()
        calendar = self._calendar()
        fire_history = self._fire_history(now)
        attention_md = self._attention_for_planner()
        behaviors_recent = self._behaviors_for_planner(now)

        return PlannerContext(
            now=now,
            memory_md=memory_md,
            history_md_recent=history_md,
            active_sessions=active_sessions,
            routines=routines,
            calendar=calendar,
            nudge_policy_state=nudge_state,
            last_decision=self._last_decision,
            user_profile=self.user_profile,
            fire_history=fire_history,
            attention_md=attention_md,
            behaviors_recent=behaviors_recent,
        )

    def _attention_for_planner(self) -> str:
        """Read attention.md, parse, splice ``_attention_sections`` back
        into a single markdown block preserving section order. Empty
        when attention.md missing or every selected section is empty."""
        if self.memory_store is None:
            return ""
        path = self.memory_store.attention_file
        if not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        from raven.memory_engine.consolidate.attention import parse_attention

        sections = parse_attention(text)
        parts: list[str] = []
        for h2 in self._attention_sections:
            body = sections.get(h2, "").strip()
            if not body:
                continue
            parts.append(h2)
            parts.append(body)
            parts.append("")
        return "\n".join(parts).rstrip()

    def _behaviors_for_planner(self, now: datetime) -> str:
        """Read behaviors.md events within the window, render folded
        single-line block. Empty when no events qualify."""
        if self.memory_store is None:
            return ""
        path = self.memory_store.behaviors_file
        if not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        from datetime import timedelta as _td

        from raven.memory_engine.consolidate.behaviors import (
            parse_behaviors,
            render_folded_block,
            slice_after_day,
        )

        cutoff = now - _td(days=self._behaviors_window_days)
        # Bounded read: behaviors.md is append-only on disk, but the
        # Planner only consumes the last ``behaviors_window_days``. Slice
        # at the first H2 day-block ≥ cutoff before parsing so reader
        # cost stays bounded as the file grows over months/years.
        text = slice_after_day(text, cutoff.date().isoformat())
        if not text:
            return ""
        events = parse_behaviors(text)
        if not events:
            return ""
        kept = []
        for ev in events:
            try:
                ev_dt = datetime.fromisoformat(ev.day)
                hh, mm = ev.end.split(":")
                ev_end = ev_dt.replace(hour=int(hh), minute=int(mm))
            except (ValueError, IndexError):
                continue
            if ev_end < cutoff:
                continue
            kept.append(ev)
        return render_folded_block(
            kept,
            max_events=self._behaviors_max_events,
        )

    def _fire_history(self, now: datetime) -> dict:
        """Snapshot recent NudgePolicy state so Planner can self-throttle.

        Returns dict with:
          - recent_fires: last 10 fire timestamps
          - topic_counts_24h / topic_counts_7d: per-tag counts
          - recent_dismissals: last 5 dismissed sessions

        All read directly from the live NudgePolicy instance — no extra
        persistence or LLM call. If policy is None, returns empty dict.
        """
        if self.nudge_policy is None:
            return {}
        try:
            from datetime import timedelta as _td

            policy = self.nudge_policy
            # Hot-reload from store for multi-process coherence.
            if getattr(policy, "_store", None) is not None:
                try:
                    policy._reload_from_store()
                except Exception:
                    pass
            day_cutoff = now - _td(days=1)
            week_cutoff = now - _td(days=7)
            recent_fires = [t.isoformat() for t in list(policy._fired_at)[-10:]]
            topic_24h, topic_7d = {}, {}
            for tag, dq in (policy._topic_fired_at or {}).items():
                topic_24h[tag] = sum(1 for t in dq if t >= day_cutoff)
                topic_7d[tag] = sum(1 for t in dq if t >= week_cutoff)
            topic_24h = {k: v for k, v in topic_24h.items() if v > 0}
            topic_7d = {k: v for k, v in topic_7d.items() if v > 0}
            dismissed = [
                {"session_key": k, "ts": v.isoformat()} for k, v in list((policy._dismissed_at or {}).items())[-5:]
            ]
            return {
                "recent_fires": recent_fires,
                "topic_counts_24h": topic_24h,
                "topic_counts_7d": topic_7d,
                "recent_dismissals": dismissed,
            }
        except Exception as exc:
            logger.warning("ContextAssembler.fire_history failed: {}", exc)
            return {}

    # ------------------------------------------------------------------
    # Sub-assemblers (each gracefully degrades to empty if source missing)

    def _refresh_prefs(self) -> None:
        """Re-read proactivity preferences from MEMORY.md."""
        if self._prefs_reader is None:
            return
        try:
            self._cached_overrides = self._prefs_reader.read()
        except Exception as exc:
            logger.warning("ProactivityPreferencesReader.read failed: {}", exc)
            self._cached_overrides = PersonalizedOverrides()

    def _memory_md(self) -> str:
        if self.memory_store is None:
            return ""
        try:
            raw = self.memory_store.read_long_term() or ""
        except Exception as exc:
            logger.warning("ContextAssembler memory read failed: {}", exc)
            return ""
        # Smart loading: section allowlist / blocklist / size cap.
        cfg = getattr(self.nudge_policy, "config", None) if self.nudge_policy else None
        if cfg is None:
            return raw
        return _filter_memory_md(raw, cfg)

    def _history_tail(self) -> str:
        """Last N lines of HISTORY.md — recency signal without the full
        file blowing up the prompt."""
        if self.memory_store is None:
            return ""
        history_path = getattr(self.memory_store, "history_file", None)
        if history_path is None:
            return ""
        p = Path(history_path)
        if not p.exists():
            return ""
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("ContextAssembler history read failed: {}", exc)
            return ""
        return "\n".join(lines[-self.history_tail_lines :])

    def _learn_routines(self, history_md: str) -> list[Routine]:
        if self.routine_learner is None or not history_md:
            return []
        try:
            return self.routine_learner.learn(history_md)
        except Exception as exc:
            logger.warning("RoutineLearner.learn failed: {}", exc)
            return []

    def _active_sessions(self, now: datetime) -> list[ActiveSession]:
        if self.session_manager is None:
            return []
        # SessionManager exposes the in-memory cache via ``sessions``.
        sessions_dict = getattr(self.session_manager, "sessions", None)
        if not isinstance(sessions_dict, dict):
            return []

        active: list[ActiveSession] = []
        for key, sess in sessions_dict.items():
            updated_at = getattr(sess, "updated_at", None)
            if not isinstance(updated_at, datetime):
                continue
            if (now - updated_at).total_seconds() > self.active_session_window:
                continue
            last_user, last_asst = self._last_turns(sess)
            active.append(
                ActiveSession(
                    key=str(key),
                    last_active_at=updated_at,
                    last_user_message=last_user,
                    last_assistant_message=last_asst,
                )
            )
        active.sort(key=lambda s: s.last_active_at, reverse=True)
        return active

    @staticmethod
    def _last_turns(sess: Any) -> tuple[str | None, str | None]:
        """Extract the latest user and assistant message text."""
        messages = getattr(sess, "messages", None) or []
        user_msg: str | None = None
        asst_msg: str | None = None
        for m in reversed(messages):
            role = m.get("role") if isinstance(m, dict) else None
            content = m.get("content") if isinstance(m, dict) else None
            if role == "user" and user_msg is None and isinstance(content, str):
                user_msg = content
            elif role == "assistant" and asst_msg is None and isinstance(content, str):
                asst_msg = content
            if user_msg and asst_msg:
                break
        return user_msg, asst_msg

    def _nudge_state(self) -> NudgePolicyState:
        if self.nudge_policy is None:
            return NudgePolicyState()
        snap = self.nudge_policy.snapshot_state()
        return NudgePolicyState(
            nudges_used_this_hour=snap.get("nudges_used_this_hour", 0),
            in_quiet_hours=snap.get("in_quiet_hours", False),
            remaining_today=snap.get("remaining_today", 10),
            hour_quota_multiplier=float(snap.get("hour_quota_multiplier", 1.0)),
        )

    def _calendar(self) -> list[str]:
        if self._calendar_fn is None:
            return []
        try:
            items = self._calendar_fn() or []
            return [str(x) for x in items if x]
        except Exception as exc:
            logger.warning("ContextAssembler calendar source failed: {}", exc)
            return []


__all__ = ["ContextAssembler"]
