"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Annotated, Any, Literal

from loguru import logger
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from raven.contracts.path_policy import WORKSPACE_DEFAULT_SENTINEL
from raven.sandbox.config import SandboxConfig


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChannelBase(Base):
    """Fields every chat channel carries, whatever transport it speaks."""

    # One directory per channel rather than per conversation: a channel is the
    # unit the user configures, and its chats are the same kind of work.
    # Declared once here so a newly added channel cannot forget it.
    workspace: str = Field(
        default="",
        description=(
            "Absolute path this channel's chats read and write files in. Leave empty for the default, "
            "~/.raven/tmp/<channel>. Must not be the agent home directory or anything inside its "
            "memory, skills or session trees."
        ),
    )


def _to_camel_key(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part.title() for part in rest)


class ChannelSocket(ChannelBase):
    """The host-side view of one channel section: the three socket fields.

    Cargo fields live with the adapter specs (``config_schema``) and travel
    through the admission door; they ride along here as extras so a loaded
    section round-trips, but nothing host-side may read them by name.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    enabled: bool = False
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class ChannelsConfig(Base):
    """Configuration for chat channels.

    Channel sections are dynamic: any adapter the spec registry discovers may
    have a section here, and no channel needs one to exist. Attribute access
    answers with a :class:`ChannelSocket` view (extras carried) for known
    adapters, so ``config.channels.telegram.enabled`` reads the same whether
    or not the file has a telegram table.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))

    def _adapter_names(self) -> set[str]:
        from raven.channels.registry import discover_channel_names

        return set(discover_channel_names())

    def channel_entries(self) -> dict[str, "ChannelSocket"]:
        """Socket views for every channel section present in the config."""
        out: dict[str, ChannelSocket] = {}
        for name, value in (self.model_extra or {}).items():
            if isinstance(value, ChannelSocket):
                out[name] = value
            elif isinstance(value, dict):
                out[name] = ChannelSocket.model_validate(value)
        return out

    def __getattr__(self, name: str) -> Any:
        extra = self.__pydantic_extra__
        if extra is not None and name in extra:
            value = extra[name]
            if isinstance(value, ChannelSocket):
                return value
            if isinstance(value, dict):
                # Sticky coercion: the socket materializes into the extras on
                # first access, so mutations persist and every reader sees one
                # object.
                socket = ChannelSocket.model_validate(value)
                extra[name] = socket
                return socket
            return value
        if not name.startswith("_") and name in self._adapter_names():
            socket = ChannelSocket()
            if extra is not None:
                extra[name] = socket
            return socket
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    @model_serializer(mode="wrap")
    def _drop_pristine_sections(self, handler: Any) -> Any:
        """Keep the file sparse: a section indistinguishable from the default
        socket (which sticky access materializes as a side effect) writes
        nothing -- absent and default-valued read identically everywhere."""
        data = handler(self)
        if not isinstance(data, dict):
            return data
        pristine: dict[str, Any] = {}
        pristine.update(ChannelSocket().model_dump())
        pristine.update(ChannelSocket().model_dump(by_alias=True))
        for name in list(self.__pydantic_extra__ or {}):
            for key in (name, _to_camel_key(name)):
                value = data.get(key)
                if isinstance(value, dict) and all(k in pristine and pristine[k] == v for k, v in value.items()):
                    del data[key]
        return data

    def enabled_channel_names(self) -> set[str]:
        """Names of every enabled IM channel. Entry-driven, so a channel
        section added to the config is covered without touching consumers
        (the gateway cron partition, cron add's target validation)."""
        return {name for name, socket in self.channel_entries().items() if socket.enabled}


class CompactionConfig(Base):
    """In-turn transcript compaction for long agentic turns.

    Off by default: the loop's only in-turn shrink is then the reactive,
    deterministic elision it has always run on a provider's overflow error.
    Enabled, two layers join it, on the same usage readings:

    - Proactive: before an LLM call, once the last observed context size
      crosses the trigger, older tool-result bodies are pruned first
      (deterministic, no LLM call) and, only if that is not enough, the
      transcript head is replaced by an LLM summary while a recent tail
      stays verbatim.
    - Reactive completion: an overflow retry that finds nothing left to
      elide may take the summary path instead of surfacing a fatal error.

    Summaries always run on the turn's own model and provider -- a pinned
    summary model outlives a model switch and then routes every summary to
    a retired endpoint. A legacy ``model`` key in saved configs is accepted
    and ignored.
    """

    enabled: bool = False
    prune: bool = True
    # None derives min(20000, resolved max output tokens) -- room for the reply.
    reserved_tokens: int | None = None
    # None keeps the sole trigger at ``window - reserved_tokens``; a fraction
    # in (0, 1) also compacts once context crosses ``trigger_ratio * window``.
    trigger_ratio: float | None = None
    # None derives 25% of the usable window clamped to [2000, 8000] -- the
    # verbatim tail preserved through a summary compaction.
    preserve_recent_tokens: int | None = None


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = WORKSPACE_DEFAULT_SENTINEL
    model: str = "anthropic/claude-opus-4-5"
    # The vendor whose credential serves ``model``. Required in practice: an id
    # alone does not name a credential -- `openrouter` serving
    # `anthropic/claude-haiku-4-5` and `anthropic` serving `claude-haiku-4-5`
    # are both valid, name different keys and different bills, and the id does
    # not distinguish them. Empty is what a config written before that rule
    # carries; ``loader`` migrates those once, writing down whichever vendor
    # they were in fact resolving to.
    provider: str = ""
    # No maxTokens here on purpose. A number in a config file cannot be right
    # for every model -- too large is a 400, too small truncates silently --
    # so the ceiling is resolved per model from the catalogue
    # (providers/rates.resolve_max_output_tokens). An old config carrying the
    # retired key is ignored rather than rejected: this model does not forbid
    # extras.
    # None (or 0) means "figure it out" -- resolved against the model's real
    # window at construction time. A positive value pins the window, taking
    # priority over whatever the model's own catalogue reports.
    context_window_tokens: int | None = None
    # In-turn transcript compaction knobs; factory-off (see CompactionConfig).
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    temperature: float = 0.1
    # Per-call wall-clock cap (seconds) for every LLM request (main loop and
    # sub-agents). Bounds a stalled backend that trickles bytes without ever
    # finishing, which an httpx per-read timeout never catches.
    llm_call_timeout: int = 600
    max_tool_iterations: int = 40
    # Cap on subagent VMs running at once, counting spawns and DAG nodes
    # together (excess queues). ge=1: a 0/negative cap would deadlock every
    # subagent (Semaphore(0)).
    max_concurrent_subagents: int = Field(default=8, ge=1)
    # Spawn rate limit per session, per rolling hour — the concurrency gate
    # alone can't stop a prompt-injected agent from spawning indefinitely (each
    # finishes, freeing a slot for the next; the cross-turn re-injection loop
    # needs no user input). A rolling window bounds a runaway to N/hour yet
    # auto-recovers, so it never permanently locks out heavy legitimate use.
    # Counted per session so one busy session can't throttle others.
    max_subagent_spawns_per_hour: int = Field(default=30, ge=1)
    # Empty-response recovery: recover turns the model ends with no visible text
    # (post-tool empty / thinking-only) instead of surfacing a dud "no response
    # to give". Budgets are per-turn.
    empty_recovery_enabled: bool = True
    post_tool_empty_max_nudges: int = 1
    thinking_prefill_max_retries: int = 2
    empty_content_max_retries: int = 3
    # Deprecated compatibility field: accepted from old configs but ignored at runtime.
    memory_window: int | None = Field(default=None, exclude=True)
    reasoning_effort: str | None = None  # low / medium / high — enables LLM thinking mode
    # Per-model request-parameter overrides, keyed by a substring of the model
    # name: {"kimi-k2.5": {"temperature": 1.0}}. Some models reject the usual
    # defaults, and hard-coding those quirks in the registry left users unable to
    # adjust them. Entries here win over the registry's built-in defaults.
    # This is also the direct channel for arbitrary sampling/serving params: an
    # unknown top-level key is auto-forwarded into extra_body by LiteLLM for
    # OpenAI-compatible backends (e.g. sglang's repetition_penalty); a nested
    # structure can be written directly as extra_body: {...}.
    model_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    enable_personalization: bool = False  # 4-step PAHF-inspired personalization flow (classify → ask → execute → learn)

    @property
    def should_warn_deprecated_memory_window(self) -> bool:
        """Return True when old memoryWindow is present without contextWindowTokens."""
        return self.memory_window is not None and "context_window_tokens" not in self.model_fields_set


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class CronConfig(Base):
    """Cron scheduler configuration.

    Delivery is bound at job creation (fire-at-origin) — there is no
    trigger-time routing config anymore. The retired ``forward_channels``
    key is stripped by the config loader for backwards compatibility.
    """

    default_timezone: str = "Asia/Shanghai"
    """Default IANA timezone for cron expressions without explicit ``--tz``."""

    notify_missed: bool = True
    """When True, the gateway observes one-shot reminders bound to other
    partitions (tui / cli sessions) that went past due unfired — their
    session was closed — and surfaces each once as a system event through
    the heartbeat wake path. Read-only observation: the foreign job itself
    is never mutated."""


class ModelOverlay(Base):
    """A name for a model no catalogue carries.

    A self-hosted deployment serves whatever was put there, and a model released
    since the bundled snapshot is in no table yet, so the picker falls back to
    showing the id. That is usually fine -- the id is the name the user gave
    their own deployment -- but it leaves no way to label several of them.

    Only what a person states about presentation. Token accounting is not in
    scope here -- `agents.defaults.contextWindowTokens` holds what a person can
    state about it, and the output ceiling resolves per model with no knob at
    all. What has no knob either is a *price* for an endpoint no catalogue
    prices; such a deployment reports unknown spend rather than borrowing a
    hosted model's rate. Adding one is a separate ask.
    """

    label: str = ""
    description: str = ""


