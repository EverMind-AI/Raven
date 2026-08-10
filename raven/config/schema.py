"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from raven.sandbox.config import SandboxConfig


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class WhatsAppConfig(Base):
    """WhatsApp channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""  # Shared token for bridge auth (auto-generated when empty)
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed phone numbers; ['*'] = anyone
    group_policy: Literal["open", "mention"] = "open"  # "open" responds to all, "mention" only when @mentioned


class TelegramConfig(Base):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = Field(default="", json_schema_extra={"required": True})  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user IDs or usernames; ['*'] = anyone
    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    reply_to_message: bool = False  # If true, bot replies quote the original message
    group_policy: Literal["open", "mention"] = (
        "mention"  # "mention" responds when @mentioned or replied to, "open" responds to all
    )


class FeishuConfig(Base):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    enabled: bool = False
    app_id: str = Field(default="", json_schema_extra={"required": True})  # App ID from Feishu Open Platform
    app_secret: str = Field(default="", json_schema_extra={"required": True})  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription
    verification_token: str = ""  # Verification Token for event subscription
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user open_ids; ['*'] = anyone
    react_emoji: str = "THUMBSUP"  # Emoji type for message reactions (e.g. THUMBSUP, OK, DONE, SMILE)
    group_policy: Literal["open", "mention"] = "mention"  # "mention" responds when @mentioned, "open" responds to all


class DingTalkConfig(Base):
    """DingTalk channel configuration using Stream mode."""

    enabled: bool = False
    client_id: str = Field(default="", json_schema_extra={"required": True})  # AppKey
    client_secret: str = Field(default="", json_schema_extra={"required": True})  # AppSecret
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed staff_ids; ['*'] = anyone


class DiscordConfig(Base):
    """Discord channel configuration."""

    enabled: bool = False
    token: str = Field(default="", json_schema_extra={"required": True})  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user IDs; ['*'] = anyone
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT
    group_policy: Literal["mention", "open"] = "mention"


class MatrixConfig(Base):
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


class EmailConfig(Base):
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


class MochatConfig(Base):
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


class SlackConfig(Base):
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


class QQConfig(Base):
    """QQ channel configuration using botpy SDK."""

    enabled: bool = False
    app_id: str = Field(default="", json_schema_extra={"required": True})  # bot AppID from q.qq.com
    secret: str = Field(default="", json_schema_extra={"required": True})  # bot AppSecret from q.qq.com
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user openids; ['*'] = public access


class WecomConfig(Base):
    """WeCom (Enterprise WeChat) AI Bot channel configuration."""

    enabled: bool = False
    bot_id: str = Field(default="", json_schema_extra={"required": True})  # Bot ID from WeCom AI Bot platform
    secret: str = Field(default="", json_schema_extra={"required": True})  # Bot Secret from WeCom AI Bot platform
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user IDs; ['*'] = anyone
    welcome_message: str = ""  # Welcome message for enter_chat event


class WeixinConfig(Base):
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


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.raven/workspace"
    model: str = "anthropic/claude-opus-4-5"
    provider: str = "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    max_tokens: int = 8192
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
    # Cap on subagent VMs running at once (excess spawns queue). ge=1: a
    # 0/negative cap would deadlock every subagent (Semaphore(0)).
    max_concurrent_subagents: int = Field(default=4, ge=1)
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

    Only consulted at cron job TRIGGER time, never at creation. Ephemeral
    channels (cli / tui — anything not in ChannelManager.enabled_channels)
    cannot deliver to themselves after the host process exits, so the
    forward_channels list resolves which real channels receive the reminder.
    """

    forward_channels: list[str] = Field(default_factory=lambda: ["*"])
    """Channels to deliver ephemeral-origin reminders to. ``["*"]`` broadcasts
    to every enabled channel. Specific names (``["telegram", "feishu"]``)
    restrict to those. Non-ephemeral channels (telegram / feishu / weixin
    etc.) ignore this list — they always pass-through to the per-job channel."""

    default_timezone: str = "Asia/Shanghai"
    """Default IANA timezone for cron expressions without explicit ``--tz``."""


class ModelOverlay(Base):
    """A name for a model no catalogue carries.

    A self-hosted deployment serves whatever was put there, and a model released
    since the bundled snapshot is in no table yet, so the picker falls back to
    showing the id. That is usually fine -- the id is the name the user gave
    their own deployment -- but it leaves no way to label several of them.

    Only what a person states about presentation. Token accounting is not in
    scope here -- `agents.defaults.contextWindowTokens` / `maxTokens` already
    hold it. What has no knob at all is a *price* for an endpoint no catalogue
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
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)
    models: list[str] = Field(default_factory=list)  # User-curated model names for the picker
    # Several full url/key/header groups under one provider section, for a
    # vendor reachable by more than one account or region. Meaningful only for
    # a plain API-key provider reached through the litellm client -- a section
    # whose auth is OAuth, or that needs more than a key and an address (Azure
    # OpenAI, Codex), gets this rejected at `make_provider` construction time
    # (wired in a later stage; this field exists regardless). Set and non-empty,
    # it replaces the flat `api_key` outright rather than merging with it; an
    # entry naming neither its own `api_base` nor `extra_headers` inherits the
    # flat ones -- see `raven.providers.endpoints.provider_endpoints` for the
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


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    user_pool: int = 4
    system_pool: int = 2
    send_max_retries: int = 3
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    log: GatewayLogConfig = Field(default_factory=GatewayLogConfig)


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


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled


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
    Used by eval harnesses (e.g. BrowseComp-Plus) that need to constrain the
    agent to a specific tool subset. Names match those in ``ToolRegistry``
    (e.g. ``read_file``, ``web_search``, or ``mcp_bcp-search_search``)."""


class Config(BaseSettings):
    """Root configuration for raven."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    cron: CronConfig = Field(default_factory=CronConfig)
    # UI language chosen during onboarding. Drives the wizard/CLI copy and the
    # agent's reply language (injected into the system prompt). "en" | "zh".
    language: Literal["en", "zh"] = "en"

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

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
        # Only gateways get a default api_base here. Standard providers
        # (like Moonshot) set their base URL via env vars in _setup_env.
        if name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                return spec.default_api_base
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
