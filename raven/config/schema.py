"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Annotated, Any, Literal

from loguru import logger
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

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


class WhatsAppConfig(ChannelBase):
    """WhatsApp channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""  # Shared token for bridge auth (auto-generated when empty)
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed phone numbers; ['*'] = anyone
    group_policy: Literal["open", "mention"] = "open"  # "open" responds to all, "mention" only when @mentioned


class TelegramConfig(ChannelBase):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = Field(default="", json_schema_extra={"required": True})  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user IDs or usernames; ['*'] = anyone
    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    reply_to_message: bool = False  # If true, bot replies quote the original message
    group_policy: Literal["open", "mention"] = (
        "mention"  # "mention" responds when @mentioned or replied to, "open" responds to all
    )


class FeishuConfig(ChannelBase):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    enabled: bool = False
    app_id: str = Field(default="", json_schema_extra={"required": True})  # App ID from Feishu Open Platform
    app_secret: str = Field(default="", json_schema_extra={"required": True})  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription
    verification_token: str = ""  # Verification Token for event subscription
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user open_ids; ['*'] = anyone
    react_emoji: str = "THUMBSUP"  # Emoji type for message reactions (e.g. THUMBSUP, OK, DONE, SMILE)
    group_policy: Literal["open", "mention"] = "mention"  # "mention" responds when @mentioned, "open" responds to all


class DingTalkConfig(ChannelBase):
    """DingTalk channel configuration using Stream mode."""

    enabled: bool = False
    client_id: str = Field(default="", json_schema_extra={"required": True})  # AppKey
    client_secret: str = Field(default="", json_schema_extra={"required": True})  # AppSecret
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed staff_ids; ['*'] = anyone


class DiscordConfig(ChannelBase):
    """Discord channel configuration."""

    enabled: bool = False
    token: str = Field(default="", json_schema_extra={"required": True})  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user IDs; ['*'] = anyone
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT
    group_policy: Literal["mention", "open"] = "mention"


class MatrixConfig(ChannelBase):
    """Matrix (Element) channel configuration."""

    enabled: bool = False
    homeserver: str = "https://matrix.org"
    access_token: str = Field(default="", json_schema_extra={"required": True})
    user_id: str = Field(default="", json_schema_extra={"required": True})  # @bot:matrix.org
    device_id: str = ""
    e2ee_enabled: bool = True  # Enable Matrix E2EE support (encryption + encrypted room handling).
    sync_stop_grace_seconds: int = (
        2  # Max seconds to wait for sync_forever to stop gracefully before cancellation fallback.
    )
    max_media_bytes: int = (
        20 * 1024 * 1024
    )  # Max attachment size accepted for Matrix media handling (inbound + outbound).
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # ['*'] = anyone
    group_policy: Literal["open", "mention", "allowlist"] = "open"
    group_allow_from: list[str] = Field(default_factory=list)
    allow_room_mentions: bool = False


class EmailConfig(ChannelBase):
    """Email channel configuration (IMAP inbound + SMTP outbound)."""

    enabled: bool = False
    consent_granted: bool = False  # Explicit owner permission to access mailbox data

    # IMAP (receive)
    imap_host: str = Field(default="", json_schema_extra={"required": True})
    imap_port: int = 993
    imap_username: str = Field(default="", json_schema_extra={"required": True})
    imap_password: str = Field(default="", json_schema_extra={"required": True})
    imap_mailbox: str = "INBOX"
    imap_use_ssl: bool = True

    # SMTP (send)
    smtp_host: str = Field(default="", json_schema_extra={"required": True})
    smtp_port: int = 587
    smtp_username: str = Field(default="", json_schema_extra={"required": True})
    smtp_password: str = Field(default="", json_schema_extra={"required": True})
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_address: str = ""

    # Behavior
    auto_reply_enabled: bool = True  # If false, inbound email is read but no automatic reply is sent
    poll_interval_seconds: int = 30
    mark_seen: bool = True
    max_body_chars: int = 12000
    subject_prefix: str = "Re: "
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed sender email addresses; ['*'] = anyone


class MochatMentionConfig(Base):
    """Mochat mention behavior configuration."""

    require_in_groups: bool = False


class MochatGroupRule(Base):
    """Mochat per-group mention requirement."""

    require_mention: bool = False


class MochatConfig(ChannelBase):
    """Mochat channel configuration."""

    enabled: bool = False
    base_url: str = "https://mochat.io"
    socket_url: str = ""
    socket_path: str = "/socket.io"
    socket_disable_msgpack: bool = False
    socket_reconnect_delay_ms: int = 1000
    socket_max_reconnect_delay_ms: int = 10000
    socket_connect_timeout_ms: int = 10000
    refresh_interval_ms: int = 30000
    watch_timeout_ms: int = 25000
    watch_limit: int = 100
    retry_delay_ms: int = 500
    max_retry_attempts: int = 0  # 0 means unlimited retries
    claw_token: str = Field(default="", json_schema_extra={"required": True})
    agent_user_id: str = ""
    sessions: list[str] = Field(default_factory=list)
    panels: list[str] = Field(default_factory=list)
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # ['*'] = anyone
    mention: MochatMentionConfig = Field(default_factory=MochatMentionConfig)
    groups: dict[str, MochatGroupRule] = Field(default_factory=dict)
    reply_delay_mode: str = "non-mention"  # off | non-mention
    reply_delay_ms: int = 120000


class SlackDMConfig(Base):
    """Slack DM policy configuration."""

    enabled: bool = True
    policy: str = "open"  # "open" or "allowlist"
    allow_from: list[str] = Field(default_factory=list)  # Allowed Slack user IDs


class SlackConfig(ChannelBase):
    """Slack channel configuration."""

    enabled: bool = False
    mode: str = "socket"  # "socket" supported
    webhook_path: str = "/slack/events"
    bot_token: str = Field(default="", json_schema_extra={"required": True})  # xoxb-...
    app_token: str = Field(default="", json_schema_extra={"required": True})  # xapp-...
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    allow_from: list[str] = Field(
        default_factory=lambda: ["*"]
    )  # Allowed Slack user IDs (sender-level); ['*'] = anyone
    group_policy: str = "mention"  # "mention", "open", "allowlist"
    group_allow_from: list[str] = Field(default_factory=list)  # Allowed channel IDs if allowlist
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)


class QQConfig(ChannelBase):
    """QQ channel configuration using botpy SDK."""

    enabled: bool = False
    app_id: str = Field(default="", json_schema_extra={"required": True})  # bot AppID from q.qq.com
    secret: str = Field(default="", json_schema_extra={"required": True})  # bot AppSecret from q.qq.com
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user openids; ['*'] = public access


class WecomConfig(ChannelBase):
    """WeCom (Enterprise WeChat) AI Bot channel configuration."""

    enabled: bool = False
    bot_id: str = Field(default="", json_schema_extra={"required": True})  # Bot ID from WeCom AI Bot platform
    secret: str = Field(default="", json_schema_extra={"required": True})  # Bot Secret from WeCom AI Bot platform
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user IDs; ['*'] = anyone
    welcome_message: str = ""  # Welcome message for enter_chat event


class WeixinConfig(ChannelBase):
    """Personal WeChat channel configuration."""

    enabled: bool = False
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # ['*'] = anyone
    base_url: str = "https://ilinkai.weixin.qq.com"
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    route_tag: str | int | None = None
    token: str = ""
    state_dir: str = ""
    poll_timeout: int = 35


class ChannelsConfig(Base):
    """Configuration for chat channels."""

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    mochat: MochatConfig = Field(default_factory=MochatConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    wecom: WecomConfig = Field(default_factory=WecomConfig)
    weixin: WeixinConfig = Field(default_factory=WeixinConfig)

    def enabled_channel_names(self) -> set[str]:
        """Names of every enabled IM channel. Field-driven, so a channel
        added to this model is covered without touching consumers (the
        gateway cron partition, cron add's target validation)."""
        return {name for name in type(self).model_fields if getattr(getattr(self, name, None), "enabled", False)}


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.raven/workspace"
    model: str = "anthropic/claude-opus-4-5"
    provider: str = "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
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
    # OpenAI, Codex), gets this rejected at `make_provider` construction time
    # (wired in a later stage; this field exists regardless). Set and non-empty,
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
    request URL's path. It used to be read off ``agents.defaults.model``, which
    made a model id double as a connection parameter: the id could carry no
    prefix without the prefix landing in the path, so Azure was the one provider
    whose ids had to be spelled differently from everyone else's. Declared here,
    the model id is free to be a model id.

    ``api_version`` was hardcoded in the client, so a tenant on a different one
    had no way to say so.
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

    A ``vertex`` flag used to sit here, documented as setting
    ``GOOGLE_GENAI_USE_VERTEXAI``. Nothing read it, and it could not have worked:
    that variable belongs to the google-genai SDK, while requests go through
    LiteLLM, which does not read it and reaches Vertex as a separate provider
    (``vertex_ai``) needing ``VERTEXAI_PROJECT`` and ``VERTEXAI_LOCATION``. It was
    settable from the CLI and covered by tests, so it read as a supported feature
    while doing nothing at all. Reaching Vertex is a change to how a request is
    routed, not a boolean on a key.
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

    @property
    def all_keys(self) -> list[str]:
        """Return all configured API keys."""
        if self.api_key_list:
            return list(self.api_key_list)
        return [self.api_key] if self.api_key else []


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


def _has_credentials(config: "ProviderConfig", spec: Any, name: str = "") -> bool:
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


class GatewayWebConfig(ChannelBase):
    """Web-app channel for the gateway: a local WebSocket JSON-RPC endpoint the
    web backend connects to as a client (ui-webui P1).

    Off by default. When enabled, the gateway hosts a ``web`` channel — its own
    streaming spine (build_web) plus a WS server — alongside the IM channels,
    reusing the TUI RPC wire protocol (token.delta / thinking.delta / tool.* /
    message.complete). Single-user by design: bound to loopback, not exposed
    off-box; ``auth_token`` (if set) is a shared secret the client sends as the
    first line, mirroring the TUI RpcServer's trust gate.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str | None = None


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
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    log: GatewayLogConfig = Field(default_factory=GatewayLogConfig)
    web: GatewayWebConfig = Field(default_factory=GatewayWebConfig)
    page: GatewayPageConfig = Field(default_factory=GatewayPageConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    api_key: str = ""  # Serper API key
    max_results: int = 5


class WebToolsConfig(Base):
    """Web tools configuration."""

    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    jina_api_key: str = ""  # Jina Reader API key
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    timeout: int = 60
    path_append: str = ""
    # Extra regex deny-patterns appended to ExecTool's built-in destructive-command
    # defaults. Empty by default. Operators (or eval harnesses running the agent
    # un-sandboxed) can add host-specific blocks, e.g. osascript / `open -a`.
    extra_deny_patterns: list[str] = Field(default_factory=list)


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
    directly (unchanged behavior) and the meta-tools are omitted.
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
    media: MediaGenConfig = Field(default_factory=MediaGenConfig)
    deep_research: DeepResearchToolConfig = Field(default_factory=DeepResearchToolConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    tool_search: ToolSearchConfig = Field(default_factory=ToolSearchConfig)
    disabled_tools: list[str] = Field(default_factory=list)
    """Tool names to unregister after default-tool registration and MCP connect.
    The general off switch for a tool this deploy does not want, and the only one
    that covers a tool with an unconfigured stand-in variant (``deep_research``),
    where clearing the tool's own config only swaps which variant registers. Also
    used by eval harnesses (e.g. BrowseComp-Plus) to constrain the agent to a
    specific tool subset. Names match those in ``ToolRegistry`` (e.g.
    ``read_file``, ``web_search``, or ``mcp_bcp-search_search``). Read at startup,
    so a change needs a restart.

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

    name: str
    kind: Literal["cli"] = "cli"
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
    command: str
    resume_command: str | None = None
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
    def _resolve_preset(self) -> "ThirdPartyCliSubagentConfig":
        self.preset = _resolve_preset_provenance(self.name, self.preset)
        return self


class ThirdPartyOpenAISubagentConfig(Base):
    """A third-party OpenAI-compatible HTTP agent (mirothinker, …) as a subagent.

    ``timeout`` is ``None`` by default, meaning no automatic limit: the run is
    ended by hand (manual stop), not by a timer. Set it to opt into an
    automatic backstop instead.
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

    @model_validator(mode="after")
    def _allow_replayed_state(self) -> "ThirdPartyOpenAISubagentConfig":
        """``stateful`` is honoured for this kind now.

        It used to be rejected on the grounds that an HTTP agent has no
        resumable session. It has one now, but Raven owns it: the message list
        is replayed from ``raven/agent/subagent/instance_state.py`` rather than
        resumed by the provider, so no ``resumeCommand`` is involved and there
        is nothing for the cli-kind cross-check to verify here.
        """
        return self

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
        incoming payload. The neighbouring ``_allow_replayed_state`` no longer
        rejects ``stateful`` because Raven now replays the message list itself
        for this kind.
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

    name: str
    kind: Literal["acp"] = "acp"
    description: str = ""
    """Operator override for the roster line. Blank means "use what the handshake
    reported" (``agentInfo.name`` plus version), which is the point of ACP: the
    agent describes itself, so a human does not have to."""
    preset: str | None = None
    """Which built-in preset this entry was created from, or ``None`` for a
    hand-written one. Provenance only -- see the cli config for why the web UI
    needs it."""
    enabled: bool = True
    command: str
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
    seed row, because not writing it is what "use the default" means; set
    ``enabled: false`` to take one off the roster.

    ``skills`` and ``tools`` are three-valued on purpose. ``null`` (the default)
    means the full catalogue, an empty list means *no* menu at all, and a list
    narrows to those entries -- the empty case has to be expressible because
    "this agent gets no skills" is a real charter, and folding it into ``null``
    would advertise the opposite of what was written.
    """

    name: str
    kind: Literal["builtin"] = "builtin"
    description: str = ""
    enabled: bool = True
    model: str | None = None
    """Model override for this agent's loop; ``None`` inherits the main loop's."""
    skills: list[str] | None = None
    tools: list[str] | None = None
    restrict_to_workspace: bool | None = None
    """``None`` inherits the manager's own setting rather than forcing one, so a
    row that says nothing about confinement cannot loosen it."""
    timeout: int | None = None
    max_output_chars: int = 30000


AgentConfig = Annotated[
    BuiltinAgentConfig | ThirdPartyCliSubagentConfig | ThirdPartyOpenAISubagentConfig | ThirdPartyAcpSubagentConfig,
    Field(discriminator="kind"),
]

# The pre-``agents`` spelling of the union, kept as an alias because "third
# party" is still the right name for the three external transports where a
# caller genuinely means those (the probe, the acp snapshot store).
ThirdPartySubagentConfig = Annotated[
    ThirdPartyCliSubagentConfig | ThirdPartyOpenAISubagentConfig | ThirdPartyAcpSubagentConfig,
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

    On by default. What that costs is measurable and fixed: the two entry tools
    add about 848 tokens of definition per request (``available_history`` on a
    200k window moves from 130.0k to 129.2k), and nothing else -- no pre-turn
    work, no LLM call, no matching. What it buys is that the builtin library is
    reachable at all; ``load_playbook`` registers only when the library is
    non-empty, and the builtin layer ships two playbooks, so in practice it is
    always offered. Turn it off with ``playbooks.enabled: false``.
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
        from raven.config.loader import raven_home

        raw = self.agents.defaults.workspace
        if raw == AgentDefaults.model_fields["workspace"].default:
            return raven_home() / "workspace"
        return Path(raw).expanduser()

    def channel_workspaces(self) -> dict[str, str]:
        """Every channel that names its own working directory.

        Keyed by the channel name as it appears in a session key
        (``web:<chat_id>``, ``qq:<open_id>``), which is the field name under
        ``channels`` -- plus ``web``, whose config lives under ``gateway``
        because the web channel is hosted by the gateway rather than dialled
        out to. Channels that left ``workspace`` empty are omitted, so the
        resolver falls back to ``<root>/<channel>`` for them.
        """
        found: dict[str, str] = {}
        for name, channel in self.channels:
            configured = getattr(channel, "workspace", "")
            if isinstance(configured, str) and configured.strip():
                found[name] = configured.strip()
        if self.gateway.web.workspace.strip():
            found["web"] = self.gateway.web.workspace.strip()
        return found

    def effective_media_config(self) -> MediaGenConfig:
        """Media config resolved for registration and auth.

        A media tool (image/speech/video) counts as configured only when the
        user set its ``model`` or ``apiKey`` under ``tools.media.<tool>``. For
        each configured tool we default a missing key to
        ``providers.openrouter.apiKey`` so the chat key can be reused without
        re-declaring it. Tools the user did not configure are left untouched
        (no key, no model) — ``AgentLoop`` registers a media tool only when it
        has a key or model, so an OpenRouter key set for chat alone never
        surfaces image/speech/video to the agent. Returns a copy so this
        resolution never mutates the raw config.
        """
        media = self.tools.media.model_copy(deep=True)
        openrouter = self.providers.get("openrouter")
        or_key = openrouter.api_key if openrouter else ""
        for tool in (media.image, media.speech, media.video):
            configured = bool(tool.api_key or tool.model)
            if configured and or_key and not tool.api_key:
                tool.api_key = or_key
        return media

    def _match_provider(self, model: str | None = None) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from raven.providers.registry import (
            PROVIDERS,
            canonical_provider_name,
            find_by_keywords,
            find_by_name,
            split_model_id,
        )

        forced = self.agents.defaults.provider
        if forced != "auto":
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
            if p and _has_credentials(p, spec):
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
            if passthrough and _has_credentials(passthrough, None, prefix):
                return passthrough, canonical_provider_name(prefix)

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = self.providers.get(spec.name)
            if p and _has_credentials(p, spec):
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
            if p and _has_credentials(p, spec):
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
        """Returns the default SkillForgeConfig. Extension blocks are
        loaded via ``load_raven_config``, not through the base
        Config. This property exists for backward compat with code that
        accesses ``config.skill_forge`` on a plain ``Config`` instance.
        """
        from raven.config.raven import SkillForgeConfig

        return SkillForgeConfig()

    model_config = ConfigDict(
        env_prefix="NANOBOT_",
        env_nested_delimiter="__",
        extra="forbid",
    )
