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

    flush_on_task_end: bool = False
    """Promote the backend's buffered writes when a run finishes.

    Only does anything for a backend that buffers capture and defers
    extraction (EverOS ``defer_extraction=true``); eager backends have
    nothing to promote. The flush happens after the final answer is
    delivered, so it changes no prompt bytes — but it does block exit on
    one request whose budget is the adapter's ``flush_timeout_s``.

    Off by default because measurement batches deliberately promote out
    of band: ``scripts/everos_flush_batch.py`` reads the
    ``.everos_sessions.jsonl`` sidecars afterwards, which keeps
    extraction cost out of every question's wall-clock and avoids N
    concurrent arms contending on the server's per-session lock. Turn it
    on for interactive / product use, where "the task is done" is the
    only boundary that will ever arrive."""


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

    everos_min_confidence: float = 0.0
    """Maturity floor for self-evolved (everos) skill hits; 0 disables.

    A skill's ``confidence`` counts how many trajectories agreed on it, so a
    floor is what separates a distilled method from a single lucky run. It is
    also the only pressure available against a space silting up, since EverOS
    ships no skill retirement. Applied client-side: the server's ``min_score``
    is a relevance floor that only the episode hybrid path reads. Hits with no
    confidence field (agent cases never carry one) are kept."""

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


class DRFlowReactiveClampConfig(_Base):
    """The reactive completion clamp, given a predicate of its own. **Default off.**

    ``AgentLoop._completion_clamp`` fires only after the provider has already
    rejected a request for size (``should_compress``). It was structurally dead:

        ``_fit_request``      (pre-call)  ``if room < MIN or room >= cap: return unchanged``
        ``_completion_clamp`` (reactive)  ``if target < MIN or target >= cap: return None``

    Both compute ``room`` from ``estimate_prompt_tokens`` on the same messages with
    the same window, so the reactive one re-derives the number the pre-call one has
    already acted on. Walk the three cases: if ``_fit_request`` lowered the cap to
    ``room``, then ``target == cap`` and the clamp returns ``None``; if it found the
    request already fits, ``room >= cap`` and it returns ``None``; if there was no
    usable room, ``target < MIN`` and it returns ``None``. **Every path returns
    None**, so ``clamp_retries`` never reaches 1, so the ``attempt >= 1`` branch
    (``min(room, cap // 2)``) is unreachable and ``_MAX_CLAMP_RETRIES = 2`` has never
    had anything to bound. Measured: ``clamps`` reads 0 on 99 of 100 questions of one
    batch - and the 100th, ``bc-781``, has no such field at all because it died of
    ``context_overflow`` at 143 turns with no ``turn_end``. **The one question that
    actually died of this failure is the one where the counter could not be read.**
    Missing data is not zero.

    Why the fix is a different predicate rather than a better estimator: a
    ``should_compress`` rejection **is the evidence that our own token estimate was
    wrong**. Recomputing it and comparing against the same threshold cannot help, no
    matter how accurate the tokenizer gets - the two sides of the comparison move
    together. So the reactive path stops consulting the estimator at all and shrinks
    the cap that was actually in force by a fixed factor. That predicate ("the server
    said no") is unavailable to ``_fit_request`` by construction, which is the whole
    point: a gate whose predicate is derivable from the gate it backs up is a copy of
    it, not a backstop.

    ⚠️ **Off by default, and it needs its own arm pair before it goes on.** Enabling
    it changes which turns survive an overflow, and ``context_overflow`` is not
    symmetric between arms - on the corpus dr@3.0 batch it was 59 of the anchor's 74
    answerless questions against 11 of the treated arm's 29. So switching it on moves
    the anchor MORE than the treated arm, i.e. it would compress delta; switching it
    on DR-only moves the treated arm and inflates delta. Either direction is a real
    distribution change with a known sign, which is exactly the kind that has to be
    pre-registered instead of explained afterwards.
    """

    enabled: bool = False
    shrink_factor: float = 0.5
    """Multiply the in-force cap by this on each reactive clamp.

    Not derived from any token count - see the class docstring. 0.5 halves it, so
    with ``_MAX_CLAMP_RETRIES = 2`` the floor is a quarter of the original reserve
    or ``_MIN_CLAMPED_COMPLETION_TOKENS``, whichever is larger."""


class DRFlowTruncationWrapupConfig(_Base):
    """dr@3.0: the dr@2.9 truncation wrap-up, now gated, budgeted and honestly counted.

    dr@2.9 shipped this deliberately ungated, on the reasoning that it repairs a defect
    the anchor suffers from most. That reasoning was right about the defect and wrong
    about the consequence: an ungated repair moves the flow-off arm, and moving the
    anchor is the mechanical reason the dr@2.9 batch's cross-batch readings could not
    be used. A repair that costs the reference frame has to earn it, and this one did
    not - measured over that batch it fired 21 times on the treated arm for **zero**
    correct answers, and 23 times on the anchor for two.

    Three defects were found together, and each alone is enough to make it useless:

    * **It was not gated**, so it moved the anchor. That is this class.
    * **It had no generation budget of its own.** 16 of 21 firings ran the wrap-up call
      itself into the completion limit - final assistant messages of 47k-72k characters
      with an empty answer - so the mechanism that repairs truncation was itself being
      truncated, and necessarily so on exactly the questions that need it most.
    * **Its success counter measured the wrong thing.** ``recovered`` was true whenever
      the call returned any text, which was 21 of 21; the share where the *scorer* could
      see an answer was 5 of 21. That gap is not rounding, it is the ``_strip_think``
      seam: a wrap-up cut mid-reasoning passes its whole chain of thought through as
      content, which reads as a recovery to a counter and as an empty answer to the
      scorer.

    One further cost, independent of all three: it converts a **detectable** failure
    (an empty answer, which is what ``base+R0`` triggers on) into an **undetectable**
    one (an answer that is present and wrong). It took the anchor's empty answers from
    66 to 52. Nothing downstream can tell those 14 apart from ordinary wrong answers.

    Default off, therefore, on every arm. The capability is repaired rather than
    deleted so it can be priced on its own with a gated pair, which is the one thing
    the ungated version made impossible.
    """

    enabled: bool = False
    max_tokens: int = 4096
    """Completion budget for the wrap-up call, independent of the turn's own.

    The turn this fires on has just proved it cannot finish writing inside the
    provider's default, so inheriting that default is inheriting the failure."""
    reasoning_effort: str | None = "low"
    """Reasoning effort for the wrap-up call.

    The task is "state the answer you already reached", not "solve it again", and the
    measured failure is a call that spent its whole budget thinking. ``None`` leaves
    the provider default, for backends that reject the parameter."""


class DRFlowSearchSaturationConfig(_Base):
    """dr@3.0: stop searching a query family that has stopped returning new documents.

    The trigger is ``k`` consecutive searches that brought back not one identity this
    turn had not already seen. Measured offline and reproduced independently on two
    batches: on the live-web axis k=10 removes 27.0% of searches (37 of 302 questions
    trigger) with a score-loss upper bound of 0.00pp, and k=5 removes 33.0% at
    <=0.66pp; on the corpus axis the docid variant at k=10 removes 36.58% for 0.83%.
    The upper bound counts questions where, after the cut, the turn went on to fetch a
    URL it had not seen before the cut AND was judged correct - so it prices the worst
    case, not the expected one. The rule stops ``web_search`` only; every URL already
    surfaced stays fetchable.

    The failure it targets is not thrift. On one 100-question batch 1,120 of 5,586
    searches were trailing dry spin (20.1%), and of the 14 questions with six or more
    trailing dry searches, **zero were judged correct**. One question ran 295 searches
    of which the last 234 returned nothing new.

    **The action is control flow, never a prompt, and that distinction is the whole
    design.** The model is ALREADY told: with snippet dedup on, a replay of
    ``_snippet_seen`` shows 41.9% of the DR arm's live-web result slots carry a
    "you have seen this" marker, and the model goes on searching regardless. Another
    sentence is a rerun of an experiment that already failed. So a saturated turn does
    not receive advice - the call does not happen.

    Off by default and gated per arm, like every other knob here: it changes which
    turns re-sample and therefore the generated distribution, and an ungated version
    would move the flow-off anchor, which is the mechanical reason the dr@2.9
    cross-batch readings could not be used.
    """

    enabled: bool = False
    identity: Literal["url", "docid"] = "url"
    """What counts as "a document already seen". ``url`` for live web, ``docid`` for
    the fixed corpus, matching the identity that axis's ledger records.

    A separate set from ``cross_query_dedup``'s and from the snippet one, on purpose.
    Those two are already deliberately separate because they answer different
    questions; sharing a set with either would make one knob silently change the
    other's trigger point, which is how an ablation stops measuring what it names."""
    k: int = 10
    """Consecutive dry searches before the rule fires.

    10 rather than 5 because the loss upper bound is exactly zero there and 0.66pp at
    5, while the saving only falls from 33.0% to 27.0% - a bad trade for a rule whose
    entire justification is that it costs nothing."""
    on_saturate: Literal["widen", "paginate", "stop"] = "paginate"
    """What a saturated turn does next.

    ``paginate`` asks the endpoint for the next page of results rather than giving up:
    probed against the live endpoint (``data_dr/runs/PROBE_serper_num_page_20260812.json``),
    ``page=2`` and ``page=3`` each return a full ten rows with 93.75% of them absent
    from page one. Depth on this path was previously believed physically unavailable -
    on the strength of a source comment with no artifact behind it, whose own next
    clause recorded the mechanism that opens it.

    ``widen`` spends the free layer first. ``num`` is honoured DOWNWARD even though it
    saturates upward: the same probe measured ``num=5`` returning exactly five rows
    against ``num=10`` returning eight or nine for the same query - five more documents
    for the SAME single call. And the default rendered width is five, so on the dr@2.9
    live-web batch 6,679 of the DR arm's 10,343 non-replay searches (64.6%; anchor
    42.9%) rendered five rows out of a response that had already paid for nine. So
    ``widen`` escalates to the endpoint's own ceiling before ``paginate`` buys a second
    call, and only then does it stop.

    ``stop`` is the honest fallback wherever paging is not implemented, which today is
    the corpus path: that service takes a width but no offset, so a paginating corpus
    arm would silently behave like a stopping one. The tool records which of the two
    actually happened rather than which was configured.

    **The default stays ``paginate``, deliberately.** The offline pricing that justifies
    this whole rule - k=10 removing 27.0% of searches at a 0.00pp loss bound - was
    computed on a two-step ladder, and inserting a step changes which searches trigger.
    Making ``widen`` the default would silently invalidate the number this knob is sold
    on. It is available, it is cheaper, and it needs its own replay before it leads."""
    max_pages: int = 2
    """How deep pagination may go before the rule falls through to stopping.

    Quota is linear in pages and the cost is arm-correlated - the DR arm searches more
    - so this is a number that belongs in a pre-registration, not a default anybody
    should raise casually."""


