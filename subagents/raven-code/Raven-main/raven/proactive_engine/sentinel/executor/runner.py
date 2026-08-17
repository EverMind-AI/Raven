"""SentinelRunner — the orchestrator binding Planner + executors into a
periodically-ticking service.

Each tick:
1. ContextAssembler.assemble()       → PlannerContext
2. ProactivePlanner.decide(ctx)      → PlannerDecision
3. _route(decision)                  → appropriate executor path:
   - skip         : record tick, no dispatch
   - nudge        : NudgePolicy.check → NudgeDispatcher.dispatch
   - nudge_inject : NudgePolicy.check → NudgeInjector.queue
   - nudge_defer  : NudgePolicy.check → DeferManager.register
   - spawn_agent  : ProactiveSpawn.dispatch (its own policy check inside)
4. Record fired / dispatched to NudgePolicy + FeedbackTracker.
5. Update ContextAssembler with last decision for next tick's prompt.

Two drive modes:
- ``await runner.tick_once()``         — single synchronous tick; useful
  for benchmark adapters and unit tests.
- ``await runner.start()`` / stop()   — long-running background task
  driven by ``interval_s`` (default 300s). Uses asyncio task, not a new
  thread. Also runs DeferManager's background loop concurrently.

Degradation:
- If Planner.decide raises, runner logs and treats as skip.
- If an executor raises, runner catches, logs, and records the failure;
  tick loop continues.
- No component is required — runner works with whatever is provided
  (e.g., benchmark mode may skip DeferManager + FeedbackTracker).
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from raven.channels.manager import ChannelManager
    from raven.memory_engine.consolidate.behaviors_extractor import (
        BehaviorsExtractor,
    )
    from raven.proactive_engine.sentinel.attention_updater import AttentionUpdater
    from raven.proactive_engine.sentinel.discover_triggers import (
        DiscoverTriggerStore,
    )
    from raven.proactive_engine.sentinel.feedback.persistence import JsonStateStore
    from raven.proactive_engine.sentinel.predictor.task_discoverer import TaskDiscoverer
    from raven.proactive_engine.sentinel.types import PlannerContext
    from raven.session.manager import SessionManager

from loguru import logger

from raven.proactive_engine.sentinel.executor.defer_manager import DeferManager
from raven.proactive_engine.sentinel.executor.dispatcher import (
    ExecutionResult,
    NudgeDispatcher,
    split_session_key,
)
from raven.proactive_engine.sentinel.executor.injector import NudgeInjector
from raven.proactive_engine.sentinel.executor.spawn import ProactiveSpawn
from raven.proactive_engine.sentinel.feedback.tracker import NudgeFeedbackTracker, new_nudge_id
from raven.proactive_engine.sentinel.planner import ProactivePlanner
from raven.proactive_engine.sentinel.predictor.context_assembler import ContextAssembler
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy
from raven.proactive_engine.sentinel.types import PlannerDecision

# Per-turn session_key published by ``on_user_inbound`` so the
# ``nudge_feedback`` tool can find the current session without changing
# the AgentLoop tool-execute signature. asyncio Tasks inherit context, so
# this propagates correctly from the user-inbound hook through the ReAct
# loop into tool execution.
current_session_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sentinel_current_session_key",
    default=None,
)


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute). Falls back to (8, 0) on bad
    input — defensive default so a typo in config doesn't disable the
    whole runner."""
    try:
        h_str, m_str = value.split(":", 1)
        h = max(0, min(23, int(h_str)))
        m = max(0, min(59, int(m_str)))
        return h, m
    except Exception:
        logger.warning(
            "SentinelRunner: invalid task_discovery_time {!r}, falling back to 08:00",
            value,
        )
        return 8, 0


@dataclass
class TickOutcome:
    """Structured summary of one tick — useful for benchmark adapters + tests."""

    decision: PlannerDecision
    result: ExecutionResult | None  # None when skip or no executor fired
    nudge_id: str | None = None  # correlates tracker events
    route: str = ""  # which executor path taken
    notes: list[str] = field(default_factory=list)