class ProviderEndpoint(Base):
    """One named URL/key group under a provider section.

    ``label`` is not decoration: it is the idempotency key a later stage
    (rotation, failover, per-endpoint health) uses to address one entry across
    edits, so two endpoints in the same list must not share one.
    """

    label: str = Field(min_length=1)
    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    # Custom headers (e.g. APP-Code for AiHubMix) -- can carry a secret, so
    # display faces redact the values (keys stay visible).
    extra_headers: dict[str, str] | None = Field(default=None, json_schema_extra={"secret": True})
    models: list[str] = Field(default_factory=list)  # User-curated model names for the picker
    # Several full url/key/header groups under one provider section, for a
    # vendor reachable by more than one account or region. Meaningful only for
    # a plain API-key provider reached through the litellm client -- a section
    # whose auth is OAuth, or that needs more than a key and an address (Azure
    # OpenAI, Codex), gets this rejected at `make_provider`. Set and non-empty,
    # it replaces the flat `api_key` outright rather than merging with it; an
    # entry inherits the flat `api_base`/`extra_headers` for whichever it does
    # not name itself -- see `raven.providers.endpoints.provider_endpoints` for the
    # one place that resolves which of the two shapes (or Gemini's
    # `api_key_list`) is in effect.
    endpoints: list[ProviderEndpoint] = Field(default_factory=list)

    @field_validator("endpoints")
    @classmethod
    def _unique_endpoint_labels(cls, value: list[ProviderEndpoint]) -> list[ProviderEndpoint]:
        """Reject a duplicate label -- see the class docstring for why one must be unique."""
        seen: set[str] = set()
        for ep in value:
            if ep.label in seen:
                raise ValueError(f"duplicate endpoint label {ep.label!r}: labels must be unique within a provider")
            seen.add(ep.label)
        return value

    # How requests spread across `endpoints` when there is more than one:
    # "sticky" keeps using the first healthy entry until it fails, "round_robin"
    # cycles through all of them. Meaningless with zero or one endpoint.
    endpoint_strategy: Literal["sticky", "round_robin"] = "sticky"
    # Keyed by model id, in any spelling: what the user knows about a model that
    # the catalogues do not. Deliberately additive rather than a change to
    # `models` -- that list already lets a model be added, and what was missing
    # was a way to describe one, so no config has to be rewritten to get it.
    model_overlay: dict[str, ModelOverlay] = Field(default_factory=dict)

    @property
    def effective_api_key(self) -> str:
        """The key to send, which is not always the ``api_key`` field.

        Declared on the base so every call site can ask without knowing which
        providers keep their key somewhere else. Gemini accepts a list, and a
        section holding only that list handed LiteLLM an empty string: the
        request left with no credential and failed at the API, having passed
        every check that only asked whether credentials existed.
        """
        return self.api_key


class AzureProviderConfig(ProviderConfig):
    """Azure OpenAI, whose connection needs more than a key and an address.

    A deployment is a name the tenant gives one model, and it goes into the
    request URL's path; declared here, it is a connection parameter separate
    from the model id, so Azure ids are spelled like every other provider's.

    ``api_version`` is configurable because tenants run different ones.
    """

    deployment: str = ""  # falls back to the model id, for configs written before this field
    api_version: str = "2024-10-21"


class GeminiProviderConfig(ProviderConfig):
    """Gemini, which accepts several keys under one section.

    Example:
        gemini:
          apiKeyList:
            - "key1"
            - "key2"

    Vertex is a separate provider (``vertex_ai``) reached through LiteLLM with
    ``VERTEXAI_PROJECT`` and ``VERTEXAI_LOCATION``; it is not a flag on this one.
    """

    #: Several keys may be listed; the first is used. Round-robin rotation was
    #: declared here once and never called -- listing keys and silently using one
    #: is the honest description of what happens.
    api_key_list: list[str] = Field(default_factory=list)

    @property
    def effective_api_key(self) -> str:
        if self.api_key_list:
            return self.api_key_list[0]
        return self.api_key


def _prefer_set_values(base: dict[str, Any], winner: dict[str, Any]) -> dict[str, Any]:
    """Merge two sections for one provider, letting a set value beat an unset one.

    The current name wins a genuine conflict, but a declared field exists as an
    empty section whether or not it was configured -- so taking it verbatim let a
    placeholder erase the credential the user had written under the provider's
    other spelling.
    """
    merged = dict(base)
    merged.update({k: v for k, v in winner.items() if v not in ("", None, [], {})})
    return merged


def section_has_credentials(config: "ProviderConfig", spec: Any, name: str = "") -> bool:
    """Is this section actually usable, or just a placeholder?

    Every declared provider exists as an empty section whether or not the user
    configured it, so "the field is there" says nothing. A spec flag must not
    stand in for evidence either: `is_local` used to answer with no api_base at
    all, and an empty declared section then beat the credentials the user had
    really written under one of that provider's other names.

    The rule itself lives in `providers.auth`, because deciding it here as well
    is what made a Gemini section holding only `api_key_list` invisible to
    routing while `provider list` showed it as configured.

    A vendor Raven carries no spec for reaches this too -- the passthrough route,
    where the section name is all there is -- so the name is passed separately
    rather than read off a spec that may not exist.
    """
    from raven.providers.auth import credential_status

    return credential_status(name or (spec.name if spec else ""), config, spec=spec).ok


