"""Raven feature configuration — extends the base Config with 4 feature blocks.

Usage:
    from raven.config import RavenConfig, load_raven_config

    cfg = load_raven_config()
    if cfg.context.engine == "curator":
        ...

Design:
    - ``RavenConfig`` composes the base ``Config`` rather than subclassing
      it. This keeps the base schema untouched and lets us add / remove
      feature blocks without breaking the base loader.
    - Each feature block has its own Pydantic model. Defaults are
      conservative: every novel feature starts OFF so a fresh install behaves
      like the base agent until features are enabled.
"""

from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from raven.config.loader import (
    EXTENSION_KEYS,
    _migrate_config,
    get_config_path,
)
from raven.config.loader import load_config as load_base_config
from raven.config.schema import Config as BaseConfig


class _Base(BaseModel):
    """Accepts both camelCase and snake_case keys.

    ``extra='forbid'`` catches typos at startup. Retired fields with
    known legacy presence are stripped explicitly in
    ``loader._migrate_config`` before Pydantic validates; unlisted
    unknown keys still raise.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# Feature 1 — Context Management (Curator)
# ---------------------------------------------------------------------------


class ContextConfig(_Base):
    """Context engine selection and tuning."""

    engine: str = "unified"
    """Deprecated — there is now a single :class:`ContextAssembler`.

    The historical ``"legacy"`` / ``"curator"`` / ``"default"`` split was
    collapsed: every turn runs the Curator history lane + the EverOS
    recall / SkillForgeRouter lanes in one engine. The field is retained (as a
    free string) so existing YAML setting ``engine: legacy`` etc. still
    loads — the value is ignored by ``build_context_engine``.
    """

    # Curator history-lane knobs.
    fast_path_threshold: float = 0.60
    """Curator Fast Path cutoff. Below this % of budget → zero-LLM pass-through."""

    curator_model: str = "gemini-2.5-flash"
    """Model used by the Curator agent loop (Slow Path). Kept small & fast."""

    curator_timeout_seconds: float = 30.0
    """Max wall time for one Curator slow-path invocation before fallback."""

    relevance_decay: float = 0.95
    """Per-turn decay factor for non-recent message relevance."""

    relevance_reference_boost: float = 0.15
    """Boost applied when assistant response references older message content."""

    protect_first_n: int = 3
    """Number of head exchanges always preserved in context."""

    archive_dir: str = "memory/.curator/archive"
    """Relative path under workspace for lossless message archives."""


# ---------------------------------------------------------------------------
# Feature 2 — Proactivity (Sentinel)
# ---------------------------------------------------------------------------


class DndWindow(_Base):
    """One Do-Not-Disturb window. Matches when current time falls inside
    [start, end) on a matching weekday — start/end are minute-precise so
    callers can express e.g. "12:00–13:30 weekday lunch + 30min spillover".

    Used to express persona-specific quiet windows beyond the global
    ``quiet_hours`` (e.g. weekday lunch break, kid pickup hour, weekend
    morning sleep-in). Tighten-only — DND can never override an active
    nudge that the global quiet_hours allowed.

    The start_minute / end_minute fields default to 0 so old yaml using
    only start_hour / end_hour keeps working.
    """

    start_hour: int  # 0-23
    end_hour: int  # 1-24 (24 = end-of-day; wraps to next day if < start_hour)
    start_minute: int = 0  # 0-59
    end_minute: int = 0  # 0-59
    weekdays: list[int] | None = None
    """List of 0=Mon .. 6=Sun. None means every day."""
    why: str = ""
    """Free-form label for logs / debugging."""

    def matches(self, now_hour: int, now_minute: int, now_weekday: int) -> bool:
        """True iff (now_weekday, now_hour, now_minute) falls inside this DND."""
        if self.weekdays is not None and now_weekday not in self.weekdays:
            return False
        cur = now_hour * 60 + now_minute
        start = self.start_hour * 60 + self.start_minute
        end = self.end_hour * 60 + self.end_minute
        if start == end:
            return False
        if start < end:
            return start <= cur < end
        # wraps midnight (e.g. 22:00 - 02:00)
        return cur >= start or cur < end


class NudgePolicyConfig(_Base):
    """Anti-spam policy for proactive nudges.

    Covers all three nudge action types (plain / inject / defer). Executors
    call NudgePolicy.check(...) before dispatch; NudgePolicy enforces these
    limits in one place so tuning is centralized.
    """

    # Per-window quotas
    max_nudges_per_hour: int = 3
    max_nudges_per_day: int = 10

    # L4 cold-start multiplier on ``max_nudges_per_hour``. NudgePolicy
    # starts at this value; ``apply_adaptive_tuning()`` later moves it
    # within [0.2, 1.5] from observed acceptance rate. Effective hourly
    # cap = max(1, int(max_nudges_per_hour * hour_quota_multiplier)).
    # Lower (e.g. 0.5) for conservative cold-start; 1.0 = trust the
    # base cap until feedback accumulates.
    hour_quota_multiplier: float = 1.0

    # L6 weekend tightener — multiplied ON TOP of hour_quota_multiplier
    # on Saturdays/Sundays. 0.5 ≈ 30% weekend/weekday ratio after the
    # int-floor. Set 1.0 to disable weekend tightening.
    weekend_quota_multiplier: float = 0.5

    # L7 weekend discretionary cap — max per weekend-day fires of tagged,
    # non-routine topics (deadline_/weekly_/... but NOT routine_/medication_/
    # daily_, and NOT anonymous/untagged reactive fires). 0 = disabled
    # (default): no weekend-specific suppression. Set N>0 to cap.
    weekend_discretionary_cap: int = 0

    # Per-session cooldown — minimum gap between any two nudges on the same session
    min_interval_seconds: int = 300

    # Quiet hours — 24h tuple (start_hour, end_hour); nudges outside high priority suppressed
    quiet_hours: tuple[int, int] = (23, 7)  # (start_hour_24, end_hour_24), local time

    # Per-persona Do-Not-Disturb windows — additional quiet bands beyond
    # the global quiet_hours. Each window is matched only on its weekdays
    # list (None = every day). High priority can still bypass these (same
    # rule as quiet_hours).
    do_not_disturb_windows: list[DndWindow] = Field(default_factory=list)

    # Dismissal cooldown — after user dismisses on a session, suppress nudges for this long
    cooldown_on_dismiss_seconds: int = 1800

    # Ignore window — a delivered nudge with no engagement after this long is
    # swept into a (soft) IGNORED signal so L2/L5 learn from silence, not just
    # explicit dismissals. 6h: long enough to not misread "not seen yet".
    ignore_window_seconds: int = 21600

    # Priority bypass — priority=high can bypass hour quota and quiet hours (but not day quota or cooldown)
    high_priority_bypasses_limits: bool = True

    # Content dedup — hash the nudge_message; reject duplicates within this window
    dedup_window_seconds: int = 86400  # 24h

    # Memory-loading filter (smart loading): controls how ContextAssembler
    # builds the memory_md slice of the Planner prompt. Defaults are pure
    # passthrough; users opt in via config when MEMORY.md grows large
    # enough that token cost / signal-to-noise becomes a concern.
    memory_section_allowlist: list[str] | None = None
    """If set, only sections whose H2 title is in this list (or in the
    always-keep priority set) are included in the Planner prompt. None
    means include all sections (default behavior)."""

    memory_section_blocklist: list[str] = Field(default_factory=list)
    """H2 titles to drop. Applied AFTER the allowlist (defense in depth).
    Always-keep priority sections (Sentinel Observations / Proactivity
    Preferences / User Information / Important Notes) are immune to the
    blocklist. Empty by default."""

    memory_max_chars: int = 0
    """If > 0 and the filtered memory exceeds this byte length, priority-
    truncate by dropping non-priority sections from the tail. 0 (default)
    means no cap."""

    # Per-topic quota — Planner returns topic_tag (e.g. "deadline_clawtrack");
    # if the same tag has fired more than the cap in the rolling window, deny.
    # Three layers stack:
    #   - hour:  catches alternating-topic burst within a single hour
    #   - day:   stops "anniversary daily countdown" spam
    #   - week:  caps slow-burn topics (book reading reminder, fitness goal)
    # Set any cap to 0 to disable that layer.
    max_per_topic_per_window: int = 1  # legacy alias for max_per_topic_per_hour
    topic_dedup_window_seconds: int = 3600  # 1h
    max_per_topic_per_day: int = 2
    # Weekly cap raised 4 → 8: deadline reminders (clawtrack 5/12-5/14,
    # birthday 5/20-5/24) need ~1 fire every 1-2 days across the
    # "deadline approaches" stretch; old cap=4 burned through the budget
    # in the first 3-4 days then denied all in-window fires that the
    # type_a scorer was looking for. 8 still bans daily-for-a-week spam
    # but allows the natural pre-deadline cadence.
    max_per_topic_per_week: int = 8

    # NudgeInjector settings
    inject_ttl_seconds: int = 1800  # 30min — inject queued longer than this is stale
    inject_max_pending_per_session: int = 3  # cap queue growth

    # DeferManager settings
    defer_idle_threshold_seconds: int = 300  # session must be idle this long before defer fires
    defer_max_wait_seconds: int = 86400  # 24h — give up if never settled


# Single source of truth for the Planner's attention.md section allowlist.
# Both ``SentinelConfig.attention_planner_sections`` and ``ContextAssembler``'s
# no-config fallback reference this, so a deploy that sets the config and one
# that relies on the fallback can't silently drift to different section sets.
DEFAULT_PLANNER_ATTENTION_SECTIONS: tuple[str, ...] = (
    "## Pending proposals",
    "## Rejected proposals (cooldown)",
    "## Recent stance log (30d)",
    "## Predicted next 3 days",
    "## Currently focused on",
    "## Recent proactive decisions (14d)",
    "## 今日 fire 计划",
)


class SentinelConfig(_Base):
    """Sentinel proactivity configuration."""

    enabled: bool = False
    """Master switch — nothing runs until this is True."""

    tick_interval_seconds: int = Field(default=1800, ge=60)
    """SentinelRunner background tick cadence (seconds). Default 30 min
    keeps production cost predictable. Lower (e.g. 60) for end-to-end
    integration testing, eval harnesses, or live debugging — every tick
    is one Planner LLM call, so be deliberate. Floor at 60s to prevent
    foot-guns; sub-minute ticks burn Planner LLM budget faster than the
    inbound window has anything new to plan against."""

    evaluator_model: str | None = None
    """Model for the Slow-Path LLM Evaluator (= ProactivePlanner).
    ``None`` (default) → inherit ``agents.defaults.model`` so a fresh
    deploy runs without extra setup. Set explicitly (e.g.
    ``"gemini-2.5-flash"``) when you want a cheaper / faster Planner
    profile decoupled from the Agent's main model — Planner fires every
    tick, so a smaller model often makes sense in high-volume deploys."""

    evaluator_base_url: str | None = None
    """Optional base URL override for Planner LLM. When set together with
    ``evaluator_model``, build_sentinel_stack creates a separate provider
    just for the Planner (independent from the Agent's main provider).
    Use to route Planner to a different backend, e.g.::

        evaluator_model: "openrouter/anthropic/claude-sonnet-4.5"
        evaluator_base_url: "https://openrouter.ai/api/v1"
        evaluator_api_key_env: "OPENROUTER_API_KEY"

    None (default) → Planner uses the same provider as the Agent."""

    evaluator_api_key_env: str = "OPENAI_API_KEY"
    """Env var name to read for the Planner provider's API key. Only
    consulted when ``evaluator_base_url`` is set. Defaults to OPENAI_API_KEY
    so any litellm-compatible OpenAI-style endpoint works without extra config."""

    evaluator_timeout_seconds: float = 3.0
    """Evaluator times out → defaults to SKIP (do not nudge)."""

    write_observations_to_memory: bool = True
    """When True, the ``SentinelObservationsProducer`` participates in the
    AttentionUpdater run and writes the ``## Sentinel Observations (auto)``
    section into ``<workspace>/user_memory/attention.md`` once per
    ``SentinelObservationsConfig.cooldown_hours``. Surfaces the diagnostic
    counts (7d signal mix, top fired topics, adaptive multiplier) so the
    user can see what Sentinel has been learning.

    Default ON: cooldown + min-feedback gate keep churn negligible, and
    all attention.md producers share one fcntl lock so concurrent writers
    are already serialized."""

    nudge_policy: NudgePolicyConfig = Field(default_factory=NudgePolicyConfig)

    # Per-executor feature flags — disable if you want Planner to emit these
    # actions but skip their execution (benchmark mode, rollout staging).
    inject_enabled: bool = True
    """Allow NudgeInjector to apply response_modifier; False → inject decisions degrade to plain nudge."""
    defer_enabled: bool = True
    """Allow DeferManager to hold pending defers; False → defer decisions degrade to plain nudge."""

    deadline_outage_fallback: bool = True
    """When the Planner LLM is unavailable, blind-fire a high-priority
    ``deadline_*`` fire-plan slot due now instead of skipping it. Restores the
    pre-defer fast-fire robustness for hard deadlines during an outage (the
    Planner can't run to check completion, so this is scoped to priority=high
    to keep the blind re-nag exposure minimal; the fire still passes the normal
    policy gates). Set False to stay silent on any Planner outage."""

    idle_threshold_seconds: int = 1800
    """IdleMonitor trigger threshold."""

    workspace_watch_paths: list[str] = Field(default_factory=list)
    """Paths WorkspaceMonitor watches. Empty = disabled."""

    workspace_ignore_patterns: list[str] = Field(
        default_factory=lambda: [
            "**/node_modules/**",
            "**/.git/**",
            "**/__pycache__/**",
            "**/*.lock",
            "**/.DS_Store",
        ]
    )

    # ── TaskDiscoverer — daily intent-detection menu ───────────────────
    # All defaults OFF; opt-in per deploy.

    task_discovery_enabled: bool = False
    """When True, SentinelRunner runs TaskDiscoverer once per local day at
    ``task_discovery_time`` to generate a menu of 3-4 actionable task
    suggestions and dispatch it to the user via the daily-active channel.

    Independent of ``write_observations_to_memory`` / nudge_policy quotas;
    discovery uses the same NudgePolicy fired-count gate to avoid stacking
    on top of reactive nudges in the same hour."""

    task_discovery_time: str = "08:00"
    """HH:MM 24-hour local time for the daily discovery batch. ±30min
    precision (next sentinel tick after the target time fires)."""

    task_discovery_max_options: int = 4
    """Maximum option count in the discovery menu. 3-4 is the sweet spot;
    > 4 yields user fatigue, < 3 yields low value per ping."""

    task_discovery_decision_ttl_min: int = 60
    """How long a PendingDecision stays consumable before expiring. Past
    TTL the menu is dropped silently and the next morning surfaces a fresh
    one."""

    task_discovery_require_confirm: bool = True
    """If True, ActionExecutor asks the user a yes/no confirmation before
    actually executing the picked option. False → execute immediately on
    pick (only enable for low-stakes actions)."""

    routine_recency_half_life_days: int = 14
    """Half-life for RoutineLearner.learn_with_decay weight. 14 means a
    routine entry from 2 weeks ago counts half as much as today's. Used
    to prioritize fresh habits over stale ones."""

    task_discovery_targets: list[str] = Field(default_factory=list)
    """Targets for the daily discovery menu. Empty (default) → no targets,
    daily batch is a no-op even if ``task_discovery_enabled=True``.

    Three accepted forms per entry:

    - ``"channel:chat_id"`` — explicit pair, e.g. ``"feishu:ou_xxxxx"``.
      Pass-through delivery; chat_id is the per-platform stable id.
    - ``"channel"`` — channel only; the runner resolves the chat_id at
      fire time via ``SessionManager.find_most_recent_chat_id(channel)``.
      Convenient when the user has only one chat per channel.
    - ``"*"`` — broadcast: at fire time expand to every channel in
      ``ChannelManager.enabled_channels`` and auto-resolve each chat_id.

    Channels referenced literally (``cli:dev`` / ``tui``) are taken at
    face value — when run under a gateway that has no adapter for them,
    they are logged + skipped, not silently forwarded. Use ``"*"`` or a
    real channel name (``"feishu"``) for broadcast intent."""

    routine_validation_enabled: bool = False
    """When True, TaskDiscoverer runs an LLM verdict on each newly-merged
    candidate routine before surfacing it. Verdicts are cached
    per-routine in routine_store so cost is paid at most once per
    routine_id. Off by default — opt-in per deploy."""

    routine_validation_confidence_floor: float = 0.6
    """Minimum LLM confidence (0-1) required for a validated candidate
    to be surfaced. Below this, the routine stays in store but does not
    appear in the daily discovery menu. Only applies to candidates with
    an attached llm_validation; unvalidated candidates pass through."""

    routine_validation_model: str | None = None
    """Model name for routine validation. None (default) → inherits the
    Planner model. Set to a cheaper tier (e.g. "claude-haiku-4-5") when
    validation runs at high volume — the call is short and deterministic,
    so a smaller model is usually adequate."""

    routine_min_history_entries: int = 10
    """RoutineLearner skips learning when HISTORY.md has fewer than this
    many parseable entries — below this floor the deterministic binning
    is statistically meaningless and we waste CPU/log noise. Lower for
    short-lived test users; raise for production where < 10 entries is
    too sparse to surface anything useful."""

    behaviors_extract: "BehaviorsExtractConfig" = Field(
        default_factory=lambda: BehaviorsExtractConfig(),
    )
    """Idle-triggered LLM extractor that synthesizes BehaviorEvents from
    session JSONL files into ``user_memory/behaviors.md`` (append-only).
    Default OFF; see ``BehaviorsExtractConfig`` for tuning."""

    daily_analysis: "DailyAnalysisConfig" = Field(
        default_factory=lambda: DailyAnalysisConfig(),
    )
    """Daily LLM bundle producing three attention.md sections from one
    call: Recent stance log (30d) / Predicted next 3 days /
    Cross-project behavior patterns (14d). Service caches the structured
    result for ``cooldown_hours``; the three producer classes each
    render one view from the cache, so cost = 1 LLM call/day even
    though three sections benefit. Default OFF."""

    sentinel_observations: "SentinelObservationsConfig" = Field(
        default_factory=lambda: SentinelObservationsConfig(),
    )
    """Tuning knobs for the ## Sentinel Observations (auto) diagnostic
    section — feedback threshold + 24h rewrite cooldown."""

    recently_abandoned: "RecentlyAbandonedConfig" = Field(
        default_factory=lambda: RecentlyAbandonedConfig(),
    )
    """Time windows for the ## Recently abandoned, worth resuming
    section. Routines silent ``silence_days``-``abandon_days`` ago
    qualify; past abandon_days drops out."""

    # ── PlannerContext input shaping ─────────────────────────────────

    behaviors_planner_window_days: int = 14
    """How many days of behaviors.md events to include in PlannerContext.
    14d gives the Planner enough recency to spot behavior shifts without
    inflating the prompt with month-old events."""

    behaviors_planner_max_events: int = 100
    """Safety cap on event count fed to Planner — when the window
    contains more events than this, keep the most-recent N and drop the
    older ones. Prevents prompt blow-up on heavy-activity weeks."""

    attention_planner_sections: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PLANNER_ATTENTION_SECTIONS),
    )
    """attention.md sections to surface in PlannerContext. Default is the 7
    decision-relevant sections — incl. the daily fire plan so the Planner sees
    today's scheduled deadline slots (one-shot deadlines defer to it) and can
    fire / suppress them with full context. Skips long-term pattern sections
    (Active threads / Archived / Project rhythm / Cross-project behavior
    patterns) which behaviors.md already feeds in folded form, plus the
    Sentinel Observations diagnostic which Planner can read off
    NudgePolicy directly."""


class BehaviorsExtractConfig(_Base):
    """Idle-triggered LLM extractor producing ``user_memory/behaviors.md``.

    Reads ``{ws}/sessions/<channel>_<chat_id>.jsonl`` from a per-session
    cursor (``user_memory/.behaviors_offsets.json``), asks the LLM to emit
    structured BehaviorEvent records for each distinct user-agent
    interaction worth remembering, and appends them under a day H2.

    Append-only — never mutates already-written events. On crash between
    append and offset save, the next run re-extracts the same tail and
    appends near-duplicate events; failure mode chosen over silent data
    loss. No automated dedup — operators edit behaviors.md manually if
    duplicates accumulate.
    """

    enabled: bool = False
    """Master switch — even with ``sentinel.enabled`` on, the extractor
    only runs when this is True. Two failure modes drive default-off:
    each idle tick costs one LLM call; behaviors.md is observable-only
    in MVP (P6 wires reads but Phase-2 behavior is still in flux)."""

    idle_seconds: int = 900
    """Min consecutive idle time (no inbound across any session) before
    the next extraction is allowed to fire. 15 min default trades 'still
    typing a long message' false-positives against late-night ticks."""

    cooldown_hours: int = 12
    """Min hours between two extraction passes regardless of idle. Caps
    LLM spend to ≤ 2 calls/day under continuous-idle pathological cases."""

    min_segment_messages: int = 5
    """Skip a session whose unprocessed message tail is shorter than
    this — below the floor the LLM either invents events or returns
    nothing, both wasteful."""

    abort_on_inbound: bool = False
    """If True, a new inbound message during an active extraction cancels
    the LLM call. Default False — LLM extractions take ~2-5s and aborting
    means losing that work; let it finish and offset advance the next
    tick. Set True only on extreme latency-sensitive deploys."""

    max_messages_per_call: int = 60
    """Cap on messages handed to a single LLM call. Sessions longer than
    this are chunked into multiple calls, each call advancing the offset
    independently. Prevents single-call cost spikes on marathon sessions."""

    model: str | None = None
    """Model for extraction. None → inherits ``SentinelConfig.evaluator_model``.
    Set to a cheaper tier for high-volume deploys."""

    @model_validator(mode="after")
    def _check_chunk_consistency(self) -> "BehaviorsExtractConfig":
        if self.max_messages_per_call < self.min_segment_messages:
            raise ValueError(
                "behaviors_extract: max_messages_per_call "
                f"({self.max_messages_per_call}) must be ≥ "
                f"min_segment_messages ({self.min_segment_messages}) — "
                "otherwise the per-call cost cap is bypassed at "
                "extraction time.",
            )
        return self


class DailyAnalysisConfig(_Base):
    """One LLM call/day producing the three attention.md sections that
    share the same '14d episodes + active routines + recent inbound'
    context: Recent stance log (30d), Predicted next 3 days, and
    Cross-project behavior patterns (14d).

    Service caches the structured result for ``cooldown_hours``; each
    of the three producers renders one view from the cache (no
    per-section LLM calls).
    """

    enabled: bool = False
    """Master switch. When False the three producers are not registered
    at all; the corresponding attention.md sections stay empty."""

    cooldown_hours: int = 24
    """Min hours between LLM calls. Defaults to 24 — daily cadence."""

    episodes_window_days: int = 14
    """How many days of episodes.md tail to feed the LLM. 14d gives
    enough history for cross-project behavior patterns without
    blowing the token budget."""

    inbound_window_hours: int = 24
    """Last N hours of session inbound messages to scan for stance
    detection. 24h aligns with the daily cadence so each inbound is
    seen at most once."""

    max_episodes: int = 200
    """Cap on episode lines fed to the LLM. Recent first; older entries
    dropped silently."""

    max_inbound_messages: int = 80
    """Cap on inbound messages fed to the LLM. Most-recent first."""

    stance_max_keep: int = 30
    """FIFO cap on the ``## Recent stance log (30d)`` section. Older
    entries fall off the front so the section stays scannable."""

    enable_prefix_fallback: bool = True
    """When True and the LLM call returns no stance entries (or fails),
    fall back to a cheap prefix heuristic over the inbound window
    (``i prefer / stop / avoid / always / from now on / ...``). Default
    on — recall is more important than precision here."""

    model: str | None = None
    """Model for the daily analysis call. None → inherits
    ``SentinelConfig.evaluator_model``."""

    stance_prefix_fallback: list[str] = Field(
        default_factory=lambda: [
            "i prefer",
            "i like",
            "i don't like",
            "stop ",
            "avoid ",
            "never ",
            "always ",
            "i want ",
            "from now on",
            "going forward",
            "请",
            "别",
            "不要",
            "应该",
            "总是",
            "永远",
        ]
    )
    """Prefix list scanned against the inbound window when the LLM call
    fails and ``enable_prefix_fallback`` is True. Lowercase-matched;
    extend to support more languages or domain-specific stance verbs."""


class SentinelObservationsConfig(_Base):
    """Knobs for the ## Sentinel Observations (auto) diagnostic section."""

    min_feedback: int = 3
    """Skip the section refresh when fewer than this many ``dispatched``
    events sit in the in-memory feedback window — nothing aggregatable
    yet at cold-start."""

    cooldown_hours: int = 24
    """Don't rewrite the section more than once per this many hours.
    The cooldown is anchored on a ``<!-- last_updated=ISO -->`` cookie
    inside the section body, so it survives process restarts."""


class RecentlyAbandonedConfig(_Base):
    """Time windows for the Recently abandoned, worth resuming section."""

    silence_days: int = 7
    """Routine must be silent for at least this many days to qualify."""

    abandon_days: int = 30
    """Routine past this many days of silence drops out of the resume
    bucket (presumed fully archived). Should be ≥ ``silence_days``."""

    @model_validator(mode="after")
    def _check_ordering(self) -> "RecentlyAbandonedConfig":
        if self.abandon_days <= self.silence_days:
            raise ValueError(
                "recently_abandoned: abandon_days "
                f"({self.abandon_days}) must be > silence_days "
                f"({self.silence_days}) — otherwise the resume window "
                "collapses to empty.",
            )
        return self


# ---------------------------------------------------------------------------
# Feature 3 — Token Efficiency (TokenWise)
# ---------------------------------------------------------------------------


class BudgetPolicyConfig(_Base):
    """Per-session / per-day spend limits."""

    warn_at_usd: float = 0.50
    hard_limit_usd: float = 2.00
    warn_at_input_tokens: int = 500_000
    track_per_session: bool = True
    track_global_daily: bool = True


class SmartRoutingConfig(_Base):
    """SmartRouter configuration."""

    enabled: bool = False
    tiers: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "light": ["gemini-2.5-flash", "claude-haiku-4-5"],
            "medium": ["claude-sonnet-4-6", "gpt-4.1-mini"],
            "heavy": ["claude-opus-4-6", "gpt-4.1"],
        }
    )
    default_tier: Literal["light", "medium", "heavy"] = "heavy"
    """Fallback tier when routing is uncertain — conservative default."""