class DRFlowSearchConfig(_Base):
    """Search-result shaping for deep research: strip everything that lets
    the model answer from the SERP without opening a page."""

    include_answer_box: bool = False
    include_knowledge_graph: bool = False
    include_snippets: bool = False
    """Default false: the shaping strips the result-list snippet so the model cannot
    answer off the SERP without opening a page.

    dr@2.4 keeps this default false but lets an arm restore it per-config
    (``search.includeSnippets=true``). The read-rate diagnosis found ranking is not the
    bottleneck: in the "surfaced but never fetched" cell the gold doc sits at rank 1
    while the model runs dozens of searches without opening it, because "title\\nurl"
    carries no signal for which candidate to open. On the fixed corpus a snippet is a
    300-char query-neighbourhood window (bcplus_serve.py), a selection signal and not an
    answer, so fetch_floor's "ground the answer in fetched pages" still holds; restore it
    only where that holds. A live-web snippet is answer-optimised and unpriced, so the
    live-web DR arms keep it off. The flow-off anchor already renders snippets
    (WebSearchTool default), so restoring it on a treated arm narrows a self-inflicted
    gap and does not move the anchor."""
    snippet_dedup_by_docid: bool = False
    """Render a snippet only the first time a result appears in a turn; later
    appearances get a short marker.

    Snippet cost is per result slot, not per search: the corpus renders one for every
    rank of every search, so a document surfaced by ten queries is paid for ten times.
    Measured on one batch, the median turn saw 192 slots over 72 distinct documents, and
    the tail is what matters - undeduplicated, the p90 turn injects several times the
    history budget, which is worse at the tail than the read-everything option this
    change was chosen over. Deduplication is what made restoring snippets cheap; without
    it the restoration is not the cheap option it was priced as.

    Off by default, and gated rather than unconditional, for two reasons. The flow-off
    anchor renders snippets on the same corpus path, so an unconditional dedup would move
    the anchor. And a repeat window is not redundant text: the corpus snippet is centred
    on the query terms, so the same document surfaced by a different query yields a
    different window. Deduplication therefore drops query-specific previews for documents
    already shown - a small but real loss, which is why it stays ablatable."""
    cross_query_dedup: bool = False
    """dr@2.8: spend a result slot on a document the turn has not seen yet.

    The result list was capped at ten in three places while the corpus retrieval
    service clamps to fifty, so 84.2% of searches (44,098 of 52,373 on the dr@2.7
    corpus batch) only ever looked at five documents. With this on, the service is
    asked for ``search_depth`` results, documents already rendered this turn are
    skipped, and the list is backfilled from deeper - so the model still reads the
    same number of lines and the context is byte-for-byte the same size.

    That last property is the reason this shape was chosen over the obvious one.
    Simply returning ten lines instead of five was measured at +4.17~6.67pp gold
    surfaced but doubles the result text, and context is exactly where the one
    mechanism that survived self-consistency on this axis operates. Deduplicating
    at unchanged width measured higher (+5.83~8.33pp at depth 50, and depth 20
    already carries about 90% of it) at no context cost at all.

    Nothing here re-ranks. Four zero-model ranking signals were priced offline on
    the same 40,363 deduplicated queries and all four lost to plain BM25: score
    fusion across queries -22~-25pp, title/URL lexical overlap +4.6~5.4pp on one
    arm but -0.83pp on its replicate, cross-query co-occurrence zero at every
    rank, and a document-length prior -39~-47pp. The value of thirty-seven queries
    is that they are thirty-seven different windows; fusing them makes one window,
    and a reranker only competes with the full-text scorer and loses. The one
    thing worth doing is keeping the windows from overlapping.

    Off by default and gated per arm, because the benefit is not arm-neutral: on
    the same batch the anchor converts a surfaced gold into a fetch 71.6% of the
    time against the DR arm's 85.8%, and at rank 2 - precisely the ranks this
    change newly exposes - 18.8% against 66.7%. An unconditional version would
    hand the two arms different amounts of the same improvement, which is an
    arm-correlated defect, and it would move the anchor besides."""
    search_depth: int = 20
    """How many results to ask the service for when ``cross_query_dedup`` is on.

    Inert without it: the request width and the rendered width are the same number
    otherwise, which is the pre-dr@2.8 behaviour. 20 rather than 50 because the
    measured curve is steeply concave - depth 10 recovers +3.75~4.58pp, depth 20
    +5.42~7.50pp, depth 50 +5.83~8.33pp - and a shallower request is cheaper for
    the retrieval service on every single call."""
    rendered_width: int = 5
    """How many results a search renders when the model does not ask for a count.

    dr@3.2. Default 5 reproduces the previous behaviour byte for byte: 5 was the
    ``WebSearchTool`` constructor default and there was no way to change it from a
    config, because ``WebSearchConfig.max_results`` (``schema.py``) is defined and
    read by nothing. So on every arm ever run - anchor included - 5 was the width.

    Adding the knob here rather than touching the constructor default is the whole
    point. Measured on the dr@3.0 live-web batch, 62.45% of the DR arm's
    non-suppressed searches rendered exactly 5 rows (6,917/11,076) while the same
    call returns 8-10 for free (``num`` is honoured downward exactly - zero
    exceptions in 14,664 pooled k=5 rows - and saturates upward). But it is NOT a
    DR property: the flow-off anchor renders 5 on 52.68% of its searches too. So
    raising it by editing ``web.py`` would move the anchor as well, which is the
    dr@2.7 "no gate ⇒ the anchor changes code too" shape that made dr@2.9's
    cross-batch readings unusable. Per-arm and default-inert, it cannot.

    ⚠️ Left at 5 on purpose, and raising it globally is expected to LOSE, not merely
    to fail to pay. Three independent reasons, strongest last:

    1. The corpus-axis offline sweep has "return 10 rows" losing to cross-query dedup
       (82.92 vs 84.58 gold-surfacing) at twice the context.
    2. Width is the same class of lever as ``cross_query_dedup``, which was measured
       at zero on two batches and is off from dr@3.2 onward: it buys gold-surfacing,
       and gold-surfacing has repeatedly failed to convert. On the dr@3.1 corpus axis
       Surface is down to 11.25pp while Selection is 17.08pp - the document is already
       on screen and does not get opened.
    3. A direction-certain harm, which is the one that matters. Snippet lines measure
       ~299 chars, so the per-search snippet load goes 1,495 -> 5,980 at width 20, and
       across a failing question's whole run roughly 107k -> 426k chars against a 64K
       window's ~200-250k. The population that would absorb that is exactly the one
       already drowning: the failure bucket runs 74-81% elided against 0.0% on
       questions answered correctly, and its snippet-to-prose ratio is already 8.2x
       versus 3.8x. Raising width feeds the dominant failure mode.

    So if this is ever raised, it must be gated on the turn having already read
    something, not switched on globally. This knob exists so that experiment becomes
    *possible without moving the anchor*, not because the width should go up.

    (Reasons 2 and 3 are Think's 20260814 delivery, from the dr@3.1 corpus batch and
    ``SELECTION_SPIN_LEDGER_20260814.md``; the mechanism is checkable in this file's
    own snippet rendering, the magnitudes are theirs and are not re-derived here.)"""
    repeat_notice: bool = True
    """Tell the model when a search is a byte-identical repeat of one it already
    ran this turn, and replay the cached results.

    Stripping snippets removes the model's only way to tell a productive query
    from a barren one, and it compensates by asking again: over one 230-question
    batch 34.2% of this flow's searches were exact repeats against 24.7% for the
    unshaped arm, and 63% of the calls inside budget-exhausted questions were
    repeats. The rollback seam cannot carry this - it is capped at 6 per turn."""
    saturation: DRFlowSearchSaturationConfig = Field(
        default_factory=DRFlowSearchSaturationConfig
    )
    """Stop or deepen a turn whose searches have stopped returning new documents."""


class DRFlowDigestConfig(_Base):
    """Targeted extraction for web_fetch: long pages are distilled by a
    cheap model to exactly the requested information."""

    enabled: bool = True
    model: str | None = None
    """Digest model. ``None`` uses the provider's default model."""
    threshold_chars: int = 8000
    timeout_seconds: float = 120.0
    """Measured on a shared production endpoint: long-prompt digest calls
    routinely exceed 45s, so a tight timeout silently degrades every fetch
    to truncation. Degradation stays as the fuse, not the norm."""
    verbatim_head_chars: int = 0
    """When > 0, append this many verbatim chars of the page head to the
    digest output, so downstream review can back-substitute exact quotes
    without re-fetching and the model can second-guess a digest that
    reports "not found" on a relevant page. 0 disables."""


class DRFlowVerifyConfig(_Base):
    """End-of-turn draft review gate (independent context, fail-open)."""

    enabled: bool = True
    model: str | None = None
    """Reviewer model. ``None`` uses the provider's default model."""
    timeout_seconds: float = 360.0
    attempt_timeout_seconds: float = 120.0
    """Per-attempt slice of ``timeout_seconds``. Reviewer calls can hang on
    a dead pooled connection after the long draft stream (the call never
    errors, it just stalls); a fresh attempt completes in seconds, so the
    budget is spent as short attempts rather than one long wait."""
    max_revisions: int = 1
    review_final_draft: bool = False
    """dr@3.0: review the draft that actually ships, even once the revision budget
    is spent - and accept it regardless of the verdict.

    One budget was doing two jobs. ``max_revisions`` is meant to cap how many times
    the turn may be sent back, but the same counter also decides whether a review
    happens at all, so the LAST draft - the one that becomes the answer - is the one
    draft nobody looks at. Measured: on the dr@2.7 corpus batch 42 of 47 rejects were
    accepted with no second review, and on one 100-question Opus batch it was 28 of
    28, i.e. every single rejected turn shipped unexamined. Those turns ended up
    correct 7 times out of 28.

    **This changes no decision and therefore no score.** The verdict is recorded and
    the draft is accepted either way; nothing is injected into history, so the
    generated distribution is byte-identical with this on or off, exactly like
    ``final_shape.record``. What it buys is the one thing missing before a
    verify-based selector can run inside a run rather than offline: a verdict on the
    draft that was actually delivered. The selector is the only flow lever measured
    to win at matched compute (corpus AUC 0.8170, live-web 0.8023), and it currently
    cannot be built because the shipping draft carries no verdict.

    Off by default: it costs one reviewer call per rejected turn, and a knob that
    spends compute belongs in a pre-registration even when its score budget is zero.

    ⚠️ Not to be confused with raising ``max_revisions``. That WOULD change the
    distribution - more re-samples, different history - and would need its own
    labelled round with a fresh anchor pair."""
    max_tokens: int = 8192
    """Reviewer completion budget. A thinking reviewer spends tokens on the
    think block BEFORE the verdict JSON; at the provider default (4096) the
    generation can be cut mid-think and the verdict never appears — the gate
    then fail-opens on every draft. Sized so think + JSON always fit."""
    constraint_rubric: bool = False
    """Ask the reviewer to check the drafted answer against EACH constraint
    the task pins (dates, places, names, quantities, relationships), one by
    one. The signature failure of deep multi-hop tasks is an answer that
    satisfies most constraints but not all."""
    strict_reject_only: bool = False
    fail_open_on_elided_evidence: bool = True
    """Degrade a rejection to a pass when the context carries elided tool results.

    A reviewer that cannot see the evidence cannot distinguish "unsupported" from
    "not shown to me", and it resolved that ambiguity against us: both recoverable
    items of one batch were correct drafts failed on every bullet with 87% and 100%
    of their tool results already replaced by the elision placeholder.
    """
    """Reject only for missing or evidence-contradicted answers: a reject
    verdict must name at least one unsupported claim, otherwise it degrades
    to a pass. Guards against an over-skeptical reviewer flipping a correct
    answer it merely could not verify from the evidence excerpt."""
    evidence_round: bool = False
    """dr@2.8: let a rejection buy retrieval instead of only a rewrite.

    A rejection names claims the evidence does not support. Answering it with
    "reuse the evidence already gathered" bounds the recoverable share at zero
    for every rejection whose cause is that the document was never retrieved --
    the largest remaining segment of the corpus-axis failure budget. With this
    on, the bounce grants a bounded, deeper, targeted retrieval round before the
    corrected answer.

    Class default false: the anchor arm builds no flow at all, but an arm that
    turns the flow on must still opt in, so this ships measurable against the
    dr@2.7 profile rather than folded into it.
    """
    evidence_round_searches: int = 6
    """Deeper searches granted per round. The bound is the point: the one
    mechanism that survived self-consistency on the corpus axis is context
    overflow prevention, and an unbounded round would spend the answer's
    context window to buy its evidence."""
    evidence_round_depth: int = 50
    """Result depth inside a round. 50 is the corpus retrieval service's own
    ceiling (``bcplus_serve.py`` clamps to it), and the depth the failure budget
    was measured at, so it neither under-reaches the measurement nor asks the
    backend for something it will silently not deliver."""