class ProvidersConfig(Base):
    """Configuration for LLM providers.

    Fields below are the providers Raven carries metadata for. Any other key is
    kept as-is and served through :meth:`get`, so a provider LiteLLM supports but
    Raven has no spec for still works from config alone.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _merge_renamed_sections(cls, data: Any) -> Any:
        """Fold a provider's pre-rename section into its current one.

        A file touched by both names holds two half-filled sections -- say the
        credentials under the old name and a model list under the new one.
        Picking either section alone drops the other's fields, so merge with the
        current name winning per field.
        """
        if not isinstance(data, dict):
            return data
        from raven.providers.registry import PROVIDERS, names_same_provider

        merged = dict(data)

        # Fold any key that spells a declared field differently into that field.
        # Extras are matched spelling-insensitively (`ProvidersConfig.get`), and
        # a declared field exists as an empty section whether or not it was
        # configured -- so without this, "azure-openai" or "OpenRouter" lands in
        # extras where the always-present empty field then wins, and a key the
        # user really wrote reads back as unset. One rule for both kinds.
        for key in [k for k in merged if k not in cls.model_fields]:
            field = next((f for f in cls.model_fields if names_same_provider(key, f)), None)
            if field is None or not isinstance(merged[key], dict):
                continue
            section = dict(merged.pop(key))
            current = merged.get(field)
            if isinstance(current, dict):
                section = _prefer_set_values(section, current)
            merged[field] = section

        for spec in PROVIDERS:
            stale = [merged.pop(a) for a in spec.name_aliases if isinstance(merged.get(a), dict)]
            if not stale:
                continue
            section: dict[str, Any] = {}
            for older in stale:
                section = _prefer_set_values(section, older)
            current = merged.get(spec.name)
            if isinstance(current, dict):
                section = _prefer_set_values(section, current)
            merged[spec.name] = section
        return merged

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: AzureProviderConfig = Field(default_factory=AzureProviderConfig)  # Azure OpenAI
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    # Z.ai, the vendor's current brand and LiteLLM's name for it. Configs
    # written before the rename say "zhipu"; both keys load.
    zai: ProviderConfig = Field(
        default_factory=ProviderConfig,
        validation_alias=AliasChoices("zai", "zhipu"),
    )
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # Alibaba Cloud Tongyi Qianwen
    # LiteLLM's own names for these two, so a model id and a config section are
    # spelled the same. Configs written before the rename keep loading.
    hosted_vllm: ProviderConfig = Field(
        default_factory=ProviderConfig,
        validation_alias=AliasChoices("hosted_vllm", "hostedVllm", "vllm"),
    )
    gemini: GeminiProviderConfig = Field(default_factory=GeminiProviderConfig)  # Google Gemini / Vertex AI
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax_global: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax_cn: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    ollama_chat: ProviderConfig = Field(
        default_factory=ProviderConfig,
        validation_alias=AliasChoices("ollama_chat", "ollamaChat", "ollama"),
    )
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig)  # Github Copilot (OAuth)

    def get(self, name: str) -> ProviderConfig | None:
        """Return one provider's config, declared field or extra key alike.

        The lookup is spelling-insensitive on both sides. A section key reaches
        here in whichever form its writer used -- LiteLLM's hyphenated vendor
        name, the camelCase this model serializes to, or the underscored field
        name -- and a caller holding a model-id prefix has only one of those. So
        this is the only place a provider name may be resolved to its config;
        reading the attribute directly sees just the one spelling.
        """
        from raven.providers.registry import canonical_provider_name, names_same_provider

        # A renamed provider keeps answering to its old name, and the declared
        # field wins: a half-migrated config holding both keys must not serve
        # the stale one.
        name = canonical_provider_name(name)
        declared = self.__dict__.get(name)
        if isinstance(declared, ProviderConfig):
            return declared
        extra = (self.model_extra or {}).get(name)
        if extra is None:
            for key, value in (self.model_extra or {}).items():
                if names_same_provider(key, name):
                    extra = value
                    break
        if isinstance(extra, ProviderConfig):
            return extra
        if isinstance(extra, dict):
            return ProviderConfig.model_validate(extra)
        return None


class ModelEndpoint(Base):
    """A routable model and the OpenAI-compatible endpoint that serves it."""

    model: str = ""
    api_base: str = ""
    api_key: str = "EMPTY"


class RoutingConfig(Base):
    """Model routing configuration.

    ``backend`` picks the router: ``ecoclaw`` (PinchBench benchmark scores, the
    original) or ``knn`` (task-level KNN over per-model rewards). Fields under
    "knn backend" are read only when ``backend == 'knn'``.
    """

    enabled: bool = False
    backend: str = "ecoclaw"  # ecoclaw | knn
    profile: str = "balanced"  # best / balanced / eco
    # OpenRouter API key for embeddings (ecoclaw backend; defaults to providers.openrouter.api_key)
    api_key: str = ""
    # knn backend: routable models paired with their endpoints
    models: list[ModelEndpoint] = Field(default_factory=list)
    # knn backend: prebuilt KNN memory (embeddings + per-model rewards/costs)
    memory_path: str = ""
    k: int = 30  # retrieval breadth: how many nearest neighbours to pull
    lambda_cost: float = 0.0  # score = reward - lambda_cost * cost
    embedding_endpoint: str = ""  # embedding service for the incoming task
    # knn backend safety gates: leave the default model only with enough evidence.
    # The pick is scored over the "similar" neighbours (cosine >= min_similarity).
    min_similarity: float = 0.6  # a neighbour counts as similar at cosine >= this
    min_similar_neighbors: int = 4  # need >= this many similar neighbours to route
    min_memory_size: int = 10  # need >= this many memory entries to route at all
    min_margin: float = 0.0  # only switch if the pick beats the default score by >= this


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    # When True, completed cron jobs (and other in-process producers) can
    # end the heartbeat sleep early via the WakeScheduler instead of
    # waiting for the next interval tick. Set False to fall back to pure
    # interval-only heartbeats.
    event_wake: bool = True
    # Minimum spacing between event-driven wake fires. Caps the Phase-1
    # decision-call rate when producers fire rapidly (e.g. an every-60s
    # cron job): events still queue, but the wake collapses to one tick
    # per window. 0 disables the guard.
    event_wake_min_interval_s: int = 300


class GatewayLogConfig(Base):
    """Gateway logging configuration.

    ``rotation`` / ``retention`` accept loguru's vocabulary: rotation by size
    (``"10 MB"``), wall-clock (``"00:00"`` for daily), or interval
    (``"1 week"``); retention as a file count (``7``) or a duration
    (``"14 days"``).

    ``level`` filters the persisted ``gateway.log`` file; ``console_level``
    filters the live stderr mirror the foreground gateway keeps printing.
    """

    rotation: str = "10 MB"
    retention: int | str = 7
    level: str = "INFO"
    console_level: str = "INFO"


class GatewayPageConfig(Base):
    """The served page (`raven serve`'s browser front end) hosted inside the
    gateway process, on the gateway's own agent loop.

    On by default: one engine then serves the page and the IM channels, so
    what the browser sees is what the channels talk to. The page follows the
    `raven serve` port policy (``port``, probing forward when taken), writes
    ``~/.raven/serve.json``, and `raven web` attaches to it. Disable to keep
    the gateway channel-only and run `raven serve` standalone instead.
    """

    enabled: bool = True
    port: int = 18792


TIER_LADDER: tuple[str, ...] = ("medium", "high", "max")
"""The built-in tiers, cheapest first. The order is what makes a clamp possible,
so it is a constant here rather than something read back off the catalogue."""

DEFAULT_TIER = "high"
"""Which built-in tier a session starts on. Applies to the built-in catalogue
only -- a deployment bringing its own modes and naming no default degrades to
its first entry instead."""

_TIER_TEXTS: dict[str, str] = {
    "medium": "The least effort a sub-agent is asked for.",
    "high": "The middle amount of effort, between the other two.",
    "max": "The most effort a sub-agent is asked for.",
}
"""One sentence per rung, saying only what differs between them.

Position is that difference and the whole of it: the built-in rungs carry no
per-tier behaviour beyond their order and the per-agent clamp. Which rung a
session *starts* on is not among the things a row may say -- `defaultMode` can
move it while these rows still stand, and the menu already marks the current one. The scope of the
control -- that raven's own effort is unchanged -- is stated once by whichever
surface draws it, not repeated on every row.

English is the message id (see :mod:`raven.i18n`); the translation happens where
the catalogue is resolved, not here, so a language chosen after this module is
imported still takes effect.
"""


class AcpModeConfig(Base):
    """One operating profile a client may switch a session to over ACP.

    The stable schema's session modes: a named profile a session runs in,
    switched with ``session/set_mode``. Two things move with a mode -- the
    tool-iteration ceiling the loop enforces, and an ``overlay`` the loop does
    not interpret at all: it reaches the hook chain as
    ``ctx.metadata["mode_overlay"]``, so a product's own hooks read their own
    knobs from it. Everything else about the agent is the connection's,
    identically, in every mode.
    """

    name: str
    description: str = ""
    max_tool_iterations: int | None = None
    """``None`` inherits ``agents.defaults.maxToolIterations``."""
    overlay: dict[str, Any] = Field(default_factory=dict)


def _builtin_modes() -> dict[str, "AcpModeConfig"]:
    return {tier: AcpModeConfig(name=tier.capitalize(), description=_TIER_TEXTS[tier]) for tier in TIER_LADDER}


class AcpConfig(Base):
    """The ACP surface's session modes.

    The three built-in tiers move what raven asks of its SUB-AGENTS, not what
    raven does: every one leaves ``maxToolIterations`` inherited and ``overlay``
    empty. A deployment that declares its own catalogue replaces this one whole;
    one that writes ``"modes": {}`` turns the surface off entirely.
    """

    modes: dict[str, AcpModeConfig] = Field(default_factory=_builtin_modes)
    default_mode: str | None = None
    """Which mode a new session starts in. Deliberately unvalidated: a product
    that declares its own catalogue without naming a default must degrade to its
    first entry (``build_mode_catalogue``), not be failed at startup by a value
    it never chose."""

    @property
    def uses_builtin_modes(self) -> bool:
        """Whether this catalogue is raven's own three rungs rather than a declared one.

        The same signal `effective_default_mode` reads, and it answers the same
        kind of question: which of these rows does raven own? It owns the text of
        its own rungs and translates them; a deployment's descriptions are its
        author's, in whatever language they wrote, and are passed through
        untouched.
        """
        return "modes" not in self.model_fields_set

    @property
    def effective_default_mode(self) -> str | None:
        """The default to start sessions on: ``high`` for the built-in tiers only.

        One field cannot say both "the built-in default" and "nobody chose", so
        the distinction is drawn from whether the catalogue itself was declared.
        Carrying it as a plain field default instead was silently wrong for any
        deployment whose own vocabulary happened to contain a rung called
        ``high``: it was accepted as a named default, and their catalogue started
        on it rather than on the first entry they wrote.

        Derived rather than written back onto ``default_mode`` in a validator:
        that would put the field in ``model_fields_set``, so a config nobody
        wrote a default into would serialise one under ``exclude_unset``.
        """
        if self.default_mode is not None:
            return self.default_mode
        return None if "modes" in self.model_fields_set else DEFAULT_TIER


class TuiConfig(Base):
    """Terminal UI launcher behavior.

    ``attach_gateway``: when a live ``raven gateway`` already hosts the page,
    ``raven tui`` relays to that engine instead of building a second one, so
    the terminal and the channels share one loop. Set false — or pass
    ``--standalone`` for one launch — to always run the embedded engine.
    """

    attach_gateway: bool = True


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    user_pool: int = 4
    system_pool: int = 2
    send_max_retries: int = 3
    # Seconds an in-flight turn may finish within on shutdown; 0.0 restores
    # cancel-immediately.
    shutdown_grace: float = 5.0
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    log: GatewayLogConfig = Field(default_factory=GatewayLogConfig)
    page: GatewayPageConfig = Field(default_factory=GatewayPageConfig)


WebSearchProvider = Literal["serper", "anysearch", "serpapi", "tavily", "exa", "brave", "firecrawl"]
WebFetchProvider = Literal["jina", "anysearch", "tavily", "exa", "firecrawl"]

#: The bare environment variable each web vendor's tool falls back to when the
#: config slot is empty, and the name ``~/.raven/env`` mirrors the slot out as.
WEB_VENDOR_ENV_VARS: dict[str, str] = {
    "serper": "SERPER_API_KEY",
    "anysearch": "ANYSEARCH_API_KEY",
    "serpapi": "SERPAPI_API_KEY",
    "jina": "JINA_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "exa": "EXA_API_KEY",
    "brave": "BRAVE_API_KEY",
    "firecrawl": "FIRECRAWL_API_KEY",
}


class WebProviderKey(Base):
    """One web vendor's credential."""

    api_key: str = ""


class WebProvidersConfig(Base):
    """Credentials keyed by vendor, not by the tool that happens to use them.

    AnySearch, Tavily, Exa and Firecrawl each serve both ``web_search`` and
    ``web_fetch`` off one account, so a key held per tool would have to be
    pasted twice and could drift into two values for the same credential. Named
    fields rather than a free dict so a misspelled vendor fails validation
    instead of being kept silently -- which needs ``extra="forbid"``, since the
    tree's default policy is to drop an unknown member. A typo here is exactly
    the "feature X did nothing" case the loader refuses to mask with defaults.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    serper: WebProviderKey = Field(default_factory=WebProviderKey)
    anysearch: WebProviderKey = Field(default_factory=WebProviderKey)
    serpapi: WebProviderKey = Field(default_factory=WebProviderKey)
    jina: WebProviderKey = Field(default_factory=WebProviderKey)
    tavily: WebProviderKey = Field(default_factory=WebProviderKey)
    exa: WebProviderKey = Field(default_factory=WebProviderKey)
    brave: WebProviderKey = Field(default_factory=WebProviderKey)
    firecrawl: WebProviderKey = Field(default_factory=WebProviderKey)

    def key_for(self, vendor: str) -> str:
        """One vendor's configured key, or an empty string for an unknown vendor."""
        if vendor not in type(self).model_fields:
            return ""
        return str(getattr(self, vendor).api_key or "")


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: WebSearchProvider = "serper"
    """Which backend ``web_search`` calls. The key lives under
    ``tools.web.providers.<name>``, so switching does not mean re-pasting one."""
    api_key: str = ""
    """The Serper key on the pre-vendor layout. Still honoured, read after
    ``tools.web.providers.serper.apiKey``; new writes go to the vendor slot."""
    max_results: int = 5


class WebFetchConfig(Base):
    """Web fetch tool configuration."""

    provider: WebFetchProvider = "jina"
    """Which backend ``web_fetch`` reads pages through. Jina is the only one that
    works without a key; a keyed backend whose key does not resolve is replaced
    by Jina at registration, and the log says so, because ``web_fetch`` is
    always offered."""


class WebToolsConfig(Base):
    """Web tools configuration."""

    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    jina_api_key: str = ""
    """The Jina key on the pre-vendor layout. Still honoured, read after
    ``tools.web.providers.jina.apiKey``; new writes go to the vendor slot."""
    providers: WebProvidersConfig = Field(default_factory=WebProvidersConfig)
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    fetch: WebFetchConfig = Field(default_factory=WebFetchConfig)

    def vendor_key(self, vendor: str) -> str:
        """The configured credential for one vendor, the legacy leaf included."""
        if key := self.providers.key_for(vendor):
            return key
        if vendor == "serper":
            return self.search.api_key
        if vendor == "jina":
            return self.jina_api_key
        return ""

    def vendor_keys(self) -> dict[str, str]:
        """Every vendor with a resolved credential, ``{vendor: key}``."""
        out = {}
        for vendor in type(self.providers).model_fields:
            if key := self.vendor_key(vendor):
                out[vendor] = key
        return out


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    timeout: int = 60
    path_append: str = ""
    # Extra regex deny-patterns appended to ExecTool's built-in destructive-command
    # defaults. Empty by default. Operators (or eval harnesses running the agent
    # un-sandboxed) can add host-specific blocks, e.g. osascript / `open -a`.
    extra_deny_patterns: list[str] = Field(default_factory=list)


class AskUserToolConfig(Base):
    """ask_user tool configuration.

    ``timeout`` is the budget for one whole ask_user call, shared across every
    question in the batch. It belongs to the surface rather than to the tool:
    a chat channel and a rendered page sit out a silent user very differently.
    """

    timeout: int = Field(default=600, gt=0)  # seconds, per call not per question


class MediaToolConfig(Base):
    """Config for a media-generation tool (key + base + model).

    Empty fields fall back at call time: ``api_key`` → ``providers.openrouter``
    / ``OPENROUTER_API_KEY``; ``api_base`` → OpenRouter; ``model`` → the tool's
    default (Nano Banana for images).
    """

    api_key: str = ""
    api_base: str = ""  # defaults to https://openrouter.ai/api/v1
    model: str = ""


class MediaGenConfig(Base):
    """Multimodal generation tools configuration.

    OpenRouter is the only backend: image + speech via chat-completions output
    modalities, and video via the async ``/videos`` endpoint (Kling).
    """

    image: MediaToolConfig = Field(default_factory=MediaToolConfig)
    speech: MediaToolConfig = Field(default_factory=MediaToolConfig)
    video: MediaToolConfig = Field(default_factory=MediaToolConfig)
    proxy: str | None = None  # HTTP/SOCKS proxy for media API calls
    output_subdir: str = "generated"  # where generated files are written under workspace


class DeepResearchToolConfig(Base):
    """MiroThinker deep-research tool configuration.

    A blocking HTTP tool that delegates a research question to the MiroThinker
    API and returns a structured result. Registered only when ``api_key`` (or
    ``MIROTHINKER_API_KEY``) is set — it is a paid, minute-scale engine, not a
    default tool. Empty ``api_base`` / ``model`` fall back at call time to the
    MiroMind endpoint and the mini engine.
    """

    api_key: str = ""
    api_base: str = ""  # defaults to https://api.miromind.ai/v1
    model: str = ""  # defaults to mirothinker-1-7-deepresearch-mini


class MCPOAuthConfig(Base):
    """What an ``auth="oauth"`` server's authorization server already told us.

    Every field restates something the OAuth handshake would otherwise learn
    over the network: the RFC 8414 metadata document (``issuer`` through
    ``scopes``), the RFC 9728 protected-resource document (``resource``,
    ``scopes``), and an RFC 7591 registration's result (``client_id``). Filling
    them in lets a connect go straight to the consent page; leaving them empty
    is the discovery-and-register path, unchanged.

    Written by a market install from the catalog entry, and hand-editable. Facts
    only -- never a client secret: raven authorizes as a public client, and a
    secret in ``config.json`` would be a secret in a world-readable file.
    """

    issuer: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    registration_endpoint: str = ""
    scopes: list[str] = Field(default_factory=list)
    resource: str = ""
    """The RFC 8707 audience the tokens are for. Seeding the protected-resource
    document needs it, and it is checked against the server URL before use --
    a value that moves the audience is refused rather than trusted."""
    client_id: str = ""
    """A client already registered with this service, so registration is skipped
    and the consent page can name the service's own app instead of "Raven"."""
    redirect_uri: str = ""
    """The redirect ``client_id`` is registered under. Required with it, and
    honoured exactly: raven's loopback port can move, and a pre-registered
    client whose redirect no longer matches would send the browser to a port
    nobody is listening on."""


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    # Disabled keeps the stanza and any stored credentials but never connects, so
    # turning a server off does not cost the user their re-authorisation.
    enabled: bool = True
    # How this server proves who it is. ``oauth`` means a browser flow whose
    # tokens land under ~/.raven/credentials/mcp/, which is why the manager has
    # to distinguish it: an apikey server that fails is broken, an oauth server
    # that fails may just be waiting for a human.
    auth: Literal["none", "apikey", "oauth"] = "none"
    oauth: MCPOAuthConfig = Field(default_factory=MCPOAuthConfig)


class ToolSearchConfig(Base):
    """Progressive tool disclosure.

    When the live tool catalog (built-ins + plugins + MCP) grows past
    ``compaction_threshold``, most tool schemas are withheld from each request and reached
    on demand through the ``tool_search`` / ``tool_call`` meta-tools, so context
    cost stops scaling with tool count and the per-turn tool list (and thus the
    prompt cache) stays stable. At or below the threshold every tool is exposed
    directly (unchanged behavior) and ``tool_search`` is omitted.

    This switch does not govern ``tool_call``, which the host registers either
    way: a tool can also be absent from the schema because its owner hid it
    there (``ToolRegistry.hide_from_schema``), and that has nothing to do with
    catalog size. Turning this off folds nothing; it does not take the name
    route away.
    """

    enabled: bool = False
    compaction_threshold: int = 50
    """Tool-catalog size that triggers compaction: at or below this many tools
    everything is exposed directly; above it, schemas are withheld."""
    search_result_limit: int = 10
    """Default number of hits ``tool_search`` returns per query."""
    always_visible: list[str] = Field(default_factory=list)
    """Extra tool names kept exposed every turn, on top of the core set."""


class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    ask_user: AskUserToolConfig = Field(default_factory=AskUserToolConfig)
    media: MediaGenConfig = Field(default_factory=MediaGenConfig)
    deep_research: DeepResearchToolConfig = Field(default_factory=DeepResearchToolConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    tool_search: ToolSearchConfig = Field(default_factory=ToolSearchConfig)
    disabled_tools: list[str] = Field(default_factory=list)
    """Tool names to withhold from the assembled tool array and refuse at dispatch.
    The general off switch for a tool this deploy does not want, and the only one
    that covers a tool with an unconfigured stand-in variant (``deep_research``),
    where clearing the tool's own config only swaps which variant registers. Also
    used by eval harnesses (e.g. BrowseComp-Plus) to constrain the agent to a
    specific tool subset. Names match those in ``ToolRegistry`` (e.g.
    ``read_file``, ``web_search``, or ``mcp_bcp-search_search``).

    Re-read once per assembled array, so a change takes effect on the next turn
    rather than the next restart. The tool stays registered either way: expressing
    the preference by unregistering made it irreversible, because nothing
    remembered what to put back.

    The MCP meta-tools -- ``list_mcp_resources``, ``list_mcp_resource_templates``,
    ``read_mcp_resource``, ``list_mcp_prompts``, ``get_mcp_prompt`` -- are the one
    exception. Raven registers and withdraws them itself as MCP servers serving
    those primitives come and go, so an entry naming one has no effect and is
    logged as such rather than silently ignored."""


def _resolve_preset_provenance(name: str, preset: str | None) -> str | None:
    """Backfill or validate a third-party subagent's ``preset`` provenance.

    Backfills ``preset`` to ``name`` only when the entry still carries a
    preset's own default name -- an entry that was renamed (this machine's
    config holds ``Coder``, ``Writer``, ``DeepResearcher``) has a genuinely
    unknowable origin and must not be guessed from other fields such as
    ``command`` or ``model``. An explicit, non-``None`` value is left alone
    unless it names no built-in preset, in which case it is rejected -- a
    hand-edited ``preset: "hermes"`` on an unrelated entry would otherwise
    both dodge the reserved-name guard and hide the real Hermes preset from
    the Presets group.

    Matched on name alone, deliberately not on ``kind``. There is exactly one
    preset per agent, and it already fixes that agent's transport, so a stored
    entry on an older transport (a cli ``codex`` from before its preset moved to
    acp) still belongs to that preset -- and saying so is what lets the UI offer
    it an upgrade. Gating on kind would instead read it as hand-written, hide the
    upgrade, and offer the preset again as unconfigured.

    The preset name set is imported inside this function, not at module
    level: ``raven.agent.subagent.presets`` imports ``SubagentsConfig`` from
    this module (deferred inside ``_normalized``), and importing back here at
    module level would put both sides of that cycle at module-init time.
    """
    from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS

    presets = THIRD_PARTY_SUBAGENT_PRESETS
    if preset is None:
        return name if name in presets else None
    if preset not in presets:
        raise ValueError(f"preset {preset!r} is not a known built-in preset name")
    return preset


class SubagentEverosConfig(Base):
    """A sub-agent's everos identity, as the host must address it to read back
    what that sub-agent wrote.

    Declared rather than discovered. The alternative -- reading the fork's own
    config.json next to its ``run.py`` -- would couple the host to a directory
    convention that lives entirely inside each fork.
    """

    user_id: str | None = None
    """Owner of this sub-agent's ``episode`` memories.

    Under ``source="trace"`` this is also a write target, not just where the
    host reads back from (as under ``source="agent"``): it is the owner
    ``prime_from_turn`` writes the captured conversation's ``user`` rows under.
    """
    agent_id: str | None = None
    """Owner of this sub-agent's ``agent_case`` memories.

    Under ``source="trace"`` this is also a write target, not just where the
    host reads back from (as under ``source="agent"``): it is the owner
    ``prime_from_turn`` writes the captured conversation's ``assistant``/``tool``
    rows under.
    """
    base_url: str | None = None
    """everos service for this sub-agent. ``None`` takes the host's own."""
    session_prefix: str = "cli:"
    """What the fork's launcher prepends to the host-minted id before handing it
    to its Raven as ``--session``. Configurable because that is a convention
    living in each fork's run.py, not something the host controls."""
    source: Literal["agent", "trace"] = "agent"
    """Where this agent's memories come from.

    ``agent`` -- the sub-agent runs everos itself and writes them; the host only
    reads them back. ``trace`` -- nobody wrote anything, so the host hands
    everos the conversation it captured and lets everos extract from it.

    Declared rather than derived from ``kind``: a cli entry may be a Raven fork
    (which writes) or an arbitrary third-party CLI such as codex (which does
    not), so the kind cannot answer this.
    """

    @model_validator(mode="after")
    def _check_owner_declared(self) -> "SubagentEverosConfig":
        if not self.user_id and not self.agent_id:
            raise ValueError("everos needs userId or agentId (it could query nothing otherwise)")
        # A trace payload splits across both tracks -- the prompt is a `user`
        # row and the work is `assistant`/`tool` -- so one id alone would write
        # half of it under an owner nothing reads back.
        if self.source == "trace" and not (self.user_id and self.agent_id):
            raise ValueError("everos source 'trace' needs both userId and agentId")
        return self


class ThirdPartyCliSubagentConfig(Base):
    """A third-party CLI agent (claude code, codex, …) callable as a native subagent.

    ``command`` is an argv template: a ``{prompt}`` token is replaced by the task
    text as a single argv token (injection-safe), ``{prompt_file}`` by a path to
    a file holding the prompt. With neither placeholder, the prompt is delivered
    on the child's stdin.

    Setting ``resume_command`` makes the agent stateful: a caller-supplied
    instance handle is bound to the CLI's own session id, substituted as
    ``{agent_id}``. With ``id_source="provisioned"`` raven mints the id and
    passes it on the create call; with ``"derived"`` the CLI mints it and raven
    reads it back out of the transcript.

    ``timeout`` is ``None`` by default, meaning no automatic limit: the run is
    ended by hand (manual stop), not by a timer. Set it to opt into an
    automatic backstop instead.
    """

    switch_only: bool = False
    """Marks a row that exists only to hold a "no" for a discovered folder.

    Such a row is a stub, not a definition: name, kind, the flag, and an empty
    ``command``. It declares no launcher because it defines nothing -- the folder
    still defines the agent, and
    :func:`raven.agent.subagent.vendored_agents.merge_product_seeds` reads only
    the flag from here. Nothing in it comes from the manifest, so nothing in it
    can go stale when the folder is upgraded.

    That shape is what makes the switch survive an older raven, which accepts the
    row, ignores this field and writes the list back without it: ``command`` is a
    field every version keeps, so an empty one still says "stub" afterwards. This
    marker is the direct answer where it survives; the empty command is the
    answer where it does not.

    False on every row anybody else writes -- an ``install.py`` entry, a hand
    edit -- which keeps their meaning exactly as it was.
    """

    owns: str | None = None
    """What kind of work this agent owns, as one clause completing "``<name>``
    ...". Rendered into the identity prompt's Delegation section so the model is
    told not to do that work itself; agents that declare nothing are absent from
    it, which is what an install with no specialists reads as.

    ``None`` is "not declared" and is filled in from the folder's manifest for a
    vendored agent, so a config written before this field existed still gets one.
    ``""`` is the user saying this agent owns nothing -- kept distinct precisely
    so that opting an agent out is possible and is not undone by that fill.
    """

    name: str
    kind: Literal["cli"] = "cli"
    description: str = ""
    owns_watched_work: bool = Field(
        default=False,
        # Both spellings of the old name. ``Base`` sets ``populate_by_name``, so
        # a row written before this field was renamed is valid under either
        # ``runsOnMachines`` or ``runs_on_machines``, and dropping one is as
        # silent as dropping both: the model ignores the unknown key and dumps
        # the new field as False, which cannot be told from the owner turning it
        # off. A dump re-serialises under the new name only, so a rewrite heals.
        validation_alias=AliasChoices("ownsWatchedWork", "owns_watched_work", "runsOnMachines", "runs_on_machines"),
    )
    """This agent owns work that has to be run AND watched to an outcome (the
    on-call shape). The watch-work nudge keys on it: an agent so marked is the
    one a run-and-watch request is steered toward. Where that work runs is the
    agent's own business -- the host records no machine for it and checks none.
    Declared in the manifest rather than probed off the checkout's binary --
    the probe was a subprocess per roster per process, and what it really asked
    was this one bit."""
    preset: str | None = None
    """Which built-in preset this entry was created from, or ``None`` for a
    hand-written one.

    Provenance only - nothing in the runtime reads it. The web UI needs it
    because ``name`` is user-editable: without it a renamed entry is
    indistinguishable from a hand-written agent, so its preset would wrongly
    reappear as unconfigured and could then be configured twice.
    """
    enabled: bool = True
    """Whether the dispatching model is offered this agent at all.

    Read only where the roster is built (see ``enabled_third_party``), never
    derived from a probe: this records what the user wants, not whether the agent
    currently works. Defaults to ``true`` so an entry written before this field
    existed keeps being advertised exactly as it was.
    """
    command: str
    resume_command: str | None = None
    mcps: list[str] | None = None
    allow_mcp_secrets: bool = False
    stateful: bool | None = None
    """Declares whether reusing an instance handle continues this agent's
    session. ``None`` derives it from ``resume_command``; an explicit value must
    agree with that (``true`` needs ``resumeCommand`` set, ``false`` needs it
    unset), so the declaration the roster advertises can never contradict the
    mechanism that would have to deliver it."""
    reads_local_files: bool = True
    """Whether this agent can open paths on this machine. A CLI agent is a local
    subprocess, so it can by default. Set ``false`` for one that runs elsewhere
    (a container / remote host without the shared filesystem): DAG nodes then
    have to pass file *contents* rather than paths."""
    id_source: Literal["provisioned", "derived"] = "provisioned"
    session_id_pattern: str | None = None
    output_pattern: str | None = None
    transcript_format: Literal["text", "codex_jsonl", "claude_stream_json", "openclaw_json", "opencode_json"] = "text"
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout: int | None = None
    max_output_chars: int = 30000
    everos: SubagentEverosConfig | None = None
    """This agent's everos identity, or ``None`` for an agent that writes no
    everos memory. Declaring it is what turns the Memory record on."""

    @model_validator(mode="after")
    def _check_stateful_matches_resume(self) -> "ThirdPartyCliSubagentConfig":
        if self.stateful is None:
            return self
        if self.stateful and not self.resume_command:
            raise ValueError("stateful is true but resumeCommand is unset (nothing can resume a session)")
        if not self.stateful and self.resume_command:
            raise ValueError("stateful is false but resumeCommand is set (remove resumeCommand to make it stateless)")
        return self

    @model_validator(mode="after")
    def _check_agent_id_placeholders(self) -> "ThirdPartyCliSubagentConfig":
        # An {agent_id} left unsubstituted reaches the CLI as a literal string
        # and the run fails obscurely, so reject the bad combinations at write time.
        in_command = "{agent_id}" in self.command
        if not self.resume_command:
            if in_command:
                raise ValueError("command uses {agent_id} but resumeCommand is unset (agent is stateless)")
            return self
        if "{agent_id}" not in self.resume_command:
            raise ValueError("resumeCommand must contain {agent_id}")
        if self.id_source == "provisioned" and not in_command:
            raise ValueError("command must contain {agent_id} when idSource is 'provisioned'")
        if self.id_source == "derived" and in_command:
            raise ValueError("command must not contain {agent_id} when idSource is 'derived'")
        return self

    @model_validator(mode="after")
    def _check_mcp_file_placeholders(self) -> "ThirdPartyCliSubagentConfig":
        templates = [self.command, *([self.resume_command] if self.resume_command else [])]
        carries = ["{mcp_file}" in template for template in templates]
        if len(set(carries)) > 1:
            raise ValueError("command and resumeCommand must either both contain {mcp_file} or both omit it")
        if self.mcps and not carries[0]:
            raise ValueError("mcps is set but command does not contain {mcp_file}")
        return self

    @model_validator(mode="after")
    def _resolve_preset(self) -> "ThirdPartyCliSubagentConfig":
        self.preset = _resolve_preset_provenance(self.name, self.preset)
        return self


class ThirdPartyOpenAISubagentConfig(Base):
    """A third-party OpenAI-compatible HTTP agent (mirothinker, …) as a subagent.

    ``timeout`` is ``None`` by default, meaning no automatic limit: the run is
    ended by hand (manual stop), not by a timer. Set it to opt into an
    automatic backstop instead.
    """

    owns: str | None = None
    """What kind of work this agent owns, as one clause completing "``<name>``
    ...". Rendered into the identity prompt's Delegation section so the model is
    told not to do that work itself; agents that declare nothing are absent from
    it, which is what an install with no specialists reads as.

    ``None`` is "not declared" and is filled in from the folder's manifest for a
    vendored agent, so a config written before this field existed still gets one.
    ``""`` is the user saying this agent owns nothing -- kept distinct precisely
    so that opting an agent out is possible and is not undone by that fill.
    """

    name: str
    kind: Literal["openai"] = "openai"
    description: str = ""
    preset: str | None = None
    """Which built-in preset this entry was created from, or ``None`` for a
    hand-written one.

    Provenance only - nothing in the runtime reads it. The web UI needs it
    because ``name`` is user-editable: without it a renamed entry is
    indistinguishable from a hand-written agent, so its preset would wrongly
    reappear as unconfigured and could then be configured twice.
    """
    enabled: bool = True
    """Whether the dispatching model is offered this agent at all.

    Read only where the roster is built (see ``enabled_third_party``), never
    derived from a probe: this records what the user wants, not whether the agent
    currently works. Defaults to ``true`` so an entry written before this field
    existed keeps being advertised exactly as it was.
    """
    base_url: str
    model: str
    api_key: str = ""
    stateful: bool = True
    """Declares whether reusing an instance handle continues this agent's
    conversation. An HTTP agent has no session of its own to resume, so a
    ``true`` here opts into Raven replaying the message list itself (see
    ``raven/agent/subagent/instance_state.py``) rather than agreeing with a
    resume command the way the cli kind's ``stateful`` does.

    Defaults to ``true``, unlike the cli kind, because the replay mechanism is
    raven's own and works against any endpoint -- so the default describes the
    mechanism that is actually there. What a ``false`` records is the opposite:
    an endpoint replay is meaningless at (mirothinker ignores a system prompt
    entirely, so a replayed transcript continues nothing). Only a preset, or the
    person who chose a custom endpoint, can know that; no probe reaches it. No
    UI surface writes this field, by decision -- a custom entry is stateful, and
    an operator who knows better edits the config file.

    A stored ``null`` reads as the default rather than as an error: the field
    used to be ``bool | None`` and entries written then have one on disk. See
    ``_null_stateful_is_the_default``."""

    @field_validator("stateful", mode="before")
    @classmethod
    def _null_stateful_is_the_default(cls, value: Any) -> Any:
        """Coerce a stored ``null`` to the default instead of rejecting it.

        This field was optional until the default became ``true``, so every
        entry written by the older form carries an explicit ``null``. Rejecting
        it would fail validation of the whole top-level ``Config`` -- raven
        would stop starting, with the config that could be fixed sitting behind
        the loader that no longer reads it. Same reasoning, and the same shape,
        as ``_drop_declared_local_file_access`` below.
        """
        return True if value is None else value

    reads_local_files: bool = False
    """Always ``false`` for this kind; ``true`` is coerced away below.

    ``OpenAIApiBackend`` has no tools and no filesystem access of its own, so
    no channel exists through which the endpoint could open a path -- being
    served from this host does not change that. The value is not inert:
    ``format_agent_listing`` renders it into the spawn / DAG tool descriptions
    as a ``local-files`` tag, which is the dispatching model's licence to hand
    this agent a path."""
    system_prompt: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    max_output_chars: int = 128000
    everos: SubagentEverosConfig | None = None
    """This agent's everos identity, or ``None`` for an agent the host records no
    memory for."""

    @model_validator(mode="before")
    @classmethod
    def _drop_unsupported_mcp_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        declared_mcps = data.get("mcps")
        allow_secrets = data.get("allowMcpSecrets", data.get("allow_mcp_secrets"))
        if declared_mcps or allow_secrets:
            logger.warning(
                "mcps/allowMcpSecrets are not supported for kind 'openai' (sub-agent {!r}); ignoring -- "
                "the backend has no tool loop",
                data.get("name") or "<unnamed>",
            )
        cleaned = dict(data)
        cleaned.pop("mcps", None)
        cleaned.pop("allowMcpSecrets", None)
        cleaned.pop("allow_mcp_secrets", None)
        return cleaned

    @model_validator(mode="after")
    def _drop_declared_local_file_access(self) -> "ThirdPartyOpenAISubagentConfig":
        """Coerce rather than reject: this field is one an older form wrote.

        The web form used to default ``readsLocalFiles`` to true and render its
        checkbox for both kinds, so a user who created an openai sub-agent and
        did not untick it has ``true`` on disk today. Raising here would surface
        as a ``ValidationError`` on the whole top-level ``Config``: raven would
        stop starting, and the UI that could fix the field is behind the config
        that no longer loads. There is no way out of that from inside the
        product.

        A hard reject is still right where the caller can act on it -- see
        ``reject_unsupported_openai_fields``, which the write path calls on an
        incoming payload.
        """
        if self.reads_local_files:
            logger.warning(
                "readsLocalFiles is not supported for kind 'openai' (sub-agent {!r}); treating it "
                "as false -- the backend has no tools and no filesystem access, so nothing can "
                "open a path here",
                self.name,
            )
            self.reads_local_files = False
        return self

    @model_validator(mode="after")
    def _resolve_preset(self) -> "ThirdPartyOpenAISubagentConfig":
        self.preset = _resolve_preset_provenance(self.name, self.preset)
        return self


ACP_UNSUPPORTED_FIELDS: tuple[str, ...] = (
    "resume_command",
    "id_source",
    "session_id_pattern",
    "output_pattern",
    "transcript_format",
    "stateful",
    "reads_local_files",
)
"""Fields a cli entry uses to *declare* behaviour, which an acp entry negotiates.

Kept as data rather than inline so the write-path rejector
(``raven.config.update_subagents.reject_unsupported_acp_fields``) and the
load-path coercion below cannot drift onto different lists.
"""

ACP_PROMPT_PLACEHOLDERS: tuple[str, ...] = ("{prompt}", "{prompt_file}", "{agent_id}")


class ThirdPartyAcpSubagentConfig(Base):
    """A third-party agent reached over ACP (Agent Client Protocol), e.g. ``hermes acp``.

    ``command`` starts a *server* and is spawned once per connection, not once
    per task: a task is delivered as a ``session/prompt`` request on the running
    connection. So unlike a cli entry it carries no ``{prompt}`` placeholder,
    and none of the fields listed in :data:`ACP_UNSUPPORTED_FIELDS`.

    Those seven are absent by design rather than by omission. Each one is a cli
    *declaration* about behaviour (can it resume, who mints the session id, how
    is its transcript shaped) whose acp counterpart comes from the ``initialize``
    handshake instead. Accepting both would make every one of them a second
    source of truth, and the first time a declaration disagreed with the
    handshake nothing in the code would know which to believe.
    """

    switch_only: bool = False
    """Marks a row that exists only to hold a "no" for a discovered folder.

    Such a row is a stub, not a definition: name, kind, the flag, and an empty
    ``command``. It declares no launcher because it defines nothing -- the folder
    still defines the agent, and
    :func:`raven.agent.subagent.vendored_agents.merge_product_seeds` reads only
    the flag from here. Nothing in it comes from the manifest, so nothing in it
    can go stale when the folder is upgraded.

    That shape is what makes the switch survive an older raven, which accepts the
    row, ignores this field and writes the list back without it: ``command`` is a
    field every version keeps, so an empty one still says "stub" afterwards. This
    marker is the direct answer where it survives; the empty command is the
    answer where it does not.

    False on every row anybody else writes -- an ``install.py`` entry, a hand
    edit -- which keeps their meaning exactly as it was.
    """

    owns: str | None = None
    """What kind of work this agent owns, as one clause completing "``<name>``
    ...". Rendered into the identity prompt's Delegation section so the model is
    told not to do that work itself; agents that declare nothing are absent from
    it, which is what an install with no specialists reads as.

    ``None`` is "not declared" and is filled in from the folder's manifest for a
    vendored agent, so a config written before this field existed still gets one.
    ``""`` is the user saying this agent owns nothing -- kept distinct precisely
    so that opting an agent out is possible and is not undone by that fill.
    """

    name: str
    kind: Literal["acp"] = "acp"
    description: str = ""
    """Operator override for the roster line. Blank means "use what the handshake
    reported" (``agentInfo.name`` plus version), which is the point of ACP: the
    agent describes itself, so a human does not have to."""
    owns_watched_work: bool = Field(
        default=False,
        # Both spellings of the old name. ``Base`` sets ``populate_by_name``, so
        # a row written before this field was renamed is valid under either
        # ``runsOnMachines`` or ``runs_on_machines``, and dropping one is as
        # silent as dropping both: the model ignores the unknown key and dumps
        # the new field as False, which cannot be told from the owner turning it
        # off. A dump re-serialises under the new name only, so a rewrite heals.
        validation_alias=AliasChoices("ownsWatchedWork", "owns_watched_work", "runsOnMachines", "runs_on_machines"),
    )
    """This agent owns work that has to be run AND watched to an outcome (the
    on-call shape). The watch-work nudge keys on it: an agent so marked is the
    one a run-and-watch request is steered toward. Where that work runs is the
    agent's own business -- the host records no machine for it and checks none.
    Declared in the manifest rather than probed off the checkout's binary --
    the probe was a subprocess per roster per process, and what it really asked
    was this one bit."""
    preset: str | None = None
    """Which built-in preset this entry was created from, or ``None`` for a
    hand-written one. Provenance only -- see the cli config for why the web UI
    needs it."""
    enabled: bool = True
    command: str
    mcps: list[str] | None = None
    allow_mcp_secrets: bool = False
    session_mcp: bool | None = None
    """Whether this agent keeps one session's MCP servers to that session.

    Three-valued. ``None`` is "not declared", and it resolves to the measured
    answer for the preset this row was created from -- ``False`` for opencode,
    ``True`` for everything else. Not a plain ``True`` default, because the field
    arrived after rows were already on disk: an ``opencode`` row written before it
    existed carries no key, and a permissive default would leave exactly the agent
    this is about delivering as if it isolated. See
    :func:`raven.agent.subagent.presets.session_mcp_for`.

    The one behavioural declaration an acp entry carries, and it is not a second
    source of truth for the reason the rest of them would be: ``initialize`` has
    no field for session isolation. Measured on the three shipped adapters --
    claude-agent-acp 0.66.0 and codex-acp 1.1.14 isolate, opencode-ai 1.18.16
    does not, and all three report the same ``mcpCapabilities``. So there is
    nothing here to disagree with the handshake about, and nothing in the
    handshake to read instead.

    ``False`` withholds this agent's MCP delivery rather than degrading it,
    because raven pools one connection per agent name: two concurrent sub-agents
    of an agent that does not isolate see each other's tools, so a node deliberately
    not granted a server reaches it through the sibling that was. Delivery is
    withheld with a note (``AcpAgentBackend._mcp_note``) instead of being sent and
    hoped over.

    An undeclared row on a hand-written agent, or on a preset that says nothing,
    resolves to ``True``: a peer that isolates is the normal case, and the ungated
    stdio baseline is what makes the field work at all. Withholding from an agent
    nobody has measured would turn off MCP for peers that work.

    An acp field only. A cli agent is one process per task and is handed its
    servers in that process's own config file, so its isolation is structural and
    there is nothing to declare; an openai agent has no tool loop and is refused
    the field on the write path."""
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    ready_timeout_ms: int = 30000
    """How long the ``initialize`` handshake may take before the agent is
    reported unreachable. Generous by default because a bridge-backed server can
    be slow to come up: ``openclaw acp`` did not answer within 20s on the host
    this was measured on, and a too-tight budget reports a working agent as
    broken."""
    timeout: int | None = None
    """Per-task ceiling for one ``session/prompt``. ``None`` means no automatic
    limit, matching the cli config: a long task is ended by hand, not a timer."""
    max_output_chars: int = 30000
    everos: SubagentEverosConfig | None = None
    """This agent's everos identity, or ``None`` for an agent the host records no
    memory for. Not in :data:`ACP_UNSUPPORTED_FIELDS` because it declares nothing
    about the agent -- it is how the *host* addresses that agent's memories, which
    no ``initialize`` handshake reports."""

    @model_validator(mode="before")
    @classmethod
    def _warn_on_declared_cli_fields(cls, data: Any) -> Any:
        """Warn about, rather than reject, a cli-only field on an acp entry.

        Warn-and-drop on load for the reason ``_drop_declared_local_file_access``
        gives: a hard reject here surfaces as a ``ValidationError`` on the whole
        top-level ``Config``, so raven would stop starting and the UI that could
        fix the field would sit behind the config that no longer loads. Dropping
        needs no code -- ``Base`` ignores unknown keys -- but the operator still
        has to be told that the field they wrote is doing nothing.

        Inspected here rather than in an ``after`` validator because by then the
        unknown key is already gone: ``Base`` does not set ``extra="allow"``, so
        ``model_extra`` is empty and there is nothing left to notice.

        The hard reject lives at the write path instead
        (``update_subagents.reject_unsupported_acp_fields``), where the caller owns
        the value and can act on the error.
        """
        if not isinstance(data, dict):
            return data
        declared = [
            spelling for field in ACP_UNSUPPORTED_FIELDS for spelling in (field, to_camel(field)) if spelling in data
        ]
        if declared:
            logger.warning(
                "{} not supported for kind 'acp' (sub-agent {!r}); ignoring -- an acp agent reports "
                "these through the initialize handshake instead",
                sorted(set(declared)),
                data.get("name") or "<unnamed>",
            )
        return data

    @model_validator(mode="after")
    def _warn_on_prompt_placeholders(self) -> "ThirdPartyAcpSubagentConfig":
        """Warn that a task placeholder in ``command`` cannot work here.

        Not a reject, for the same startup reason as above, and not a coercion
        either because there is no correct value to substitute. Left in place, the
        placeholder reaches the child as a literal argv token, the handshake
        fails, and the agent is reported ``unreachable`` with the launch error --
        a degraded but self-explaining state, which beats not starting.
        """
        found = [p for p in ACP_PROMPT_PLACEHOLDERS if p in self.command]
        if found:
            logger.warning(
                "acp sub-agent {!r} has task placeholder(s) {} in `command`, which starts a server "
                "rather than one task; they will be passed through literally and the handshake will "
                "fail",
                self.name,
                found,
            )
        return self

    @model_validator(mode="after")
    def _resolve_preset(self) -> "ThirdPartyAcpSubagentConfig":
        self.preset = _resolve_preset_provenance(self.name, self.preset)
        return self


class BuiltinAgentConfig(Base):
    """An in-process raven agent loop, callable as a named sub-agent.

    The same backend the main loop dispatches an unnamed spawn to
    (:class:`RavenLoopBackend`), differing only in which skills and tools it may
    reach. It is on the same table as the external agents so that ``spawn`` and a
    DAG node pick from one roster: a built-in agent that is only reachable by
    omitting the ``subagent`` argument is an agent the model cannot be told about.

    A row here is an *override* of the package's own seed rows (see
    ``raven.agent.subagent.builtin_agents``), matched by ``name`` -- writing one
    is how a user retunes ``research-raven``'s skills, and writing a name the
    package does not ship is how they add a fifth. There is no way to delete a
    seed row, because not writing it is what "use the default" means -- nor to
    switch one off: ``enabled`` is the one field an override may not speak to, and
    ``merge_builtin_seeds`` discards it. An unnamed ``spawn`` and a dag node with
    no ``subagent`` both dispatch to the generic seed, so a roster without it is a
    roster with a hole where the default lands.

    ``skills`` and ``tools`` are three-valued on purpose. ``null`` (the default)
    means the full catalogue, an empty list means *no* menu at all, and a list
    narrows to those entries -- the empty case has to be expressible because
    "this agent gets no skills" is a real charter, and folding it into ``null``
    would advertise the opposite of what was written.
    """

    owns: str | None = None
    """What kind of work this agent owns, as one clause completing "``<name>``
    ...". Rendered into the identity prompt's Delegation section so the model is
    told not to do that work itself; agents that declare nothing are absent from
    it, which is what an install with no specialists reads as.

    ``None`` is "not declared" and inherits the seed's own value when this row
    overrides one (``_overrides`` drops a field still holding its default), so a
    row written to retune ``skills`` cannot silently strip a seed's ownership.
    ``""`` is the user saying this agent owns nothing -- kept distinct precisely
    so that opting an agent out is possible and is not undone by that inherit.
    """

    name: str
    kind: Literal["builtin"] = "builtin"
    description: str = ""
    enabled: bool = True
    model: str | None = None
    """Model override for this agent's loop; ``None`` inherits the main loop's."""
    skills: list[str] | None = None
    tools: list[str] | None = None
    mcps: list[str] | None = None
    restrict_to_workspace: bool | None = None
    """``None`` inherits the manager's own setting rather than forcing one, so a
    row that says nothing about confinement cannot loosen it."""
    timeout: int | None = None
    max_output_chars: int = 30000


AgentConfig = Annotated[
    BuiltinAgentConfig | ThirdPartyCliSubagentConfig | ThirdPartyOpenAISubagentConfig | ThirdPartyAcpSubagentConfig,
    Field(discriminator="kind"),
]


class PlaybookRouterConfig(Base):
    """How far the per-turn playbook listing is narrowed.

    Same two knobs as ``skillForge.router``, and for the same reason: the
    expensive part of advertising a playbook is its description plus parameter
    table, and a library of a hundred cannot spend that on every turn. Only the
    *description* half is narrowed -- the ``name`` enum stays the whole library,
    so a retrieval miss never makes a playbook unreachable.
    """

    top_k: int = Field(default=5, ge=1)
    """How many playbooks get a full description in the tool this turn."""

    over_fetch_factor: int = Field(default=2, ge=1)
    """Rank this many times ``top_k`` before cutting back, mirroring the skill
    router. One local source means there is nothing to fuse, so this only widens
    the window the ranking is computed over."""


class PlaybookConfig(Base):
    """The stored playbook library, and how it is offered to the model.

    ``enabled`` gates the library being loaded at all. There is no per-message
    matching cost any more: the model decides whether to use a playbook, from the
    same tool table it decides everything else from, so nothing runs ahead of the
    turn and no gate call is spent on a message that mentions a trigger word.

    On by default. What that costs is measurable and fixed: both entries
    register whenever the feature is on -- together about 848 tokens per request
    on a populated library (``available_history`` on a 200k window moves from
    130.0k to 129.2k), and nothing else: no pre-turn work, no LLM call, no
    matching. The builtin layer ships empty, and the loader registers over an
    empty library too, describing itself in one sentence as having nothing
    installed -- it used to be withheld until the user layer held a playbook,
    which withheld it from the session that created the first one. Turn it off
    with ``playbooks.enabled: false``.
    """

    enabled: bool = True
    dir: str | None = None
    """Override for the user layer of the library; defaults to
    ``<agent_home>/playbooks``. The builtin layer ships with the package and
    is not configurable — a user playbook of the same name shadows it."""

    model: str | None = None
    """Model for composing a ``prompt``-mode graph on the CLI path, which has no
    model of its own; defaults to the loop's own model. The in-conversation path
    does not use it -- the main model composes from the guidance directly."""

    disabled: list[str] = Field(default_factory=list)
    """Deny list of playbook names not offered on this machine. Local state lives
    here rather than in playbook.md (the distribution unit): enable/disable edit
    this list, for builtin and user playbooks alike.

    Disabled means *not listed*: the model cannot see it, so it cannot call it --
    which is the whole of what disabling can mean now that there is no passive
    matcher left to mute. An explicit ``raven playbook run`` still resolves one,
    because that is the user's own hand."""

    router: PlaybookRouterConfig = Field(default_factory=PlaybookRouterConfig)


class SubagentsConfig(Base):
    """The one table of agents raven can dispatch to.

    ``agents`` holds every kind in one list -- ``builtin`` rows (an in-process
    raven loop) beside the three external transports -- because ``spawn`` and a
    DAG node have to pick from the same roster. Split across two lists, an agent
    reachable from one entry point and not the other is a state neither the model
    nor the user can see, which is what this list being single fixes.

    Read under its old key ``thirdParty`` as well, so a config written before the
    rename keeps loading; the write path emits ``agents``.
    """

    agents: list[AgentConfig] = Field(
        default_factory=list,
        validation_alias=AliasChoices("agents", "thirdParty", "third_party"),
    )

    @model_validator(mode="after")
    def _dedupe_names(self) -> "SubagentsConfig":
        """Drop later rows that repeat a name, keeping the first, with a warning.

        A hand-edited config with two rows of one name used to load fine and let
        the second silently win whichever dict was built last, so which agent
        answered depended on construction order.

        Warn-and-drop rather than reject, on the same reasoning as
        ``_warn_on_declared_cli_fields``: raising here surfaces as a
        ``ValidationError`` on the whole top-level ``Config``, so raven would stop
        starting and the UI that could fix the duplicate would sit behind the
        config that no longer loads. Keeping the *first* row makes the outcome
        deterministic, which is the property that was actually missing. The hard
        reject lives at the write path (``update_subagents.set_agents``), where the
        caller owns the value and can act on the error.
        """
        seen: set[str] = set()
        kept: list[Any] = []
        for cfg in self.agents:
            if cfg.name in seen:
                logger.warning(
                    "sub-agent {!r} is declared more than once; ignoring the later row(s) -- "
                    "remove the duplicate from subagents.agents",
                    cfg.name,
                )
                continue
            seen.add(cfg.name)
            kept.append(cfg)
        if len(kept) != len(self.agents):
            self.agents = kept
        return self


class CliConfig(Base):
    """CLI surface configuration."""

    turn_summary: bool = True
    """Render a one-line tokens/cost summary after each successful CLI turn."""


class Config(BaseSettings):
    """Root configuration for raven."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    cli: CliConfig = Field(default_factory=CliConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    cron: CronConfig = Field(default_factory=CronConfig)
    subagents: SubagentsConfig = Field(default_factory=SubagentsConfig)
    playbooks: PlaybookConfig = Field(default_factory=PlaybookConfig)
    tui: TuiConfig = Field(default_factory=TuiConfig)
    acp: AcpConfig = Field(default_factory=AcpConfig)
    # UI language chosen during onboarding. Drives the wizard/CLI copy and the
    # agent's reply language (injected into the system prompt). "en" | "zh".
    language: Literal["en", "zh"] = "en"

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path.

        The default follows ``RAVEN_HOME`` rather than being the literal it is
        declared as. Sessions, uploads, exports and the skill pool all live here,
        so an instance pointed at another home that kept this one would quietly
        read and write the first installation's conversations -- which is what a
        separate home exists to avoid. An explicitly configured workspace is
        always used as written.
        """
        from raven.config.paths import default_workspace

        raw = self.agents.defaults.workspace
        if raw == AgentDefaults.model_fields["workspace"].default:
            return default_workspace()
        return Path(raw).expanduser()

    def channel_workspaces(self) -> dict[str, str]:
        """Every channel that names its own working directory.

        Keyed by the channel name as it appears in a session key
        (``qq:<open_id>``), which is the field name under ``channels``.
        Channels that left ``workspace`` empty are omitted, so the resolver
        falls back to ``<root>/<channel>`` for them.
        """
        found: dict[str, str] = {}
        for name, channel in self.channels:
            configured = getattr(channel, "workspace", "")
            if isinstance(configured, str) and configured.strip():
                found[name] = configured.strip()
        return found

    def effective_media_config(self) -> MediaGenConfig:
        """Media config resolved for registration and auth.

        A media tool (image/speech/video) counts as configured only when the
        user set its ``model`` or ``apiKey`` under ``tools.media.<tool>``. For
        each configured tool we default a missing key to
        ``providers.openrouter.apiKey`` so the chat key can be reused without
        re-declaring it. Tools the user did not configure are left untouched
        (no key, no model) — ``AgentLoop`` withholds a media tool until it has
        a key or model, so an OpenRouter key set for chat alone never
        surfaces image/speech/video to the agent. Returns a copy so this
        resolution never mutates the raw config.
        """
        media = self.tools.media.model_copy(deep=True)
        openrouter = self.providers.get("openrouter")
        or_key = openrouter.api_key if openrouter else ""
        for tool in (media.image, media.speech, media.video):
            borrow_openrouter_key(tool, or_key)
        return media

    def _match_provider(self, model: str | None = None) -> tuple["ProviderConfig | None", str | None]:
        """The section serving ``model`` and its registry name.

        An explicit ``agents.defaults.provider`` answers outright -- which,
        since the provider became required, is every config the loader has
        migrated. The derivation below stays for exactly two callers: that
        migration, which needs to know what a pre-rule config was in fact
        resolving to before it writes the answer down, and a config the
        migration could not write to.

        The derivation is worth reading once, because it is why the field is
        required now. A prefixed id is answered by the provider it names, but a
        *bare* id falls to keyword matching in ``PROVIDERS`` order -- so with
        both anthropic and openrouter configured, ``gpt-4.1`` resolved to
        openrouter over openai for no better reason than a list index. That is
        a guess about whose key pays for the call.
        """
        from raven.providers.registry import (
            PROVIDERS,
            canonical_provider_name,
            find_by_keywords,
            find_by_name,
            split_model_id,
        )

        forced = self.agents.defaults.provider
        if forced and forced != "auto":
            # Return the canonical name: callers look the spec up by it, and a
            # config still naming the provider the old way would find nothing.
            forced = canonical_provider_name(forced)
            p = self.providers.get(forced)
            return (p, forced) if p else (None, None)

        model_id = model or self.agents.defaults.model
        prefix, _ = split_model_id(model_id)

        # A curated `models` entry is the user naming the vendor, so it outranks the
        # prefix/keyword rule below: OpenRouter's catalog is full of names carrying
        # another vendor's prefix (`openai/...`), which that rule attributes to that
        # vendor -- or, when it has no key, to whatever the last rung falls back to.
        # Must stay the same predicate as serving_provider_for_model, which gates what
        # the model picker may store: disagreement means a model validates as servable
        # and is then routed to a different vendor's api_base and api_key.
        for spec in PROVIDERS:
            p = self.providers.get(spec.name)
            if p is not None and model_id in self._offered_models(spec):
                return p, spec.name

        # `spec.claims` is the whole prefix-beats-keyword rule: a prefixed id is
        # answered only by the provider it names (so `github-copilot/...codex`
        # cannot match openai_codex, and no vendor's key is posted to another's
        # endpoint), while a bare id falls to keywords in registry order.
        for spec in PROVIDERS:
            if not spec.claims(model_id):
                continue
            p = self.providers.get(spec.name)
            if p and section_has_credentials(p, spec):
                return p, spec.name

        # Explicit prefix naming a provider Raven has no spec for: LiteLLM knows
        # the vendor, so credentials under that name are enough to reach it.
        #
        # Only where there is genuinely no spec. A provider that has one has
        # already been offered above and turned down for want of credentials --
        # letting it back in here on `api_key` alone reinstated exactly the
        # material this rejected it for missing: Azure with a key and no address
        # routed here, while display and startup both called it unconfigured.
        if prefix and find_by_name(prefix) is None:
            passthrough = self.providers.get(prefix)
            if passthrough and section_has_credentials(passthrough, None, prefix):
                return passthrough, canonical_provider_name(prefix)

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = self.providers.get(spec.name)
            if p and section_has_credentials(p, spec):
                return p, spec.name

        # Fallback: gateways first, then others (follows registry order).
        # OAuth providers are NOT valid fallbacks -- they require explicit model
        # selection.
        #
        # Once an id names a vendor -- by prefix, or by a keyword that only one
        # vendor answers to -- reaching this point means that vendor has no
        # credentials. Only a gateway or a local deployment may answer then,
        # because they route whatever they are handed; a direct vendor would be
        # receiving a competitor's model id along with its own key. Getting here
        # having named nobody ("llama-3.3-70b") carries no such claim, so any
        # credentialed provider is a legitimate guess.
        names_a_vendor = bool(prefix) or find_by_keywords(model_id) is not None
        for spec in PROVIDERS:
            if names_a_vendor and not (spec.is_gateway or spec.is_local):
                continue
            if spec.is_oauth:
                continue
            p = self.providers.get(spec.name)
            if p and section_has_credentials(p, spec):
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.effective_api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for gateway/local providers."""
        from raven.providers.registry import find_by_name

        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        # Only gateways / local providers get a default api_base here. A
        # standard provider (like Moonshot) reaches its base URL through the env
        # vars LiteLLMProvider._setup_env writes; what is returned here travels
        # as the per-call ``api_base`` kwarg, which would override LiteLLM's own
        # routing for that vendor.
        if name:
            spec = find_by_name(name)
            if spec and spec.usable_default_api_base:
                return spec.usable_default_api_base
        return None

    def _provider_is_configured(self, spec) -> bool:
        """Whether ``spec``'s section carries enough config to be usable.

        Deliberately the same predicate as the ``configured`` flag in
        ``update_providers.list_providers``, which is what the web model picker
        filters its options on: the picker's offer set and any accept/reject
        check built on this must agree, or one offers what the other refuses.
        Asking ``providers.auth`` is how they stay the same predicate rather than
        two spellings of it -- reading the flat key here let it stand in for an
        ``endpoints`` list whose entry carries none, and since
        ``provider_endpoints`` ignores the flat field once ``endpoints`` is set,
        the section read as configured while every request from it 401s.

        ``include_external`` for the same reason display asks with it: this
        reports on what is true now, and an OAuth provider's section is
        legitimately empty until its token file exists.
        """
        p = self.providers.get(spec.name)
        if p is None:
            return False
        from raven.providers.auth import credential_status

        return credential_status(spec.name, p, spec=spec, include_external=True).ok

    def _offered_models(self, spec) -> list[str]:
        """Models ``spec`` serves, per the config -- empty when it is unconfigured.

        Exactly what the web model picker lists for the provider: its curated
        ``models``, or the registry default when that list is empty. Shared by
        ``_match_provider`` (routing) and ``serving_provider_for_model`` (the
        accept check) so the two cannot drift apart.
        """
        p = self.providers.get(spec.name)
        if p is None or not self._provider_is_configured(spec):
            return []
        return list(p.models) or ([spec.default_model] if spec.default_model else [])

    def serving_provider_for_model(self, model: str) -> str | None:
        """Name of a configured provider that can actually serve ``model``, else ``None``.

        The question ``get_provider_name`` answers is "which credentials would
        this call use", and its last two ladder rungs fall back to any
        configured provider, so it never reports a model as unservable. This
        answers "can anything serve it at all": a model is servable when a
        configured provider offers it (its curated ``models``, or the registry
        default when that list is empty -- exactly what the picker lists) or
        when the registry resolves the model name to a configured provider.
        """
        from raven.providers.registry import PROVIDERS, find_by_model

        for spec in PROVIDERS:
            if model in self._offered_models(spec):
                return spec.name

        spec = find_by_model(model)
        if spec is not None and self._provider_is_configured(spec):
            return spec.name
        return None

    @property
    def skill_forge(self):
        """The default SkillForgeConfig.

        Extension blocks live on ``RavenConfig`` (``load_raven_config``); a plain
        ``Config`` answers with defaults so code that reads ``config.skill_forge``
        works on either.
        """
        from raven.config.raven import SkillForgeConfig

        return SkillForgeConfig()

    model_config = ConfigDict(
        env_prefix="NANOBOT_",
        env_nested_delimiter="__",
        extra="forbid",
    )


def borrows_openrouter_key(tool: MediaToolConfig) -> bool:
    """Whether this section is in the one state that borrows: configured (it
    names a model or a key) yet keyless. Unconfigured sections borrow nothing,
    which is what keeps a chat credential from quietly enabling tools that
    bill per call."""
    return bool((tool.api_key or tool.model) and not tool.api_key)


def borrow_openrouter_key(tool: MediaToolConfig, openrouter_key: str) -> None:
    """The media key-borrow rule, stated once, applied in place."""
    if borrows_openrouter_key(tool) and openrouter_key:
        tool.api_key = openrouter_key


def live_web_search_key(section: Any) -> str | None:
    """The Serper key from a raw ``tools.web.search`` subtree, or ``None``.

    Validation and the credential read live here for the reason
    :func:`live_media_tool_config`'s do: the caller (``config.live``) holds raw
    file subtrees and must not handle credential fields itself. ``None`` is
    "no usable answer" -- no section, or one the schema rejects -- and the
    caller keeps what it had; an empty key in a valid section is a real
    answer, which is how a key gets revoked without a restart.
    """
    if not isinstance(section, dict):
        return None
    try:
        return WebSearchConfig.model_validate(section).api_key
    except Exception:  # noqa: BLE001 - an invalid candidate dispenses no new answer
        return None


def live_web_provider_key(section: Any, vendor: str) -> str | None:
    """One vendor's key from a raw ``tools.web.providers`` subtree, or ``None``.

    The vendor-slot half of :func:`live_web_search_key`, and ``None`` means the
    same thing: no usable answer, so the caller keeps what it had. An empty key
    in a valid subtree is a real answer, which is how a key gets revoked
    without a restart. An unknown vendor reads as empty rather than raising --
    the selection is validated where it is set, and this reader must not turn a
    stale spelling into a startup failure.
    """
    if not isinstance(section, dict):
        return None
    try:
        return WebProvidersConfig.model_validate(section).key_for(vendor)
    except Exception:  # noqa: BLE001 - an invalid candidate dispenses no new answer
        return None


def live_media_tool_config(section: Any, openrouter_section: Any) -> "MediaToolConfig | None":
    """One media tool's section as a live file has it, resolved by the same
    rule as :meth:`Config.effective_media_config`.

    Takes raw file subtrees because the caller (``config.live``) holds no
    validated ``Config``; validation happens here so the credential handling
    stays in this module, next to the rule it applies. ``None`` is "no usable
    answer" and the caller keeps what it had: the tool's own section failing
    validation, and equally the borrow's input -- a configured-but-keyless
    tool whose ``providers.openrouter`` slice is present but invalid gets no
    new answer, never a valid-looking config with the borrowed key dropped.
    """
    if section is not None and not isinstance(section, dict):
        return None
    try:
        cfg = MediaToolConfig.model_validate(section or {})
    except Exception:  # noqa: BLE001 - a torn read is not worth a turn
        return None
    if borrows_openrouter_key(cfg):
        # The borrow is a second input to the combined answer, so its slice is
        # admitted on the same terms as the tool's own: absent means "nothing
        # to lend" (a real, keyless answer), while present-but-invalid rejects
        # the WHOLE answer -- degrading it to an empty borrow would hand the
        # caller a valid-looking config that silently dropped the credential
        # the last valid file lent.
        if openrouter_section is not None:
            if not isinstance(openrouter_section, dict):
                return None
            try:
                openrouter_key = ProviderConfig.model_validate(openrouter_section).api_key
            except Exception:  # noqa: BLE001 - an invalid candidate dispenses no new answer
                return None
            borrow_openrouter_key(cfg, openrouter_key)
    return cfg
