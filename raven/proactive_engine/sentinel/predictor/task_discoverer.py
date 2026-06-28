"""TaskDiscoverer — daily intent-detection batch.

Fired once per local day from SentinelRunner.tick_with_context (mirroring
SentinelMemoryWriter's same-day-guard pattern). Each run:

1. Reads memory + recent history + sentinel observations + candidate
   routines + fire_history (everything Discovery needs for grounding).
2. Calls an LLM to emit 3-4 ``TaskOption`` objects (or 0 if nothing's
   worth surfacing today).
3. Persists the result as a ``PendingDecision`` (one per channel/to,
   superseding any prior live menu).
4. Hands the menu off to ``NudgeDispatcher.dispatch_options`` which
   renders it as markdown and posts it via the spine DeliveryHub.

Default OFF (``sentinel.task_discovery_enabled`` controls the
SentinelRunner-side guard; this class is a no-op factory if you
instantiate it but never call ``run``).
"""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from raven.proactive_engine.sentinel.predictor.prompts import (
    build_discovery_prompt,
    discovery_tool_schema,
)
from raven.proactive_engine.sentinel.types import (
    PendingDecision,
    Routine,
    TaskOption,
)

if TYPE_CHECKING:
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.providers.base import LLMProvider
    from raven.proactive_engine.sentinel.predictor.context_assembler import ContextAssembler
    from raven.proactive_engine.sentinel.feedback.tracker import NudgeFeedbackTracker
    from raven.proactive_engine.sentinel.executor.dispatcher import NudgeDispatcher
    from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy
    from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
    from raven.proactive_engine.sentinel.predictor.routine_aggregator import RoutineAggregator
    from raven.proactive_engine.sentinel.predictor.routine_learner import RoutineLearner
    from raven.proactive_engine.sentinel.predictor.routine_validator import RoutineValidator
    from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore


# History lookback for the discovery prompt — long enough to capture
# overnight context, short enough to stay within the prompt budget.
DEFAULT_HISTORY_LOOKBACK_HOURS = 24