class ToolResultLifecycleConfig(_Base):
    """Tool result lifecycle management (the three-phase pruner)."""

    enabled: bool = False
    full_retention_turns: int = 3
    summary_retention_turns: int = 10
    placeholder_text: str = "[Tool result archived — retrievable via Curator]"
    summary_model: str = "gemini-2.5-flash"


class TokenWiseConfig(_Base):
    """TokenWise cross-cutting token/cost optimization."""

    enabled: bool = True
    """Master switch. Disabling skips all strategies."""

    usage_tracking: bool = True
    """Record token usage per call — cheap and informative; on by default."""

    cache_optimization: bool = True
    """Apply Anthropic cache_control breakpoints. No-op on other providers."""

    max_cache_breakpoints: int = 4
    """Anthropic API limit; kept configurable for forward-compat."""

    skill_lazy_loading: bool = False
    """Only inject skill summaries relevant to the current message."""

    tool_result_lifecycle: ToolResultLifecycleConfig = Field(default_factory=ToolResultLifecycleConfig)
    smart_routing: SmartRoutingConfig = Field(default_factory=SmartRoutingConfig)
    budget: BudgetPolicyConfig = Field(default_factory=BudgetPolicyConfig)


# ---------------------------------------------------------------------------
# Feature 4 — SkillForge
# ---------------------------------------------------------------------------
#
# SkillForge owns retrieval + execution + feedback emission. Evolution
# is handled by the embedded ``everos`` pipeline (see
# ``raven.memory_engine.skill_local.evolver.everos``).
#
# The config is intentionally kept flat. Component-level knobs
# (embedding model, BM25 parameters, RRF k, etc.) live in the
# scaffold dataclasses inside ``skill_forge/`` and stay at their
# defaults for now. Owners will promote individual fields here when
# they need user-facing knobs.