class DRFlowBudgetNoteConfig(_Base):
    """Budget visibility for the model: a budget line appended to tool
    results (persisted history is the only train-serve-safe channel)."""

    enabled: bool = True
    warn_ratio: float = 0.8


class DRFlowForceFinalizeConfig(_Base):
    """Forced-termination backstop: a terminal turn whose visible answer
    is empty (all reasoning) or a degenerate spin must not silently become
    the final answer. First a persisted commit nudge re-samples the turn;
    if the retry still carries no answer, an independent-context call
    salvages the best evidence-supported candidate from working memory."""

    enabled: bool = False
    max_nudges: int = 1
    model: str | None = None
    """Salvage model. ``None`` uses the provider's default model."""
    timeout_seconds: float = 240.0
    attempt_timeout_seconds: float = 240.0
    """Equal by default = one attempt, no mid-flight restart. A 35B MoE
    writing a 4k-token salvage under batch load routinely needs more than a
    minute; the earlier 60s attempt window turned every slow-but-healthy
    generation into three discarded restarts and a fail-open (measured: 62
    stalls, 18 exhausted budgets, and every context-overflow turn failing)."""
    max_tokens: int = 8192
    """Salvage completion budget, and the 4096 -> 8192 raise did NOT fix it.

    The salvage model prefills a think block, is asked for an answer plus evidence,
    and reads a ~17KB payload (median). At 4096 the budget went to thinking and the
    closing tag was never emitted, so ``visible_answer`` returned "" and the failure
    was filed as ``no_visible_answer`` - 13/13 and 15/15 of one batch's failures.
    After the raise the same physical event is filed as ``length`` (that branch did
    not exist before), so the relabel is instrument, not behaviour: the only
    comparable quantity is the failure RATE, 62.2% -> 50.0%, p=0.23. Two batches
    later salvage still fails more often than it succeeds (``length`` on 10/120 and
    13/120 against 11 and 7 commits).

    Do NOT raise this further. The committed answers need 121-389 tokens, so 8192 is
    already 21x the requirement, and this model on this endpoint has been observed
    stopping at exactly 16,384 on main-loop completions - a larger cap does not
    terminate the runaway shape. The binding constraint is that thinking cannot be
    switched off on this endpoint at all; see ``reasoning_effort``."""
    reasoning_effort: str | None = "low"
    """Salvage is extraction, not research - but on this endpoint this field is a NO-OP.

    It is transmitted (the provider puts it in the request body), yet the served
    Qwen3.6 template gates thinking on ``enable_thinking``, and no code path in this
    package sends ``chat_template_kwargs``: grep for it returns nothing, and the data
    agrees (5,902 of 5,978 assistant contents carry a closing tag while 0 carry an
    opening one, i.e. the template prefills it). So the salvage call runs with
    thinking ON and nothing bounding it but ``max_tokens``.

    Closing thinking needs a provider-layer channel that does not exist yet. Until it
    does, this field documents an intent rather than an effect, and lowering
    ``max_tokens`` on the strength of it would only cut the budget."""
    evidence_items: int = 8
    evidence_item_chars: int = 2000
    reasoning_excerpt_chars: int = 8000
    """Chars of the answerless response fed to the salvage call as
    researcher notes — candidates usually live in the reasoning."""


class DRFlowSpinBreakerConfig(_Base):
    """Restart-language circuit breaker. Density rollbacks replay a spin;
    the breaker instead intercepts the restart moment ("start from
    scratch", "completely different approach") and redirects the turn to
    commit an answer from the evidence already gathered. Joint criteria
    keep legitimate re-anchoring alive: repeated restart language, late
    budget, and entity overlap with the previous restart."""

    enabled: bool = False
    phrase_hits: int = 2
    """Restart-phrase iterations needed before intercepting; the first
    "different approach" of a turn is normal research, not a spin entry."""
    min_budget_ratio: float = 0.5
    """Minimum spent fraction of the iteration or context budget —
    restarts early in the turn are exploration, not degeneration."""
    min_entity_overlap: int = 2
    """Entities shared with the previous restart for it to count as
    re-plowing the same ground; a restart introducing new entities is a
    legitimate re-anchor and passes."""
    max_triggers: int = 1


class DRFlowFetchFloorConfig(_Base):
    """Search-without-fetch pathology guard: many searches with zero page
    opens cannot ground an answer (results are titles and links only).
    Appends an in-history note — the only train-serve-safe channel —
    nudging the model to open sources."""

    enabled: bool = False
    min_searches: int = 5
    max_notes: int = 2


class DRFlowFetchGateConfig(_Base):
    """Search-without-fetch guard at the action-space layer (dr@3.3).

    ``fetch_floor`` above observes the same streak and appends a sentence; that
    channel was measured on the four dr@3.0 arms at +4.2pp / +2.3pp on a ~10%
    base rate, i.e. real and an order of magnitude too small. This one withholds
    ``web_search`` from the iteration instead, so the refusal is not a move the
    model can re-request - the dr@3.0 saturation stop, which refuses in the tool's
    return value, was re-requested 195 times on one question.

    Off by default and gated on the flow, so the anchor cannot reach it.

    ``k`` defaults to 15, and that number was chosen after looking at the batch
    that motivated the rule: healthy first-fetch position has p90 = 13, and the
    advisory's second note at streak 10 is where the per-step fetch probability
    collapses. A data-chosen threshold has to be reported as one; the
    pre-registration says so in its own multiplicity section.
    """

    enabled: bool = False
    k: int = 15
    release_after_failed_fetches: int = 2
    """Consecutive failed fetch attempts while closed that reopen search for the
    rest of the turn. A threshold that can strand a run is structurally a
    zero-score bucket only the treated arm can fall into."""