class TaskDiscoverer:
    """Daily LLM-driven generator of TaskOption candidates."""

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        pending_store: "PendingDecisionStore",
        dispatcher: "NudgeDispatcher",
        provider: "LLMProvider",
        model: str,
        context_assembler: "ContextAssembler | None" = None,
        routine_store: "RoutineStore | None" = None,
        routine_learner: "RoutineLearner | None" = None,
        routine_aggregator: "RoutineAggregator | None" = None,
        routine_validator: "RoutineValidator | None" = None,
        feedback: "NudgeFeedbackTracker | None" = None,
        policy: "NudgePolicy | None" = None,
        routine_half_life_days: int = 14,
        max_options: int = 4,
        decision_ttl_min: int = 60,
        history_lookback_hours: int = DEFAULT_HISTORY_LOOKBACK_HOURS,
        min_occurrences_to_surface: int = 3,
        validator_confidence_floor: float = 0.6,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.memory_store = memory_store
        self.pending_store = pending_store
        self.dispatcher = dispatcher
        self.provider = provider
        self.model = model
        # Spine submit, late-bound (gateway scheduler only exists once the loop
        # runs; this is built in the sync prologue). Wired via set_submit before
        # any discovery run; the supersede notice submits a SENTINEL-origin turn.
        self._submit = None
        self.context_assembler = context_assembler
        self.routine_store = routine_store
        self.routine_learner = routine_learner
        self.routine_aggregator = routine_aggregator
        self.routine_validator = routine_validator
        self.feedback = feedback
        self.policy = policy
        self.routine_half_life_days = routine_half_life_days
        self.max_options = max_options
        self.decision_ttl_min = decision_ttl_min
        self.history_lookback_hours = history_lookback_hours
        # Floor on occurrences before surfacing — one/two-shot patterns are
        # high false-positive. Tuned via
        # SentinelConfig.task_discovery_min_occurrences_to_surface.
        self.min_occurrences_to_surface = max(1, min_occurrences_to_surface)
        # Floor for validated candidates; unvalidated (llm_validation=None)
        # pass through so feature-off behavior is unchanged.
        self.validator_confidence_floor = max(0.0, min(1.0, validator_confidence_floor))
        self._now_fn = now_fn or datetime.now

    # ------------------------------------------------------------------
    # Public — run one discovery pass

    async def run(
        self, *, channel: str, to: str
    ) -> PendingDecision | None:
        """Run discovery once for the given (channel, to). Returns the
        persisted PendingDecision (and dispatches the menu) on success,
        or None if the LLM declined to emit options.

        Never raises — failures degrade to None + a warning log so the
        sentinel tick loop isn't poisoned by a bad LLM call."""
        try:
            return await self._run_inner(channel=channel, to=to)
        except Exception as exc:
            logger.exception("TaskDiscoverer.run failed: {}", exc)
            return None

    # ------------------------------------------------------------------
    # Internals

    async def _run_inner(
        self, *, channel: str, to: str
    ) -> PendingDecision | None:
        now = self._now_fn()
        now_ms = int(now.timestamp() * 1000)
        since_ms = now_ms - self.history_lookback_hours * 60 * 60_000

        memory_md = self.memory_store.read_long_term()
        history_recent = self.memory_store.read_history_since(since_ms)
        sentinel_obs = _extract_sentinel_observations_block(memory_md)
        # Refresh first so each pass sees fresh decay-weighted candidates
        # without losing user-confirmed status.
        self._refresh_routines(now_ms=now_ms)
        # Stage 1 LLM gate: validate uncached candidates. Cached verdicts
        # skip the call — cost is paid at most once per routine_id.
        await self._validate_uncached_routines(now_ms=now_ms)
        # Polish raw candidates (description + semantic_group) via LLM.
        await self._aggregate_routines()
        routines_summary = self._format_candidate_routines(now_ms)
        fire_summary = self._format_fire_history()

        messages = build_discovery_prompt(
            now=now,
            memory_md=memory_md,
            history_recent=history_recent,
            sentinel_observations_block=sentinel_obs,
            candidate_routines_summary=routines_summary,
            fire_history_summary=fire_summary,
            max_options=self.max_options,
        )

        response = await self.provider.chat_with_retry(
            messages=messages,
            tools=discovery_tool_schema(),
            model=self.model,
            tool_choice="auto",
        )

        options = self._parse_options(response, now_ms=now_ms)
        options = self._annotate_overdue(options, now)[:self.max_options]
        if not options:
            logger.info(
                "TaskDiscoverer: LLM emitted 0 options — skipping today's "
                "menu for ({}, {})",
                channel, to,
            )
            return None

        decision_id = f"dec_{secrets.token_hex(4)}"
        decision = PendingDecision(
            decision_id=decision_id,
            channel=channel,
            to=to,
            created_at_ms=now_ms,
            ttl_min=self.decision_ttl_min,
            options=options,
        )

        # Render preview now so NudgePolicy.check sees the same content
        # that will dispatch (content is used for the dedup hash).
        from raven.proactive_engine.sentinel.executor.dispatcher import render_menu_markdown
        session_key = f"{channel}:{to}"
        menu_preview = render_menu_markdown(decision)

        # NudgePolicy gate — discovery participates in the same
        # quiet_hours / quota / cooldown / dedup rules as reactive nudges.
        if self.policy is not None:
            check = self.policy.check(
                "nudge", session_key=session_key,
                content=menu_preview, priority="medium",
            )
            if check.verdict == "deny":
                logger.info(
                    "TaskDiscoverer: discovery dispatch denied by NudgePolicy"
                    " for {} — reason={!r}",
                    session_key, check.reason,
                )
                return None

        self.pending_store.sweep_expired(now_ms=now_ms)
        superseded_awaiting = self.pending_store.put(decision) or []

        try:
            await self.dispatcher.dispatch_options(decision)
        except Exception as exc:
            logger.warning(
                "TaskDiscoverer: dispatch_options failed for {}: {}: {}",
                decision_id, type(exc).__name__, exc,
            )
            # Keep the decision persisted so user can respond once channel
            # is back; TTL handles eventual cleanup. Skip the supersede
            # notice — telling the user "your pick is gone" with no menu
            # to re-pick is worse than silent supersession.
            return decision

        # Dispatch succeeded — only NOW emit the supersede notice so menu
        # and notice arrive together or not at all (no half-state where
        # notice promises a menu that never comes).
        if superseded_awaiting:
            await self._notify_superseded_awaiting(
                channel=channel, to=to,
                superseded_ids=superseded_awaiting,
            )

        # Record the fire so the next reactive tick respects the same
        # hour quota / session cooldown.
        if self.policy is not None:
            try:
                self.policy.record_fired(
                    "nudge", session_key=session_key,
                    content=menu_preview,
                )
            except Exception as exc:
                logger.warning(
                    "TaskDiscoverer: policy.record_fired failed: {}", exc,
                )

        # Feed adaptive tuning's 7-day rolling acceptance rate.
        # action="discovery_menu" distinguishes this signal downstream.
        if self.feedback is not None:
            try:
                self.feedback.record_dispatched(
                    decision.decision_id,
                    action="discovery_menu",
                    session_key=session_key,
                    priority="medium",
                    source="task_discovery",
                    details={"option_count": len(options)},
                )
            except Exception as exc:
                logger.warning(
                    "TaskDiscoverer: feedback.record_dispatched failed: {}",
                    exc,
                )

        logger.info(
            "TaskDiscoverer: dispatched menu {} with {} options to ({}, {})",
            decision_id, len(options), channel, to,
        )
        return decision

    def set_submit(self, submit) -> None:
        self._submit = submit

    async def _notify_superseded_awaiting(
        self,
        *,
        channel: str,
        to: str,
        superseded_ids: list[str],
    ) -> None:
        """Inform the user that a previously-pending pick was replaced
        by today's fresh menu. Prevents the silent-data-loss footgun
        where a user replied '/pick 2' and is mid-confirm when a new
        discovery run drops the original menu."""
        notice = (
            "ℹ️ 您之前未确认的任务建议已被今天的新菜单替换。"
            "如需继续之前的选择，请在新菜单中重新挑选。"
        )
        try:
            assert self._submit is not None
            from raven.spine import ChatType, Origin, Source, TurnRequest

            # Fire-and-forget: the notice turn's output is not read back, and the
            # reply rides emit -> hub -> outlet. Consistent with the action-reply
            # path.
            self._submit(TurnRequest(
                origin=Origin.SENTINEL,
                source=Source(
                    channel=channel, chat_id=to, sender_id="sentinel", chat_type=ChatType.DM
                ),
                text=notice,
                conversation=f"{channel}:{to}",
            ))
            logger.warning(
                "TaskDiscoverer: superseded {} awaiting-confirm "
                "decision(s) for ({}, {}); user notified",
                len(superseded_ids), channel, to,
            )
        except Exception as exc:
            logger.warning(
                "TaskDiscoverer: superseded notify failed: {}: {}",
                type(exc).__name__, exc,
            )

    # ------------------------------------------------------------------
    # Routine refresh + rendering

    def _refresh_routines(self, *, now_ms: int) -> None:
        """If a learner + store are wired up, re-derive candidates from
        HISTORY.md and merge into the persistent store. Cheap-no-op
        when either is absent (keeps the discoverer usable in test
        harnesses that don't care about routines)."""
        if self.routine_learner is None or self.routine_store is None:
            return
        try:
            history_full = ""
            if self.memory_store.history_file.exists():
                history_full = self.memory_store.history_file.read_text(
                    encoding="utf-8"
                )
            learned = self.routine_learner.learn_with_decay(
                history_full,
                half_life_days=self.routine_half_life_days,
            )
            self.routine_store.merge(learned, now_ms=now_ms)
        except Exception as exc:
            logger.warning(
                "TaskDiscoverer._refresh_routines failed: {}: {}",
                type(exc).__name__, exc,
            )

    async def _validate_uncached_routines(self, *, now_ms: int) -> None:
        """For each candidate without llm_validation, run Stage 1 validation
        and persist the verdict. Skipped silently when validator/store
        aren't wired.

        Pre-filters by ``min_occurrences_to_surface`` so we don't burn LLM
        calls on candidates the surfacing layer would drop anyway."""
        if self.routine_validator is None or self.routine_store is None:
            return
        try:
            candidates = self.routine_store.candidates()
        except Exception as exc:
            logger.warning(
                "RoutineValidator: store.candidates() failed: {}: {}",
                type(exc).__name__, exc,
            )
            return
        uncached = [
            r for r in candidates
            if r.llm_validation is None
            and r.occurrence_count >= self.min_occurrences_to_surface
        ]
        if not uncached:
            return
        # Bound history read to the learner's 60-day window — full
        # HISTORY.md (potentially MBs) would just get truncated inside
        # the validator.
        sixty_days_ms = 60 * 86400 * 1000
        history_md = ""
        try:
            history_md = self.memory_store.read_history_since(now_ms - sixty_days_ms)
        except Exception as exc:
            logger.warning("RoutineValidator: history read failed: {}", exc)
        accepted = rejected = errors = 0
        for r in uncached:
            try:
                verdict = await self.routine_validator.validate(
                    r, history_md, now_ms=now_ms,
                )
            except Exception as exc:
                errors += 1
                logger.warning(
                    "RoutineValidator.validate raised for {}: {}: {}",
                    r.id, type(exc).__name__, exc,
                )
                continue
            if verdict is None:
                # Soft failure — leave uncached so a future pass can retry.
                errors += 1
                continue
            if verdict.is_routine:
                accepted += 1
            else:
                rejected += 1
            try:
                self.routine_store.set_llm_validation(r.id, verdict)
            except Exception as exc:
                logger.warning(
                    "RoutineStore.set_llm_validation failed for {}: {}",
                    r.id, exc,
                )
        logger.info(
            "RoutineValidator: validated {} candidates → "
            "{} accepted, {} rejected, {} errors",
            len(uncached), accepted, rejected, errors,
        )

    async def _aggregate_routines(self) -> None:
        """If an aggregator is wired up, polish candidate routines'
        descriptions + semantic_group via LLM. Skipped silently when no
        aggregator is configured (test harness / staged rollout)."""
        if self.routine_aggregator is None or self.routine_store is None:
            return
        try:
            candidates = self.routine_store.candidates()
            if not candidates:
                return
            await self.routine_aggregator.aggregate(candidates)
        except Exception as exc:
            logger.warning(
                "TaskDiscoverer._aggregate_routines failed: {}: {}",
                type(exc).__name__, exc,
            )

    def _format_candidate_routines(self, now_ms: int) -> str:
        routines: list[Routine] = []
        if self.routine_store is not None:
            try:
                routines = self.routine_store.candidates()
            except Exception as exc:
                logger.warning("RoutineStore.candidates failed: {}", exc)
        if not routines:
            return ""
        # Filter below min_occurrences — <3 hits is statistical noise that
        # the LLM treats as a pattern.
        qualified = [r for r in routines if r.occurrence_count >= self.min_occurrences_to_surface]
        # Stage 1 LLM gate: drop not-a-routine and below-floor confidence.
        # Unvalidated candidates pass through so validator-off behavior
        # is unchanged.
        qualified = [r for r in qualified if _passes_llm_gate(r, self.validator_confidence_floor)]
        if not qualified:
            return ""
        qualified.sort(key=lambda r: r.weight or r.occurrence_count, reverse=True)
        lines: list[str] = []
        for r in qualified[:6]:
            descr = r.description or r.pattern
            lines.append(
                f"- {r.id}: {descr} (count={r.occurrence_count}, "
                f"weight={r.weight:.2f})"
            )
        return "\n".join(lines)

    def _format_fire_history(self) -> str:
        if self.context_assembler is None:
            return ""
        try:
            fh = self.context_assembler._fire_history(self._now_fn())
        except Exception as exc:
            logger.warning("ContextAssembler._fire_history failed: {}", exc)
            return ""
        recent = fh.get("recent_fires", [])
        if not recent:
            return ""
        # ``recent_fires`` is list[str] of ISO timestamps. Stay defensive
        # in case the producer is later enriched to list[dict].
        lines: list[str] = []
        for f in recent[:8]:
            if isinstance(f, dict):
                tag = f.get("topic_tag") or "(untagged)"
                preview = (f.get("message_preview") or "")[:50]
                lines.append(f"- topic={tag} preview={preview!r}")
            else:
                lines.append(f"- fired_at={f}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Parse LLM response

    def _parse_options(
        self,
        response: Any,
        *,
        now_ms: int,
    ) -> list[TaskOption]:
        """Pull the emit_task_options tool call out of the LLM response
        and convert each entry into a TaskOption. Returns empty list on
        any parsing failure or empty/missing call."""

        if not getattr(response, "has_tool_calls", False):
            return []
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            return []

        first = tool_calls[0]
        args = getattr(first, "arguments", None)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                logger.warning("TaskDiscoverer: tool args not JSON: {!r}", args)
                return []
        if not isinstance(args, dict):
            return []

        raw_options = args.get("options") or []
        if not isinstance(raw_options, list):
            return []

        out: list[TaskOption] = []
        seen_ids: set[str] = set()
        for raw in raw_options:
            if not isinstance(raw, dict):
                continue
            try:
                option = self._raw_to_option(raw, now_ms=now_ms,
                                              seen_ids=seen_ids)
            except Exception as exc:
                logger.warning("TaskDiscoverer: bad option {!r}: {}", raw, exc)
                continue
            if option is None:
                continue
            seen_ids.add(option.id)
            out.append(option)
        # NB: no max_options cap here — the caller truncates only AFTER
        # _annotate_overdue has floated overdue items to the front, so a
        # high-signal overdue option emitted late by the LLM is not dropped.
        return out

    @staticmethod
    def _annotate_overdue(
        options: list[TaskOption], now: datetime,
    ) -> list[TaskOption]:
        """Flag past-deadline options and float them to the front.

        Overdue items are the highest-signal nudges (a due date slipped),
        so we surface them — prefixed with a ``⚠️ 逾期 M/D`` marker and
        ordered ahead of on-time options — rather than dropping them.
        ``deadline`` is the LLM-emitted ISO date already validated in
        ``_raw_to_option``; empty means no due date.
        """
        today = now.date()
        overdue: list[TaskOption] = []
        current: list[TaskOption] = []
        for opt in options:
            try:
                dl = date.fromisoformat(opt.deadline) if opt.deadline else None
            except ValueError:
                dl = None
            if dl is not None and dl < today:
                opt.title = f"⚠️ 逾期 {dl.month}/{dl.day} {opt.title}"
                overdue.append(opt)
            else:
                current.append(opt)
        # Earliest-overdue first (ISO date string order == chronological);
        # on-time options keep their LLM-emitted order.
        overdue.sort(key=lambda o: o.deadline)
        return overdue + current

    @staticmethod
    def _raw_to_option(
        raw: dict[str, Any],
        *,
        now_ms: int,
        seen_ids: set[str],
    ) -> TaskOption | None:
        # Drop partials — every schema-required field must be present.
        for required in ("title", "why", "type", "exec_kind", "exec_payload"):
            if required not in raw:
                return None
        title = (raw.get("title") or "").strip()
        if not title:
            return None
        why = (raw.get("why") or "").strip()
        opt_type = raw.get("type", "ad_hoc")
        if opt_type not in ("routine_confirm", "ad_hoc"):
            opt_type = "ad_hoc"
        exec_kind = raw.get("exec_kind") or "reply"
        if exec_kind not in ("reply", "tool", "spawn", "routine_confirm"):
            return None
        exec_payload = raw.get("exec_payload")
        if not isinstance(exec_payload, dict):
            return None
        # routine_confirm options must reference an existing routine_id
        if exec_kind == "routine_confirm":
            if not exec_payload.get("routine_id"):
                return None
        source = raw.get("source", "history")
        if source not in ("history", "memory", "routine"):
            source = "history"
        priority = raw.get("priority", "medium")
        if priority not in ("low", "medium", "high"):
            priority = "medium"
        # Keep only parseable ISO dates; drop free-text / garbage to "".
        deadline = (raw.get("deadline") or "").strip()
        if deadline:
            try:
                date.fromisoformat(deadline)
            except ValueError:
                deadline = ""

        # Per-decision option id; uniqueness lets menu pick-by-index work.
        for _ in range(8):
            oid = f"opt_{secrets.token_hex(3)}"
            if oid not in seen_ids:
                break
        else:  # extremely unlikely collision — fall back to time-based suffix
            oid = f"opt_{int(time.time() * 1000) % 100000:05d}"

        return TaskOption(
            id=oid,
            title=title[:80],
            why=why[:160],
            type=opt_type,
            exec_kind=exec_kind,
            exec_payload=exec_payload,
            source=source,
            priority=priority,
            created_at_ms=now_ms,
            deadline=deadline,
        )


_OBSERVATIONS_BLOCK_RE = re.compile(
    r"(?ms)^## Sentinel Observations \(auto\)\s*\n"
    r"<!--\s*sentinel:auto[^>]*-->\s*\n"
    r"(.*?)"
    r"<!--\s*/sentinel:auto\s*-->"
)


def _extract_sentinel_observations_block(memory_md: str) -> str:
    """Pull the body of the ``## Sentinel Observations (auto)`` section
    out of MEMORY.md so the discovery prompt sees it as a separate
    grounded signal (rather than as part of the long-term memory blob).
    Returns "" if the section is missing."""
    if not memory_md:
        return ""
    m = _OBSERVATIONS_BLOCK_RE.search(memory_md)
    if not m:
        return ""
    return m.group(1).strip()


def _passes_llm_gate(r: Routine, confidence_floor: float) -> bool:
    """Apply the Stage 1 LLM verdict if present. Unvalidated candidates
    pass through unchanged (validator may be off or call may have failed)."""
    v = r.llm_validation
    if v is None:
        return True
    if not v.is_routine:
        return False
    return v.confidence >= confidence_floor


__all__ = [
    "TaskDiscoverer",
    "DEFAULT_HISTORY_LOOKBACK_HOURS",
]