class EverOSConfig(_Base):
    """Embedded everos extraction pipeline configuration.

    When enabled, every completed user→agent turn is funneled into a
    local pipeline that distills an AgentCase + zero-or-more SkillOps
    into ``<workspace>/.cache/skills.db``. No external services
    required (replaces the EverOS HTTP path for skill extraction).
    """

    enabled: bool = False
    # Note: the per-turn tool-call gate (formerly min_tool_calls / min_messages
    # here) is now sourced from skill_forge.detect_min_tool_calls so the same
    # threshold drives any future auto-detect surface in addition to this
    # pipeline.
    # Number of similar existing skills shown to the skill_extractor
    # LLM as candidates for ``update``. 5 is enough — overlap between
    # turn-derived candidates above this rank is rare, and the prompt
    # budget for supporting_cases scales with this number.
    max_skills_top_k: int = 5
    # Confidence floor: skills falling below this after a downward
    # adjustment are soft-deleted on the spot.
    retire_confidence: float = 0.1
    # Skip the skill_extractor LLM call when ``case.quality_score`` is
    # below this floor. Low-quality distillations tend to produce noisy
    # / contradictory skills more often than reusable ones; the case is
    # still persisted (useful for retrieval / audit).
    min_quality_for_skill_extract: float = 0.2
    # 3-tier value gate placed before case extraction (in _flush_segment).
    # Only segments that pass at least one tier are extracted:
    #   Tier 1 (fast-pass): has_user_feedback AND >=2 user messages in segment
    #   Tier 2 (fast-pass): total tool_calls > complex_task_tool_call_threshold
    #   Tier 3 (cheap LLM): detect_llm asked whether trajectory is worth
    #                        learning from; false → skip, true → extract
    complex_task_tool_call_threshold: int = 20