class DRFlowFinalShapeConfig(_Base):
    """Terminal-answer shaping: make the turn's ending an observable.

    ``final_answer`` in the evaluation record is the last non-empty assistant
    ``content``, verbatim. Measured on a 302-question live-web batch: anchor
    median 1,759 chars / p90 41,683, and only 15.6% of anchor answers carry any
    answer marker at all (28.4% treated). One sampled treated answer opens
    "This is very helpful. According to the Nagada website: ..." - reasoning
    filed as an answer. ``finish_shape`` on the same arm was
    ``closed_with_answer`` 238 / ``closed_but_silent`` 25 / ``unclosed_think``
    39, with ``answer_visible=False`` on 21.2% (overflow 25, mid-think
    generation cap 25, unclosed think 10, iteration cap 4). So the turn's
    ending is a *passive* event today - something truncated it - and a report
    axis run on that shape would measure truncation rate, not report quality.

    Two independently switchable pieces, because their risk is not comparable:

    ``record`` is a **read-only observer**. It computes
    :func:`raven.agent.flow.final_shape.shape_final_answer` on the terminal
    content and records the result in the observer payload. It does not touch
    ``final_content``, the persisted message sequence, or anything the model
    reads, so the generated distribution is byte-identical with it on or off -
    score budget 0 by construction, and the flow-off anchor cannot move even if
    someone enables it there by mistake.

    ``require_marker`` appends one clause to the DR contract asking for an
    explicit ``<answer>`` marker. That **is** a distribution change (it changes
    what the model reads) and carries the usual obligations: a version bump, a
    fresh anchor pair, and a ``scripts/stamp_dr_segment.py`` artifact - the prompt
    never enters ``traj_raw.jsonl``, so the stamp is the only place this change
    is verifiable after the fact. dr@1.7 shipped three metadata keys that the
    persistence whitelist dropped and measured as its predecessor; "the code
    says so" is not acceptance.

    Both default **on** as of dr@2.6: a turn that ends on an explicit marker is
    the shipped behaviour, and a caller who does nothing should get it.

    They defaulted off through dr@2.5 on the stated grounds that "the anchor
    arms run this same code path". That reason was carried over from dr@2.4's
    snippet knob, where it holds - corpus retrieval is reached with the flow
    off, so an unconditional default really would have moved the anchor. It
    does not hold here: this hop is assembled inside
    :func:`raven.agent.flow.dr.build_dr_flow`, which returns ``None`` when the
    flow is off, and every anchor arm sets ``drFlow.enabled=false``. The
    reference frame is unreachable from this default in either state, which is
    what ``test_assembly_carries_the_record_flag_and_anchor_gets_nothing``
    pins. A safety argument is about a position in the call graph, so it does
    not travel between two knobs just because their risk language matches.

    What the flip *does* reach is every **enabled** arm that never mentioned
    this block, which is why each one now pins the pair explicitly rather than
    inheriting it. An inherited default is invisible in the arm's own config,
    so it changes what the arm measures without changing the file that
    describes the arm.
    """

    record: bool = True
    """Record the shaped form in the observer payload. Distribution-free.

    Free in both directions - it reads the terminal content and writes beside
    it - so the reason to default it on is that a run which did not record it
    cannot answer "how did this turn end?" afterwards, and that question is not
    reconstructible from a trajectory."""

    require_marker: bool = True
    """Ask the model for an explicit ``<answer>`` marker. Distribution change.

    Turning this off restores the dr@2.5 contract byte for byte, which is the
    supported way to run a measurement against a reading taken before the
    flip; ``test_off_state_contract_is_byte_identical_to_pre_flip`` pins it."""
    report_structure: bool = True
    """dr@2.8: ask for a research report. Product surface only. Distribution change.

    dr@3.4 (folded): the clause becomes a fixed three-section template - ``## Answer``,
    ``## Findings``, ``## Limitations``, exact headings, all three present every
    time - instead of an ordered-prose request with optional per-question
    sections. Same knob, same gating; only the appended text changed.

    dr@3.4 (folded): the template also overrides formatting instructions in the question
    itself - a question asking for one word, JSON, or a table is still answered
    as the three-section report, the requested shape satisfied inside it. The
    override passage has its own switch, ``report_format_override`` below; with
    this knob on and that one off, the clause is the pre-override template byte for
    byte. Benchmark profiles pin this off, so a bench answer keeps taking its
    format from the task text.

    The contract is five rules of research discipline and says nothing about the
    shape of the reply. That is deliberate on the measurement side - a bench answer
    gets its format from the task text ("organize the results in one Markdown table
    with the following columns: ..."), the same way a real user would ask, and a
    system prompt that pre-announced the format per benchmark would be measuring the
    router rather than the agent. The side effect is that ordinary use has no report
    shape either, which is what this adds.

    On by default because the default *is* the product: a caller who writes no
    config should get a research report. Every bench profile pins it off explicitly,
    so their DR segment stays byte-identical to the one their published readings were
    taken under - ``scripts/stamp_dr_segment.py`` is the artifact that proves it, and
    the batch runner's knob gate fails the batch if a bench arm ever inherits this.

    Prompt-side only, and it will stay that way. Restructuring a finished answer into
    a report is the failure class this repo has measured twice: an upstream framework's
    extraction stage carried gold on 71/120 questions into 58/120 boxed fields -
    inventing nothing, dropping 10.83pp - and dr@1.6's salvage seam failed the same
    way. ``final_shape``'s transform is pure, additive and non-shortening by
    construction; a restructure cannot be, so it is asked for during generation
    instead."""
    report_format_override: bool = True
    """dr@3.4: the report template takes precedence over formatting instructions
    in the question itself. Product surface only. Distribution change when
    ``report_structure`` is on; inert when it is off, because the passage lives
    inside the report clause.

    Its own switch because the override has a price worth measuring - it trades
    per-question format compliance for a stable reply shape - and an unablatable
    claim in a prompt cannot be priced. Off restores the pre-override clause byte for
    byte (the template that fixed the layout only where the question was silent),
    which both keeps the pre-override prompt reproducible from this build
    and lets a same-batch A/B run template-vs-template+override;
    ``test_the_dr_prompt_bytes_match_the_batch_that_measured_them`` pins both
    states."""
    report_depth: bool = False
    """The deep report template. Product surface only. Distribution change when
    ``report_structure`` is on; inert when it is off, because the switch only
    selects which report clause that knob appends.

    On, the segment builder renders ``_DR_REPORT_STRUCTURE_CLAUSE_DEEP`` in
    place of the dr@3.4 template: same three sections, ``## Findings`` upgraded
    from a findings list to a full argued report (causal narrative, per-datum
    source and as-of date, disagreements adjudicated in the open, facts
    separated from forward-looking judgments, tracking signals inside
    ``## Limitations``). Off, the clause is byte-identical to dr@3.4 - the same
    escape hatch ``require_marker`` documents above.

    Off by default because it is unmeasured: a bundle of prompt commitments
    priced together. It carries no label of its own - under the launch
    convention (see ``version``) labels advance when a batch finishes, not when
    a distribution changes - so an A/B arm that switches it on pins the current
    default plus a ``-depth`` suffix in its own config (the suffix convention
    ``dr@3.4-askuser`` established; the validator names the required base
    whenever a stale one is pinned), while examples never pin ``version`` at
    all (the loader test enforces both). Product
    profiles that want the deep template set ``reportDepth: true`` explicitly
    rather than inheriting a default, for the reason stated in the class
    docstring. When reading the A/B, judge the ``report_shape`` counters with
    ``off_level_headings`` alongside ``well_formed``: the deep clause invites
    ``###`` subheadings, and a stem-matched ``###`` inside Findings can mark a
    genuinely missing section as present."""
    report_reminder: bool = True
    """dr@3.4: repeat the template as a short block on the current user message.

    Product surface only, and inert unless ``report_structure`` is on - it
    restates that clause, so without it there is nothing to restate. Distribution
    change: the model reads it.

    The clause it repeats sits in the system prompt, which is the weakest place
    for an instruction a conversation can argue with. Measured on this repo's demo
    sessions (30 turns carrying the template): 8/14 research turns well-formed
    against 2/9 of the turns the conversation gate ruled non-research - the
    reformat follow-ups, whose sessions held two or three earlier outline replies.
    Against that few-shot, recency is the variable worth moving, not wording.

    Rides the current user message and is stripped before persist, the same seam
    and lifetime as the research memo, so it never becomes history and never
    accumulates. See ``flow/report_shape.py``."""
    report_bounce: bool = False
    """dr@3.4: bounce a terminal draft that is missing a template section, once.

    Product surface only, inert unless ``report_structure`` is on. Distribution
    change of the strongest kind - it decides which turns are re-sampled.

    Off by default, unlike the reminder, and the reason is measurement rather
    than doubt. The 43%-malformed baseline above is an upper bound: the session
    record stamps ``flow_version`` but not the ``finalShape`` knobs, so some of
    those turns may have run with the template off entirely. A knob that spends a
    whole extra generation on every malformed turn should be priced before it
    ships on, and this one can be: turn it on for a product profile, then read
    ``report_shape_gate.bounces`` against ``report_shape_gate.shipped_malformed``
    (the gate's own friction; ``report_shape`` next to it carries the shape that
    actually shipped). The reminder is the half that is cheap enough to default
    on.

    Price it with ``record`` on. The rewrite's shrink is
    ``report_shape_gate.draft_chars`` against ``final_shape.visible_chars``, and
    only the first of those is exported unconditionally - a ``record: false`` arm
    would collect a numerator with no denominator, i.e. no way to ask whether a
    bounce cost evidence, which is the one risk this gate is designed around.

    Deterministic and model-free, so unlike the verify gate it is affordable on
    every turn - which is the point, since the stratum it exists for is the one
    the verify gate never sees."""
    process_appendix: bool = True
    """dr@2.8: append a deterministic research trail. Product surface only.

    An auditable report needs a record of how the answer was reached. Asking the
    model for it in the contract buys a *self-report* - unverifiable, competing with
    evidence for the window, and free to be wrong in the flattering direction; this
    repo already trusted one such probe (``arm_env``'s tool-surface stamp) and it
    misses whatever nobody happened to call. The real trail is in the ledger, written
    as each event happened, so the body is the model's and the appendix is computed:
    zero model tokens, nothing to invent, every line checkable.

    Distribution-neutral by construction, unlike the two clauses above. It runs after
    the turn's last generation and attaches to the returned value, not to the
    persisted message, so no model reads it in this turn or as history in the next.
    Bench arms pin it off anyway, because it would append text a grader would have to
    parse around.

    It also carries the only report-quality number here that needs no judge and does
    not reward length: a URL cited in the answer that appears in no fetch record is a
    fabricated citation, and ``citation_grounding_rate`` counts them. Null rather
    than 1.0 when the answer cites nothing - an answer with no sources is not
    perfectly grounded, it is unmeasured."""


class DRFlowConversationConfig(_Base):
    """Multi-turn behaviour for DR mode. Product surface; measures nothing.

    Every DR reading in the ladder is turn one of a fresh session - the bench
    harness sends one message per question - so nothing here has ever run under
    measurement, and nothing here is on by default. It is also structurally
    unreachable from a bench arm even if a config turned it on: every knob below
    is read on turn two or later, and a bench turn is always turn one.

    See ``raven/agent/flow/conversation.py`` for why the gate cuts behaviour and
    never prompt text, and why every classifier failure resolves to a research
    turn.
    """

    enabled: bool = False
    gate: Literal["always", "agentic"] = "agentic"
    """Turn two onwards: ``always`` keeps DR on for every turn (the pre-dr@3.0
    behaviour, made explicit); ``agentic`` asks a classifier per turn.

    Turn one is not covered by either - it follows ``DRFlowConfig.enabled``. A
    conversation's first message is the one the user opened the product to ask,
    and there is no conversation yet for a classifier to read."""
    gate_model: str | None = None
    """``None`` uses the loop's model. A small fast model is the point of the
    knob: this call sits in front of every turn's first token of latency."""
    gate_max_tokens: int = 1024
    """Completion budget for the verdict, which is two JSON fields.

    256 was sized for those two fields alone. On a reasoning model the budget goes
    to ``reasoning_content`` first and ``content`` comes out empty, so the verdict
    read as ``gate_unparsed`` and the turn ran research it did not need - observed
    on a live deepseek-v4-flash turn at reasoning effort high, the same failure the
    judge hit at 512.

    Raised only far enough to leave headroom, because the cure for that failure is
    ``gate_reasoning_effort``, not a bigger cap. A cap is not free here: the model
    that motivated the change is one that WILL spend it, this call sits in front of
    every turn's first token, and once the spend exceeds
    ``gate_timeout_seconds`` the timeout fails open to the most expensive outcome
    there is - a research turn nobody asked for."""
    gate_reasoning_effort: str | None = "low"
    """Reasoning effort for the gate call.

    Same disease and the same prescription as ``forceFinalize.reasoningEffort``:
    the task is a two-field classification over six messages, and the measured
    failure is a call that spent its whole budget thinking. ``None`` leaves the
    provider default, for backends that reject the parameter.

    There is deliberately no fallback to ``reasoning_content`` when ``content``
    comes out empty. It could only ever flip the outcome one way - failing open
    already yields ``research=true``, so anything read out of the reasoning
    channel can only turn a needed research turn OFF - and it would be read from a
    channel that carries discarded drafts. That is the same unsafe direction the
    ``finish_reason == "length"`` guard in ``conversation.py`` already refuses."""
    gate_timeout_seconds: float = 20.0
    gate_history_messages: int = 6
    gate_history_chars: int = 4000
    research_memo: bool = True
    """Carry a compact record of what was searched and opened into later turns.

    On because the alternative is not "no memo" but "a lossy one": tool results
    are truncated to 16k chars when persisted and then dropped whole by the
    trimmer, so a later turn asking about a cited source is answered from
    whatever survived. Inert unless ``enabled``."""
    memo_max_chars: int = 2000
    memo_max_sources: int = 12
    memo_max_queries: int = 12
    memo_max_opened: int = 200
    """How many opened URLs the fabricated-citation check will accept from earlier turns.

    A second bound rather than a larger ``memoMaxSources`` because the two serve
    opposite pressures: the rendered source list costs context on every remaining turn
    and wants to be short, while the check's accept-set never reaches the model and
    wants to be complete. Sharing one number makes it wrong on one of them - and it was
    wrong on the side that accuses the answer of inventing a citation. Bounded at all
    only so a very long conversation cannot grow the session file without limit."""
    identity_scope: Literal["turn", "topic"] = "turn"
    """Scope of the web tools' "already seen this" sets.

    ``turn`` is the measured behaviour: every set clears at the turn boundary
    because on a benchmark item the same query on a later turn is a re-check,
    not a loop. ``topic`` keeps them across a session's turns so a follow-up does
    not re-search and re-open the previous turn's pages.

    ⚠️ ``topic`` has a real failure mode and that is why it is not the default:
    the saturation rule's identity set carries over too, so a follow-up whose
    first searches legitimately re-find the previous turn's pages accumulates a
    dry streak it did not earn, and search can close earlier than on turn one.
    The stop flag and the streak itself are always cleared - only the identity
    set carries - which bounds the effect but does not remove it."""


