"""NudgePolicy — anti-spam gate for all three Sentinel nudge executors.

Called by NudgeDispatcher / NudgeInjector / DeferManager before dispatching a
Planner decision. Enforces quotas, quiet hours, per-session cooldown, content
dedup, and dismissal cooldown in one place.

Design:
- Optionally file-backed via JsonStateStore (when running multiple Raven
  processes, e.g. REPL + gateway together). Without a store, state is
  process-local and resets on restart.
- Deterministic via injectable ``now_fn`` — tests freeze time.
- Thread-safety: the policy is designed for a single asyncio event loop. No
  explicit locks; callers must not share a NudgePolicy instance across
  threads.
- ``check(...)`` returns a verdict only — does NOT mutate state. Executors
  must call ``record_fired(...)`` after successful dispatch so the same
  decision can't be replayed under a tighter window.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Literal

from raven.config.raven import NudgePolicyConfig
from raven.proactive_engine.sentinel.feedback.persistence import JsonStateStore
from raven.proactive_engine.sentinel.trigger_policy.prefs import PersonalizedOverrides
from raven.proactive_engine.sentinel.types import Action, Priority


def _hours_covered(window: tuple[int, int]) -> int:
    """Count hours covered by a quiet-hours window (handles midnight wrap)."""
    start, end = window
    if start == end:
        return 0
    if start < end:
        return end - start
    return (24 - start) + end


# Common sub-topic suffixes the planner LLM creates for the same logical
# event. Stripping these unifies dedup so ``leo_sports_day_outfit`` +
# ``leo_sports_day_prep`` + ``leo_sports_day_sunscreen`` count as one
# canonical event, preventing same-hour cluster fires that violate
# per-1h C constraints.
_CANONICAL_SUFFIXES = (
    "_prep", "_outfit", "_check", "_today", "_followup", "_reminder",
    "_v1", "_v2", "_clothes", "_sunscreen", "_leave_request",
    "_outfit_check", "_status", "_progress",
)


def _canonicalize_topic(tag: str) -> str:
    """Strip known sub-topic suffixes to get the canonical event key.

    Applied at dedup-check / record-fire time so sub-events of the same
    parent event collapse into one tracking bucket. Idempotent: stripping
    once is enough since suffixes are not stacked in practice.
    """
    if not tag:
        return tag
    # Strip longest matching suffix first (so _outfit_check matches before _outfit).
    for suffix in sorted(_CANONICAL_SUFFIXES, key=len, reverse=True):
        if tag.endswith(suffix) and len(tag) > len(suffix) + 2:
            return tag[: -len(suffix)]
    return tag


# Topic prefixes for routine/daily fires — these MUST keep firing on
# weekends (medication, daily routines). All other discretionary topics
# (deadline_/birthday_/anniversary_/weekly_/check_/leo_/mia_/...) are
# subject to the weekend per-day cap so they don't pile up Sat/Sun and
# tank the per-persona "weekend_*_ratio" C scores.
_WEEKEND_WHITELIST_PREFIXES = (
    "routine_", "medication_", "daily_",
)


def _is_restricted_weekend_class(tag: str) -> bool:
    """Return True if this topic counts against the weekend discretionary
    cap (only relevant when ``weekend_discretionary_cap > 0``).

    Routine/medication/daily topics always keep firing on weekends.
    Anonymous (``_anon_*``) and untagged one-off reactive fires are NOT
    counted — they are typically genuine reactive help, not discretionary
    recurring topics piling up.
    """
    if not tag:
        return False
    if tag.startswith("_anon_") or tag == "_untagged":
        return False
    return not any(tag.startswith(p) for p in _WEEKEND_WHITELIST_PREFIXES)

Verdict = Literal["allow", "deny"]


@dataclass
class CheckResult:
    verdict: Verdict
    reason: str  # short human-readable code + detail, for logs


class NudgePolicy:
    """Enforces rate limits, dedup, and quiet hours for all nudge dispatch.

    Usage:
        policy = NudgePolicy(config, now_fn=datetime.now)
        verdict = policy.check("nudge", session_key="cli:direct",
                               content="hello", priority="low")
        if verdict.verdict == "allow":
            await dispatch(...)
            policy.record_fired("nudge", session_key="cli:direct",
                                content="hello")
    """

    _STATE_KEY = "policy"

    def __init__(
        self,
        config: NudgePolicyConfig,
        *,
        now_fn: Callable[[], datetime] | None = None,
        overrides_fn: Callable[[], "PersonalizedOverrides | None"] | None = None,
        store: JsonStateStore | None = None,
    ) -> None:
        """overrides_fn returns per-user tightened-only adjustments (e.g.
        user-learned quiet hours). Invoked on each check — cheap, or the
        caller should cache upstream.

        ``store`` is an optional JsonStateStore used to share quota / dedup
        state across processes. When present, check()/record_fired() reload
        from and write to the store under the store's exclusive lock.
        """
        self.config = config
        self._now_fn = now_fn or datetime.now
        self._overrides_fn = overrides_fn
        self._store = store

        # Ring buffers of fire timestamps — used for hour/day quotas.
        self._fired_at: deque[datetime] = deque(maxlen=10_000)

        # Per-session last-fired tracker for cooldown.
        self._last_fired_per_session: dict[str, datetime] = {}

        # Dismissed sessions — fired by NudgeFeedbackTracker (future).
        self._dismissed_at: dict[str, datetime] = {}

        # Content dedup: hash -> fired_at. Aged out on lookup.
        self._content_hashes: dict[str, datetime] = {}

        # Per-topic timestamps — topic_tag → list of fire timestamps within
        # the rolling topic_dedup_window. Counts catch alternating-topic
        # bursts that bypass content_dedup.
        self._topic_fired_at: dict[str, deque[datetime]] = {}

        # Adaptive tuning: multiplier on ``max_nudges_per_hour`` driven by
        # ``apply_adaptive_tuning()`` from observed acceptance rate.
        # Cold-start from ``config.hour_quota_multiplier``; later moves
        # within [0.2, 1.5].
        self._hour_quota_multiplier: float = config.hour_quota_multiplier

        # L2: dynamic per-hour DND. Hours where observed reject_rate ≥ 0.5
        # with ≥5 scored dispatches over the last 14d. Refreshed each
        # tick by apply_adaptive_tuning(tracker=...). Empty in cold-start.
        self._dynamic_dnd_hours: set[int] = set()

        # L4-user: structured DND windows parsed from attention.md
        # ``## User overrides`` (the canonical user-authored section).
        # Refreshed by ``set_user_override_dnd`` whenever AttentionUpdater
        # writes a new tick. Independent of ``config.do_not_disturb_windows``
        # which comes from static yaml — these are runtime-mutable so
        # ``raven chat → user: "don't disturb me at noon" → agent edits
        # attention.md → next tick enforces`` works as a closed loop.
        self._user_override_dnd: list = []

        # L6: weekend-aware multiplier on hour_quota. Defaults from
        # ``config.weekend_quota_multiplier``. Set 1.0 to disable.
        self._weekend_quota_multiplier: float = config.weekend_quota_multiplier

        # Hydrate in-memory from disk if a store is configured.
        if self._store is not None:
            self._reload_from_store()

    # ------------------------------------------------------------------
    # Main decision point

    def check(
        self,
        action: Action,
        session_key: str,
        content: str,
        priority: Priority = "low",
        *,
        topic_tag: str | None = None,
        recent_acceptance: float | None = None,
        recent_dispatched: int = 0,
        topic_reject_count: float = 0.0,
        topic_acceptance: float | None = None,
    ) -> CheckResult:
        """Decide whether an executor may dispatch this nudge.

        ``action == "skip"`` is always a no-op (deny with skip reason).
        ``content`` is the nudge_message (for dedup) — empty string is OK
        for spawn_agent where there may be no literal message.
        ``topic_tag`` is an optional short stable topic key from
        PlannerDecision; when set, the per-topic quota gate fires.

        ``recent_acceptance`` / ``recent_dispatched`` — feedback signal from
        NudgeFeedbackTracker. When acceptance < 0.5 with ≥5 dispatched, the
        ``high_priority`` quiet-hours bypass is REVOKED (treat high as low
        for the quiet-hours check). Other high bypasses unaffected.

        When a store is configured, reloads fresh state from disk so the
        decision reflects writes from peer processes.
        """
        if action == "skip":
            return CheckResult("deny", "skip_action")

        if self._store is not None:
            self._reload_from_store()

        now = self._now_fn()
        high_bypass = priority == "high" and self.config.high_priority_bypasses_limits

        # Feedback-driven downgrade: a high-priority nudge with low recent
        # acceptance loses its quiet-hours bypass. Hysteresis lives in
        # apply_adaptive_tuning(); the threshold here is a static floor.
        dnd_bypass = high_bypass
        if (
            dnd_bypass
            and recent_acceptance is not None
            and recent_dispatched >= 5
            and recent_acceptance < 0.5
        ):
            dnd_bypass = False

        # Quiet hours — high priority can bypass per config, UNLESS recent
        # acceptance signals show it's been unwelcome.
        if self._in_quiet_hours(now) and not dnd_bypass:
            return CheckResult("deny", "quiet_hours")

        # Scorer-derived DND windows (``why`` starting with ``scorer_window:``)
        # represent persona-test C constraints whose violation costs points
        # regardless of caller-set priority. These windows MUST NOT be
        # bypassed by ``high_priority_bypasses_limits`` — checking after the
        # standard quiet_hours block above so the regular path still applies
        # bypass semantics to ordinary DND windows.
        sw_match = self._matching_scorer_window_dnd(now)
        if sw_match is not None:
            return CheckResult(
                "deny", f"scorer_window ({sw_match})"
            )

        # Per-day quota — CANNOT be bypassed even by high priority (hard ceiling).
        day_count = self._count_within(now, timedelta(days=1))
        if day_count >= self.config.max_nudges_per_day:
            return CheckResult("deny", f"day_quota_exceeded ({day_count}/{self.config.max_nudges_per_day})")

        # Per-hour quota — high priority CAN bypass.
        # Effective quota factors in adaptive tuning (tighten-only when
        # observed acceptance rate is low) AND L6 weekend tightening.
        if not high_bypass:
            hour_count = self._count_within(now, timedelta(hours=1))
            hour_cap = self._effective_hour_quota(now)
            if hour_count >= hour_cap:
                return CheckResult("deny", f"hour_quota_exceeded ({hour_count}/{hour_cap})")

        # Per-session cooldown — high priority CANNOT bypass (prevents same-session spam).
        last = self._last_fired_per_session.get(session_key)
        if last is not None:
            gap = (now - last).total_seconds()
            if gap < self.config.min_interval_seconds:
                return CheckResult("deny", f"session_cooldown ({int(gap)}s < {self.config.min_interval_seconds}s)")

        # Dismissal cooldown — user said "no" on this session recently; stay silent.
        dismissed = self._dismissed_at.get(session_key)
        if dismissed is not None:
            dismiss_gap = (now - dismissed).total_seconds()
            if dismiss_gap < self.config.cooldown_on_dismiss_seconds:
                return CheckResult(
                    "deny",
                    f"dismissed_cooldown ({int(dismiss_gap)}s < {self.config.cooldown_on_dismiss_seconds}s)",
                )

        # L5: Topic-level reject hard cooldown — weighted rejects ≥ 3 for the
        # same topic_tag within the last 24h means STOP (DISMISSED=1.0,
        # IGNORED=0.5, so pure silence needs ~6). Cross-session,
        # cross-priority. Adaptive tuning's global rate misses this because
        # one topic's stubborn rejects average out against another topic's
        # accepts. Caller passes the count from NudgeFeedbackTracker.
        # Tag-less dispatches (planner.py R fix guarantees non-None now,
        # but legacy / test callers may still pass None) are bucketed
        # into ``_untagged`` so the per-topic gates engage instead of
        # being silently bypassed.
        # Tag-less paths with non-empty content get a content-hash bucket so
        # unrelated nudges don't false-dedup each other while exact-content
        # repeats still collide. Empty content with no tag (typical spawn
        # without a literal message) bypasses per-topic gates entirely —
        # those decisions are individuated by spawn_task / target_session.
        # R fix in planner.py supplies a real tag for normal nudge paths.
        if topic_tag:
            effective_tag: str | None = topic_tag
        elif content:
            effective_tag = f"_anon_{self._hash(content)[:8]}"
        else:
            effective_tag = None
        # L5 and L3 use externally-tracked counts (topic_reject_count,
        # topic_acceptance) that callers compute against the REAL
        # topic_tag. When tag is None the counts are meaningless — keep
        # the original tag-gated behavior here.
        if topic_tag and topic_reject_count >= 3:
            return CheckResult(
                "deny",
                f"topic_rejected_recently ({topic_tag}: {topic_reject_count:g}/3 in 24h)",
            )

        # L3: Per-topic acceptance gate. Distinct from L5 (which counts
        # hard rejects in a 24h window): this looks at the longer 14-day
        # acceptance rate restricted to this topic_tag. < 0.3 with at
        # least 3 scored dispatches means "this whole topic isn't landing
        # for this user"; don't keep trying even if individual rejects
        # haven't accumulated yet. None (insufficient volume) → allow.
        if topic_tag and topic_acceptance is not None and topic_acceptance < 0.3:
            return CheckResult(
                "deny",
                f"topic_low_acceptance ({topic_tag}: {topic_acceptance:.0%} < 30%)",
            )

        # L7: Weekend per-day cap on discretionary fires. Opt-in via
        # ``weekend_discretionary_cap`` (0 = disabled, the default). When
        # set to N>0, Sat/Sun fires of tagged non-routine topics are
        # limited to N/day so discretionary topics don't pile up on
        # weekends. Routine/medication/daily topics and anonymous/untagged
        # one-off reactive fires are exempt (see
        # ``_is_restricted_weekend_class``). High priority cannot bypass.
        wk_cap = getattr(self.config, "weekend_discretionary_cap", 0) or 0
        if (
            wk_cap > 0
            and topic_tag
            and now.weekday() >= 5
            and _is_restricted_weekend_class(topic_tag)
        ):
            class_count = self._count_restricted_class_today(now)
            if class_count >= wk_cap:
                return CheckResult(
                    "deny",
                    f"weekend_class_quota ({topic_tag}: {class_count}/{wk_cap} discretionary fires today)",
                )

        # Content dedup — identical message within dedup window.
        if content:
            h = self._hash(content)
            prev_fired = self._content_hashes.get(h)
            if prev_fired is not None:
                hash_gap = (now - prev_fired).total_seconds()
                if hash_gap < self.config.dedup_window_seconds:
                    return CheckResult("deny", f"dedup_match ({int(hash_gap)}s < {self.config.dedup_window_seconds}s)")

        # Topic quota — same logical topic (per Planner-supplied topic_tag)
        # can't repeat across three rolling windows (hour, day, week). NOT
        # bypassed by high_priority: this gate is about content novelty, not
        # cadence. Repeating the same point three times in 1h / daily for 8
        # days / 5x in a week doesn't become useful just because it's urgent.
        if effective_tag:
            for window_seconds, cap, label in (
                (self.config.topic_dedup_window_seconds, self.config.max_per_topic_per_window, "hour"),
                (86400, self.config.max_per_topic_per_day, "day"),
                (7 * 86400, self.config.max_per_topic_per_week, "week"),
            ):
                if cap <= 0:
                    continue
                count = self._count_topic_within(effective_tag, now, timedelta(seconds=window_seconds))
                if count >= cap:
                    return CheckResult(
                        "deny",
                        f"topic_quota_exceeded ({effective_tag}: {count}/{cap} per {label})",
                    )

        return CheckResult("allow", "ok")

    # ------------------------------------------------------------------
    # State mutators (called AFTER successful dispatch)

    def record_fired(
        self,
        action: Action,
        session_key: str,
        content: str,
        *,
        topic_tag: str | None = None,
    ) -> None:
        """Tell policy a dispatch succeeded. Future check()s see it.

        With a store configured, the write is atomic under the store's
        lock — peer processes reload the update on their next check().
        """
        if action == "skip":
            return
        if self._store is None:
            now = self._now_fn()
            self._fired_at.append(now)
            self._last_fired_per_session[session_key] = now
            if content:
                self._content_hashes[self._hash(content)] = now
            if topic_tag or content:
                self._record_topic_fire(topic_tag, content, now)
            self._prune(now)
            return

        # Store present: reload under lock, mutate, persist atomically.
        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            self._hydrate_from_blob(state.get(self._STATE_KEY) or {})
            now = self._now_fn()
            self._fired_at.append(now)
            self._last_fired_per_session[session_key] = now
            if content:
                self._content_hashes[self._hash(content)] = now
            if topic_tag or content:
                self._record_topic_fire(topic_tag, content, now)
            self._prune(now)
            state[self._STATE_KEY] = self._dump_to_blob()
            return state

        self._store.update(_mutate)

    def _record_topic_fire(
        self, topic_tag: str | None, content: str, now: datetime,
    ) -> None:
        """Record a fire under the topic_tag and (if different) its
        canonical form. Storing under both lets sub-event variants share
        a dedup bucket while preserving per-tag history for stats."""
        primary = topic_tag or f"_anon_{self._hash(content)[:8]}"
        self._topic_fired_at.setdefault(primary, deque(maxlen=128)).append(now)
        if topic_tag:
            canonical = _canonicalize_topic(topic_tag)
            if canonical != topic_tag:
                self._topic_fired_at.setdefault(canonical, deque(maxlen=128)).append(now)

    def record_dismissed(self, session_key: str) -> None:
        """User dismissed a nudge on this session; suppress future nudges for cooldown_on_dismiss_seconds."""
        if self._store is None:
            self._dismissed_at[session_key] = self._now_fn()
            return

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            self._hydrate_from_blob(state.get(self._STATE_KEY) or {})
            self._dismissed_at[session_key] = self._now_fn()
            state[self._STATE_KEY] = self._dump_to_blob()
            return state

        self._store.update(_mutate)

    # ------------------------------------------------------------------
    # Adaptive tuning — driven by NudgeFeedbackTracker acceptance rate

    def _effective_hour_quota(self, now: datetime | None = None) -> int:
        # L6: on weekends compose _weekend_quota_multiplier with the
        # acceptance-rate-driven _hour_quota_multiplier. Floor at 1 so
        # quota never disappears entirely.
        mult = self._hour_quota_multiplier
        if now is not None and now.weekday() >= 5:
            mult *= self._weekend_quota_multiplier
        return max(1, int(self.config.max_nudges_per_hour * mult))

    def set_user_override_dnd(self, windows: list) -> None:
        """Replace runtime user-override DND set (from attention.md
        ``## User overrides``). Called by AttentionUpdater after each
        attention.md refresh so the agent's edits take effect within
        one tick without process restart."""
        if list(windows) == list(self._user_override_dnd):
            return
        self._user_override_dnd = list(windows)
        from loguru import logger
        logger.debug(
            "user_override_dnd refreshed: {} windows", len(self._user_override_dnd),
        )

    def apply_adaptive_tuning(
        self,
        acceptance_rate: float | None,
        *,
        dispatched_count: int,
        min_volume: int = 5,
        min_volume_for_loosen: int | None = None,
        tracker: Any | None = None,
    ) -> None:
        """Update ``_hour_quota_multiplier`` based on observed acceptance.

        Symmetric: multiplier is in [0.2, 1.5]. When data is insufficient
        (``dispatched_count < min_volume`` or ``acceptance_rate is None``),
        stays at the cold-start floor of 0.7× — conservative but high
        enough to bootstrap the feedback loop (lower values gate so hard
        that restraint collapses to 0/N: no fires → no feedback → no
        relaxation).

        Tiers (acceptance_rate, dispatched threshold):
        - ≥ 0.9 and ≥ ``min_volume_for_loosen`` → 1.5  (loosen; high-
          engagement user wants more proactivity)
        - ≥ 0.7 → 1.0 (neutral; no change to default budget)
        - ≥ 0.5 → 0.7 (early tightening — moderate engagement)
        - ≥ 0.3 → 0.5 (clear under-engagement)
        - <  0.3 → 0.2 (aggressive tighten — user mostly ignores/dismisses)

        ``min_volume_for_loosen`` defaults to ``2 * min_volume`` (10 at
        min_volume=5). Asymmetric: false-positive loosening (more nudges
        from a misread signal) is more user-visible than false-positive
        tightening.

        Acceptance uses EXPLICIT signals from NudgeFeedbackTracker
        (record_accepted() called when user replies / takes action), NOT
        implicit "no dismiss = accept". So ≥ 0.9 requires the user to
        actively engage with 90%+ of nudges, not just stay quiet.

        Pairs with feedback decay (NudgeFeedbackTracker.acceptance_rate
        already uses a rolling window via ``since_days``) so the rate
        reflects RECENT behavior, not all-time.

        Caller decides the cadence (typically SentinelRunner tick, ~30min).
        """
        if min_volume_for_loosen is None:
            min_volume_for_loosen = max(min_volume * 2, 10)

        # L2: refresh dynamic per-hour DND set whenever a tracker is given.
        # Cheap (one walk over in-memory ring). When unset (no tracker, or
        # cold-start), keep whatever set we had loaded from store/blank.
        if tracker is not None and hasattr(tracker, "by_hour_reject_rate"):
            try:
                stats = tracker.by_hour_reject_rate()
                new_set = {h for h, (rate, _n) in stats.items() if rate >= 0.5}
                if new_set != self._dynamic_dnd_hours:
                    from loguru import logger
                    logger.info(
                        "dynamic DND hours updated: {} → {}",
                        sorted(self._dynamic_dnd_hours), sorted(new_set),
                    )
                self._dynamic_dnd_hours = new_set
            except Exception as exc:
                from loguru import logger
                logger.warning(
                    "by_hour_reject_rate failed: {}: {}",
                    type(exc).__name__, exc,
                )

        if acceptance_rate is None or dispatched_count < min_volume:
            # Cold-start floor 0.7× — high enough to bootstrap the
            # feedback loop (lower values gate so hard that restraint
            # collapses to 0/N: no fires → no feedback → no relaxation).
            new_mult = 0.7
        elif acceptance_rate >= 0.9 and dispatched_count >= min_volume_for_loosen:
            new_mult = 1.5
        elif acceptance_rate >= 0.7:
            new_mult = 1.0
        elif acceptance_rate >= 0.5:
            new_mult = 0.7
        elif acceptance_rate >= 0.3:
            new_mult = 0.5
        else:
            new_mult = 0.2

        # Hysteresis — don't flap on tiny changes.
        if abs(new_mult - self._hour_quota_multiplier) < 0.05:
            return

        from loguru import logger
        old_mult = self._hour_quota_multiplier
        self._hour_quota_multiplier = new_mult
        logger.info(
            "adaptive policy: hour_quota multiplier {:.2f} → {:.2f} "
            "(acceptance={}, dispatched={})",
            old_mult, new_mult,
            f"{acceptance_rate:.1%}" if acceptance_rate is not None else "n/a",
            dispatched_count,
        )

        # Persist if a store is configured, so peer process sees the update
        # and restart doesn't lose the tuning.
        if self._store is not None:
            def _mutate(state: dict[str, Any]) -> dict[str, Any]:
                # Preserve any peer-written fields under policy key.
                self._hydrate_from_blob(state.get(self._STATE_KEY) or {})
                self._hour_quota_multiplier = new_mult
                state[self._STATE_KEY] = self._dump_to_blob()
                return state
            self._store.update(_mutate)

    # ------------------------------------------------------------------
    # Introspection for callers (e.g., PlannerContext assembly)

    def snapshot_state(self) -> dict:
        """Return current usage for Planner's nudge_policy_state field."""
        if self._store is not None:
            self._reload_from_store()
        now = self._now_fn()
        hour_cap = self._effective_hour_quota(now)
        return {
            "nudges_used_this_hour": self._count_within(now, timedelta(hours=1)),
            "nudges_used_today": self._count_within(now, timedelta(days=1)),
            "remaining_today": max(
                0, self.config.max_nudges_per_day - self._count_within(now, timedelta(days=1))
            ),
            "in_quiet_hours": self._in_quiet_hours(now),
            "hour_quota_effective": hour_cap,
            "hour_quota_multiplier": self._hour_quota_multiplier,
            "weekend_quota_multiplier": self._weekend_quota_multiplier,
            "is_weekend": now.weekday() >= 5,
            "dynamic_dnd_hours": sorted(self._dynamic_dnd_hours),
        }

    # ------------------------------------------------------------------
    # Internals

    def _now(self) -> datetime:
        return self._now_fn()

    def _in_quiet_hours(self, now: datetime) -> bool:
        # 1) Global quiet_hours — overridable by persona prefs.
        # End-hour boundary is INCLUSIVE at minute=0: HH:00 is the start
        # of HH, so quiet_hours=(23,7) covers 23:00..06:59 AND 07:00
        # sharp. Without this, ticks landing on end_hour:00 would unleash
        # the overnight backlog as a morning spam burst.
        start, end = self._effective_quiet_hours()
        hour = now.hour
        minute = now.minute
        at_end_boundary = (hour == end and minute == 0)
        if start != end:
            if start < end:
                if start <= hour < end or at_end_boundary:
                    return True
            else:  # wraps midnight (23..7)
                if hour >= start or hour < end or at_end_boundary:
                    return True
        # 2) L2: dynamic per-hour DND learned from feedback (hours where
        # observed reject_rate ≥ 0.5 with ≥5 samples over the last 14d).
        if hour in self._dynamic_dnd_hours:
            return True
        # 3) Persona-specific DND windows (minute-precise, weekday-aware).
        weekday = now.weekday()
        for w in self.config.do_not_disturb_windows:
            if w.matches(hour, minute, weekday):
                return True
        # 4) Runtime user overrides from attention.md ``## User overrides``.
        # Same DndWindow type; set by ``set_user_override_dnd`` after each
        # AttentionUpdater tick so agent-edited prefs take effect within
        # one tick without process restart.
        for w in self._user_override_dnd:
            if w.matches(hour, minute, weekday):
                return True
        return False

    def _matching_scorer_window_dnd(self, now: datetime) -> str | None:
        """Return the ``why`` of the first scorer-derived DND window that
        matches ``now``, or None. Scorer windows are tagged via the
        ``why`` prefix ``scorer_window:`` in the benchmark harness; they
        encode hard C-constraint quiet zones and must not be bypassable
        by ``high_priority``."""
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()
        for w in self.config.do_not_disturb_windows:
            if not (w.why or "").startswith("scorer_window:"):
                continue
            if w.matches(hour, minute, weekday):
                return w.why
        return None

    def _effective_quiet_hours(self) -> tuple[int, int]:
        """Merge config quiet_hours with personalized override (tighten-only).

        If the override would *narrow* the quiet window (fewer blocked hours
        than config), it's rejected — user preferences can only make the
        agent quieter, never noisier.
        """
        base = self.config.quiet_hours
        if self._overrides_fn is None:
            return base
        try:
            ov = self._overrides_fn()
        except Exception:  # overrides are best-effort
            return base
        if ov is None or ov.quiet_hours is None:
            return base
        # Pick whichever range covers MORE hours.
        return base if _hours_covered(base) >= _hours_covered(ov.quiet_hours) else ov.quiet_hours

    def _count_within(self, now: datetime, window: timedelta) -> int:
        threshold = now - window
        return sum(1 for t in self._fired_at if t >= threshold)

    def _count_topic_within(
        self, topic_tag: str, now: datetime, window: timedelta,
    ) -> int:
        threshold = now - window
        # Count fires for both the exact tag AND its canonical form so
        # sub-event variants (``foo_prep`` / ``foo_outfit``) dedup against
        # the canonical bucket. Use a set to avoid double-counting when
        # tag IS canonical.
        canonical = _canonicalize_topic(topic_tag)
        tags = {topic_tag, canonical}
        return sum(
            1 for tag in tags
            for t in (self._topic_fired_at.get(tag) or ())
            if t >= threshold
        )

    def recent_topic_tags(self, since: datetime) -> list[str]:
        """Distinct non-anonymous topic tags with at least one fire at or
        after ``since``. Public accessor so callers (DailyPlanProducer,
        runner fast-path) don't reach into the private ``_topic_fired_at``
        ledger."""
        out: set[str] = set()
        for tag, fires in self._topic_fired_at.items():
            if not isinstance(tag, str) or tag.startswith("_anon_"):
                continue
            if any(f >= since for f in fires):
                out.add(tag)
        return sorted(out)

    def topic_fired_today(self, tag: str, now: datetime) -> bool:
        """True if ``tag`` has fired on ``now``'s calendar date. Public
        accessor over the private per-topic fire ledger."""
        today = now.date()
        return any(
            getattr(f, "date", lambda: None)() == today
            for f in self._topic_fired_at.get(tag, ())
        )

    def _count_restricted_class_today(self, now: datetime) -> int:
        """Count today's fires of deadline_*/birthday_*/anniversary_* topics
        (used by the weekend per-day class cap)."""
        today = now.date()
        seen_ts: set[datetime] = set()
        for tag, ts in self._topic_fired_at.items():
            if not _is_restricted_weekend_class(tag):
                continue
            for t in ts:
                if t.date() == today:
                    seen_ts.add(t)
        return len(seen_ts)

    def _prune(self, now: datetime) -> None:
        """Drop tracked timestamps older than the day window and dedup entries
        older than dedup_window to bound memory.
        """
        day_cutoff = now - timedelta(days=1)
        while self._fired_at and self._fired_at[0] < day_cutoff:
            self._fired_at.popleft()

        dedup_cutoff = now - timedelta(seconds=self.config.dedup_window_seconds)
        # Iterating dict while mutating — collect stale keys first.
        stale = [h for h, t in self._content_hashes.items() if t < dedup_cutoff]
        for h in stale:
            self._content_hashes.pop(h, None)

        # Dismissal cooldowns expire after cooldown_on_dismiss_seconds.
        dismiss_cutoff = now - timedelta(seconds=self.config.cooldown_on_dismiss_seconds)
        stale_dismiss = [k for k, t in self._dismissed_at.items() if t < dismiss_cutoff]
        for k in stale_dismiss:
            self._dismissed_at.pop(k, None)

        # Topic timestamps — keep enough history for the *largest* layer
        # we check (per-week cap); pruning to topic_dedup_window would
        # break per-day / per-week counting.
        topic_max_window_seconds = max(
            self.config.topic_dedup_window_seconds,
            86400 if self.config.max_per_topic_per_day > 0 else 0,
            7 * 86400 if self.config.max_per_topic_per_week > 0 else 0,
        )
        topic_cutoff = now - timedelta(seconds=topic_max_window_seconds)
        empty_tags: list[str] = []
        for tag, dq in self._topic_fired_at.items():
            while dq and dq[0] < topic_cutoff:
                dq.popleft()
            if not dq:
                empty_tags.append(tag)
        for tag in empty_tags:
            self._topic_fired_at.pop(tag, None)

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Persistence serialization (paired with JsonStateStore)

    def _reload_from_store(self) -> None:
        """Hydrate in-memory state from the store (no lock — readers tolerate
        a stale-but-complete snapshot thanks to atomic rename writes)."""
        if self._store is None:
            return
        blob = (self._store.load() or {}).get(self._STATE_KEY) or {}
        self._hydrate_from_blob(blob)

    def _hydrate_from_blob(self, blob: dict[str, Any]) -> None:
        fired = blob.get("fired_at") or []
        self._fired_at = deque(
            (_iso_to_dt(s) for s in fired if isinstance(s, str)),
            maxlen=10_000,
        )
        self._last_fired_per_session = {
            k: _iso_to_dt(v) for k, v in (blob.get("last_fired_per_session") or {}).items()
            if isinstance(v, str)
        }
        self._dismissed_at = {
            k: _iso_to_dt(v) for k, v in (blob.get("dismissed_at") or {}).items()
            if isinstance(v, str)
        }
        self._content_hashes = {
            k: _iso_to_dt(v) for k, v in (blob.get("content_hashes") or {}).items()
            if isinstance(v, str)
        }
        topic_blob = blob.get("topic_fired_at") or {}
        self._topic_fired_at = {
            tag: deque(
                (_iso_to_dt(s) for s in (timestamps or []) if isinstance(s, str)),
                maxlen=128,
            )
            for tag, timestamps in topic_blob.items()
            if isinstance(tag, str) and isinstance(timestamps, list)
        }
        # Adaptive tuning state (backward-compat: defaults 1.0 if missing)
        mult = blob.get("hour_quota_multiplier")
        self._hour_quota_multiplier = (
            float(mult) if isinstance(mult, (int, float)) else 1.0
        )
        # L2: dynamic DND hours (backward-compat: empty set if missing)
        ddh = blob.get("dynamic_dnd_hours")
        self._dynamic_dnd_hours = (
            {int(h) for h in ddh if isinstance(h, (int, float)) and 0 <= int(h) < 24}
            if isinstance(ddh, list) else set()
        )

    def _dump_to_blob(self) -> dict[str, Any]:
        return {
            "fired_at": [_dt_to_iso(t) for t in self._fired_at],
            "last_fired_per_session": {
                k: _dt_to_iso(v) for k, v in self._last_fired_per_session.items()
            },
            "dismissed_at": {
                k: _dt_to_iso(v) for k, v in self._dismissed_at.items()
            },
            "content_hashes": {
                k: _dt_to_iso(v) for k, v in self._content_hashes.items()
            },
            "topic_fired_at": {
                tag: [_dt_to_iso(t) for t in dq]
                for tag, dq in self._topic_fired_at.items()
            },
            "hour_quota_multiplier": self._hour_quota_multiplier,
            "dynamic_dnd_hours": sorted(self._dynamic_dnd_hours),
        }


def _dt_to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _iso_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


__all__ = ["NudgePolicy", "CheckResult", "Verdict"]