class LocalDirConfig(_Base):
    """One local skill directory entry (R1)."""

    path: str
    """Absolute or ``~``-relative path. Expanded at startup."""

    enabled: bool = True
    """False → directory completely skipped."""

    name: str | None = None
    """Display name for logs. None → derived from path basename."""

    always_enabled: bool = True
    """False → skills from this dir with ``always: true`` are excluded
    from always injection (but still retrievable via select)."""


class SkillForgeConfig(_Base):
    """SkillForge configuration.

    ``enabled=True`` (default, R8) activates the SkillForge retrieval/
    injection pipeline. Set ``enabled=False`` to fall back to the
    pre-refactor behavior of handing the full skill directory to the LLM
    (component stubs that return empty lists also cause ``ContextBuilder``
    to fall back to the full directory automatically).

    Evolution is handled by the embedded ``everos``
    extraction pipeline, configured via
    ``skill_forge.everos``. The LLM used by that pipeline
    is selected by ``skill_forge.evolve_model`` (falls back to the
    active agent model when unset).

    Other lifecycle fields (auto_detect / auto_evolve / retirement ...)
    are placeholders from the original spec. No local code reads them;
    preserved for now to avoid breaking user configs.
    """

    # --- Master switch + location ---
    enabled: bool = True
    """Master switch (R8: default True). Activates the SkillForge
    retrieval/injection pipeline."""

    router: "SkillForgeRouterConfig" = Field(
        default_factory=lambda: SkillForgeRouterConfig(),
    )
    """Multi-source RRF routing policy (weights / over-fetch / dedup /
    Mass + Hub remote sources) — config key ``skillForge.router``. The
    router is a component of the SkillForge subsystem, so it nests here
    rather than living as a sibling top-level block. Forward-ref +
    ``model_rebuild`` (below): ``SkillForgeRouterConfig`` is defined later
    in this module."""

    local_dirs: list[LocalDirConfig] = Field(default_factory=list)
    """Local skill directories to mount (R1). List order = priority:
    later entries override earlier on name collision. Legacy
    ``skills_dir`` auto-migrated via model_validator (R5)."""

    scan_max_depth: int = 5
    """Maximum directory depth when scanning for SKILL.md files (R2).
    Paths deeper than this below a layer root are silently skipped.
    Prevents unbounded filesystem walks on huge mirrors."""

    # --- Retrieval / reranker knobs ---
    embedding_model: str = "default"
    """Dense embedding model identifier. MUST match the embedding model
    that produced ``mass_library_db``'s stored vectors, otherwise dense
    retrieval returns garbage because the query vector lives in a different
    space. Configure this to match the embedding service and corpus used by
    your deployment."""

    embedding_url: str = "http://localhost:1357"
    """Remote embedding service base URL.

    Retrieval calls ``POST <embedding_url>/embed``. Override this with
    ``REMOTE_EMBEDDING_URL`` or user config when using a hosted embedding
    service."""

    reranker_enabled: bool = True
    """Run a reranker pass after dense retrieval. On by default — adds
    200-500ms per query (cross-encoder GPU inference) but lifts mass-pool
    precision noticeably. Disable when latency matters more than ranking."""

    reranker_model: str = "default"
    """Reranker model label used for configuration and observability."""

    reranker_url: str = "http://localhost:1357"
    """Remote reranker service base URL.

    Reranking calls ``POST <reranker_url>/score`` with
    ``{"prompts": [...]}`` and reads ``{"scores": [...]}``. Override this
    with ``REMOTE_RERANKER_URL`` or user config when using a hosted reranker
    service."""

    embedding_api_key: str | None = None
    """Optional bearer token for the configured embedding service."""

    reranker_api_key: str | None = None
    """Optional bearer token for the configured reranker service."""

    embedding_dimensions: int | None = None
    """Request specific embedding dimensions (for models that support it)."""

    top_k: int = 5
    """Number of skills returned by ``select()``."""

    # --- Dual-pool fusion weights (R6) ---
    local_pool_top_k: int = 10
    """Candidate count from the local BM25 pool per query."""

    mass_pool_top_k: int = 10
    """Candidate count from the mass dense pool per query (post-rerank)."""

    local_weight: float = 1.3
    """RRF weight for local-pool candidates (mass is implicitly 1.0).
    Recommended range [1.2, 1.5]. Values < 1.0 or > 2.0 are rejected."""

    mass_reranker_overfetch: int = 20
    """When reranker is enabled, mass pool fetches this many candidates
    for rescoring, then truncates to ``mass_pool_top_k`` before RRF."""

    # --- Query rewrite knobs ---
    rewrite_enabled: bool = True
    """Enable a second retrieval path with LLM-rewritten queries."""

    rewrite_max_tokens: int = 8192
    """Output token budget for the rewriter LLM call. Defaults to 8192 to
    leave headroom for Qwen3-style reasoning traces (~3-4k tokens) on top
    of the actual rewrite output. The previous 1024 budget caused frequent
    finish_reason=length truncations with empty visible content, which
    surfaced as 'Failed to parse rewrite response as JSON' fallbacks."""

    mass_library_db: str | None = None
    """Path to a pre-built SQLite skill library (the "mass pool").
    Set to ``None`` to disable the mass pool entirely — only the file-based
    local pool (workspace + builtin + everos) will be used. Set this to a
    deployment-specific database path when shipping a pre-built skill library.

    When set, ``SkillService`` attaches the file in **read-only** mode at
    startup and uses its (metadata + embedding) rows for dense retrieval
    of curated mass skills.

    Lifecycle:
      - Operator builds the DB offline via
        ``raven skill import-files <skills_dir> --db <path>`` followed
        by ``raven skill rebuild-index --db <path>`` (encodes
        embeddings into the same file).
      - Deploys the resulting ``.db`` file alongside the runtime.
      - At runtime, Raven never modifies it — replace the file to
        update the library.

    Body, frontmatter and embeddings live inline in the DB; SKILL.md
    files for mass-library skills are not required on disk.
    """

    # --- Skill injection mode (full_body vs summary) ---
    injection_mode: str = "full_body"
    """How selected skills are surfaced to the agent.

    - ``"full_body"`` (default, OpenSpace style): load_skills_for_context
      inlines the full SKILL.md body of up to ``inject_max`` LLM-gate-
      selected candidates into the system prompt. Higher token cost but
      guarantees content visibility. Pairs with ``llm_gate_enabled=True``
      below — the gate cuts a 15-skill candidate pool down to ~2 truly
      relevant ones, so per-turn token cost stays bounded (~2-10K).
    - ``"summary"``: build_skills_summary renders an XML directory of
      (name, description, available) tuples. Agent must call ``read_file``
      on a skill's SKILL.md to access its body — progressive disclosure,
      cheaper in tokens but Round-D eval showed agents often skip the
      read step entirely (top1_kw rate ~0.62 vs ~0.80 with full_body)."""

    inject_max: int = 2
    """Max skills inlined when ``injection_mode='full_body'``. Each skill body
    typically adds 1-5K tokens."""

    disable_always: bool = False
    """When True, ``get_always_skills()`` returns [] and select() filters
    out always:true skills. R8 default: False (always skills inject)."""

    always_max: int = 5
    """Max always skills injected per turn (R3). Exceeding this truncates
    by local_dirs list order + alphabetical, with a WARN listing dropped
    skill names."""

    # --- LLM gate selector (default-on, mirrors openspace select_skills_with_llm) ---
    llm_gate_enabled: bool = True
    """When ``True`` (default), ``select()`` resolves a pool of
    ``llm_gate_pool_size`` candidates after RRF merge, then asks an LLM to
    plan + filter down to ``llm_gate_max_select`` skills. Empty result is
    valid ("inject nothing"). Costs one LLM call per ``select()`` invocation
    but eliminates the ~30% noise-injection rate of pure-RRF top-K (Round D
    obs.: irrelevant skills polluting the prompt). Disable to skip the
    extra LLM call (rare; useful when LLM provider is unavailable)."""

    llm_gate_max_select: int = 2
    """Upper bound on skills the gate may select. Mirrors ``inject_max``."""

    llm_gate_pool_size: int = 10
    """Candidate pool size handed to the gate (after RRF). Aligned
    with RRF output size (local_pool_top_k + mass_pool_top_k dedupe)."""

    llm_gate_model: str | None = None
    """Optional model override for gate calls. ``None`` → use the
    provider's default chat model (typically the agent's main model)."""

    llm_gate_temperature: float = 0.0
    """Sampling temperature for gate calls. 0.0 for deterministic
    filtering. Reasoning models may need 0.6 to engage <think>."""

    llm_gate_max_tokens: int = 8192
    """Output token budget for the gate LLM call. Defaults to 8192 to
    leave headroom for Qwen3-style reasoning traces (~3-4k tokens) on top
    of the gate's JSON answer. The previous 4096 budget caused empty
    content (finish_reason=length) on the 27B model in ~50% of calls,
    forcing a legacy top-N fallback that returned 5 skills instead of
    the configured llm_gate_max_select."""

    # --- Producer refresh trigger (optional, zero-config via .refresh_endpoint sentinel) ---
    refresh_url: str | None = None
    """Producer-side refresh service base URL (e.g.
    ``http://producer-host:8765``). When set, ``raven skill refresh
    <source>`` POSTs ``<url>/refresh?source=...`` to trigger an immediate
    git pull + ingest on the producer. The actual refresh runs producer-side;
    the consumer just sends the trigger and lets the ``.stale`` flag
    mechanism propagate the update.

    Zero-config: when unset, the ``skill refresh`` CLI auto-discovers
    the endpoint from ``<mass_library_db>/../.refresh_endpoint`` (a
    single-line text file written by the producer admin during
    ``export_to_mass_library --refresh-endpoint=URL``). 99% of users
    don't need to set this field."""

    # --- Evolver model ---
    evolve_model: str | None = None
    """LLM used by the embedded ``everos`` evolver for case
    distillation and skill rewrites. When ``None`` (default), the evolver
    falls back to the active agent model (``agents.defaults.model`` /
    provider default). Set explicitly to pin a stronger model for quality
    rewrites — e.g. ``"claude-opus-4-6"``."""

    # --- Detect / extraction gating (wired into everos) ---
    detect_model: str = "gemini-2.5-flash"
    """LLM used for the cheap per-turn classification work — today that's
    the everos boundary detector (multi-turn task split). A
    smaller / faster model than ``evolve_model`` is intentional: boundary
    detection runs on every accumulated turn pair, while the heavier
    extractors only run when a segment is actually flushed."""

    detect_min_tool_calls: int = 3
    """Minimum tool calls in the current turn for it to enter the
    extraction pipeline at all. Coding work worth replaying almost always
    exercises ≥ this many tools; thinner turns get filtered before any
    LLM is invoked. Set to 0 to disable the gate."""

    # --- Legacy placeholders (not wired; see class docstring) ---
    stats_tracking: bool = True
    """Record per-skill invocation stats. Cheap, enables future features."""

    auto_detect: bool = False
    """End-of-session LLM check for new skill candidates."""

    auto_evolve: bool = False
    """Automatic skill improvement based on feedback. Requires auto_detect."""

    evolve_trigger_success_rate: float = 0.70
    """Evolution fires when success_rate drops below this over recent invocations."""

    evolve_trigger_min_invocations: int = 10
    """Don't evolve skills used fewer than this many times."""

    draft_first_activation: bool = True
    """New auto-created skills start as 'draft'; promoted to 'active' after first success."""

    retirement_idle_days: int = 90
    """Active skill unused for this long → deprecated."""

    # --- Embedded extraction pipeline (everos) ---
    everos: EverOSConfig = Field(default_factory=EverOSConfig)
    """Embedded everos extraction pipeline. Distinct from the
    SkillForge master switch above: the retrieval/injection path can be
    enabled (``skill_forge.enabled=True``) without extraction, and vice
    versa."""

    # --- Validators ---

    @model_validator(mode="before")
    @classmethod
    def _migrate_skills_dir(cls, data: dict) -> dict:
        """R5: auto-convert legacy ``skills_dir`` → ``local_dirs``."""
        if not isinstance(data, dict):
            return data
        for old_key in ("skills_dir", "skillsDir"):
            old_val = data.pop(old_key, None)
            if old_val and "local_dirs" not in data and "localDirs" not in data:
                data["local_dirs"] = [{"path": old_val}]
                warnings.warn(
                    f"skill_forge.{old_key} is deprecated, use local_dirs "
                    f"instead. Auto-converted to local_dirs=[{{path: {old_val!r}}}]. "
                    f"This field will be removed in a future release.",
                    DeprecationWarning,
                    stacklevel=2,
                )
        lw = data.get("local_weight") or data.get("localWeight")
        if lw is not None:
            lw = float(lw)
            if lw < 1.0 or lw > 2.0:
                raise ValueError(f"local_weight={lw} out of valid range [1.0, 2.0]")
        return data