class DRFlowAskUserConfig(_Base):
    """Turn-boundary user interaction: one clarifying question round, optionally
    with an outline of the research to come. Product surface; measures nothing.

    Resolved against ``conversation.enabled`` in ``build_dr_flow``: without a
    second turn there is nowhere for an answer to arrive, so the handoff would be
    a dead end. That makes a bench arm doubly unreachable - the gate is only
    built when both are on, and ``toolsAllowlist`` does not carry the tool.

    Carries no ``version`` of its own, for the same reason ``conversation`` does
    not: a label marks a measured build, and this cannot appear in a measurement.
    Product batches name it with the profile suffix ``dr@3.4-askuser``, which the
    validator accepts because it checks the base label only.
    """

    enabled: bool = False
    mode: Literal["when_needed", "first_turn"] = "first_turn"
    """When the contract ASKS for a clarify round, not whether the tool exists.

    ``first_turn`` (default): turn one of a conversation always asks, later turns
    ask only when something is genuinely undecidable without the user.
    ``when_needed``: every turn asks only on that condition.

    Product-only either way - ``enabled`` is False by default and resolves against
    ``conversation.enabled``, so no benchmark arm can reach either value.

    ⚠️ This is a REQUEST, not a guarantee. Nothing can make a model emit a tool
    call; the gate can only offer the tool and record what happened. ``asked`` on a
    first turn under this mode is therefore a compliance rate, and it has to be
    read rather than assumed. Enforcement (bouncing a first turn that did not ask)
    is deliberately absent: it would re-sample turns, which is the strongest kind
    of distribution change, and the compliance rate has to be known first.

    ⚠️ The cost, stated rather than absorbed. A question has a threshold - only the
    user can settle it - and requiring one removes the threshold, so the failure
    mode is filler questions ("how deep would you like?") that teach users to
    ignore the round entirely, including the time it matters. Two measured prices
    ride along: dr@3.4 found replies whose history carries an outline well-formed
    2/9 against 8/14 elsewhere, and this mode pays that on every conversation; and
    a question that needed no clarification pays a full round trip for nothing.
    ``when_needed`` is the byte-identical way back, and D-arm ask-rate data (see
    README) is what should decide between them."""
    delivery: Literal["handoff", "tool"] = "handoff"
    """How the questions reach the user once the model calls ``ask_user``.

    ``handoff`` (default, the dr@3.4-askuser measured behaviour): the gate
    short-circuits the turn, the rendered questions become the reply, and the
    user's next message carries the answer. ``tool`` routes the call through the
    blocking ``QuestionBroker`` round trip instead: the user answers a structured
    prompt (TUI / gateway), the answers come back as the tool result, and the
    SAME turn researches on them - the question text never becomes the reply, and
    no pending / chain round is spent. On a surface with no broker wired (ACP,
    ``raven agent -m``) the gate falls back to the handoff, so the round is never
    silently dropped.

    Byte-visible: the contract clause, the identity rewrite and the tool
    description all change with it, so ``handoff`` is the byte-identical way back
    to the measured prompt. Product-only either way - resolved against the same
    two switches as the rest of this block."""
    outline: bool = True
    """Ask for an outline (sub-questions, evidence kind, deliverable) alongside
    the questions.

    Default on because that is what the product is: one round trip carrying both
    halves, the same argument as ``finalShape.reportStructure``. Every shipping
    profile pins it explicitly anyway.

    There is deliberately no "outline only, no questions" state. A question has a
    threshold - only the user can settle it - and an outline has none, so a model
    that wants to look diligent can always produce one; a mode that let an outline
    alone end the turn would degrade "one optional confirmation" into "every
    research item costs an extra round trip". ``questions`` non-empty is therefore
    a necessary condition for the handoff, enforced by the gate rather than by the
    tool schema: ``"questions": []`` satisfies a JSON-Schema ``required``."""
    max_rounds: int = 1
    """How many times ONE clarify chain may ask, not how many times a session may.

    The count passes THROUGH the turn that consumes the pending state, and the
    chain closes when a turn asks nothing further. Scoping it to the session
    instead would spend the budget on a conversation's first research question and
    refuse the second one - and a session holding two unrelated questions is a
    convention this code cannot enforce, so the failure would be silent.

    Handoff accounting only. ``delivery="tool"`` opens no chain - the round
    trip writes no pending, so nothing here debits - and its budget is
    structural instead: the gate grants one round trip per turn and withdraws
    the tool once it is spent (``withdrawn_after_ask``)."""
    first_iteration_only: bool = True
    """Register the tool on the first iteration of a research turn only.

    Withheld afterwards through ``HookDecision.modified_tools`` rather than
    refused in a return value: dr@3.3 measured what refusing costs - 72 of 84
    questions searched again on the next step, one turn accumulating 195
    suppression records."""
    brief_requires_answer_check: bool = True
    """Run the ``is_reply_to`` predicate before injecting the research brief.

    Off means every message following a handoff is treated as its answer. The
    predicate decides ONLY whether the brief is injected; whether the turn runs
    research stays with the conversation gate, which exists for exactly the
    "reformat what you just wrote" case a crude predicate would misread.

    ⚠️ Not inert while ``brief`` is off. The same verdict drives
    ``set_chain_round(0)`` on the ``new_request`` branch, and the chain count is
    what makes the next turn ``chain_exhausted`` - so at ``maxRounds: 1`` this
    knob decides whether the tool is offered again on the turn after a handoff.
    ``reply_overlap`` is recorded either way, but it is a column partly censored by
    its own threshold: stratify on whether a second handoff happened before
    fitting anything to it."""
    reply_overlap_threshold: float = 0.05
    """Character-bigram overlap below which a long message is read as a new
    request rather than an answer.

    Characters, not words: the questions can be Chinese, where "word overlap"
    needs a tokenizer, and a coarse predicate whose errors cost one paragraph of
    context does not justify a new dependency. Biased towards "answer": a new
    request misread as an answer costs an extra paragraph in the brief, while an
    answer misread as a new request throws away everything the user just said.

    Measured against the whole handoff, options included - see
    ``PendingClarify.reference_text`` for why the question stems alone scored two
    real answers at 0.000."""
    max_questions: int = 3
    max_outline_items: int = 5
    prompt_clause: bool = True
    """Render the contract clause. Off registers the tool without asking for it -
    diagnostic only, and it re-opens the mismatch the clause exists to close."""
    brief: bool = False
    """Inject the deterministic ``original question + Q/A + outline`` block on the
    turn that answers. Verbatim transcription, never a summary: the block is
    computed, so a misread predicate leaves a reply that plainly is not an answer
    rather than a fabricated question-answer pair.

    ⚠️ Off, and its benefit is unproven. A pending is popped by the immediately
    following user message, so at the moment of injection the block's two halves
    ARE the two most recent messages in the conversation - the original question,
    then the handoff - and they are the last things any trimmer would reach. The
    block adds salience, not facts. Two live sessions ran with it dark (the
    predicate read both replies as new requests) and both answering turns picked
    up the clarified scope correctly.

    Its value is premised on ``maxRounds > 1``: a second round's brief would carry
    the FIRST round's answers, which by then are no longer adjacent and the
    adjacency argument above stops holding. At ``maxRounds: 1`` there is no such
    case. Turn it on with that, not on its own."""