class SentinelRunner:
    """Orchestrate the proactivity tick.

    Minimal required components: ``planner`` + ``assembler`` + ``policy``.
    Executors are injected; any can be None (e.g., benchmark harness with
    only the dispatcher).
    """

    # JsonStateStore sub-key for engagement persistence. Keeps the
    # NudgePolicy / NudgeInjector keys side-by-side under one state.json.
    _STATE_KEY = "engagement"

    # Max age (sim-time seconds) for a cached skip decision to short-circuit
    # the Planner on an unchanged context. Beyond this, Planner re-evaluates
    # even if signature matches — prevents sleepy personas from locking skip
    # indefinitely and starving the adaptive feedback loop.
    _FAST_PATH_SKIP_TTL_S = 3600

    def __init__(
        self,
        *,
        planner: ProactivePlanner,
        assembler: ContextAssembler,
        policy: NudgePolicy,
        dispatcher: NudgeDispatcher | None = None,
        injector: NudgeInjector | None = None,
        defer_manager: DeferManager | None = None,
        spawn: ProactiveSpawn | None = None,
        feedback: NudgeFeedbackTracker | None = None,
        attention_updater: "AttentionUpdater | None" = None,
        behaviors_extractor: "BehaviorsExtractor | None" = None,
        task_discoverer: "TaskDiscoverer | None" = None,
        task_discovery_time: str = "08:00",
        task_discovery_targets: list[tuple[str, str]] | None = None,
        session_manager: "SessionManager | None" = None,
        discover_trigger_store: "DiscoverTriggerStore | None" = None,
        interval_s: int = 1800,
        enabled: bool = True,
        now_fn: Callable[[], datetime] | None = None,
        decision_source: str = "planner_tick",
        engagement_window_seconds: int = 86400,
        deadline_outage_fallback: bool = True,
        store: "JsonStateStore | None" = None,
    ) -> None:
        self.planner = planner
        self.assembler = assembler
        self.policy = policy
        self.dispatcher = dispatcher
        self.injector = injector
        self.defer_manager = defer_manager
        # Defer fires share the exact nudge routing (sentinel:direct fan-out).
        if self.defer_manager is not None:
            self.defer_manager.set_target_resolver(self._resolve_nudge_targets)
        self.spawn = spawn
        self.feedback = feedback
        self.attention_updater = attention_updater
        self.behaviors_extractor = behaviors_extractor
        # Idle tracker — last user inbound timestamp. BehaviorsExtractor
        # tick path reads it; on_user_inbound updates it. None until the
        # first inbound, in which case extractor sees "infinite idle"
        # and the cooldown gate decides whether to run.
        self._last_inbound_ts: datetime | None = None
        # Fire-and-forget asyncio tasks kicked off by _refresh_memory_state.
        # Strong references are required — Python's loop holds tasks as
        # weakrefs, so a discarded reference can let the task be GC'd
        # before it finishes (especially the slow LLM-backed behaviors
        # extractor tick).
        self._background_tasks: set[asyncio.Task] = set()
        self.memory_writer = None
        self.task_discoverer = task_discoverer
        self.task_discovery_time = _parse_hhmm(task_discovery_time)
        # Each entry is ``(channel, chat_id)`` where ``chat_id == ""`` means
        # auto-resolve via SessionManager; ``channel == "*"`` means broadcast
        # to ChannelManager.enabled_channels. See ``_resolve_target``.
        self.task_discovery_targets = list(task_discovery_targets or [])
        # ChannelManager is late-bound via ``set_channel_manager`` because
        # gateway builds it AFTER the runner.
        self._delivery_session_manager = session_manager
        self._channel_manager: "ChannelManager | None" = None
        self._discover_trigger_store = discover_trigger_store
        self.interval_s = interval_s
        self.enabled = enabled
        self._now_fn = now_fn or datetime.now
        self._decision_source = decision_source
        self._engagement_window = engagement_window_seconds
        self._deadline_outage_fallback = deadline_outage_fallback
        # Track date-of-last memory writer attempt to ensure we run at most
        # once per local day even if multiple ticks fall in the same day.
        self._last_memory_write_date: "datetime | None" = None
        # Same shape, separate slot — we want discovery to run on a
        # day-aligned guard independent of memory-writer cooldown.
        self._last_task_discovery_date: "datetime | None" = None
        # Daily-cadence guard for FeedbackTracker cleanup; keeps the
        # JSONL log bounded so apply_adaptive_tuning's 7-day window
        # never has to skim months of stale events.
        self._last_feedback_cleanup_date: "datetime | None" = None

        self._running = False
        self._tick_task: asyncio.Task | None = None
        self._defer_task: asyncio.Task | None = None
        self._trigger_task: asyncio.Task | None = None

        # Engagement tracking: per-session list of recently-dispatched
        # nudges (nudge_id, dispatched_at). When a user-originated inbound
        # arrives on a tracked session within engagement_window_seconds
        # (default 24h — proactive nudges are notification-style, users
        # often reply hours later after seeing the alert), the most recent
        # nudge moves to ``_awaiting_llm_feedback`` for the main LLM to
        # classify via the ``nudge_feedback`` tool — or is marked dismissed
        # immediately if the inbound starts with ``/dismiss`` (deterministic
        # fast path).
        self._pending_engagement: dict[str, list[tuple[str, datetime]]] = {}
        # Nudges popped from ``_pending_engagement`` but awaiting the
        # main LLM's intent classification. Drained either by the
        # ``nudge_feedback`` tool (LLM classified) or by
        # ``finalize_pending_feedback`` (after_send: turn ended without
        # a classification → record neutral).
        self._awaiting_llm_feedback: dict[str, list[tuple[str, datetime]]] = {}

        # Optional cross-process persistence. The longrun eval and
        # production gateway both split the Sentinel pipeline across
        # multiple subprocesses (``sentinel ticks --live`` dispatches;
        # ``agent --message`` handles the reply). Without a shared store
        # ``_pending_engagement`` / ``_awaiting_llm_feedback`` are
        # in-memory dicts that don't survive subprocess boundaries — so
        # ``on_user_inbound`` always sees an empty queue and the new
        # LLM-feedback path is inert. The store fixes that by sharing
        # the engagement state through ``state.json`` (same JsonStateStore
        # NudgePolicy / NudgeInjector use).
        self._store = store
        if self._store is not None:
            self._hydrate_engagement_from_store()

    # ------------------------------------------------------------------
    # Lifecycle

    async def start(self) -> None:
        if not self.enabled:
            logger.info("SentinelRunner disabled — not starting")
            return
        if self._running:
            logger.warning("SentinelRunner already running")
            return
        self._running = True
        # Startup discovery-trigger drain lives in the gateway, not here —
        # it must run after channels.start_all() has built each adapter's
        # client to avoid a "client not initialized" race.
        self._tick_task = asyncio.create_task(self._run_tick_loop())
        # Discover-trigger drain runs on its own fast cadence — it's
        # event-driven (CLI write), not time-driven, so it has no
        # business sharing the LLM tick's 10-minute sleep. Without this,
        # ``raven sentinel discover-now`` violates its own name and
        # waits up to ``interval_s`` before firing.
        if self._discover_trigger_store is not None:
            self._trigger_task = asyncio.create_task(self._run_trigger_loop())
        if self.defer_manager is not None:
            self._defer_task = asyncio.create_task(self.defer_manager.run_forever())
        logger.info("SentinelRunner started (tick every {}s)", self.interval_s)

    async def consume_pending_triggers(self) -> None:
        """Public entry for the gateway's delayed startup drain. Idempotent;
        safe to call any time. See ``_maybe_consume_discover_triggers``."""
        await self._maybe_consume_discover_triggers()

    async def stop(self) -> None:
        self._running = False
        if self.defer_manager is not None:
            self.defer_manager.stop()
        for t in (self._tick_task, self._defer_task, self._trigger_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._tick_task = None
        self._defer_task = None
        self._trigger_task = None
        logger.info("SentinelRunner stopped")

    _TRIGGER_POLL_INTERVAL_S = 2

    async def _run_trigger_loop(self) -> None:
        """Fast-poll CLI-queued discover triggers. Decoupled from the
        LLM tick so ``raven sentinel discover-now`` actually fires
        within seconds. ``consume_all`` short-circuits when the file is
        empty, so cost is fcntl + tiny read every poll — negligible."""
        while self._running:
            try:
                await asyncio.sleep(self._TRIGGER_POLL_INTERVAL_S)
                if not self._running:
                    break
                await self._maybe_consume_discover_triggers()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("SentinelRunner trigger drain error: {}", exc)

    async def _run_tick_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if not self._running:
                    break
                await self.tick_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("SentinelRunner tick error: {}", exc)

    # ------------------------------------------------------------------
    # Single tick — benchmark-friendly

    async def tick_once(self) -> TickOutcome:
        """One end-to-end tick. Safe to call synchronously by benchmarks.

        Assembles context via ContextAssembler then delegates to
        ``tick_with_context``. Never raises — failures become skip outcomes.
        """
        try:
            ctx = self.assembler.assemble()
        except Exception as exc:
            logger.exception("ContextAssembler failed: {}", exc)
            return TickOutcome(
                decision=PlannerDecision(action="skip", reason=f"assembler_error: {type(exc).__name__}"),
                result=None,
                route="error",
            )
        return await self.tick_with_context(ctx)

    def _maybe_retune_policy(self) -> None:
        """Feed observed acceptance rate into NudgePolicy's adaptive tuner.

        Also passes the tracker so L2's dynamic per-hour DND set can be
        refreshed in the same call (cheap walk over the in-memory ring).
        """
        if self.feedback is None:
            return
        try:
            # Sweep long-silent nudges into IGNORED first so the retune and
            # downstream gates see fresh time-based negatives, not only
            # explicit dismissals.
            self.feedback.sweep_ignored(
                window_seconds=getattr(
                    self.policy.config,
                    "ignore_window_seconds",
                    21600,
                ),
            )
            counts = self.feedback.counts(since_days=7)
            rate = self.feedback.acceptance_rate(since_days=7)
            self.policy.apply_adaptive_tuning(
                rate,
                dispatched_count=counts.get("dispatched", 0),
                tracker=self.feedback,
            )
        except Exception as exc:
            logger.warning("adaptive tuning skipped: {}: {}", type(exc).__name__, exc)

    def _policy_feedback_kwargs(
        self,
        topic_tag: str | None = None,
    ) -> dict[str, Any]:
        """Snapshot acceptance signal for NudgePolicy.check.

        Used by every _route_* before calling policy.check so the policy
        can apply feedback-driven gates:
          - recent_acceptance / recent_dispatched: 7-day global rate (L1)
          - topic_reject_count: 24h weighted rejects, DISMISSED=1 + IGNORED=0.5 (L5)
          - topic_acceptance: 14-day per-topic acceptance rate (L3)

        Returns empty dict when no feedback tracker is wired — policy
        falls back to its static gates.
        """
        if self.feedback is None:
            return {}
        try:
            counts = self.feedback.counts(since_days=7)
            rate = self.feedback.acceptance_rate(since_days=7)
            kwargs: dict[str, Any] = {
                "recent_acceptance": rate,
                "recent_dispatched": counts.get("dispatched", 0),
            }
            if topic_tag:
                kwargs["topic_reject_count"] = self.feedback.recent_topic_rejects(topic_tag)
                kwargs["topic_acceptance"] = self.feedback.topic_acceptance_rate(topic_tag)
            return kwargs
        except Exception as exc:
            logger.warning(
                "policy feedback snapshot failed: {}: {}",
                type(exc).__name__,
                exc,
            )
            return {}

    def _maybe_cleanup_feedback(self, retention_days: int = 30) -> None:
        """Daily: trim NudgeFeedbackTracker's JSONL to the last
        ``retention_days``. The adaptive tuner only looks at 7 days so
        anything older is dead weight on disk + parse path."""
        if self.feedback is None:
            return
        now = self._now_fn()
        if self._last_feedback_cleanup_date is not None and self._last_feedback_cleanup_date.date() == now.date():
            return
        try:
            res = self.feedback.cleanup_older_than(days=retention_days)
            if res.get("dropped"):
                logger.info(
                    "feedback cleanup: kept={} dropped={} (retention={}d)",
                    res.get("kept", 0),
                    res.get("dropped", 0),
                    retention_days,
                )
        except Exception as exc:
            logger.warning("feedback cleanup skipped: {}: {}", type(exc).__name__, exc)
        self._last_feedback_cleanup_date = now

    async def _refresh_memory_state(self) -> None:
        """Refresh attention.md sections + run idle-gated behaviors extractor.

        Synchronously awaited from ``tick_once`` so both one-shot CLI
        invocations (``sentinel tick``) AND the long-running tick loop
        see the writes complete before the next step. Fire-and-forget
        hits a one-shot exit race — ``asyncio.run`` cancels pending
        tasks before they can write the file.

        Per-tick cost: AttentionUpdater is cheap (8 deterministic
        producers; only DailyAnalysisService hits an LLM, cached 24h).
        BehaviorsExtractor only runs when its idle gate passes, so
        typically a no-op on hot ticks.
        """
        if self.attention_updater is not None:
            try:
                await self.attention_updater.update()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "AttentionUpdater.update raised: {}: {}",
                    type(exc).__name__,
                    exc,
                )
            # Re-parse ## User overrides each tick after attention.md
            # refresh so agent-edited DND windows reach NudgePolicy
            # without process restart. Cheap regex on one H2 section.
            try:
                from raven.proactive_engine.sentinel.trigger_policy.derive_dnd import (
                    parse_user_overrides_dnd,
                )

                attention_file = self.attention_updater.memory_store.attention_file
                if attention_file.exists():
                    content = attention_file.read_text(encoding="utf-8")
                    self.policy.set_user_override_dnd(
                        parse_user_overrides_dnd(content),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "user-overrides DND reload failed: {}: {}",
                    type(exc).__name__,
                    exc,
                )
        if self.behaviors_extractor is not None:
            try:
                await self.behaviors_extractor.tick(
                    idle_seconds_observed=self._observed_idle_seconds(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "BehaviorsExtractor.tick raised: {}: {}",
                    type(exc).__name__,
                    exc,
                )
        self._last_memory_write_date = self._now_fn()

    _maybe_write_observations = _refresh_memory_state

    _SENTINEL_INFINITE_IDLE_SECONDS = 10**9  # ~ 31 years; any idle gate clears

    def _observed_idle_seconds(self) -> int:
        """Seconds since the last user inbound. Returns a large sentinel
        when no inbound has been recorded yet so the extractor's idle
        gate clears unconditionally; the cooldown gate is the real
        guard at cold-start."""
        if self._last_inbound_ts is None:
            return self._SENTINEL_INFINITE_IDLE_SECONDS
        delta = self._now_fn() - self._last_inbound_ts
        return max(0, int(delta.total_seconds()))

    def set_channel_manager(self, channel_manager: "ChannelManager | None") -> None:
        """Late-bind ChannelManager — gateway builds it after the runner.
        Idempotent; ``None`` clears."""
        self._channel_manager = channel_manager

    def _resolve_target(
        self,
        channel: str,
        chat_id: str,
    ) -> list[tuple[str, str]]:
        """Resolve one target into concrete ``(channel, chat_id)`` deliveries.

        Three input shapes:

        - ``channel="*"`` → broadcast to ChannelManager.enabled_channels,
          one delivery per channel with chat_id from SessionManager.
        - ``chat_id == ""`` → real-channel auto-resolve via
          ``SessionManager.find_most_recent_chat_id(channel)``.
        - both non-empty → explicit pair.

        Channels not in ``enabled_channels`` under an attached
        ChannelManager (i.e. ephemeral literals like ``cli``/``tui``) are
        logged and skipped — broadcast intent must use ``"*"`` explicitly.
        When no ChannelManager is attached (CLI subprocess / ``--inproc``
        / benchmark harness), the explicit pair passes through verbatim
        so eval harnesses still write PendingDecisions.
        """
        enabled_channels = set(self._channel_manager.enabled_channels) if self._channel_manager is not None else set()

        def _lookup_chat_id(ch: str) -> str | None:
            if self._delivery_session_manager is None:
                return None
            return self._delivery_session_manager.find_most_recent_chat_id(ch)

        if channel == "*":
            if not enabled_channels:
                logger.warning(
                    "Sentinel discovery: target '*' but no ChannelManager attached — skip",
                )
                return []
            results: list[tuple[str, str]] = []
            for ch in sorted(enabled_channels):
                cid = _lookup_chat_id(ch)
                if cid:
                    results.append((ch, cid))
                else:
                    logger.warning(
                        "Sentinel discovery: '*' → {} skipped (no recent session)",
                        ch,
                    )
            return results

        # No ChannelManager bound — fall back to verbatim pass-through for
        # explicit pairs (eval / --inproc / benchmark). Channel-only
        # entries still get an auto-resolve attempt against SessionManager.
        if not enabled_channels:
            if chat_id:
                return [(channel, chat_id)]
            cid = _lookup_chat_id(channel)
            if cid:
                return [(channel, cid)]
            logger.warning(
                "Sentinel discovery: target {!r} (no chat_id, no CM) — nothing to deliver, skip",
                channel,
            )
            return []

        if channel not in enabled_channels:
            logger.warning(
                "Sentinel discovery: target channel {!r} is ephemeral or "
                "unconfigured (enabled: {}) — skip. Use '*' for broadcast "
                "or a real channel name.",
                channel,
                sorted(enabled_channels),
            )
            return []

        if chat_id:
            return [(channel, chat_id)]
        cid = _lookup_chat_id(channel)
        if cid:
            return [(channel, cid)]
        logger.warning(
            "Sentinel discovery: target {!r} has no chat_id and no recent session — skip",
            channel,
        )
        return []

    def _resolve_nudge_targets(self, target: str) -> list[tuple[str, str]]:
        """Resolve a nudge's ``target_session`` to concrete delivery pairs.

        A deadline / daily-plan nudge targets the internal ``sentinel:direct``
        pseudo-session, which has no channel adapter. Fan it out to the same
        configured proactive targets as discovery menus so it reaches the user;
        with none configured, fall back to the single most-recent active
        session. A Planner-issued reactive nudge naming a real channel (e.g.
        ``feishu:ou_xxx``) delivers there directly.
        """
        channel, chat_id = split_session_key(target)
        if channel == "sentinel":
            raw = [pair for tch, tcid in self.task_discovery_targets for pair in self._resolve_target(tch, tcid)]
            if not raw:
                # No configured proactive targets — deliver to the single
                # most-recent active session (like cron's default channel),
                # not a broadcast across every channel. Without this a
                # default deploy (empty target list) drops the nudge.
                raw = self._resolve_active_session()
        else:
            raw = self._resolve_target(channel, chat_id)
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str]] = []
        for pair in raw:
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
        return out

    def _resolve_active_session(self) -> list[tuple[str, str]]:
        """Single most-recent active session for a ``sentinel:direct`` nudge
        with no configured proactive targets.

        Mirrors cron's single-channel delivery (origin / ``default_channel``)
        rather than broadcasting: an anticipatory reminder reaches the user
        where they were last active, so a multi-channel user isn't pinged on
        every channel at once. Sessions on channels outside an attached
        ChannelManager's ``enabled_channels`` are skipped (e.g. a stale feishu
        session must not capture a REPL nudge whose only real surface is cli).
        """
        sm = self._delivery_session_manager
        if sm is None:
            return []
        enabled = set(self._channel_manager.enabled_channels) if self._channel_manager is not None else set()
        lister = getattr(sm, "list_sessions", None)
        if callable(lister):
            try:
                sessions = lister()
            except Exception:  # noqa: BLE001
                sessions = []
            for entry in sessions:  # sorted by updated_at descending
                key = entry.get("key", "") if isinstance(entry, dict) else ""
                ch, _, cid = str(key).partition(":")
                if not cid or (enabled and ch not in enabled):
                    continue
                return [(ch, cid)]
            return []
        # SessionManager without list_sessions: probe enabled channels.
        for ch in sorted(enabled):
            cid = sm.find_most_recent_chat_id(ch)
            if cid:
                return [(ch, cid)]
        return []

    async def _maybe_run_task_discovery(self) -> None:
        """Daily TaskDiscoverer pass. Same per-process guard pattern as
        ``_maybe_write_observations``: only one attempt per local day.
        Cross-process safety lives in PendingDecisionStore (newer write
        supersedes older live decision on the same address) so two
        processes producing menus at 08:00 only result in one menu the
        user actually sees."""
        if self.task_discoverer is None:
            return
        if not self.task_discovery_targets:
            return
        now = self._now_fn()
        # Already ran today.
        if self._last_task_discovery_date is not None and self._last_task_discovery_date.date() == now.date():
            return
        # Time-of-day gate: only run after the configured local time.
        target_h, target_m = self.task_discovery_time
        if (now.hour, now.minute) < (target_h, target_m):
            return

        for channel, chat_id in self.task_discovery_targets:
            resolved = self._resolve_target(channel, chat_id)
            if not resolved:
                continue
            for ch, cid in resolved:
                try:
                    await self.task_discoverer.run(channel=ch, to=cid)
                    logger.info(
                        "Sentinel discovery fired: {} → {} (origin={!r})",
                        ch,
                        cid,
                        channel if not chat_id else f"{channel}:{chat_id}",
                    )
                except Exception as exc:
                    logger.warning(
                        "TaskDiscoverer.run failed for ({}, {}): {}: {}",
                        ch,
                        cid,
                        type(exc).__name__,
                        exc,
                    )
        # Per-day guard advances even if every target failed — retrying
        # next tick would just re-fail and spam logs.
        self._last_task_discovery_date = now

    async def _maybe_consume_discover_triggers(self) -> None:
        """Drain CLI-queued triggers and fire ``discover_now`` for each.
        Bypasses the daily date/time gate — a trigger IS the user's
        explicit fire-now intent."""
        if self._discover_trigger_store is None:
            return
        try:
            triggers = self._discover_trigger_store.consume_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "DiscoverTriggerStore.consume_all raised: {}: {}",
                type(exc).__name__,
                exc,
            )
            return
        if not triggers:
            return
        logger.info(
            "Sentinel: consuming {} discover trigger(s) from CLI",
            len(triggers),
        )
        for trg in triggers:
            try:
                await self.discover_now(trg.channel, trg.to)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "discover-trigger {} ({}, {}) failed: {}: {}",
                    trg.id,
                    trg.channel,
                    trg.to,
                    type(exc).__name__,
                    exc,
                )

    async def discover_now(self, channel: str, to: str) -> None:
        """Force-run a discovery pass right now (skips date + time-of-day
        gates). Routes through ``_resolve_target`` for the same syntax
        rules as the daily batch — ``"*"`` broadcasts; ephemeral literals
        are skipped under an attached ChannelManager."""
        if self.task_discoverer is None:
            logger.warning("discover_now: no TaskDiscoverer configured")
            return
        resolved = self._resolve_target(channel, to)
        if not resolved:
            return
        for ch, cid in resolved:
            try:
                await self.task_discoverer.run(channel=ch, to=cid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "discover_now: TaskDiscoverer.run failed for ({}, {}): {}: {}",
                    ch,
                    cid,
                    type(exc).__name__,
                    exc,
                )

    async def tick_with_context(self, ctx: "PlannerContext") -> TickOutcome:
        """Run Planner → route with a caller-supplied context.

        Benchmark adapters use this to test the full Sentinel pipeline
        (Planner + NudgePolicy + executors) without depending on the
        ContextAssembler's session/memory sources.
        """
        # Daily: trim feedback log to retention window. Must run BEFORE
        # _maybe_retune_policy so the adaptive tuner sees the freshly
        # filtered acceptance_rate (otherwise a one-day-stale tail keeps
        # influencing multiplier).
        self._maybe_cleanup_feedback()
        # Adaptive policy: recompute hour-quota multiplier from observed
        # acceptance rate before the Planner sees snapshot_state.
        self._maybe_retune_policy()
        # Refresh attention.md / behaviors.md every tick. Per-section
        # cooldowns (Sentinel Observations 24h, DailyAnalysis 24h
        # cache, behaviors extractor idle+cooldown) live inside each
        # producer's ``should_run`` / service ``get`` so the lock
        # acquire stays cheap.
        await self._refresh_memory_state()
        # Ad-hoc CLI triggers are drained by ``_run_trigger_loop`` on a
        # 2s cadence — not here. The LLM tick used to do it inline, but
        # that made ``discover-now`` wait up to ``interval_s`` before
        # firing, contradicting its own name.
        await self._maybe_run_task_discovery()
        # New: scheduled-fire fast-path. If attention.md ``## 今日 fire 计划``
        # has an entry due within ±20 min of now AND that topic hasn't fired
        # today, dispatch directly without the planner LLM. Keeps Daily
        # Planning's slot-time + topic_tag commitments instead of letting
        # the per-tick LLM redrift the schedule.
        # Parse the fire plan once and thread it through the fast path,
        # fallback, and skip-warning — one read + one self._now_fn() per tick.
        now = self._now_fn()
        due_slots = self._due_plan_slots(now)
        scheduled = self._fast_path_scheduled_fire(now, due_slots)
        if scheduled is not None:
            outcome = await self._route(scheduled)
            self.assembler.remember_last_decision(scheduled)
            return outcome

        fast = self._fast_path_rules(ctx, due_slots)
        if fast is not None:
            self.assembler.remember_last_decision(fast)
            return TickOutcome(decision=fast, result=None, route="fast_path_skip")

        try:
            decision = await self.planner.decide(ctx)
        except Exception as exc:
            logger.exception("Planner.decide failed: {}", exc)
            # Planner-down safety net: a high-priority deadline slot due now
            # would otherwise be silently dropped (the fast path defers all
            # deadlines to the now-unavailable Planner).
            fallback = self._fallback_deadline_fire(now, due_slots)
            if fallback is not None:
                logger.warning(
                    "planner down — firing high-priority deadline {} via fallback",
                    fallback.topic_tag,
                )
                outcome = await self._route(fallback)
                self.assembler.remember_last_decision(fallback)
                return outcome
            self.assembler.remember_last_decision(None)
            return TickOutcome(
                decision=PlannerDecision(action="skip", reason=f"planner_error: {type(exc).__name__}"),
                result=None,
                route="error",
            )

        # Planner ran (online): if it left a due deadline_* fire-plan slot
        # unfired, that is the one otherwise-silent failure surface — log it.
        self._warn_unfired_due_deadline(decision, now, due_slots)
        outcome = await self._route(decision)
        # Stash context signature + timestamp on skip decisions so the next
        # tick's fast-path rule (b) can detect "context unchanged since last
        # skip" with a TTL fallback.
        if decision.action == "skip":
            setattr(decision, "_ctx_signature", self._context_signature(ctx))
            setattr(decision, "_ts_at", self._now_fn())
        self.assembler.remember_last_decision(decision)
        return outcome

    # ------------------------------------------------------------------
    # Rule-based fast path (short-circuits Planner LLM call)
    #
    # Three rules, all safe-to-deny: if any returns a skip decision we use it
    # and don't touch the LLM. Intentionally conservative — any case that
    # *might* want a nudge falls through to the Planner.

    # Sentinel ticks land at HH:00 and HH:30 (30-min cadence). With ±5 min
    # slack, a plan time like 06:50 falls between ticks 06:30 (gap 20 min)
    # and 07:00 (gap 10 min) — neither matches. Widen to ±20 min so any
    # plan time within the half-hour grid gets caught by the next tick.
    # Combined with the planner prompt asking for HH:00 / HH:30 alignment,
    # this is belt-and-braces.
    _SCHEDULED_FIRE_SLACK_S = 1200  # ±20 min window around plan time

    # One-shot, completable slot classes. The fast path does NOT fire these —
    # they fall through to the Planner, which reads the episodes tail and can
    # tell a still-pending deadline from one the user already finished
    # (completion is a language-level judgment the LLM does reliably across
    # languages). The Planner also sees the daily fire plan (it is in
    # ``attention_planner_sections``), so it knows the slot is due today.
    # Recurring classes (routine_/medication_/daily_/…) fast-fire.
    _DEADLINE_SLOT_PREFIXES = ("deadline_", "birthday_", "anniversary_")

    @classmethod
    def _is_deadline_slot(cls, tag: str) -> bool:
        """True for one-shot, completable slot classes (vs recurring)."""
        return any(tag.startswith(p) for p in cls._DEADLINE_SLOT_PREFIXES)

    def _has_due_high_priority_deadline(
        self,
        due_slots: "list[tuple[dict, str]]",
    ) -> bool:
        """True if any due slot is a ``priority=high`` deadline_* slot."""
        return any(
            self._is_deadline_slot(tag) and entry.get("priority", "low").lower() == "high" for entry, tag in due_slots
        )

    def _due_plan_slots(self, now: datetime) -> list[tuple[dict, str]]:
        """Return ``(entry, tag)`` for each ``## 今日 fire 计划`` slot due within
        ``_SCHEDULED_FIRE_SLACK_S`` (±20 min) of ``now`` with a non-empty
        topic_tag. Shared by the fast path, the planner-down deadline fallback,
        and the skipped-deadline warning so they never drift on parse / window
        logic. A list (not a generator) so a stray truth-test can't pass.
        """
        if self.attention_updater is None:
            return []
        attention_file = self.attention_updater.memory_store.attention_file
        if not attention_file.exists():
            return []
        try:
            content = attention_file.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            from raven.proactive_engine.sentinel.trigger_policy.derive_dnd import (
                parse_daily_plan,
            )

            plan = parse_daily_plan(content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily-plan parse failed: {}: {}", type(exc).__name__, exc)
            return []
        due: list[tuple[dict, str]] = []
        for entry in plan or []:
            time_hhmm = entry.get("time_hhmm", "")
            if ":" not in time_hhmm:
                continue
            try:
                h_s, m_s = time_hhmm.split(":", 1)
                slot = now.replace(
                    hour=int(h_s),
                    minute=int(m_s),
                    second=0,
                    microsecond=0,
                )
            except ValueError:
                continue
            if abs((now - slot).total_seconds()) > self._SCHEDULED_FIRE_SLACK_S:
                continue
            tag = entry.get("topic_tag", "").strip()
            if not tag:
                continue
            due.append((entry, tag))
        return due

    @staticmethod
    def _slot_to_decision(entry: dict, tag: str, *, source: str) -> PlannerDecision:
        """Build the templated nudge a daily-plan slot fires."""
        rationale = (entry.get("rationale") or "").strip()
        user_message = (entry.get("user_message") or "").strip()
        priority_str = entry.get("priority", "low").lower()
        time_hhmm = entry.get("time_hhmm", "")
        return PlannerDecision(
            action="nudge",
            topic_tag=tag,
            priority=priority_str,  # type: ignore[arg-type]
            proactivity_score=0.85,
            target_session="sentinel:direct",
            # reason keeps the evidence-citing rationale (logs / scoring);
            # nudge_message is the user-facing line the daily plan produced.
            reason=f"daily_plan slot {time_hhmm}: {rationale[:120]}",
            nudge_message=user_message or rationale or f"提醒：{tag}",
            raw_llm_response={"source": source},
        )

    def _fast_path_scheduled_fire(
        self,
        now: datetime,
        due_slots: "list[tuple[dict, str]] | None" = None,
    ) -> PlannerDecision | None:
        """Return a synthetic PlannerDecision if a ``## 今日 fire 计划``
        entry is due within ``_SCHEDULED_FIRE_SLACK_S`` (±20 min) of
        ``now`` and that topic hasn't already fired today.

        Cron-style execution layer on top of the LLM-driven plan: when
        DailyPlanProducer schedules ``- 06:50 routine_morning_med | ...``
        and the sentinel tick lands at 06:48-06:52, we dispatch a templated
        nudge with the planned topic_tag — no Planner LLM call, no redrift
        on time/tag. Outside the slot window, the Planner LLM still runs.

        ``due_slots`` lets the tick pass a pre-parsed plan (computed once and
        shared with the rules / fallback / warning); None recomputes.
        """
        for entry, tag in self._due_plan_slots(now) if due_slots is None else due_slots:
            # One-shot deadline-style slots are routed to the Planner, not
            # fast-fired: only the Planner (reading the episodes tail) can tell
            # a still-pending deadline from one the user already finished, so
            # firing here blind would re-nag a completed task. The Planner sees
            # this slot via the fire-plan attention section. Recurring slots
            # have no "done" state and fast-fire below. (A planner-down safety
            # net for high-priority deadlines lives in _fallback_deadline_fire.)
            if self._is_deadline_slot(tag):
                continue
            # Already fired today? The per-topic ledger on the policy is the
            # canonical state — peer ticks update it, so this stays correct
            # across subprocess invocations.
            if self.policy.topic_fired_today(tag, now):
                continue
            return self._slot_to_decision(entry, tag, source="fast_path_scheduled_fire")
        return None

    def _fallback_deadline_fire(
        self,
        now: datetime,
        due_slots: "list[tuple[dict, str]] | None" = None,
    ) -> PlannerDecision | None:
        """Planner-down safety net: re-fire a high-priority ``deadline_*`` slot
        the fast path deferred, used only when ``Planner.decide`` raised.

        The fast path defers one-shot deadlines to the Planner so a finished
        task isn't re-nagged. But with the Planner LLM down, that deferral
        would silently drop a hard deadline on its day. This restores the
        pre-defer fast-fire for exactly those slots — scoped to ``priority ==
        "high"`` so the blind re-nag (completion is uncheckable with the LLM
        down) stays minimal, and only for fire-plan slots (a deadline never
        scheduled into the plan was never fast-fireable, so this is no worse
        than before). The fire still passes the normal policy gates via
        ``_route``. Disabled by ``deadline_outage_fallback = False``.
        """
        if not self._deadline_outage_fallback:
            return None
        for entry, tag in self._due_plan_slots(now) if due_slots is None else due_slots:
            if not self._is_deadline_slot(tag):
                continue
            if entry.get("priority", "low").lower() != "high":
                continue
            if self.policy.topic_fired_today(tag, now):
                continue
            return self._slot_to_decision(entry, tag, source="fallback_planner_down")
        return None

    def _warn_unfired_due_deadline(
        self,
        decision: PlannerDecision,
        now: datetime,
        due_slots: "list[tuple[dict, str]] | None" = None,
    ) -> None:
        """Log when the Planner ran (online) but left a due ``deadline_*``
        fire-plan slot unfired — an online "already done" misjudgment would
        otherwise be silent. Scope: only deadlines the fast path lets through
        to the Planner. A low-priority deadline dropped by the quiet-hours
        fast-path rule is intentional silence ("prefer silent over
        over-nudging"), not covered here — only the high-priority path
        bypasses that rule to reach the Planner.

        Logs only; does not fire (the online completion judgment is trusted).
        Gives the longrun eval a hook to catch a false "already done" call on
        a still-pending hard deadline.
        """
        fired = decision.topic_tag if decision.action == "nudge" else None
        for _entry, tag in self._due_plan_slots(now) if due_slots is None else due_slots:
            if not self._is_deadline_slot(tag):
                continue
            if tag == fired or self.policy.topic_fired_today(tag, now):
                continue
            logger.warning(
                "planner skipped due deadline slot {} (decision={}, reason={!r})",
                tag,
                decision.action,
                (decision.reason or "")[:120],
            )

    def _fast_path_rules(
        self,
        ctx: "PlannerContext",
        due_slots: "list[tuple[dict, str]] | None" = None,
    ) -> PlannerDecision | None:
        # A due high-priority deadline must reach the Planner (or, if it is
        # down, the fallback): quiet hours is bypassable for high priority at
        # the policy layer (high_priority_bypasses_limits), and a fast-path
        # skip here would both defeat that and drop it silently (the skip
        # warning runs only after the Planner). So short-circuit neither rule.
        if due_slots is None:
            due_slots = self._due_plan_slots(self._now_fn())
        if self._has_due_high_priority_deadline(due_slots):
            return None

        # Rule (a): config-level quiet hours hard-hit. Policy itself denies
        # nudges here anyway; running LLM only to have the decision denied
        # downstream is pure waste.
        nps = ctx.nudge_policy_state
        if nps is not None and nps.in_quiet_hours:
            return PlannerDecision(
                action="skip",
                reason="fast_path: in quiet_hours",
            )

        # Rule (b): same context as last skip tick. When memory/history/sessions
        # haven't changed since the previous tick and that tick was a skip,
        # running Planner again can only produce the same skip — dedup. TTL
        # ensures Planner gets re-consulted at least once per hour even on
        # static contexts (without it, a sleepy persona can lock skip for days
        # and never let the policy multiplier relax via fresh decisions).
        last = ctx.last_decision
        if last is not None and last.action == "skip":
            sig = self._context_signature(ctx)
            prev_sig = getattr(last, "_ctx_signature", None)
            prev_ts = getattr(last, "_ts_at", None)
            now = self._now_fn()
            stale = prev_ts is not None and (now - prev_ts).total_seconds() > self._FAST_PATH_SKIP_TTL_S
            if prev_sig is not None and prev_sig == sig and not stale:
                dec = PlannerDecision(
                    action="skip",
                    reason="fast_path: context unchanged since last skip",
                )
                # stash for the next tick's comparison
                setattr(dec, "_ctx_signature", sig)
                setattr(dec, "_ts_at", now)
                return dec

        return None

    @staticmethod
    def _context_signature(ctx: "PlannerContext") -> str:
        """Hash memory + history + session keys to detect unchanged contexts."""
        import hashlib

        h = hashlib.blake2b(digest_size=16)
        h.update((ctx.memory_md or "").encode("utf-8"))
        h.update(b"|")
        h.update((ctx.history_md_recent or "").encode("utf-8"))
        h.update(b"|")
        for s in ctx.active_sessions or []:
            h.update(f"{s.key}@{s.last_active_at.isoformat()}".encode("utf-8"))
            h.update(b",")
        return h.hexdigest()

    # ------------------------------------------------------------------
    # Routing

    async def _route(self, decision: PlannerDecision) -> TickOutcome:
        action = decision.action
        if action == "skip":
            return TickOutcome(decision=decision, result=None, route="skip")

        target = decision.target_session or "sentinel:direct"

        if action == "nudge":
            return await self._route_nudge(decision, target)
        if action == "nudge_inject":
            return await self._route_inject(decision, target)
        if action == "nudge_defer":
            return await self._route_defer(decision, target)
        if action == "spawn_agent":
            return await self._route_spawn(decision, target)

        # Shouldn't reach here — Planner validates actions — but fail safe.
        return TickOutcome(
            decision=decision,
            result=None,
            route="unknown_action",
            notes=[f"unrecognized action: {action}"],
        )

    # --- individual routes -------------------------------------------

    async def _route_nudge(self, decision: PlannerDecision, target: str) -> TickOutcome:
        if self.dispatcher is None:
            return self._degraded(decision, target, "no_dispatcher", "nudge")

        content = decision.nudge_message or ""
        check = self.policy.check(
            decision.action,
            target,
            content,
            decision.priority,
            topic_tag=decision.topic_tag,
            **self._policy_feedback_kwargs(decision.topic_tag),
        )
        if check.verdict == "deny":
            return TickOutcome(
                decision=decision,
                result=ExecutionResult(delivered=False, reason=f"policy:{check.reason}"),
                route="nudge_denied",
            )

        delivery_targets = self._resolve_nudge_targets(target)
        if not delivery_targets:
            return TickOutcome(
                decision=decision,
                result=ExecutionResult(delivered=False, reason="no_delivery_target"),
                route="nudge_no_target",
            )

        try:
            result = await self.dispatcher.dispatch(decision, delivery_targets)
        except Exception as exc:
            logger.exception("NudgeDispatcher raised: {}", exc)
            return TickOutcome(
                decision=decision,
                result=ExecutionResult(delivered=False, reason=f"dispatcher_error:{type(exc).__name__}"),
                route="nudge_error",
            )

        nudge_id = new_nudge_id()
        if result.delivered:
            self.policy.record_fired(
                decision.action,
                target,
                content,
                topic_tag=decision.topic_tag,
            )
            self._record_dispatched(nudge_id, decision, target)
        return TickOutcome(decision=decision, result=result, nudge_id=nudge_id, route="nudge")

    async def _route_inject(self, decision: PlannerDecision, target: str) -> TickOutcome:
        if self.injector is None:
            return self._degraded(decision, target, "no_injector", "inject")

        content = decision.nudge_message or ""
        check = self.policy.check(
            decision.action,
            target,
            content,
            decision.priority,
            topic_tag=decision.topic_tag,
            **self._policy_feedback_kwargs(decision.topic_tag),
        )
        if check.verdict == "deny":
            return TickOutcome(
                decision=decision,
                result=ExecutionResult(delivered=False, reason=f"policy:{check.reason}"),
                route="inject_denied",
            )

        try:
            self.injector.queue(target, content, source=self._decision_source)
        except Exception as exc:
            logger.exception("NudgeInjector.queue raised: {}", exc)
            return TickOutcome(
                decision=decision,
                result=ExecutionResult(delivered=False, reason=f"injector_error:{type(exc).__name__}"),
                route="inject_error",
            )

        # "Delivered" = queued; real delivery happens when AgentLoop next
        # produces a reply and the response_modifier pops the queue.
        nudge_id = new_nudge_id()
        self.policy.record_fired(
            decision.action,
            target,
            content,
            topic_tag=decision.topic_tag,
        )
        self._record_dispatched(nudge_id, decision, target, extra={"note": "queued"})
        return TickOutcome(
            decision=decision,
            result=ExecutionResult(
                delivered=True, reason="queued", delivery_time=self._now_fn(), details={"session_key": target}
            ),
            nudge_id=nudge_id,
            route="inject",
        )

    async def _route_defer(self, decision: PlannerDecision, target: str) -> TickOutcome:
        if self.defer_manager is None:
            return self._degraded(decision, target, "no_defer_manager", "defer")

        content = decision.nudge_message or ""
        check = self.policy.check(
            decision.action,
            target,
            content,
            decision.priority,
            topic_tag=decision.topic_tag,
            **self._policy_feedback_kwargs(decision.topic_tag),
        )
        if check.verdict == "deny":
            return TickOutcome(
                decision=decision,
                result=ExecutionResult(delivered=False, reason=f"policy:{check.reason}"),
                route="defer_denied",
            )

        try:
            defer_id = self.defer_manager.register(decision, target_session=target)
        except Exception as exc:
            logger.exception("DeferManager.register raised: {}", exc)
            return TickOutcome(
                decision=decision,
                result=ExecutionResult(delivered=False, reason=f"defer_error:{type(exc).__name__}"),
                route="defer_error",
            )

        # We do NOT record_fired until the defer actually dispatches —
        # otherwise a deferred nudge that later gets cancelled would eat
        # the quota. DeferManager's on_dispatch callback (future work) is
        # where record_fired should land. For now, we leave that gap
        # documented; it only matters for overlapping defers on the same
        # session.
        return TickOutcome(
            decision=decision,
            result=ExecutionResult(
                delivered=False,
                reason="deferred",
                defer_id=defer_id,
                details={"session_key": target, "defer_id": defer_id},
            ),
            route="defer",
        )

    async def _route_spawn(self, decision: PlannerDecision, target: str) -> TickOutcome:
        if self.spawn is None:
            return self._degraded(decision, target, "no_spawn", "spawn")
        try:
            result = await self.spawn.dispatch(decision)
        except Exception as exc:
            logger.exception("ProactiveSpawn.dispatch raised: {}", exc)
            return TickOutcome(
                decision=decision,
                result=ExecutionResult(delivered=False, reason=f"spawn_error:{type(exc).__name__}"),
                route="spawn_error",
            )
        nudge_id = new_nudge_id()
        if result.delivered:
            self._record_dispatched(
                nudge_id, decision, target, extra={"task_id": (result.details or {}).get("task_id")}
            )
        return TickOutcome(decision=decision, result=result, nudge_id=nudge_id, route="spawn")

    # ------------------------------------------------------------------
    # Shared helpers

    def _degraded(
        self,
        decision: PlannerDecision,
        target: str,
        reason: str,
        route: str,
    ) -> TickOutcome:
        """When a required executor isn't wired, log + degrade gracefully.

        Planner still gets its decision recorded (for last_decision feedback)
        but no delivery happens. Useful for partial configurations (e.g.,
        benchmark mode with only dispatcher + injector).
        """
        logger.info(
            "SentinelRunner degraded: action={} → {} (reason={})",
            decision.action,
            route,
            reason,
        )
        return TickOutcome(
            decision=decision,
            result=ExecutionResult(
                delivered=False,
                reason=f"degraded:{reason}",
                details={"session_key": target},
            ),
            route=f"{route}_degraded",
            notes=[reason],
        )

    def _record_dispatched(
        self,
        nudge_id: str,
        decision: PlannerDecision,
        target: str,
        extra: dict | None = None,
    ) -> None:
        # Always track for engagement even if feedback tracker is absent
        # — the acceptance/dismissal signal is useful for NudgePolicy too.
        now = self._now_fn()
        self._pending_engagement.setdefault(target, []).append((nudge_id, now))
        self._persist_engagement()

        if self.feedback is None:
            return
        # Stash topic_tag in details so the Observations writer can
        # later compute per-topic accept/dismiss rates by joining
        # accept/dismiss events back to the dispatch record by nudge_id.
        details = dict(extra) if extra else {}
        if decision.topic_tag:
            details["topic_tag"] = decision.topic_tag
        try:
            self.feedback.record_dispatched(
                nudge_id,
                action=decision.action,
                session_key=target,
                priority=decision.priority,
                proactivity_score=decision.proactivity_score,
                source=self._decision_source,
                details=details,
            )
        except Exception as exc:
            logger.warning("FeedbackTracker.record_dispatched failed: {}", exc)

    # ------------------------------------------------------------------
    # User-reply feedback — AgentLoop's on_user_inbound hook calls this.

    def on_user_inbound(self, msg: Any) -> None:
        """Resolve recent nudge(s) on the session and route engagement.

        Called by AgentLoop for every user-originated inbound (not
        Sentinel-origin). Behavior:
        - Session key derived from req.conversation (falls back to channel:chat_id).
        - Publishes session_key into the ``current_session_key``
          contextvar so the ``nudge_feedback`` tool can find this
          session when the main LLM calls it later in the turn.
        - If content starts with '/dismiss' (any case) → mark most recent
          pending nudge as dismissed + tell NudgePolicy to cool this
          session down. Deterministic fast path; no LLM required.
        - Otherwise, **defer classification to the main LLM** — move the
          most recent nudge into ``_awaiting_llm_feedback``. The
          ``nudge_feedback`` tool may consume it during the ReAct loop;
          if not, ``finalize_pending_feedback`` (after_send) records it
          as NEUTRAL. Recording every non-/dismiss reply as ACCEPTED
          would wrongly inflate acceptance_rate on natural-language
          dismissals like "stop reminding me" and tighten the adaptive quota
          in the wrong direction.
        - Stale entries (beyond engagement_window_seconds) are dropped
          on access.
        """
        try:
            # A menu-pick execution (Sentinel-injected, sentinel_action_origin)
            # is not a new nudge reaction — the accept was already recorded when
            # the user picked, in decision_consumer. Skip engagement resolution
            # so it isn't double-counted. (The legacy bus path skipped the whole
            # user-inbound hook chain for this turn; on the spine path the chain
            # runs, so this per-hook guard restores that behavior.)
            sentinel = getattr(msg, "sentinel", None)
            if sentinel is not None and sentinel.action_origin:
                return

            source = getattr(msg, "source", None)
            session_key = getattr(msg, "conversation", None)
            if not session_key and source is not None:
                channel = source.channel or ""
                chat_id = source.chat_id or ""
                session_key = f"{channel}:{chat_id}" if (channel or chat_id) else ""
            if not session_key:
                return

            # Publish session_key for the nudge_feedback tool.
            current_session_key.set(session_key)

            # Update idle tracker so BehaviorsExtractor.tick() sees
            # accurate idle_seconds_observed on the next tick.
            self._last_inbound_ts = self._now_fn()

            # Reload engagement state from disk — in eval / gateway setups
            # dispatch and reply happen in different subprocesses, so the
            # in-memory dict is empty here on a fresh agent subprocess.
            if self._store is not None:
                self._hydrate_engagement_from_store()

            pending = self._expire_engagement(session_key)
            if not pending:
                return

            content = (getattr(msg, "text", "") or "").strip()
            is_dismissal = content.lower().startswith("/dismiss")

            # Most recent nudge is the relevant one.
            nudge_id, dispatched_at = pending.pop()
            # Drop any remaining stale nudges on the same session — a user
            # reply acknowledges the whole batch; we don't keep shadowing
            # multiple acceptance events for one reply.
            self._pending_engagement.pop(session_key, None)

            if is_dismissal:
                self.policy.record_dismissed(session_key)
                if self.feedback is not None:
                    self.feedback.record_dismissed(nudge_id, reason=content[:120] or None)
                self._persist_engagement()
                logger.info(
                    "nudge_dismissed id={} session={} via {!r}",
                    nudge_id,
                    session_key,
                    content[:40],
                )
                return

            # Defer: let the main LLM classify via the nudge_feedback
            # tool. If the LLM doesn't call the tool, finalize_pending_feedback
            # records this as NEUTRAL at after_send time.
            self._awaiting_llm_feedback.setdefault(session_key, []).append((nudge_id, dispatched_at))
            self._persist_engagement()
            logger.debug(
                "nudge_awaiting_llm_classification id={} session={}",
                nudge_id,
                session_key,
            )
        except Exception as exc:
            logger.warning(
                "SentinelRunner.on_user_inbound failed: {}: {}",
                type(exc).__name__,
                exc,
            )

    # ------------------------------------------------------------------
    # LLM-mediated feedback — NudgeFeedbackTool calls this from inside
    # the ReAct loop. ``finalize_pending_feedback`` is the after_send
    # safety net for turns where the LLM didn't call the tool.

    def consume_feedback_via_tool(
        self,
        session_key: str,
        sentiment: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Record LLM-classified feedback on the most-recently-deferred
        nudge for ``session_key``.

        sentiment ∈ {"accepted", "dismissed", "snoozed", "irrelevant"}.
        - accepted   → record_accepted
        - dismissed  → policy cooldown + record_dismissed
        - snoozed    → policy cooldown + record_dismissed (reason
                       prefixed with "snoozed:") — snooze is a soft
                       dismiss for the current session window
        - irrelevant → record_neutral (user replied but didn't address
                       the nudge — e.g. asked an unrelated question)

        Returns ``{"recorded": True, "nudge_id": ..., "signal": ...}``
        on success, or ``{"recorded": False, "reason": ...}`` if no
        nudge was awaiting classification.
        """
        # Reload from disk first — another subprocess (the sentinel-ticks
        # dispatcher) may have appended new entries we haven't seen yet.
        if self._store is not None:
            self._hydrate_engagement_from_store()
        queue = self._awaiting_llm_feedback.get(session_key) or []
        if not queue:
            return {"recorded": False, "reason": "no_awaiting_nudge"}
        nudge_id, _ = queue.pop()
        if not queue:
            self._awaiting_llm_feedback.pop(session_key, None)
        else:
            self._awaiting_llm_feedback[session_key] = queue
        self._persist_engagement()

        sentiment = (sentiment or "").lower().strip()
        if sentiment == "accepted":
            if self.feedback is not None:
                self.feedback.record_accepted(nudge_id, context=reason)
            logger.info(
                "nudge_accepted (llm-classified) id={} session={}",
                nudge_id,
                session_key,
            )
            return {"recorded": True, "nudge_id": nudge_id, "signal": "accepted"}
        if sentiment in ("dismissed", "snoozed"):
            self.policy.record_dismissed(session_key)
            if self.feedback is not None:
                tagged = f"snoozed: {reason}" if (sentiment == "snoozed" and reason) else (reason or sentiment)
                self.feedback.record_dismissed(nudge_id, reason=tagged)
            logger.info(
                "nudge_dismissed (llm-classified {}) id={} session={}",
                sentiment,
                nudge_id,
                session_key,
            )
            return {"recorded": True, "nudge_id": nudge_id, "signal": sentiment}
        # Default: irrelevant / unknown sentiment → neutral.
        if self.feedback is not None:
            self.feedback.record_neutral(nudge_id, reason=reason or sentiment or None)
        logger.info(
            "nudge_neutral (llm-classified irrelevant) id={} session={}",
            nudge_id,
            session_key,
        )
        return {"recorded": True, "nudge_id": nudge_id, "signal": "neutral"}

    def finalize_pending_feedback(self, session_key: str) -> int:
        """Flush anything still awaiting classification for ``session_key``
        as NEUTRAL — the LLM had its chance via ``nudge_feedback`` and
        didn't take it, so by design we record "user engaged but
        intent unclear" rather than falling back to "treat as accepted".

        Called from the SentinelFeedbackHook's after_send phase.
        Returns the number of entries flushed.
        """
        # Reload from disk before flushing — entries written by other
        # subprocesses must also be drained, otherwise neutral wouldn't
        # land at all in cross-process mode.
        if self._store is not None:
            self._hydrate_engagement_from_store()
        queue = self._awaiting_llm_feedback.pop(session_key, None) or []
        if not queue:
            return 0
        self._persist_engagement()
        if self.feedback is None:
            return len(queue)
        for nudge_id, _ in queue:
            try:
                self.feedback.record_neutral(
                    nudge_id,
                    reason="no_llm_classification",
                )
            except Exception as exc:
                logger.warning(
                    "FeedbackTracker.record_neutral failed: {}",
                    exc,
                )
        logger.debug(
            "nudge_feedback_finalized n={} session={}",
            len(queue),
            session_key,
        )
        return len(queue)

    def _expire_engagement(self, session_key: str) -> list[tuple[str, datetime]]:
        pending = self._pending_engagement.get(session_key)
        if not pending:
            return []
        now = self._now_fn()
        window = self._engagement_window
        fresh = [(nid, ts) for nid, ts in pending if (now - ts).total_seconds() <= window]
        if not fresh:
            self._pending_engagement.pop(session_key, None)
            self._persist_engagement()
            return []
        if len(fresh) != len(pending):
            self._pending_engagement[session_key] = fresh
            self._persist_engagement()
        return fresh

    # ------------------------------------------------------------------
    # Cross-process persistence — mirrors NudgePolicy's _STATE_KEY pattern.

    def _hydrate_engagement_from_store(self) -> None:
        """Load engagement state from disk on construction. Tolerant of
        missing / malformed entries — a corrupt blob shouldn't take down
        a fresh subprocess; we'd rather lose engagement correlation than
        crash the runner."""
        if self._store is None:
            return
        try:
            blob = (self._store.load() or {}).get(self._STATE_KEY) or {}
        except Exception as exc:
            logger.warning("engagement state load failed: {}", exc)
            return
        self._pending_engagement = _decode_engagement(blob.get("pending"))
        self._awaiting_llm_feedback = _decode_engagement(blob.get("awaiting_llm_feedback"))

    def _persist_engagement(self) -> None:
        """Write current engagement state back to the store under an
        exclusive fcntl lock. No-op when no store is configured (in-memory
        mode for tests / single-process gateway)."""
        if self._store is None:
            return
        blob = {
            "pending": _encode_engagement(self._pending_engagement),
            "awaiting_llm_feedback": _encode_engagement(self._awaiting_llm_feedback),
        }
        state_key = self._STATE_KEY

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            state[state_key] = blob
            return state

        try:
            self._store.update(_mutate)
        except Exception as exc:
            logger.warning("engagement state persist failed: {}", exc)


def _encode_engagement(
    src: dict[str, list[tuple[str, datetime]]],
) -> dict[str, list[list[str]]]:
    """Serialize an engagement dict to a JSON-friendly shape:
    ``{session_key: [[nudge_id, iso_ts], ...]}``."""
    return {sk: [[nid, ts.isoformat()] for nid, ts in entries] for sk, entries in src.items() if entries}


def _decode_engagement(
    blob: Any,
) -> dict[str, list[tuple[str, datetime]]]:
    """Inverse of ``_encode_engagement``. Defensive: skips entries
    where nudge_id isn't a str or timestamp doesn't parse — a single
    bad row shouldn't drop the whole session."""
    if not isinstance(blob, dict):
        return {}
    out: dict[str, list[tuple[str, datetime]]] = {}
    for sk, entries in blob.items():
        if not isinstance(sk, str) or not isinstance(entries, list):
            continue
        decoded: list[tuple[str, datetime]] = []
        for row in entries:
            if not isinstance(row, list) or len(row) != 2:
                continue
            nid, ts_str = row
            if not isinstance(nid, str) or not isinstance(ts_str, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            decoded.append((nid, ts))
        if decoded:
            out[sk] = decoded
    return out


__all__ = ["SentinelRunner", "TickOutcome", "current_session_key"]