# ---------------------------------------------------------------------------
# CFG-1 — Plugin / Memory backend / SkillForgeRouter
# ---------------------------------------------------------------------------


class PluginsConfig(_Base):
    """Plugin-system top-level config.

    ``disabled`` is the user opt-out list keyed by plugin id (matches
    the ``id`` in ``raven-plugin.toml``). ``config`` is the per-
    plugin config slice the registry hands to each plugin's factory
    via :class:`PluginContext.config` — its shape is determined by
    each plugin's own ``config_schema`` in the manifest, so the host
    treats it as a free-form dict.
    """

    disabled: list[str] = Field(default_factory=list)
    """Plugin ids the user opted out of (e.g. ``["everos-memory"]``)."""

    config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-plugin configuration, keyed by plugin id. Each plugin's
    factory receives ``ctx.config = plugins.config.get(<id>, {})``."""


class MemoryConfig(_Base):
    """Which memory backend is active + per-track identity wiring.

    ``backend`` is the name of an activated ``memory_backend``
    contribution (set ``None`` to disable backend-driven memory and
    operate purely on raven-core's MemoryStore + MemoryConsolidator).

    The two id fields are bare, backend-native strings. The host passes
    ``user_id`` for the user-track recall and ``agent_id`` for the
    agent-track recall (``backend.recall`` takes one XOR the other).
    EverOS routes each to its matching store; flat backends (mem0 /
    MemOS / Letta) use ``user_id`` and return empty for the agent call.
    Each value must match the corresponding id the active backend
    stamps on stored messages (e.g. ``plugins.config["everos-memory"]``
    ``user_id`` / ``agent_id``) for stored memory to be retrievable.
    """

    backend: str | None = "everos"
    """Activated backend contribution name. ``None`` disables the
    plugin-driven memory path; AgentLoop continues with raven-core's
    MemoryStore alone."""

    user_id: str = "default"
    """Bare user identity passed as ``backend.recall(user_id=...)`` for
    the user-track recall channel inside ``ContextAssembler.assemble``."""

    agent_id: str = "default"
    """Bare agent identity passed as ``backend.recall(agent_id=...)`` by
    ``EverosSkillSource`` for agent-track skill recall."""

    memory_top_k: int = 5
    """Top-K passed to ``backend.recall(user_id=user_id)`` per turn for
    the ``# Recalled memory`` block."""


class HubSourceConfig(_Base):
    """Skill Hub remote-source settings (the OpenAPI skill marketplace).

    Discovery only needs ``endpoint`` (+ optional ``api_key``); reading a
    skill's body / downloading its zip is driven by the ``read_skill`` /
    ``use_skill`` tools, which reuse this same config."""

    endpoint: str | None = None
    """Skill Hub base URL, e.g. ``"https://mss.evermind.ai"``. ``None``
    disables the Hub source — SkillForgeRouter degrades to Local + Mass +
    Everos."""

    api_key: str | None = None
    """Bearer token sent as ``Authorization: Bearer <api_key>``."""

    timeout_s: float = 2.0
    """Per-request timeout (hot turn-path)."""

    min_safety: float = 0.7
    """Skills with ``score_safety`` below this are filtered out of the
    catalog (and refused by ``use_skill``)."""

    source: str = "raven"
    """Download ``source`` tag for Hub usage stats."""


class SkillForgeRouterConfig(_Base):
    """Multi-source skill routing policy.

    Sources themselves are hardcoded (Local + Mass + Everos) per the
    project-wide design decision; this block tunes the weighted RRF
    and per-source plumbing.
    """

    enabled: bool = True
    """Master switch. ``False`` makes the host bypass SkillForgeRouter
    entirely (used by tests / restricted deployments)."""

    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "local": 1.0,
            "everos": 0.9,
            "hub": 0.85,
        },
    )
    """Per-source RRF weight. Higher = more rank mass when the same skill
    surfaces from multiple sources. Local highest (hand-curated); Hub
    (the remote marketplace, replaces the retired Mass source) lowest as
    imported/unvalidated; Everos in between (task-specific, auto-evolved)."""

    over_fetch_factor: int = 2
    """Each source is asked for ``top_k * factor`` hits before fusion
    narrows back to ``top_k``. Larger factors give better cross-source
    coverage at the cost of per-source query work."""

    dedup_by: Literal["name", "qualified_id"] = "name"
    """Cross-source dedup key for the RRF fusion. ``"name"`` collapses
    a same-named skill across sources into one slot; ``"qualified_id"``
    keeps them as separate entries (useful for telemetry experiments)."""

    top_k: int = 5
    """Final top-K returned from ``SkillForgeRouter.select``."""

    hub: HubSourceConfig = Field(default_factory=HubSourceConfig)


