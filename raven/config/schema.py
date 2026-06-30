"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
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
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user IDs or usernames; ['*'] = anyone
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    reply_to_message: bool = False  # If true, bot replies quote the original message
    group_policy: Literal["open", "mention"] = "mention"  # "mention" responds when @mentioned or replied to, "open" responds to all


class FeishuConfig(Base):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    enabled: bool = False
    app_id: str = ""  # App ID from Feishu Open Platform
    app_secret: str = ""  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription (optional)
    verification_token: str = ""  # Verification Token for event subscription (optional)
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user open_ids; ['*'] = anyone
    react_emoji: str = (
        "THUMBSUP"  # Emoji type for message reactions (e.g. THUMBSUP, OK, DONE, SMILE)
    )
    group_policy: Literal["open", "mention"] = "mention"  # "mention" responds when @mentioned, "open" responds to all


class DingTalkConfig(Base):
    """DingTalk channel configuration using Stream mode."""

    enabled: bool = False
    client_id: str = ""  # AppKey
    client_secret: str = ""  # AppSecret
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed staff_ids; ['*'] = anyone


class DiscordConfig(Base):
    """Discord channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed user IDs; ['*'] = anyone
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT
    group_policy: Literal["mention", "open"] = "mention"


class MatrixConfig(Base):
    """Matrix (Element) channel configuration."""

    enabled: bool = False
    homeserver: str = "https://matrix.org"
    access_token: str = ""
    user_id: str = ""  # @bot:matrix.org
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
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_use_ssl: bool = True

    # SMTP (send)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_address: str = ""

    # Behavior
    auto_reply_enabled: bool = (
        True  # If false, inbound email is read but no automatic reply is sent
    )
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
    claw_token: str = ""
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
    bot_token: str = ""  # xoxb-...
    app_token: str = ""  # xapp-...
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Allowed Slack user IDs (sender-level); ['*'] = anyone
    group_policy: str = "mention"  # "mention", "open", "allowlist"
    group_allow_from: list[str] = Field(default_factory=list)  # Allowed channel IDs if allowlist
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)


class QQConfig(Base):
    """QQ channel configuration using botpy SDK."""

    enabled: bool = False
    app_id: str = ""  # bot AppID from q.qq.com
    secret: str = ""  # bot AppSecret from q.qq.com
    allow_from: list[str] = Field(
        default_factory=lambda: ["*"]
    )  # Allowed user openids; ['*'] = public access


class WecomConfig(Base):
    """WeCom (Enterprise WeChat) AI Bot channel configuration."""

    enabled: bool = False
    bot_id: str = ""  # Bot ID from WeCom AI Bot platform
    secret: str = ""  # Bot Secret from WeCom AI Bot platform
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
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.1
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
    enable_personalization: bool = (
        False  # 4-step PAHF-inspired personalization flow (classify → ask → execute → learn)
    )
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


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)
    models: list[str] = Field(default_factory=list)  # User-curated model names for the picker


class GeminiProviderConfig(ProviderConfig):
    """Gemini provider configuration with Vertex AI and multi-key support.

    Example YAML:
        gemini:
          vertex: true
          api_key_list:
            - "key1"
            - "key2"
    """

    vertex: bool = False  # When true, sets GOOGLE_GENAI_USE_VERTEXAI=True for Vertex AI
    api_key_list: list[str] = Field(default_factory=list)  # Multiple API keys for rotation

    def next_api_key(self) -> str:
        """Return the next API key using round-robin rotation.

        Falls back to single api_key if api_key_list is empty.
        """
        import itertools

        if not hasattr(self, "_key_cycle"):
            keys = self.api_key_list if self.api_key_list else ([self.api_key] if self.api_key else [])
            object.__setattr__(self, "_key_cycle", itertools.cycle(keys) if keys else None)
        cycle = getattr(self, "_key_cycle", None)
        if cycle is None:
            return self.api_key or ""
        return next(cycle)

    @property
    def effective_api_key(self) -> str:
        """Get the current effective API key (first from list, or single key)."""
        if self.api_key_list:
            return self.api_key_list[0]
        return self.api_key

    @property
    def all_keys(self) -> list[str]:
        """Return all configured API keys."""
        if self.api_key_list:
            return list(self.api_key_list)
        return [self.api_key] if self.api_key else []


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # Alibaba Cloud Tongyi Qianwen
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: GeminiProviderConfig = Field(default_factory=GeminiProviderConfig)  # Google Gemini / Vertex AI
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig)  # Github Copilot (OAuth)


class RoutingConfig(Base):
    """EcoClaw-style model routing configuration."""

    enabled: bool = False
    profile: str = "balanced"  # best / balanced / eco
    # OpenRouter API key for embeddings (defaults to providers.openrouter.api_key)
    api_key: str = ""


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

    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    jina_api_key: str = ""  # Jina Reader API key
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    timeout: int = 60
    path_append: str = ""


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


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled


class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    media: MediaGenConfig = Field(default_factory=MediaGenConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
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
        or_key = self.providers.openrouter.api_key
        for tool in (media.image, media.speech, media.video):
            configured = bool(tool.api_key or tool.model)
            if configured and or_key and not tool.api_key:
                tool.api_key = or_key
        return media

    def _match_provider(
        self, model: str | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from raven.providers.registry import PROVIDERS

        forced = self.agents.defaults.provider
        if forced != "auto":
            p = getattr(self.providers, forced, None)
            return (p, forced) if p else (None, None)

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_base:
                return p, spec.name

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
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
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for gateway/local providers."""
        from raven.providers.registry import find_by_name

        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        # Only gateways get a default api_base here. Standard providers
        # (like Moonshot) set their base URL via env vars in _setup_env
        # to avoid polluting the global litellm.api_base.
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
