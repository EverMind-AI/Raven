"""Sentinel-stack builders shared by the gateway, agent and sentinel commands.

Cross-process coordination: when ``config.sentinel.enabled`` is true, a
single JsonStateStore at ``~/.raven/sentinel/state.json`` is shared
across NudgePolicy / NudgeInjector / DeferManager so REPL and gateway,
when running simultaneously, agree on quotas + pending injects + defers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from raven.config.paths import get_sentinel_dir

if TYPE_CHECKING:
    from pathlib import Path

    from raven.agent.loop import AgentLoop
    from raven.config.raven import SentinelConfig
    from raven.config.schema import Config
    from raven.providers.base import LLMProvider
    from raven.session.manager import SessionManager


def build_attention_path(
    *,
    memory_store,
    session_manager,
    sentinel_cfg,
    provider,
    model: str,
    feedback,
    pending_store,
    routine_store,
    policy,
    now_fn=None,
):
    """Build the attention.md producer list + (optional) behaviors extractor.

    Shared by ``build_sentinel_stack`` and the ``sentinel tick`` / ``ticks``
    CLI commands so both code paths refresh attention.md / behaviors.md
    consistently. Without this shared helper, ``runner._refresh_memory_state``
    would see ``attention_updater is None`` and skip the writes silently.

    Returns ``(AttentionUpdater, BehaviorsExtractor | None)``. The extractor
    is None when ``sentinel_cfg.behaviors_extract.enabled`` is False.
    """
    from raven.proactive_engine.sentinel.attention_producers import (
        ActiveThreadsProducer,
        ArchivedPatternsProducer,
        CurrentlyFocusedProducer,
        PendingProposalsProducer,
        ProjectRhythmProducer,
        RecentlyAbandonedProducer,
        RecentProactiveDecisionsProducer,
        RejectedCooldownProducer,
        SentinelObservationsProducer,
    )
    from raven.proactive_engine.sentinel.attention_updater import (
        AttentionUpdater,
    )

    _kw = {"now_fn": now_fn} if now_fn is not None else {}
    producers = [
        PendingProposalsProducer(pending_store),
        RejectedCooldownProducer(pending_store),
        RecentProactiveDecisionsProducer(feedback),
        ActiveThreadsProducer(routine_store),
        RecentlyAbandonedProducer(
            routine_store,
            silence_days=sentinel_cfg.recently_abandoned.silence_days,
            abandon_days=sentinel_cfg.recently_abandoned.abandon_days,
        ),
        ArchivedPatternsProducer(routine_store),
        ProjectRhythmProducer(memory_store),
        CurrentlyFocusedProducer(memory_store, session_manager),
    ]
    if sentinel_cfg.write_observations_to_memory:
        producers.append(
            SentinelObservationsProducer(
                memory_store=memory_store,
                feedback=feedback,
                policy=policy,
                config=sentinel_cfg.sentinel_observations,
                **_kw,
            )
        )
    if sentinel_cfg.daily_analysis.enabled:
        from raven.proactive_engine.sentinel.attention_producers import (
            BehaviorPatternsProducer,
            Predicted3DProducer,
            StanceLogProducer,
        )
        from raven.proactive_engine.sentinel.predictor.daily_analysis import (
            DailyAnalysisService,
        )

        daily_analysis = DailyAnalysisService(
            memory_store=memory_store,
            routine_store=routine_store,
            session_manager=session_manager,
            provider=provider,
            config=sentinel_cfg.daily_analysis,
            model=sentinel_cfg.daily_analysis.model or model,
            **_kw,
        )
        producers.extend(
            [
                StanceLogProducer(
                    analysis=daily_analysis,
                    memory_store=memory_store,
                    config=sentinel_cfg.daily_analysis,
                ),
                Predicted3DProducer(analysis=daily_analysis),
                BehaviorPatternsProducer(analysis=daily_analysis),
            ]
        )
    # DailyPlanProducer is independent of DailyAnalysisService — runs
    # against the same LLM provider but produces a today-scoped fire
    # schedule. Skips silently when no provider is configured (cold
    # bootstraps before a model is wired).
    if provider is not None:
        from raven.proactive_engine.sentinel.attention_producers import (
            DailyPlanProducer,
        )

        producers.append(
            DailyPlanProducer(
                memory_store=memory_store,
                provider=provider,
                policy=policy,
                model=sentinel_cfg.daily_analysis.model or model,
                **_kw,
            )
        )
    attention_updater = AttentionUpdater(
        memory_store=memory_store,
        producers=producers,
        **_kw,
    )

    behaviors_extractor = None
    if sentinel_cfg.behaviors_extract.enabled:
        from raven.memory_engine.consolidate.behaviors_extractor import (
            BehaviorsExtractor,
        )

        behaviors_extractor = BehaviorsExtractor(
            memory_store=memory_store,
            session_manager=session_manager,
            provider=provider,
            config=sentinel_cfg.behaviors_extract,
            model=sentinel_cfg.behaviors_extract.model or model,
            **_kw,
        )
    return attention_updater, behaviors_extractor


def _migrate_legacy_feedback_log(legacy: "Path", new_path: "Path") -> None:
    """Move ``{ws}/sentinel_feedback.jsonl`` to ``{sentinel_dir}/feedback.jsonl``.

    The feedback ledger feeds NudgePolicy's user-level acceptance-rate
    model, so its natural home is alongside ``state.json`` in the sentinel
    dir. No-op when ``legacy`` is absent. Appends if both exist so
    engagement history survives migration.
    """
    if not legacy.is_file():
        return
    new_path.parent.mkdir(parents=True, exist_ok=True)
    if not new_path.exists():
        legacy.replace(new_path)
        return
    with legacy.open("rb") as src, new_path.open("ab") as dst:
        dst.write(src.read())
    legacy.unlink()


def build_sentinel_stack(
    config: "Config",
    sentinel_cfg: "SentinelConfig",
    session_manager: "SessionManager",
    provider: "LLMProvider",
    *,
    state_path: "Path | None" = None,
    now_fn=None,
    planner_provider: "LLMProvider | None" = None,
    planner_model: "str | None" = None,
    include_discover_triggers: bool = True,
):
    """Build the Sentinel stack if enabled in config.

    Returns ``(runner, response_modifier, on_user_inbound)`` — pass the
    latter two to ``AgentLoop`` at construction. When Sentinel is disabled
    returns ``(None, None, None)``.

    Call ``attach_sentinel_spawn(runner, agent)`` AFTER AgentLoop is
    constructed to finalize the ProactiveSpawn wiring.

    ``config`` is the base Config (has ``workspace_path`` + agent defaults);
    ``sentinel_cfg`` is the Sentinel feature block from RavenConfig. The
    two are kept separate because RavenConfig composes (rather than
    extends) base Config.

    ``state_path`` overrides the JsonStateStore file (default
    ``<sentinel_dir>/state.json``). Eval harnesses pass a per-persona path
    so parallel scenarios don't stomp on each other's cross-process state.
    """
    if not sentinel_cfg.enabled:
        return None, None, None

    # Local imports — Sentinel modules pull in heavy transitive deps
    # (memory store, routine learner); skip the cost when disabled.
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.proactive_engine.sentinel import (
        ContextAssembler,
        DeferManager,
        NudgeDispatcher,
        NudgeFeedbackTracker,
        NudgeInjector,
        NudgePolicy,
        ProactivePlanner,
        RoutineLearner,
        SentinelAssembly,
        SentinelRunner,
    )
    from raven.proactive_engine.sentinel.feedback.persistence import JsonStateStore

    # One JSON file, one lock, shared by the three state-carrying components.
    store = JsonStateStore(state_path or (get_sentinel_dir() / "state.json"))

    # Fake-clock injection for longrun eval — each "now"-reading component
    # routes through this callable. Defaults to real wall.
    _kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    policy = NudgePolicy(sentinel_cfg.nudge_policy, store=store, **_kwargs)
    injector = (
        NudgeInjector(
            ttl_seconds=sentinel_cfg.nudge_policy.inject_ttl_seconds,
            max_pending_per_session=sentinel_cfg.nudge_policy.inject_max_pending_per_session,
            store=store,
            **_kwargs,
        )
        if sentinel_cfg.inject_enabled
        else None
    )
    dispatcher = NudgeDispatcher(**_kwargs)
    defer_mgr = (
        DeferManager(
            dispatcher,
            lambda k: session_manager.get_or_create(k) if k else None,
            idle_threshold_seconds=sentinel_cfg.nudge_policy.defer_idle_threshold_seconds,
            max_wait_seconds=sentinel_cfg.nudge_policy.defer_max_wait_seconds,
            store=store,
            **_kwargs,
        )
        if sentinel_cfg.defer_enabled
        else None
    )

    memory_store = MemoryStore(config.workspace_path)
    learner = RoutineLearner(
        min_history_entries=sentinel_cfg.routine_min_history_entries,
    )
    assembler = ContextAssembler(
        memory_store=memory_store,
        session_manager=session_manager,
        routine_learner=learner,
        nudge_policy=policy,
        attention_planner_sections=sentinel_cfg.attention_planner_sections,
        behaviors_planner_window_days=sentinel_cfg.behaviors_planner_window_days,
        behaviors_planner_max_events=sentinel_cfg.behaviors_planner_max_events,
        **_kwargs,  # propagates now_fn for fake-clock alignment
    )
    # Planner LLM: three resolution paths, in priority order:
    #   1. Caller passed planner_provider explicitly (eval harness path)
    #   2. SentinelConfig.evaluator_base_url is set → build a separate
    #      provider just for the Planner (e.g. route Planner to OpenRouter
    #      while Agent stays on local qwen)
    #   3. Fall through to the Agent's main provider
    effective_planner_model = planner_model or sentinel_cfg.evaluator_model or config.agents.defaults.model
    if planner_provider is None and sentinel_cfg.evaluator_base_url:
        import os as _os

        from raven.providers.litellm_provider import LiteLLMProvider

        api_key = _os.environ.get(sentinel_cfg.evaluator_api_key_env or "")
        if not api_key:
            from loguru import logger as _logger

            _logger.warning(
                "sentinel.evaluator_base_url set but env {!r} is empty — Planner will fall back to default provider",
                sentinel_cfg.evaluator_api_key_env,
            )
        else:
            planner_provider = LiteLLMProvider(
                api_key=api_key,
                api_base=sentinel_cfg.evaluator_base_url,
                default_model=effective_planner_model,
                model_overrides=config.agents.defaults.model_overrides,
            )
    planner = ProactivePlanner(
        provider=planner_provider or provider,
        model=effective_planner_model,
    )
    feedback_dir = state_path.parent if state_path else get_sentinel_dir()
    feedback_path = feedback_dir / "feedback.jsonl"
    if state_path is None:
        _migrate_legacy_feedback_log(
            config.workspace_path / "sentinel_feedback.jsonl",
            feedback_path,
        )
    feedback = NudgeFeedbackTracker(feedback_path)
    feedback.load()

    # PendingDecisionStore + RoutineStore are always built so the
    # decision_consumer hook in AgentLoop can attach unconditionally AND
    # so the attention.md producers (Active threads / Archived patterns /
    # etc.) have a backing store even when task_discovery_enabled is off
    # (they just render empty in that case).
    from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
    from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore

    pending_store_path = (state_path.parent if state_path else get_sentinel_dir()) / "pending_decisions.json"
    pending_store = PendingDecisionStore(pending_store_path)
    routine_store_path = (state_path.parent if state_path else get_sentinel_dir()) / "routines.json"
    routine_store = RoutineStore(routine_store_path)

    routine_aggregator = None
    task_discoverer = None
    task_discovery_targets: list[tuple[str, str]] = []

    # Parsed unconditionally: these are the proactive delivery targets, not
    # just the discovery-menu fan-out. Daily-plan / deadline nudges target
    # the internal sentinel:direct session and resolve through these via
    # _resolve_nudge_targets. Gating the parse on task_discovery_enabled
    # left the list empty and silently dropped every such nudge whenever the
    # menu was off (the default state). Three forms:
    #   "channel:chat_id" → (channel, chat_id)
    #   "channel"         → (channel, "")  — chat_id resolved at fire time
    #   "*"               → ("*", "")      — broadcast, expanded by runner
    # Empty or whitespace-only entries are skipped with a warning so
    # a config typo doesn't kill the whole stack.
    from loguru import logger as _logger

    for raw in sentinel_cfg.task_discovery_targets:
        entry = raw.strip()
        if not entry:
            _logger.warning(
                "Sentinel: empty task_discovery_targets entry — skipping",
            )
            continue
        if entry == "*":
            task_discovery_targets.append(("*", ""))
            continue
        if ":" in entry:
            ch, chat_id = entry.split(":", 1)
            if not ch or not chat_id:
                _logger.warning(
                    "Sentinel: task_discovery_target {!r} missing channel or chat_id around ':' — skipping",
                    raw,
                )
                continue
            task_discovery_targets.append((ch, chat_id))
        else:
            task_discovery_targets.append((entry, ""))

    if sentinel_cfg.task_discovery_enabled:
        from raven.proactive_engine.sentinel.predictor.routine_aggregator import RoutineAggregator
        from raven.proactive_engine.sentinel.predictor.task_discoverer import TaskDiscoverer

        routine_aggregator = RoutineAggregator(
            provider=planner_provider or provider,
            model=effective_planner_model,
            routine_store=routine_store,
        )
        routine_validator = None
        if sentinel_cfg.routine_validation_enabled:
            from raven.proactive_engine.sentinel.predictor.routine_validator import RoutineValidator

            routine_validator = RoutineValidator(
                provider=planner_provider or provider,
                model=sentinel_cfg.routine_validation_model or effective_planner_model,
            )
        task_discoverer = TaskDiscoverer(
            memory_store=memory_store,
            pending_store=pending_store,
            dispatcher=dispatcher,
            provider=planner_provider or provider,
            model=effective_planner_model,
            context_assembler=assembler,
            routine_store=routine_store,
            routine_learner=learner,
            routine_aggregator=routine_aggregator,
            routine_validator=routine_validator,
            feedback=feedback,
            policy=policy,  # share NudgePolicy w/ reactive nudges
            routine_half_life_days=sentinel_cfg.routine_recency_half_life_days,
            max_options=sentinel_cfg.task_discovery_max_options,
            decision_ttl_min=sentinel_cfg.task_discovery_decision_ttl_min,
            validator_confidence_floor=sentinel_cfg.routine_validation_confidence_floor,
            **_kwargs,
        )

    attention_updater, behaviors_extractor = build_attention_path(
        memory_store=memory_store,
        session_manager=session_manager,
        sentinel_cfg=sentinel_cfg,
        provider=planner_provider or provider,
        model=effective_planner_model,
        feedback=feedback,
        pending_store=pending_store,
        routine_store=routine_store,
        policy=policy,
        now_fn=now_fn,
    )

    # File-backed inbox for CLI-queued ``discover-now`` triggers. Only
    # the gateway should drain this — REPL agent has no real channel
    # adapters, so if it consumes a feishu trigger it can't dispatch
    # the menu and the trigger is wasted (a real bug seen in the wild
    # when REPL and gateway both poll the shared default sentinel_dir).
    if include_discover_triggers:
        from raven.proactive_engine.sentinel.discover_triggers import (
            DiscoverTriggerStore,
        )

        discover_trigger_store = DiscoverTriggerStore(
            (state_path.parent if state_path else get_sentinel_dir()) / "discover_triggers.json"
        )
    else:
        discover_trigger_store = None

    runner = SentinelRunner(
        planner=planner,
        assembler=assembler,
        policy=policy,
        dispatcher=dispatcher,
        injector=injector,
        defer_manager=defer_mgr,
        spawn=None,  # set via attach_sentinel_spawn after AgentLoop is built
        feedback=feedback,
        attention_updater=attention_updater,
        behaviors_extractor=behaviors_extractor,
        task_discoverer=task_discoverer,
        task_discovery_time=sentinel_cfg.task_discovery_time,
        task_discovery_targets=task_discovery_targets,
        session_manager=session_manager,  # auto-resolve chat_id + broadcast lookup
        discover_trigger_store=discover_trigger_store,
        enabled=sentinel_cfg.enabled,
        interval_s=sentinel_cfg.tick_interval_seconds,
        deadline_outage_fallback=sentinel_cfg.deadline_outage_fallback,
        store=store,  # share engagement state across subprocesses
        **_kwargs,  # propagates now_fn so daily gate sees fake-clock
    )
    # Handed over for the post-AgentLoop attach step, on the runner so callers
    # do not thread five values through by hand.
    runner.assembly = SentinelAssembly(
        pending_store=pending_store,
        routine_store=routine_store,
        planner_provider=planner_provider or provider,
        planner_model=effective_planner_model,
        now_fn=now_fn,
    )

    response_modifier: Callable[[str, str], str] | None = injector
    on_user_inbound = runner.on_user_inbound
    return runner, response_modifier, on_user_inbound


def attach_sentinel_spawn(runner, agent: "AgentLoop") -> None:
    """Wire ProactiveSpawn once AgentLoop exists (circular-dep resolution)."""
    if runner is None:
        return
    from raven.proactive_engine.sentinel import ProactiveSpawn

    runner.spawn = ProactiveSpawn(agent.subagents, runner.policy)


def attach_sentinel_decision_consumer(
    runner,
    agent: "AgentLoop",
    *,
    sentinel_cfg: "SentinelConfig",
) -> None:
    """Wire DecisionRouter + ActionExecutor + DecisionConsumer once
    AgentLoop exists. Sets ``agent.decision_consumer`` so AgentLoop's
    _process_message hook short-circuits user replies into the menu
    pipeline.

    Depends on ``runner.assembly``, set by build_sentinel_stack. No-op when
    sentinel/runner is None."""
    if runner is None:
        return
    assembly = getattr(runner, "assembly", None)
    if assembly is None or assembly.pending_store is None:
        return  # build_sentinel_stack didn't run / pending store not wired
    pending_store = assembly.pending_store

    from loguru import logger as _logger

    from raven.proactive_engine.sentinel.executor.action_executor import ActionExecutor
    from raven.proactive_engine.sentinel.executor.decision_consumer import DecisionConsumer
    from raven.proactive_engine.sentinel.executor.decision_router import DecisionRouter

    now_fn = assembly.now_fn
    _kwargs = {"now_fn": now_fn} if now_fn is not None else {}

    planner_provider = assembly.planner_provider
    planner_model = assembly.planner_model

    # Health check: require_confirm=True without an LLM provider
    # works for clear yes/no ("yes" / "confirm" / "/cancel") via regex
    # but ambiguous replies fall through silently (decision stays
    # awaiting until TTL). Surface this so operators know.
    if sentinel_cfg.task_discovery_require_confirm and (planner_provider is None or planner_model is None):
        _logger.warning(
            "Sentinel: task_discovery_require_confirm=True but no LLM "
            "provider/model configured for DecisionRouter — only "
            "deterministic yes/no regex (`yes`/`确认`/`no`/`取消`/etc.) "
            "will work; ambiguous user replies will fall through to "
            "normal conversation and the decision will stay awaiting "
            "until the TTL expires. Configure sentinel.evaluator_base_url"
            " or set task_discovery_require_confirm=False to silence "
            "this warning."
        )

    router = DecisionRouter(
        pending_store=pending_store,
        provider=planner_provider,
        model=planner_model,
        **_kwargs,
    )
    executor = ActionExecutor(
        routine_store=assembly.routine_store,
        cron_service=agent.cron_service,
        tool_registry=agent.tools,
        subagent_manager=agent.subagents,
        **_kwargs,
    )
    consumer = DecisionConsumer(
        router=router,
        executor=executor,
        pending_store=pending_store,
        feedback=runner.feedback,
        require_confirm=sentinel_cfg.task_discovery_require_confirm,
        **_kwargs,
    )
    agent.decision_consumer = consumer
    # AgentLoop's __init__ wired the hook chain from its constructor
    # kwargs, but ``decision_consumer`` hadn't been built yet (it needs
    # agent.tools / agent.subagents for ActionExecutor), so the
    # construction-time ``if decision_consumer is not None`` check
    # short-circuited. Without this post-hoc append, user replies to
    # discovery menus would skip the PendingDecisionStore bookkeeping
    # and decisions would stay ``pending`` until TTL expiry.
    # Idempotent: a re-attach (fixture reuse, hot-reload, double-wire
    # by a future caller) must not append twice — a duplicate adapter
    # would run the menu pipeline twice per user inbound and fire
    # ActionExecutor for every pick a second time.
    from raven.agent.hook.adapters import DecisionConsumerAdapter

    if not any(isinstance(h, DecisionConsumerAdapter) for h in agent.hooks):
        agent.hooks.append(DecisionConsumerAdapter(consumer))


def build_wake(hb_cfg: Any, *, is_busy: Callable[[], bool]) -> tuple[Any, Any]:
    """The event-wake pair for heartbeat: ``(wake, system_events)``, or
    ``(None, None)`` when event wake is off.

    In-process producers (cron completions, missed-reminder observers) can end
    the heartbeat sleep early instead of waiting for the next interval; the busy
    check covers spine-dispatched turns only -- exactly the lane a wake must
    never compete with.
    """
    if not hb_cfg.event_wake:
        return None, None
    from raven.proactive_engine.system_events import SystemEventQueue
    from raven.proactive_engine.wake import WakeScheduler

    return WakeScheduler(is_busy=is_busy, min_interval_s=hb_cfg.event_wake_min_interval_s), SystemEventQueue()


def build_heartbeat(
    config: Any,
    provider: Any,
    *,
    model: str,
    on_execute: Callable[[str], Awaitable[str]],
    wake: Any,
    system_events: Any,
) -> Any:
    """The heartbeat service over the host's submit closure. Deliver-only: the
    hub already delivered the reply, so ``on_notify`` stays None."""
    from raven.proactive_engine.schedulers.heartbeat.service import HeartbeatService

    hb_cfg = config.gateway.heartbeat
    return HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=model,
        on_execute=on_execute,
        on_notify=None,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        wake=wake,
        system_events=system_events,
    )


__all__ = [
    "build_sentinel_stack",
    "attach_sentinel_spawn",
    "attach_sentinel_decision_consumer",
    "build_heartbeat",
    "build_wake",
]