# Resolve the forward-ref ``SkillForgeConfig.router: "SkillForgeRouterConfig"``
# now that ``SkillForgeRouterConfig`` exists in module scope.
SkillForgeConfig.model_rebuild()


# ---------------------------------------------------------------------------
# Feature 5 — Runtime Discipline
# ---------------------------------------------------------------------------


class CheckpointConfig(_Base):
    """Per-turn shadow-git checkpoint of the workspace.

    When active, the agent loop commits the workspace to an out-of-band
    shadow git repo at the end of each turn (covering both normal and
    max-iteration exits). This is the safety net behind Bug2: a truncated
    multi-file edit leaves a recoverable snapshot, and the next turn gets a
    recovery prompt listing what the interrupted turn changed.

    Activation is gated by ``policy`` and the AgentLoop's ``interactive``
    flag (set per call site by the CLI / TUI / gateway entry points):

    - ``"always"``     — active in every AgentLoop, including ``-m``
                          one-shot commands.
    - ``"interactive"`` — active only when constructed for a multi-turn
                          session (REPL, TUI, gateway). One-shot commands
                          have no "next turn" to inject recovery into, so
                          paying the snapshot cost there is wasted.
    - ``"never"``      — disabled entirely; loop is byte-identical to the
                          pre-Bug2 baseline (no commits, no interrupt
                          reclassification, no recovery injection).

    Default ``"interactive"`` matches mature competitors (Claude Code,
    Cursor) which transparently checkpoint long sessions while leaving
    one-shot batch invocations untouched.
    """

    policy: Literal["always", "interactive", "never"] = "interactive"
    """When the per-turn shadow-git snapshot is active. See class
    docstring for the interaction with the AgentLoop ``interactive`` flag."""

    shadow_dir: str = ".raven/shadow.git"
    """Shadow git-dir, relative to the workspace. The real workspace is the
    work-tree; the user's own ``.git`` is never touched."""