class DRFlowConfig(_Base):
    """Deep-research flow: a versioned set of observers, prompt section,
    tool shaping and budgets attached to the agent loop's seams.

    ``version`` is stamped as a prefix on every trajectory line's
    ``flow_version`` — bump it whenever any default here (or the DR
    prompt section) changes, because these settings define the training
    distribution the flow produces.
    """

    enabled: bool = False
    version: str = "dr@3.5"
    """dr@3.5 is the first label under the LAUNCH convention (user's call,
    2026-08-25): a label names the code a BATCH was run under, so it advances
    when a batch finishes, not when a distribution changes. dr@3.4's web batch
    (``eval_web_dr34_dsv4f0731_20260821``, four arms, adjudicated) closed it out,
    which is why this build is dr@3.5 even though everything landed since is
    default-off or pure instrumentation.

    That convention VOIDS the 2026-08-20 fold record: dr@3.5/3.6/3.7 are no
    longer "labels upstream already spent", they are simply the next three rungs,
    and the ``_FOLDED_VERSIONS`` table that used to reject them is gone. Checked
    before removing it - nothing outside this file pins any of the three: no
    config under ``pipeline/configs`` (live or retired), no batch script, and no
    ``arm_env.json`` under ``data_dr/runs/``. That last clause has power rather
    than being vacuous, which had to be established separately: an arm DOES stamp
    the label, in two fields - ``dr_version`` ("dr@3.4 (enabled=True)") and
    ``config_drflow_version`` - and ``FOURCELL.json`` and ``student_runtime.json``
    carry it too. So the empty result is a real absence, not a grep over a field
    that was never written. A label nothing wears cannot be mislabelled by
    reusing it.

    (Retained from the fold note, because it is what makes the convention safe:
    the join key for a reading was never the label, it is ``dr_segment_sha``
    (AGENTS.md 0.2), so renaming or reusing a label cannot detach a reading from
    the build that produced it.)

    History, kept because the surfaces it names are still in this build.
    dr@3.4 folded four labels into one (user's call, 2026-08-20): the
    turn-scoped flow consumers this label originally named, plus the fixed report
    template, its precedence rule, the per-turn reminder and the optional shape
    bar - upstream carried those last three as dr@3.5, dr@3.6 and dr@3.7 before
    the fold.

    Folded rather than left as four bumps because a label protects readings that
    already carry it, and no reading in this repo wears one of the four. Widening
    a label no data wears cannot mislabel anything - the same reasoning that kept
    dr@3.3 from bumping.

    How that was established - and the 2026-08-21 "correction" of it was itself
    wrong, corrected again 2026-08-25. The original note said "every
    ``arm_env.json`` under ``data_dr/runs/`` stamps dr@3.3 or older". The 08-21
    pass called that an overstatement on the grounds that an arm records
    ``config_sha``/``dr_segment_sha``/``git_head``/``tree_diff_sha`` "and no label
    at all, so it can neither confirm nor deny wearing one". That is false, and
    checking a real file is what showed it: ``arm_env.json`` carries ``dr_version``
    and ``config_drflow_version``, both spelling the label out, so the original
    claim was directly checkable all along. Worth keeping as a shape rather than
    just fixing: a correction that says "the record cannot answer this" is the
    kind that stops anyone from looking, so it needs the same evidence bar as the
    claim it retracts - here, one ``json.load``.

    The conclusion holds either way, and the indirect leg is still the load-bearing
    one because it does not depend on any file's field list: the join key for a
    reading was never the label, it is ``dr_segment_sha`` (AGENTS.md 0.2), so
    renaming or reusing a label cannot detach a reading from the build that
    produced it. ``data_dr/runs/`` does not exist in any worktree either.
    ⚠️ The pre-fold docstrings asserted otherwise in two places - "the one dr@3.4
    batch (w302s100)" and "dr@3.5 runs were taken under the
    template-without-override clause" - and neither run exists in this repo's
    ``data_dr/runs/``. Those claims are recorded here as UNVERIFIED rather than
    deleted: if either batch turns up, its readings sit under the pre-override
    clause and this fold has to be revisited.

    Cost of the fold, same as dr@3.3's: dr@3.4 now corresponds to several
    behaviourally different commits, so a batch citing it can be traced only
    through its own ``_src_snapshot`` + ``iso_manifest.json``, not through the
    label.

    What the folded-in surfaces do: the report template stops being a request
    nothing reads back - a per-turn reminder rides the current user message
    (``finalShape.reportReminder``, default on) and a deterministic markdown bar
    can bounce a draft missing a section (``finalShape.reportBounce``, default
    off). The system prompt is byte-identical to the pre-reminder build's; the
    reminder is what the model reads differently. The template itself is a fixed
    three-section layout (``## Answer`` / ``## Findings`` / ``## Limitations``) in
    place of dr@2.8's ordered-prose request, and it takes precedence over
    formatting instructions in the question itself; see
    ``DRFlowFinalShapeConfig.report_structure``. The override rides its own
    switch, so the pre-override clause stays reproducible from this build byte
    for byte.

    Anchor: cannot move. Every folded-in surface reads assembly fields or
    resolves to off unless ``report_structure`` is on, which every bench profile
    pins off.

    dr@3.4 also carries the out-of-band reasoning waiver on the closing-tag bar
    (``flow/answer_text.closing_tag_bar``): a response that delivered its reasoning
    via ``reasoning_content`` - the channel LiteLLM keeps separate from ``content`` -
    holds only answer text in ``content``, so requiring ``</think>`` there erased
    every complete answer. Measured on the OpenRouter live-web config: 4 of 4 turns
    force-finalized as ``empty_visible_answer`` while carrying a full report, the
    commit nudge burned a re-sample each time, and the verify gate (which reads the
    same bar as of this build) never saw a draft.

    Folded into dr@3.4 rather than bumped (user's call, 2026-08-18), same reasoning
    as the dr@3.3 turn-task fold: a bump protects a reading that already carries the
    old label, and the one dr@3.4 batch run before the fold (w302s100) pinned
    ``thinkClosingTagRequired: false`` on every arm - a configuration in which the
    waiver is structurally unreachable, so no reading is mislabelled by widening the
    label. The recurring cost is the same as dr@3.3's: the label maps to more than
    one commit, and provenance for any batch has to come from its own
    ``_src_snapshot`` plus the per-arm fingerprints in ``iso_manifest.json``, never
    from the label alone.

    It cannot move an anchor: every changed site keys on the flow's bar, which is
    False whenever the flow is off, and the verify/finalize gates are flow hooks
    that do not exist on the flow-off arm."""
    max_iterations: int | None = None
    """Iteration budget for DR turns. ``None`` keeps the agent default."""
    context_window_tokens: int | None = None
    """Context window for DR turns. ``None`` keeps the agent default (same
    pattern as ``max_iterations`` above), so declaring this field changes no
    existing batch's behavior.

    Exists to let a DR arm's window be A/B'd independently of the flow-off
    anchor's - today ``agents.defaults.contextWindowTokens`` is the only
    knob, and it sets ``available_history`` on both arms (AGENTS.md #0.3), so
    raising it to test a budget-control fix on the treated arm would also
    move the anchor. Capability only: nothing in this build sets it.

    ``agents.defaults.contextWindowTokens`` is itself a fallback, not a
    declaration: ``AgentLoop._window_for`` resolves the model's real window
    first and only falls back to this value when the model is unknown to the
    resolver. On a model the resolver knows, setting this field would still
    be silently overridden the same way the top-level default already is -
    the biggest candidate use (re-pricing the timeout/iteration-cap
    population on a strong backend) has to wait for that resolution order to
    be made explicit, not for anything in the token estimator."""
    tools_allowlist: tuple[str, ...] = ("web_search", "web_fetch")
    """Tools visible to the model in DR mode; everything else is
    unregistered at assembly. Every extra tool is context noise, a
    mis-call surface and a distribution drift risk. Empty = no slimming.

    A shell is deliberately absent. When retrieval is pinned to a fixed
    corpus, a shell reaches the open web around it, so the containment the
    measurement rests on is void without anything in the config looking
    wrong. Every eval arm already pins this list explicitly, so dropping
    ``exec`` from the default changes no measured distribution."""
    think_closing_tag_required: bool = True
    """Treat a terminal turn carrying no ``</think>`` as having produced no
    answer.

    The served template prefills the opening tag, so a complete turn always
    carries a closing one; a turn cut mid-reasoning carries neither tag, and
    then reasoning is indistinguishable from an answer by inspection. Without
    this, ``visible_answer`` hands the whole chain of thought back as the
    answer, the answerless terminal seam never fires, and the reasoning is
    persisted and scored as the final answer. Measured on the dr@1.4 batch:
    56 of 230 questions ended that way, judged correct 0/56.

    Turn this off for a serving stack that emits no think tags at all -- there
    every answer would otherwise be erased.

    dr@3.4 (folded in): waived per-response when the reasoning arrived
    out-of-band (``reasoning_content``) - content then carries only answer
    text and a closing tag can never appear in it, so the bar's premise is
    void for that response. Measured cost of not waiving it, on a
    channel-separated stack: 4 of 4 live-web turns force-finalized
    ``empty_visible_answer`` while carrying a full report, and the verify gate
    never saw a draft - the bar erased every complete answer. (Recorded here
    from the README ladder, which was the only place carrying the number.) The flag now means "require the tag on responses
    whose reasoning is inline". A stack that never inlines reasoning should
    still set this false: a response that skipped reasoning entirely carries
    neither channel and would be erased by the bar."""

    minimal_context: bool = True
    """Drop the assistant-product prompt segments (bootstrap / memory /
    skills) so the system prompt carries only identity + DR contract."""
    prompt_section_override: str | None = None
    """Replace the DR CONTRACT section. The identity block is unaffected."""
    identity_override: str | None = None
    """Replace the DR identity block. The contract is unaffected.

    Neither override can delete the other: DR mode drops the product identity
    segment, so an override that removed the identity would leave the model with no
    statement of its tool surface and no untrusted-content boundary rule.
    """
    measured_guidance: bool = True
    """Include the four empirical claims the DR identity states to the model.

    They shipped inside dr@2.0's four-change bundle with no way to ablate them, and
    the risk runs both ways - "correct runs are short" can be read as "commit early".
    Set false on one arm to price them.
    """
    """Replace the DR contract text wholesale. ``None`` keeps the built-in one.

    The built-in contract ends with "state the answer first, then the key
    evidence" -- correct when a grader reads an answer, wrong when the grader
    parses a fixed token at the very end of the output. A task that pins its
    own output shape needs the contract to agree with it, and editing the
    module constant would silently redefine every version label that has
    already been measured. Set this only in a profile carrying its own
    ``version`` string, never to patch a measured lineage version."""
    fetch_max_chars: int = 14_000
    """Page-text cap for ``web_fetch`` in DR mode. Must stay below the
    loop's persisted-tool-result cap (16k) with room for the untrusted
    fence and in-history notes, so the persisted trajectory equals what
    the model saw (train-serve homomorphism)."""
    search: DRFlowSearchConfig = Field(default_factory=DRFlowSearchConfig)
    truncation_wrapup: DRFlowTruncationWrapupConfig = Field(
        default_factory=DRFlowTruncationWrapupConfig
    )
    """dr@2.9's completion-truncation wrap-up, gated off by default from dr@3.0."""
    reactive_clamp: DRFlowReactiveClampConfig = Field(
        default_factory=DRFlowReactiveClampConfig
    )
    """The reactive completion clamp, off by default. Structurally unreachable on
    every arm until switched on, therefore no version bump - same reasoning as
    dr@3.0's multi-turn surface."""
    digest: DRFlowDigestConfig = Field(default_factory=DRFlowDigestConfig)
    verify: DRFlowVerifyConfig = Field(default_factory=DRFlowVerifyConfig)
    budget_note: DRFlowBudgetNoteConfig = Field(default_factory=DRFlowBudgetNoteConfig)
    force_finalize: DRFlowForceFinalizeConfig = Field(default_factory=DRFlowForceFinalizeConfig)
    spin_breaker: DRFlowSpinBreakerConfig = Field(default_factory=DRFlowSpinBreakerConfig)
    fetch_floor: DRFlowFetchFloorConfig = Field(default_factory=DRFlowFetchFloorConfig)
    fetch_gate: DRFlowFetchGateConfig = Field(default_factory=DRFlowFetchGateConfig)
    """dr@3.3. Off by default, and unlike the reactive clamp this one DOES carry a
    version bump: an arm that switches it on changes both what the model reads and
    which tools it can call, so the label has to be able to tell that arm apart."""
    final_shape: DRFlowFinalShapeConfig = Field(default_factory=DRFlowFinalShapeConfig)
    conversation: DRFlowConversationConfig = Field(default_factory=DRFlowConversationConfig)
    """dr@3.0 product surface: multi-turn. Off by default and turn-two-onwards
    only, so no bench arm can reach it - the harness sends one message per
    question. Carries no ``version`` of its own for that reason: a label marks a
    measured build, and this cannot appear in a measurement."""
    ask_user: DRFlowAskUserConfig = Field(default_factory=DRFlowAskUserConfig)
    """Product surface: a clarify round at the turn boundary. Resolved against
    ``conversation.enabled``, and carries no ``version`` of its own for the same
    reason that one does not - see DRFlowAskUserConfig."""

    _SUPERSEDED_VERSIONS = (
        "dr@1", "dr@1.0", "dr@1.1", "dr@1.2", "dr@1.3", "dr@1.4", "dr@1.5", "dr@1.6", "dr@1.7", "dr@1.8",
        "dr@1.9", "dr@2.0", "dr@2.1", "dr@2.2", "dr@2.3", "dr@2.4", "dr@2.5", "dr@2.6",
        "dr@2.7", "dr@2.8", "dr@2.9", "dr@3.0", "dr@3.1", "dr@3.2", "dr@3.3",
        # dr@3.4 retired itself by finishing its batch - that IS the launch
        # convention. Its four arms are adjudicated and its configs still say
        # dr@3.4 inside their own ``_iso`` snapshot, which is what a re-run reads,
        # so superseding the live label cannot restamp that reading.
        "dr@3.4",
    )

    # There is no second table. Under the launch convention a label names the
    # code a batch ran under, so dr@3.5/3.6/3.7 are ordinary future rungs and
    # the "folded, therefore rejected" tier they used to occupy has no members
    # left. Removed rather than emptied: a branch that can never fire is
    # indistinguishable from one that is absent, and this repo has been bitten
    # by a never-firing clause before.

    @model_validator(mode="after")
    def _version_matches_build(self) -> "DRFlowConfig":
        """Reject a superseded label on this build.

    This build's flow semantics are dr@3.4: everything dr@3.3 carried, plus the
    turn-scoped flow consumers, the fixed report template, its precedence rule,
    the report-template reminder and the optional shape bar.

    (dr@3.4, folded: reminder + shape bar) The template gains the two things a prompt clause cannot give it.
    ``finalShape.reportReminder`` (default ON) appends a ~60-token restatement to
    the current user message at assembly, stripped before persist like the
    research memo, so the instruction is adjacent to the generation instead of
    thousands of tokens up the context - measured motivation: 8/14 well-formed on
    research turns against 2/9 on the gate's non-research turns, whose sessions
    carried earlier outline replies as few-shot. ``finalShape.reportBounce``
    (default OFF) installs ``ReportShapeGate``, a deterministic markdown check
    that bounces a draft missing a section back once; it is the only DR observer
    NOT wrapped in ``GatedHook``, deliberately, because the non-research turn is
    the stratum it exists for and it makes no model call. The system prompt is
    byte-identical to the pre-reminder build - both surfaces keep their shas - but the reminder
    changes what the model reads on every product turn, which is the bump.

    Anchor: cannot move. Both read assembly fields, and ``build_dr_flow`` returns
    None on the flow-off arm; both additionally resolve to off unless
    ``report_structure`` is on, which every bench profile pins off.

    (dr@3.4, folded: format override) ``_DR_REPORT_STRUCTURE_CLAUSE`` gains the precedence rule: the
    three-section layout overrides formatting instructions in the question
    itself, and the clause names where a requested shape goes instead. The knob,
    its gating and its numbering are unchanged; only the appended text moved, so
    the bench contract (both optional clauses off) and the marker-only surface
    keep their measured shas byte for byte. It is a distribution change on every
    arm that runs ``finalShape.reportStructure=true`` - the product surface,
    including the live-web profile - and no run was ever taken under any of the three superseded labels (see the fold
    note on ``version``), which is why this is a fold and not a bump.
    The passage rides its own switch (``finalShape.reportFormatOverride``,
    default on, byte-identical to the label's semantics at that default); off
    restores the pre-override clause byte for byte, so the pre-override prompt
    stays reproducible and the override can be priced same-batch.

    (dr@3.4, folded: fixed template) ``_DR_REPORT_STRUCTURE_CLAUSE`` is rewritten from an ordered-prose
    request into a fixed three-section template with exact headings (``## Answer``
    / ``## Findings`` / ``## Limitations``, all three present every time). Folded
    into dr@3.4 rather than bumped: dr@3.4 itself never carried a batch either, so
    there is no reading to protect from either clause.

    Anchor: cannot move. The clause is appended inside ``DRModeSegmentBuilder``,
    which only exists on a flow assembly; the flow-off arm has no DR segment at
    all, and every bench profile pins ``reportStructure`` off explicitly.

    (dr@3.4) Everything dr@3.3 carried, plus the cross-turn scoping fixes,
    per-query pagination depth, and the out-of-band reasoning waiver on the
    closing-tag bar (folded in later; see ``version``).

    (dr@3.4) Three flow consumers stop re-deriving "this turn's messages" from the
    whole assembled context, via ``AgentHookContext.turn_base``: the fetch gate's
    first-iteration scan (a previous turn ending on a >=15 unread-search tail closed
    ``web_search`` on the next turn's first iteration, with no notice because the
    newest message is the user's question), the fetch floor's watermark (previous
    turns' tool results double-booked into the new turn's streak), and the verify
    gate's elision snapshot (elision placeholders persist, so one left on disk by any
    earlier turn degraded every later turn's reject to a pass for the rest of the
    session). On a single-turn benchmark the slice IS the whole list, so no measured
    arm's bytes move; only conversations change. Pagination depth is additionally
    keyed on the query family (``SearchSaturation.page_for``): the ladder's
    escalation level stays turn-global, but a brand-new query is no longer sent to
    page 2 - Serper ranks 11-20 for terms whose first ten ranks nobody had seen, whose
    near-certain empty return then scored as dry and fed the ``stop`` rung. That is a
    distribution change on saturation-enabled arms, hence the label.

    Anchor: cannot move. Every touched consumer exists only on a flow assembly - the
    flow-off arm has no hooks and no saturation rule - and the turn slice is
    byte-identical to the full list on a single-turn run.

    The remainder below is the dr@3.1 entry this docstring carried when the ladder
    was last rewritten in place; kept for the same reason the README ladder is
    append-only. (5) and (7) are product-surface only and reach no arm; they are
    listed so the label's contents are complete.

    (9) The replay cache is keyed on the page as well as the two widths. dr@3.0 made pagination
    reachable and left ``page`` out of that key, so a page-2 request could be answered from the
    page-1 entry the same query had cached earlier in the turn. Measured on the 302 per-question
    ledgers of the dr@3.0 live-web DR arm: of the content-returning searches issued while the rule
    had escalated to page 2, 206 replayed page-1 content against 83 real page-2 requests - 71.3%
    of the paginated searches handed back exactly the page the rung existed to move past. The
    second-order effect is worse than the waste: a replay is scored as a dry search
    (``SearchSaturation.observe(())``), so every false page-turn pushed the turn one step closer to
    ``stopped``, i.e. the broken rung fed the rung after it. The source comment guarding this key
    already stated the rule - resolving a request dimension after the lookup lets the wider or
    deeper request replay the narrower answer - and named two of the three dimensions.

    Anchor: cannot move. ``page`` is 1 without a saturation rule and cannot rise when ``paginates``
    is False, which the corpus path forces; a tuple element that is constant across every key moves
    no key relative to any other. The flow-off arm additionally never reaches this code, because
    ``repeat_notice`` is a ``DRFlowSearchConfig`` field and the tool's own default is False, so the
    anchor has no replay cache at all (measured: 0 replay rows on both anchor arms of the dr@3.0
    web batch, against 916 and 753 on the two DR arms). The label is bumped because the DR arm's
    generated distribution changes, not because the reference frame does.

    (9b) The search ledger gains ``sat_event``, non-null only on the row whose ``observe`` actually
    escalated a rung. ``sat_action`` is a sticky state label - once a turn latches ``stopped``,
    every later row of that turn repeats it - and the first analysis of a batch carrying it counted
    rows as firings, reporting stops as outnumbering page-turns by 67x where the events are 25
    against 27 and the whole ladder moved on 40 of 292 questions. Deliberately the same string as
    ``sat_action`` rather than a name of its own, because two fields able to disagree about which
    rung fired would need a rule for which one wins and there is no honest rule. Written as null
    rather than omitted on an arm without the rule, so "did not fire" and "was never installed"
    stay different rows - the lesson ``dedup_skipped`` cost two versions to learn.

    What this does NOT claim: that pagination now pays. It claims only that the rung now does what
    its own pricing assumed it did. The 27.0%-of-searches / 0.00pp-loss figure that justifies the
    saturation rule was computed on a two-rung ladder, while both dr@3.0 arms ran ``onSaturate:
    "widen"`` - a three-rung ladder whose realized suppression was 14.00%. That number still needs
    its own replay, and this repair changes which searches reach the third rung, so it needs it
    more than before.

    (7) Multi-turn conversation support - a per-turn decision about whether the research machine
    runs, a research memo carried across turns, and an optional conversation-scoped identity set
    for the web tools. It does not bump the label because it cannot appear in a measurement, and
    that is structural rather than a matter of defaults: the bench harness sends exactly one
    message per question, every knob in ``DRFlowConversationConfig`` is read on turn two or later,
    and the single field that reaches a measured code path (``identityScope``, which selects what
    the web tool's per-turn reset clears) is forced back to the measured value by ``build_dr_flow``
    whenever the surface is disabled. The whole block is also off by default, but the default is
    the weaker of the two guarantees and is not the one this paragraph rests on.

    (8) Three repairs to the process appendix and the turn invariant checker, all of them the same
    defect: a criterion whose scope was "this turn" being fed conversation-scoped input once a
    conversation could have more than one turn. None of them is gated on this config, and each is
    listed here because that makes the anchor question theirs to answer rather than the label's.

    (8a) The fabricated-citation check accepts pages an earlier turn of the same conversation
    opened, reported as ``opened_earlier`` so the scope travels with the rate. Its accept-set is
    bounded separately from the rendered source list (``memoMaxOpened`` vs ``memoMaxSources``),
    because one number cannot be right for two pressures pointing opposite ways - short for
    context, complete for the check - and it was wrong on the side that accuses the answer.

    (8b) The URL extractor's stop set was ASCII-only. On Chinese output nothing terminated the
    match, so a cited URL swallowed the rest of its clause, matched no fetch record, and a page the
    run really had opened was named as invented. Fixed direction, Chinese output only, present
    since dr@2.8. Measured on one real conversation: 69 extracted citations became 62 and the
    false accusations went 4 -> 0. ASCII extraction is unchanged, which is the property that
    matters for the label - every published ``citation_grounding_rate`` was taken on that branch -
    but a Chinese-language reading of that counter from any earlier build reads low.

    (8c) ``turn_invariants`` is a per-turn checker and was handed the whole assembled message list.
    On a bench run those are the same list, because the harness sends one message per question and
    the history is empty, so no measured stamp moves; in a conversation, one failed fetch in turn
    one made every later turn report ``tool_error_result``, including turns that called no tool.
    Measured: one real error read as ten violations, now one. It is a structural equivalence rather
    than a gate, and the difference matters - a gate would have kept the anchor still by
    construction, whereas this rests on the bench's message-per-question shape staying true.

    (6) The two DR observers that divide by the context window now divide by the window the
    turn actually runs on. ``BudgetNoteObserver`` writes that quotient into the model's own
    history as ``[budget: ... context ~N%]`` and ``SpinEntryBreaker`` gates on it, while both
    were handed the configured default (65,536) and the loop's own shrink path had been
    resolving the real window per model since well before this. The divergence had no symptom
    on the served student, whose model id resolves to nothing, so both branches fell back to
    the same number and agreed by accident - the defect was latent for the entire dr@2.x
    ladder and became visible only on the first batch run against a model the resolver knows,
    where the note claimed 110% of context while ``elided_in_context`` was 0 of 128, i.e. not
    one tool result had been dropped. Both consumers are built only inside the flow assembly,
    which does not exist on the anchor, so this cannot move the anchor. ``_make_token_budget``
    is deliberately left on the configured value: it sets ``available_history`` on BOTH arms,
    which makes changing it an anchor-moving change owed its own labelled round.

    What this bump does NOT claim: a score effect. The note is a channel that works only by
    being obeyed, and obedience is a property of the model, not of the flow - the same
    sentence was read as an instruction by one model (which converged early) and recited then
    ignored by another (which recited "the budget is at 96% context. I need to stop" and went
    on to search dozens more times). So the repair makes the sentence true; whether a true
    sentence is worth more than a false one is a per-model question this build cannot settle.

    A label marks a measured build, not an edit, and no batch has ever run under dr@2.8 - so
    the usual reasoning would fold (4) into it rather than mint a label nobody can read. That
    reasoning does not apply here, and the reason is the whole point of the bump: every dr@2.8
    change is class-default-off and per-arm gated, so the flow-off anchor is untouched and the
    label can honestly say so. (4) is deliberately NOT gated on DRFlowConfig - it repairs a
    defect the ANCHOR arm suffers from most - so it moves both arms. Folding an anchor-moving
    change under a label documented as anchor-preserving would make the label lie about the
    one property anyone reads it for.

    (4) A wrap-up for turns killed by the completion budget rather than the tool-calling
    budget. On the dr@2.7 web batch, 22 of 60 HLE anchor items ended with
    ``finish_reason='length'`` on turn one - turns=1, n_search=0, elision 8.3%, so not a
    context overflow, just one generation that never finished writing - and 21 of those 22
    carried no visible answer, which the scorer marks wrong by rule. The treated arm showed
    5.0%. Two nets already existed and neither could reach these turns:
    ``_synthesize_final_on_exhaustion`` is arm-neutral but guarded on
    ``iteration >= max_iterations``, unreachable on turn one; and ``terminal_answerless`` is a
    flow hook, absent from the anchor entirely, behind a predicate keyed on
    ``_think_closing_tag_required``, which is False whenever the flow is off, so an unclosed
    think block reads there as an answer. Sampling showed the model had usually already
    reached the answer and said so before talking itself out of it, so the wrap-up prompt
    forbids re-derivation rather than inviting it. Priced at +2.32pp over the batch and
    strongly per-source (HLE 22.7%, BrowseComp 4.9%, xbench 0%) - a retrieval/coverage proxy,
    not a score forecast. Because it moves the anchor, published live-web deltas measured
    against the un-repaired anchor cannot be reused as a baseline: a counterfactual backfill
    put dr-base at +5.30pp -> +3.31pp, i.e. the one significant live-web cell stops being
    significant. That is an argument for re-measuring, not for leaving the defect in.

    (1) Cross-query dedup on the corpus path: ask the retrieval service for ``search_depth``,
    render the same width as before, skip documents this turn already listed and backfill from
    deeper. Three places capped the result list at ten while the service clamps to fifty, so
    84.2% of searches (44,098 of 52,373 on the dr@2.7 corpus batch) only ever saw five
    documents. The shape matters: returning ten lines instead of five measured +4.17~6.67pp
    gold surfaced but doubles the result text, while deduplicating at unchanged width measured
    +5.83~8.33pp at depth 50 (depth 20 carries about 90% of it) at no context cost. Nothing
    re-ranks - four zero-model ranking signals were priced offline and all four lost to plain
    BM25. The rendered width is preserved unconditionally, so the knob can only ever replace
    result lines, never delete them; that is what makes it safe on the live-web path, which
    has no deeper pool to backfill from (``num`` is inert on this endpoint) and where 52.89%
    of the DR arm's slots were repeats, i.e. plain dedup would have deleted about half the
    lines. Getting the corpus behaviour there needs pagination: separate change, separate
    quota profile.

    (2) The result-list snippet is restored on the live-web DR arms, together with the
    per-docid dedup that made restoring it cheap - only together, because the priced change is
    the pair and dr@2.4 already taught that lesson on the corpus axis. The note two hundred
    lines above ("a live-web snippet is answer-optimised
    and unpriced") is what changed: it has now been priced. On the dr@2.7 web batch the anchor
    received 6,897,883 characters of snippet and the DR arm zero; splitting by where the gold
    answer string first appears, the anchor reached it through search on 99 questions and
    through fetch on 40, with 67 reachable from search alone, while the DR arm was 76 / 106
    with only 7 from search alone. The arm had moved roughly sixty questions off a channel it
    had already paid for and bought them again through the reader, at 3.36x the fetches
    (1,003 -> 3,373) for a paired gold-surfaced gain of +2.04pp (b=21 c=15, ruler +-4.00pp,
    i.e. not resolvable), with conversion quality flat (82.2% against 84.1%). The marginal
    cost is close to zero - the snippet arrives with a search already paid for and spends only
    context, and context is the one resource this axis has to spare: elision fires on
    27.15/25.17/26.82/30.13% of questions across the four arms, so overflow prevention barely
    engages here, and the DR arm's total evidence intake was 7.28M characters against the
    anchor's 18.76M.

    (3) A verify rejection may buy retrieval
    rather than only a rewrite, behind ``verify.evidence_round`` (class default off). The
    rejection already names which claims lack support; until now the bounce answered that
    with "reuse the evidence already gathered", which recovers nothing whenever the cause
    is that the document was never retrieved. Inside a bounded round the search tool may
    ask the corpus service for its full depth, and the spin breaker - whose forced-report
    note carries the same prohibition - stands down once and records that it did. The
    live-web path is deliberately not deepened: measured against the endpoint we use, the
    ``num`` parameter changes nothing (10/20/50/100 all returned 7-8 organic results) and
    depth there needs pagination, which is a separate change with a separate quota cost.
    Deepening both from one switch would ship a silent no-op on one axis.

    (5) The research trail reaches the product surface at all. ``final_shape.process_appendix``
    has defaulted on since dr@2.8, but the trail is computed from the client-side ledger and
    the only writer of ``RAVEN_WEB_LEDGER`` in this tree is the batch launcher - so the one
    configuration that asked for an appendix was the one configuration that could not get one,
    and it degraded quietly: ``build_appendix`` returned ``ledger_not_configured``, the answer
    came back intact, nothing logged. The nine bench profiles all pin the knob off, which is
    why no arm ever exercised the path that needed it. A turn-scoped ledger is now opened when
    the knob is on and no launcher named a file. This carries no measurement obligation and
    does not touch this label's flow semantics: it is unreachable from any arm that pins the
    knob off and from every flow-off anchor (no assembly exists there), and the ledger is
    write-only during generation, so nothing the model reads changes in either state. It
    landed after the first dr@2.9 batch began; that batch runs
    ``student_sglang_web_dr.json``, which pins the knob off, so it cannot have been reached -
    and its own source snapshot is frozen either way.

    dr@2.7 repairs the reader path: a resolver
    failure stops being a refusal on the fetch that a reader service performs on our
    behalf, and a transport failure that never produced a response is retried twice
    before the page is abandoned. Both change what the model reads on a live-web arm,
    which is what a label is for; the corpus path returns before either and is byte
    identical. Neither is gated on the flow. Gating a reliability repair would leave the
    flow-off anchor on the broken path, manufacturing the arm-correlated instrument
    defect that the gate on tool-surface health exists to catch - the measured batch that
    motivated this had its treated arm at a 1.5% fetch-failure rate against the anchor's
    2.9%, so the repair does not flatter the treated side.

    dr@2.6 turns terminal-answer shaping on by
    default and has every shipped config write both of its values out rather than inherit
    them, because an inherited default does not appear in the file that describes a run and
    so can change what a run does without leaving a trace where anyone looks. The default is
    unreachable from a flow-off arm - the hop is assembled inside ``build_dr_flow``, which
    returns None there - so the measurement anchor cannot move with it.

    dr@2.5 introduced that shaping in two independently gated pieces. ``final_shape.record``
    is a read-only observer: it computes the shaped form of the terminal answer and records
    it beside the raw one, touching neither the persisted message sequence nor anything the
    model reads, so its score budget is 0 by construction rather than by promise.
    ``final_shape.require_marker`` appends one clause to the DR contract asking for an
    explicit ``<answer>`` marker - that one IS a distribution change and needs a fresh
    anchor pair plus a ``scripts/stamp_dr_segment.py`` artifact, because the prompt never enters
    the persisted trajectory and the stamp is the only place it is verifiable afterwards.
    The motivation is not score: ``final_answer`` is the last assistant message verbatim,
    only 15.6% of anchor answers carry any answer marker, and the turn's ending is a passive
    truncation event rather than an action - a report axis run on that shape would measure
    truncation rate instead of report quality, so the ordering is shaping first, report axis
    second. Shaping is additive only ("may improve a record, never blank one"): the upstream
    framework we benchmarked runs an extraction stage that carried gold on 71/120 into a
    boxed field on 58/120, producing nothing new and dropping 10.83pp, and our own dr@1.6
    salvage seam failed the same way.

    dr@2.4 restores the corpus SERP snippet on
    the arm that measures candidate selection: the shaping strips snippets by default
    (see DRFlowSearchConfig), but the read-rate diagnosis found gold sitting at rank 1,
    on-screen and never opened, so an arm probing that failure sets
    ``search.includeSnippets=true`` per-config to hand the model a selection signal. The
    class default stays false, so the live-web DR arms - where a search snippet is
    answer-optimised, not a fixed 300-char corpus window - are unchanged, and the
    flow-off anchor (which already renders snippets) does not move. dr@1.9 removed the two
    restart idioms the identity segment itself taught the model, and raised the salvage
    budget. dr@2.0
    replaced the product identity segment with a DR one (eight of its instructions
    named tools no DR arm has), carved elided evidence out of the reviewer's
    unsupported-claim rule, de-hedged the salvage prompt, stopped a committed salvage
    re-firing the terminal seam, and closed the subagent registry's unconditional
    subprocess tool. dr@2.1 restores the dr@2.0 prompt bytes that a later refactor
    moved, widens the elision trigger from the evidence pack to the whole context,
    and adds arm-neutral counters.

    dr@2.3 is a maturity release: make the instruments truthful, gate the readings, and
    close the two remaining containment leaks. Score budget is 0 for everything except
    the containment fixes, which touch only arms where sub-agents can spawn - and spawn
    is disabled on every arm in the adjudication set, so the corpus axis distribution is
    unchanged. Its headline instrument fix is ``status``: it read "ok" on 600/600 rows of
    the block-1 five-arm batch, including 13 answerless runs, 3 that hit the context
    window and 3 that hit the iteration cap. That is not a cosmetic gap - the pipeline's
    own salvage path fires only on ``status == "no_answer"``, so a constant "ok" made
    ``--finalizer`` dead code while the flag sat on the command line looking enabled.
    dr@2.3 also names the cause of every answerless run instead of leaving 10 of 13
    unattributed, and promotes answer_rate to a pre-registered secondary endpoint whose
    non-empty SET is persisted, not just its rate, because a rate cannot be intersected.

    dr@2.2 was a fix-and-instrument release with a score budget of 0, accepted by
    fingerprint rather than by score: the answerless_shape counter stops miscounting a
    committed salvage as shape-answerless (dr@2.1 introduced that, it fires only on
    treated arms, and its direction understates the treated advantage), the corpus
    endpoint token gains the batch component (without it every arm whose directory name
    repeats across batches shared one token, so the anchors' per-question retrieval log
    was a five-batch union while the treated arms' was single-batch), and the external
    baseline arm's build is archived as a patch. None of the three changes what the
    model reads, so the anchor pair carries over. Tool-layer benchmark containment
    shipped in the same window but defaults to off, so it changes no existing arm's
    distribution; enabling it is its own labelled version with a fresh anchor pair.

    The match is on the BASE label, so a profile suffix such as "-futurex" survives
    while a superseded base label is still rejected. Exact membership let
    "dr@1.9-futurex" through, which is the one outcome AGENTS.md 0.2 exists to stop.

    One rejection class, since 2026-08-25. There used to be a second - a FOLDED
    tier for dr@3.5-dr@3.7, labels upstream had spent before the 2026-08-20 fold.
    The launch convention retired that tier rather than emptying it: a label now
    names the code a batch ran under, so those three are ordinary future rungs and
    nothing is refused for having been absorbed. What survives from that episode is
    the reason the whole scheme is safe to renumber: a reading's join key is
    ``dr_segment_sha`` (AGENTS.md 0.2), never the label.
    Note what this validator still does NOT do: it is a denylist, so an invented
    label ("dr@9.9-foo") passes. Closing that needs the current label's own
    successors enumerated, which is a different guarantee from this one.
    """
        # Match the base label, not the whole string. A suffixed variant such as
        # "dr@1.9-futurex" is not a member of the tuple, so exact membership let a
        # superseded label through and the batch was silently mislabelled - the one
        # outcome AGENTS.md 0.2 exists to prevent. Suffixes are legitimate (they name
        # a profile, not a semantics), so the base version is what has to be checked.
        base = self.version.split("-", 1)[0]
        if self.enabled and base in self._SUPERSEDED_VERSIONS:
            # Name the current label from the field default, never a literal. A literal
            # here is one more place a bump has to reach, and the batch scripts already
            # taught us what that costs: a gate that hardcodes the expected version
            # becomes the drift source it exists to catch.
            current = type(self).model_fields["version"].default
            raise ValueError(
                f"drFlow.version={self.version!r} predates this build's flow "
                f"semantics (base label {base!r}); set drFlow.version to {current!r} "
                "or later, keeping any profile suffix"
            )
        return self


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

    # Deep-research flow (see DRFlowConfig).
    dr_flow: DRFlowConfig = Field(default_factory=DRFlowConfig)

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
        except (json.JSONDecodeError, OSError) as e:
            # Loud, because the consequence is not "no config": every extension
            # block - drFlow included - silently starts from class defaults, so
            # an unreadable file runs a DIFFERENT measured configuration while
            # the command line still names the intended one.
            logging.getLogger(__name__).warning(
                "config: could not parse %s (%s); ALL extension blocks (drFlow "
                "included) fall back to class defaults",
                actual_path,
                e,
            )
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