class RuntimeConfig(_Base):
    """Runtime discipline — the 5th feature pillar.

    Houses the opt-in runtime safety nets. Bug2 ships ``checkpoint``;
    later phases add ``journal`` / ``verifier`` / ``done_gate`` /
    ``loop_detection`` (Bug3, us) and ``session`` (Bug1, dev) as sibling
    sub-configs. All default off so the all-off baseline equals 68a3be7.
    """

    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)


class TracingConfig(_Base):
    """Observability tracing (in-tree ``raven.tracing``).

    On by default; every ``raven`` command auto-installs non-invasive
    instrumentation before any AgentLoop is built. ``RAVEN_TRACING=0`` is an
    explicit env kill-switch that overrides this block. View captured traces
    with ``raven tracing`` (or ``/tracing`` in the TUI).
    """

    enabled: bool = True
    port: int = 4318
    preview_len: int = 500


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


class RavenConfig(_Base):
    """Raven root config. Composes the base Config with feature extensions."""

    # Feature blocks
    context: ContextConfig = Field(default_factory=ContextConfig)
    sentinel: SentinelConfig = Field(default_factory=SentinelConfig)
    token_wise: TokenWiseConfig = Field(default_factory=TokenWiseConfig)
    # SkillForge subsystem — its RRF routing policy nests at
    # ``skill_forge.router`` (config key ``skillForge.router``), no longer a
    # separate top-level ``skillRouter`` block.
    skill_forge: SkillForgeConfig = Field(default_factory=SkillForgeConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)

    # CFG-1: plugin system + memory backend.
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    # The full base config (agents, channels, providers, tools, routing).
    # Kept as a nested field so we can round-trip YAML with the base loader.
    base: BaseConfig = Field(default_factory=BaseConfig)


def load_raven_config(config_path: Path | None = None) -> RavenConfig:
    """Load both the base Config and the Raven extension blocks
    (``context`` / ``sentinel`` / ``token_wise`` / ``skill_forge``) from
    the same JSON config file.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Extension blocks fall through to their dataclass defaults when the
    JSON has no entry for them; explicit ``null`` values are also
    treated as "use default" rather than rejected.
    """
    base = load_base_config(config_path)

    overrides: dict = {}
    actual_path = config_path or get_config_path()
    if actual_path.exists():
        try:
            with open(actual_path, encoding="utf-8") as f:
                data = json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            data = {}
        # Apply the same migrations the base loader uses so legacy fields
        # (e.g. ``agents.defaults.everos``) end up in their new
        # home (``skillForge.everos``) before we extract blocks.
        data = _migrate_config(data, pop_extension_keys=False)
        # CFG-1 deprecation surface: warn once when the user still has
        # the legacy ``skill_forge.mass_library_db`` field set without
        # the new ``skill_router.mass.endpoint``. The two coexist for
        # one release; CLEANUP removes the legacy field.
        _warn_mass_library_db_deprecated(data)
        for key in EXTENSION_KEYS:
            if key in data and data[key] is not None:
                overrides[key] = data[key]

    return RavenConfig(base=base, **overrides)


def _warn_mass_library_db_deprecated(data: dict) -> None:
    """Single-shot deprecation warning for ``skill_forge.mass_library_db``.

    Fires when the user has the old field set AND has not switched to
    the new ``skill_router.mass.endpoint``. We don't auto-migrate
    because the old field is a local SQLite path and the new field is
    an HTTP endpoint — semantically different, so the user must pick
    one consciously.
    """
    legacy = None
    for skill_forge_key in ("skill_forge", "skillForge"):
        block = data.get(skill_forge_key)
        if isinstance(block, dict):
            legacy = block.get("mass_library_db") or block.get("massLibraryDb")
            if legacy:
                break
    if not legacy:
        return
    new = None
    # The remote skill library is now the Hub source at
    # skillForge.router.hub (Mass was retired; Hub replaces it).
    for sf_key in ("skill_forge", "skillForge"):
        block = data.get(sf_key)
        if isinstance(block, dict):
            router = block.get("router")
            if isinstance(router, dict):
                new = (router.get("hub") or {}).get("endpoint")
                if new:
                    break
    if new:
        # User already set the new field — they're mid-migration. No
        # warning, just a one-line info log.
        logging.getLogger(__name__).info(
            "config: both skill_forge.mass_library_db (legacy) and "
            "skillForge.router.hub.endpoint are set; the legacy field is "
            "ignored by the Skill Hub source and will be removed.",
        )
        return
    warnings.warn(
        "skill_forge.mass_library_db is deprecated and the local-matmul "
        "mass-library path has been removed. Switch to "
        "skillForge.router.hub.endpoint = '<URL>' to point at the remote "
        "Skill Hub. The legacy field is read but ignored by the new "
        "SkillForgeRouter / Skill Hub path.",
        DeprecationWarning,
        stacklevel=2,
    )
