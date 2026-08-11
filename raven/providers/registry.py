"""
Provider Registry — what Raven knows about a vendor that LiteLLM does not.

Any vendor LiteLLM supports already works without an entry here: a key under
its name in ``providers`` plus a "<vendor>/<model>" model id is enough -- the
key reaches LiteLLM as a per-call argument (see ``ProvidersConfig.get``).

Add a ProviderSpec only for what LiteLLM cannot tell us:
  - keywords, so a bare model name ("kimi-k2.5") finds its vendor;
  - default_model / display_name for the wizard and the pickers;
  - a gateway that fronts other vendors (OpenRouter, AiHubMix), an OAuth flow,
    a non-LiteLLM path (Azure), prompt caching, per-model param defaults;
  - a second env var to mirror, or another vendor's driver we are reached through.
A provider's ``name`` IS LiteLLM's spelling for it wherever LiteLLM has one, so
there is one name per provider and nothing to reconcile. A vendor LiteLLM spells
differently is renamed here, with the old spelling kept in ``name_aliases`` so
saved configs keep loading; only a borrowed driver states ``via_driver``.

Order matters — it controls match priority and fallback. Gateways first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderSpec:
    """One LLM provider's metadata. See PROVIDERS below for real examples.

    Placeholders in env_extras values:
      {api_key}  — the user's API key
      {api_base} — api_base from config, or this spec's default_api_base
    """

    # identity
    name: str  # config field name, e.g. "dashscope"
    keywords: tuple[str, ...]  # model-name keywords for matching (lowercase)
    # LiteLLM env var, e.g. "DASHSCOPE_API_KEY". Also shown in the model picker
    # as the variable to export. Empty for OAuth and direct providers.
    env_key: str = ""
    display_name: str = ""  # shown in `raven status`

    # model prefixing
    # This provider is reached through ANOTHER vendor's LiteLLM driver: SiliconFlow
    # speaks OpenAI's API, MiniMax speaks Anthropic's. It names the driver, never
    # this provider -- so it is deliberately absent from `route_names`, and
    # "openai/gpt-4o" cannot be answered by SiliconFlow's key.
    #
    # A vendor LiteLLM merely spells differently is NOT this: adopt LiteLLM's
    # spelling as `name` and keep ours in `name_aliases` (see hosted_vllm).
    via_driver: str = ""
    # Prefix LiteLLM's metadata table files this provider's models under, when it
    # differs from the routing prefix ("minimax-global/MiniMax-M3" is priced at
    # "minimax/MiniMax-M3"). None: the two coincide.
    metadata_prefix: str | None = None
    # What the user is billed on. "plan" is a subscription (ChatGPT, a Copilot
    # seat): no per-token figure describes a call. Declared rather than inferred
    # from ``is_oauth`` -- Vertex is OAuth and metered.
    billing: str = "per_token"
    skip_prefixes: tuple[str, ...] = ()  # don't prefix if model already starts with these
    # Former names this provider answered to, so model ids saved under the old
    # one ("zhipu/glm-4.6") still resolve after a rename.
    name_aliases: tuple[str, ...] = ()

    # extra env vars, e.g. (("ZHIPUAI_API_KEY", "{api_key}"),)
    env_extras: tuple[tuple[str, str], ...] = ()

    # gateway / local detection
    is_gateway: bool = False  # routes any model (OpenRouter, AiHubMix)
    is_local: bool = False  # local deployment (vLLM, Ollama)
    detect_by_key_prefix: str = ""  # match api_key prefix, e.g. "sk-or-"
    detect_by_base_keyword: str = ""  # match substring in api_base URL
    default_api_base: str = ""  # fallback base URL

    # gateway behavior
    strip_model_prefix: bool = False  # strip "provider/" before re-prefixing

    # per-model param overrides, e.g. (("kimi-k2.5", {"temperature": 1.0}),)
    model_overrides: tuple[tuple[str, dict[str, Any]], ...] = ()

    # OAuth-based providers (e.g., OpenAI Codex) don't use API keys
    is_oauth: bool = False  # if True, uses OAuth flow instead of API key

    # Provider supports cache_control on content blocks (e.g. Anthropic prompt caching)
    supports_prompt_caching: bool = False

    # Whether a tool result may carry an image block. None = decide by probing
    # the resolved LiteLLM target (see supports_image_tool_result); set it only
    # to override that, e.g. a gateway whose backend is known to be Anthropic.
    #
    # This is an API-shape property, not a model property: a vision model still
    # cannot see an image delivered in a tool result if the wire format has
    # nowhere to put one. OpenAI's Chat Completions types `role:"tool"` content
    # as `string | ChatCompletionContentPartText[]` — image is excluded at the
    # schema level, so the fallback is to hand the model a text placeholder and
    # attach the picture to a following user message.
    image_tool_result_override: bool | None = None

    # Whether the models behind this provider can see images at all. None = ask
    # the gateway catalogue per model (see supports_vision), which answers "yes"
    # whenever it has no entry -- so a provider it does not carry needs nothing
    # set here. Set it to False for a provider whose models the catalogue lists
    # as vision-capable but this route does not serve that way, and to True to
    # overrule a listing that is wrong in the other direction. Source-level, like
    # image_tool_result_override: there is no config surface for either.
    vision_override: bool | None = None

    # Onboard wizard fallback for agents.defaults.model when /v1/models is empty
    default_model: str = ""

    # Some providers are served by a dedicated client rather than LiteLLM (Azure
    # needs an api-version and a deployment name; Codex speaks the Responses API
    # over OAuth). They take no LiteLLM route prefix.
    bypasses_litellm: bool = False

    # Which client constructs the provider, when it is not LiteLLM's. Stated here
    # so adding a family does not mean editing the factory's dispatch.
    client: str = ""

    # The endpoint is the user's to supply and there is no default that works:
    # Azure gives every tenant its own resource URL, a self-hosted endpoint is
    # wherever the user put it. Distinct from `default_api_base`, which is a
    # working address the user may override, and from `is_local`, which needs an
    # address but no key.
    requires_api_base: bool = False

    @property
    def label(self) -> str:
        return self.display_name or self.name.title()

    @property
    def usable_default_api_base(self) -> str:
        """The shipped default address ``Config.get_api_base`` would actually serve.

        Non-empty only for a gateway or local deployment -- a direct vendor's
        ``default_api_base`` travels via env vars instead and the reader never
        hands it out. Stated once so the credential gate cannot accept a
        default the reader then refuses to serve (see ``providers.auth``).
        """
        return self.default_api_base if (self.is_gateway or self.is_local) else ""

    @property
    def model_prefix(self) -> str:
        """Route prefix LiteLLM needs on this provider's model ids.

        LiteLLM routes on "<vendor>/<model>", and `name` IS LiteLLM's spelling
        wherever LiteLLM has one -- that is the point of adopting it. Only a
        provider reached through someone else's driver sends a different prefix.
        """
        if self.bypasses_litellm:
            return ""
        return self.via_driver or self.name

    @property
    def route_names(self) -> frozenset[str]:
        """Every normalized model-id prefix that names THIS provider.

        `name` is LiteLLM's own spelling wherever LiteLLM has one, so there is
        nothing to reconcile here -- just this provider's current name plus the
        ones it used to answer to. A borrowed driver (`via_driver`) is absent by
        construction: it names the vendor whose API is spoken, not this one.

        Callers must compare prefixes against this rather than rebuilding the
        set, so the rule cannot drift apart across call sites again.
        """
        return frozenset(normalize_provider_name(n) for n in (self.name, *self.name_aliases))

    def matches_keywords(self, model: str) -> bool:
        """Does this model id mention this vendor by name, prefix aside?

        Keyword matching is what a bare id ("kimi-k2.5") has to go on. It says
        nothing about routing on its own -- see `claims`.
        """
        model_lower = model.lower()
        model_normalized = model_lower.replace("-", "_")
        return any(
            kw.lower() in model_lower or kw.lower().replace("-", "_") in model_normalized for kw in self.keywords
        )

    def claims(self, model: str) -> bool:
        """Would `model` route to this provider under ``provider: auto``?

        The one place the prefix-beats-keyword rule is written down. A prefix
        names its owner and only its owner answers; a keyword is all a bare id
        offers. Asking a spec directly, rather than each caller re-deriving the
        rule from a prefix and a keyword list, is what keeps the credential
        path and the wizard's guard from disagreeing about the same model id.
        """
        prefix, _ = split_model_id(model)
        if prefix:
            return prefix in self.route_names
        return self.matches_keywords(model)


# ---------------------------------------------------------------------------
# PROVIDERS — the registry. Order = priority. Copy any entry as template.
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    # === Custom (any OpenAI-compatible endpoint, via LiteLLM) ==============
    ProviderSpec(
        name="custom",
        keywords=(),
        env_key="",
        display_name="Custom",
        # Route through LiteLLM as a generic OpenAI-compatible gateway: the
        # `openai/` prefix + configured api_base reach any OpenAI-compatible
        # endpoint, with LiteLLM providing streaming / retry / tool-calling.
        # Matched only via an explicit `provider: custom` selection (keywords
        # empty).
        via_driver="openai",
        is_gateway=True,
        requires_api_base=True,
        default_api_base="http://localhost:8000/v1",
    ),
    # === Azure OpenAI ======================================================
    # Served by AzureOpenAIProvider, not LiteLLM (hence ``client`` below): Azure
    # needs an api-version and takes a deployment name where every other provider
    # takes a model id.
    ProviderSpec(
        name="azure_openai",
        client="azure",
        keywords=("azure", "azure-openai"),
        env_key="",
        display_name="Azure OpenAI",
        bypasses_litellm=True,
        requires_api_base=True,
    ),
    # === Gateways (detected by api_key / api_base, not model name) =========
    # Gateways can route any model, so they win in fallback.
    # OpenRouter: global gateway, keys start with "sk-or-"
    ProviderSpec(
        name="openrouter",
        keywords=("openrouter",),
        env_key="OPENROUTER_API_KEY",
        display_name="OpenRouter",
        skip_prefixes=(),
        env_extras=(),
        is_gateway=True,
        is_local=False,
        detect_by_key_prefix="sk-or-",
        detect_by_base_keyword="openrouter",
        default_api_base="https://openrouter.ai/api/v1",
        strip_model_prefix=False,
        model_overrides=(),
        supports_prompt_caching=True,
        default_model="openrouter/anthropic/claude-sonnet-4-5",
    ),
    # AiHubMix: global gateway, OpenAI-compatible interface.
    # strip_model_prefix=True: it doesn't understand "anthropic/claude-3",
    # so we strip to bare "claude-3" then re-prefix as "openai/claude-3".
    ProviderSpec(
        name="aihubmix",
        keywords=("aihubmix",),
        env_key="OPENAI_API_KEY",  # OpenAI-compatible
        display_name="AiHubMix",
        via_driver="openai",  # → openai/{model}
        skip_prefixes=(),
        env_extras=(),
        is_gateway=True,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="aihubmix",
        default_api_base="https://aihubmix.com/v1",
        strip_model_prefix=True,  # anthropic/claude-3 → claude-3 → openai/claude-3
        model_overrides=(),
    ),
    # SiliconFlow: OpenAI-compatible gateway, model names keep org prefix
    ProviderSpec(
        name="siliconflow",
        keywords=("siliconflow",),
        env_key="OPENAI_API_KEY",
        display_name="SiliconFlow",
        via_driver="openai",
        skip_prefixes=(),
        env_extras=(),
        is_gateway=True,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="siliconflow",
        default_api_base="https://api.siliconflow.cn/v1",
        strip_model_prefix=False,
        model_overrides=(),
    ),
    # VolcEngine: OpenAI-compatible gateway
    ProviderSpec(
        name="volcengine",
        keywords=("volcengine", "volces", "ark"),
        env_key="OPENAI_API_KEY",
        display_name="VolcEngine",
        skip_prefixes=(),
        env_extras=(),
        is_gateway=True,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="volces",
        default_api_base="https://ark.cn-beijing.volces.com/api/v3",
        strip_model_prefix=False,
        model_overrides=(),
    ),
    # === Standard providers (matched by model-name keywords) ===============
    # Anthropic. Model ids go out as "anthropic/claude-*": LiteLLM resolves that
    # and a bare "claude-*" to the same provider and wire model, and an explicit
    # prefix is what keeps another vendor's key from answering for the id.
    ProviderSpec(
        name="anthropic",
        keywords=("anthropic", "claude"),
        env_key="ANTHROPIC_API_KEY",
        display_name="Anthropic",
        skip_prefixes=(),
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",
        strip_model_prefix=False,
        model_overrides=(),
        supports_prompt_caching=True,
        default_model="anthropic/claude-sonnet-5",
    ),
    # OpenAI: LiteLLM recognizes "gpt-*" natively, no prefix needed.
    ProviderSpec(
        name="openai",
        keywords=("openai", "gpt"),
        env_key="OPENAI_API_KEY",
        display_name="OpenAI",
        skip_prefixes=(),
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        strip_model_prefix=False,
        model_overrides=(),
        default_model="openai/gpt-5.5",
    ),
    # OpenAI Codex: uses OAuth, not API key.
    ProviderSpec(
        name="openai_codex",
        client="codex",
        keywords=("openai-codex",),
        env_key="",  # OAuth-based, no API key
        display_name="OpenAI Codex",
        bypasses_litellm=True,
        skip_prefixes=(),
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="codex",
        default_api_base="https://chatgpt.com/backend-api",
        metadata_prefix="chatgpt",
        billing="plan",
        strip_model_prefix=False,
        model_overrides=(),
        is_oauth=True,  # OAuth-based authentication
        # No static default: every id we shipped here came back "not supported
        # when using Codex with a ChatGPT account", and the slugs an account does
        # offer are only knowable by asking it (see ``codex_catalog``). Empty
        # makes the wizard ask for one rather than write a rejected id.
    ),
    # Github Copilot: uses OAuth, not API key.
    ProviderSpec(
        name="github_copilot",
        keywords=("github_copilot", "copilot"),
        env_key="",  # OAuth-based, no API key
        display_name="Github Copilot",
        billing="plan",
        skip_prefixes=("github_copilot/",),
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",
        strip_model_prefix=False,
        model_overrides=(),
        is_oauth=True,  # OAuth-based authentication
        default_model="github_copilot/gpt-4o",
    ),
    # DeepSeek: needs "deepseek/" prefix for LiteLLM routing.
    ProviderSpec(
        name="deepseek",
        keywords=("deepseek",),
        env_key="DEEPSEEK_API_KEY",
        display_name="DeepSeek",
        skip_prefixes=("deepseek/",),  # avoid double-prefix
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        strip_model_prefix=False,
        model_overrides=(),
        default_model="deepseek/deepseek-v4-flash",
    ),
    # Gemini: needs "gemini/" prefix for LiteLLM.
    ProviderSpec(
        name="gemini",
        keywords=("gemini",),
        env_key="GEMINI_API_KEY",
        display_name="Gemini",
        skip_prefixes=("gemini/",),  # avoid double-prefix
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",
        strip_model_prefix=False,
        model_overrides=(),
        default_model="gemini/gemini-2.5-flash",
    ),
    # Z.ai (formerly Zhipu AI): named after the vendor's current brand, which is
    # also what LiteLLM calls it. Old configs saying "zhipu" still load.
    # Also mirrors key to ZHIPUAI_API_KEY (some LiteLLM paths check that).
    # skip_prefixes: don't add "zai/" when already routed via gateway.
    ProviderSpec(
        name="zai",
        keywords=("zhipu", "glm", "zai"),
        name_aliases=("zhipu",),  # model ids written before the rename
        env_key="ZAI_API_KEY",
        display_name="Z.ai",
        skip_prefixes=("zhipu/", "zai/", "openrouter/", "hosted_vllm/"),
        env_extras=(("ZHIPUAI_API_KEY", "{api_key}"),),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        strip_model_prefix=False,
        model_overrides=(),
        default_model="zai/glm-4.6",
    ),
    # DashScope: Qwen models, needs "dashscope/" prefix.
    ProviderSpec(
        name="dashscope",
        keywords=("qwen", "dashscope"),
        env_key="DASHSCOPE_API_KEY",
        display_name="DashScope",
        skip_prefixes=("dashscope/", "openrouter/"),
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        strip_model_prefix=False,
        model_overrides=(),
        default_model="dashscope/qwen-plus",
    ),
    # Moonshot: Kimi models. Kimi K2.5 enforces temperature >= 1.0.
    ProviderSpec(
        name="moonshot",
        keywords=("moonshot", "kimi"),
        env_key="MOONSHOT_API_KEY",
        display_name="Moonshot",
        skip_prefixes=("moonshot/", "openrouter/"),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        strip_model_prefix=False,
        model_overrides=(("kimi-k2.5", {"temperature": 1.0}),),
        # Needed by `provider test` and the wizard preflight, which probe
        # /v1/models before any LiteLLM call resolves an endpoint.
        # intl; api.moonshot.cn in China. LiteLLM defaults to the same host, but
        # `provider test` and the wizard probe /v1/models before any LiteLLM call.
        default_api_base="https://api.moonshot.ai/v1",
    ),
    # MiniMax: needs "minimax/" prefix for LiteLLM routing.
    # Uses OpenAI-compatible API at api.minimax.io/v1.
    ProviderSpec(
        name="minimax",
        keywords=("minimax",),
        env_key="MINIMAX_API_KEY",
        display_name="MiniMax",
        skip_prefixes=("minimax/", "openrouter/"),
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        strip_model_prefix=False,
        model_overrides=(),
        # Needed by `provider test` and the wizard preflight, which probe
        # /v1/models before any LiteLLM call resolves an endpoint.
        default_api_base="https://api.minimax.io/v1",
        default_model="minimax/MiniMax-M3",
    ),
    ProviderSpec(
        name="minimax_global",
        client="minimax_oauth",
        keywords=("minimax-global",),
        env_key="",
        display_name="MiniMax Global (OAuth)",
        via_driver="anthropic",
        skip_prefixes=("anthropic/",),
        default_api_base="https://api.minimax.io/anthropic/v1",
        metadata_prefix="minimax",
        billing="plan",
        is_oauth=True,
        default_model="minimax-global/MiniMax-M3",
    ),
    ProviderSpec(
        name="minimax_cn",
        client="minimax_oauth",
        keywords=("minimax-cn",),
        env_key="",
        display_name="MiniMax CN (OAuth)",
        via_driver="anthropic",
        skip_prefixes=("anthropic/",),
        default_api_base="https://api.minimaxi.com/anthropic/v1",
        metadata_prefix="minimax",
        billing="plan",
        is_oauth=True,
        default_model="minimax-cn/MiniMax-M3",
    ),
    # === Local deployment (matched by config key, NOT by api_base) =========
    # vLLM / any OpenAI-compatible local server.
    # Detected when config key is "vllm" (provider_name="vllm").
    ProviderSpec(
        name="hosted_vllm",
        keywords=("vllm",),
        env_key="HOSTED_VLLM_API_KEY",
        display_name="vLLM/Local",
        name_aliases=("vllm",),
        skip_prefixes=(),
        env_extras=(),
        is_gateway=False,
        is_local=True,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        default_api_base="",  # user must provide in config
        strip_model_prefix=False,
        model_overrides=(),
    ),
    # === Ollama (local, OpenAI-compatible) ===================================
    ProviderSpec(
        name="ollama_chat",
        keywords=("ollama", "nemotron"),
        env_key="OLLAMA_API_KEY",
        display_name="Ollama",
        name_aliases=("ollama",),
        skip_prefixes=("ollama/", "ollama_chat/"),
        env_extras=(),
        is_gateway=False,
        is_local=True,
        detect_by_key_prefix="",
        detect_by_base_keyword="11434",
        default_api_base="http://localhost:11434",
        strip_model_prefix=False,
        model_overrides=(),
    ),
    # === Auxiliary (not a primary LLM provider) ============================
    # Groq: mainly used for Whisper voice transcription, also usable for LLM.
    # Needs "groq/" prefix for LiteLLM routing. Placed last — it rarely wins fallback.
    ProviderSpec(
        name="groq",
        keywords=("groq",),
        env_key="GROQ_API_KEY",
        display_name="Groq",
        skip_prefixes=("groq/",),  # avoid double-prefix
        env_extras=(),
        is_gateway=False,
        is_local=False,
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        strip_model_prefix=False,
        model_overrides=(),
        default_model="groq/openai/gpt-oss-120b",
    ),
)


# ---------------------------------------------------------------------------
# Name and model-id primitives
#
# Every comparison of a provider name or a model-id prefix in this codebase goes
# through these. They exist because the same rule used to be spelled out at each
# call site, and the spellings drifted -- one site lowercased, another did not;
# one counted LiteLLM's name for a vendor as that vendor's prefix, another did
# not. A fix then landed at one site and missed the rest.
# ---------------------------------------------------------------------------


def normalize_provider_name(name: str | None) -> str:
    """The single spelling every provider-name comparison is made in.

    Callers hand this whatever a config or a command line gave them, absent
    values included, so an empty name normalizes rather than raising.

    LiteLLM hyphenates some vendors ("nano-gpt") where this project's config
    fields are snake_case, and either may arrive in any case, so neither form
    may be compared raw. camelCase keys are not decomposed here -- splitting on
    capitals cannot tell "azureOpenai" (two words) from "OpenRouter" (one), so
    that comparison is made in the safe direction, by camelCasing the snake name
    (see `ProvidersConfig.get`).
    """
    return (name or "").strip().lower().replace("-", "_")


CRED_OAUTH = "oauth"  # a token file, written by `raven provider login`
CRED_LOCAL = "local"  # reached by address; there is no key
CRED_ENDPOINT = "endpoint"  # a key plus a base URL the user supplies
CRED_KEY = "key"  # a key alone, including vendors Raven carry no spec for


def credential_kind(provider: str | None) -> str:
    """Which of the four credential shapes this provider uses.

    Every decision about how a provider is set up follows from this: whether to
    ask for a key, an address, both, or neither. It lives here because it is a
    fact about the provider, and because the two places that need it -- the
    wizard and the model picker -- had answered it separately, with the picker
    knowing only two shapes: it offered a local deployment a key prompt it cannot
    use and no address field, which is the one thing it needs.

    Derived rather than stored: a spec is optional metadata, and a vendor Raven
    holds no spec for is reached with a key like most others.
    """
    spec = find_by_name(provider) if provider else None
    if spec is None:
        return CRED_KEY
    if spec.is_oauth:
        return CRED_OAUTH
    if spec.is_local:
        return CRED_LOCAL
    if spec.requires_api_base:
        return CRED_ENDPOINT
    return CRED_KEY


def endpoints_unsupported_reason(provider_name: str | None) -> str | None:
    """Why ``provider_name``'s config cannot carry an ``endpoints`` list, or None
    if it can.

    Shared by every path that could write one -- `make_provider` at build time,
    `add_provider_endpoint`, and the TUI `/model` picker's ``model.add_endpoint``
    -- so a section rejected at build time is rejected at write time too,
    instead of being accepted by the write paths and only failing later when
    something tries to build a provider from it. Codex, MiniMax OAuth and Azure
    (``client`` set) each connect through one dedicated client and one account,
    not several; an OAuth section reached through litellm instead (``is_oauth``,
    e.g. github_copilot, which has no dedicated client) is the same shape.
    ``endpoints`` is meaningful only for a plain API-key vendor reached through
    litellm.
    """
    spec = find_by_name(provider_name) if provider_name else None
    if spec is None or not (spec.client or spec.is_oauth):
        return None
    return (
        f"{provider_name} does not support multiple endpoints -- remove the `endpoints` "
        "field from its config; this provider connects through a single account, not several"
    )


def litellm_spelling(name: str | None) -> str:
    """How LiteLLM spells this vendor, which is the only form usable as a prefix.

    Names are matched spelling-insensitively everywhere else, so the one arriving
    here may be underscored where LiteLLM hyphenates -- and LiteLLM rejects the
    underscored form outright ("nano_gpt/..." comes back as "LLM Provider NOT
    provided"), which turns a provider that configured cleanly into one that
    cannot be called.
    """
    from raven.providers.litellm_provider_names import LITELLM_PROVIDER_NAMES

    wanted = normalize_provider_name(name)
    for candidate in LITELLM_PROVIDER_NAMES:
        if normalize_provider_name(candidate) == wanted:
            return candidate
    return wanted


def public_model_prefix(spec: "ProviderSpec") -> str:
    """The prefix a user writes to reach THIS provider, which is not always the
    one that goes on the wire.

    ``model_prefix`` answers "what does LiteLLM route on", and for a provider
    LiteLLM does not carry it is empty by design. But a model id still has to say
    who serves it: written bare, it is claimed by keyword and fallback instead --
    "gpt-5.6-sol" resolves to OpenAI, so a Codex model configured that way is sent
    somewhere it does not exist. A provider reached through another vendor's
    driver has the same split: the wire prefix names that vendor.

    Both cases resolve back here through ``route_names``, which is built from this
    same spelling.
    """
    return spec.name.replace("_", "-")


def names_same_provider(key: str, name: str) -> bool:
    """Do these two strings name the same provider?

    One provider is written three ways and all three must resolve to it: the
    snake_case field name, LiteLLM's hyphenated spelling, and the camelCase this
    project's models serialize to. The camelCase comparison is made forwards, by
    camelCasing the snake name -- splitting a key on capitals cannot tell
    "azureOpenai" (two words) from "OpenRouter" (one).

    Every place that matches a config key against a provider name uses this, so
    the read path and the write path cannot answer it differently: they did, and
    a section stored under one spelling was invisible to `provider get/set` while
    the runtime read it happily -- then a write added a second section and the
    real credential became unreachable.
    """
    from pydantic.alias_generators import to_camel

    if key == name:
        return True
    # Case-insensitive, so "AzureOpenai" counts too -- still built forwards from
    # the snake name, never by splitting the key on its capitals.
    if key.lower() == to_camel(name).lower():
        return True
    return normalize_provider_name(key) == normalize_provider_name(name)


def metadata_model_id(model: str) -> str | None:
    """The id LiteLLM's metadata table files this model under, when it differs.

    Returns None when the routing id is already the metadata id -- the caller
    then keeps whatever candidates it had. One answer here rather than a mapping
    beside every consumer: price, context window and capability lookups all ask
    the same question.
    """
    spec = find_by_name(split_model_id(model)[0]) if "/" in (model or "") else None
    if spec is None or spec.metadata_prefix is None:
        return None

    _, rest = split_model_id(model)
    return f"{spec.metadata_prefix}/{rest}" if spec.metadata_prefix else rest


def split_model_id(model: str) -> tuple[str, str]:
    """Split a model id into its normalized route prefix and the rest.

    The prefix is routing (which provider), the remainder is the vendor's own
    model id. Returns ("", model) for a bare id -- no prefix means no claim
    about the provider, which is a different case from an unrecognized one.
    """
    head, sep, rest = (model or "").partition("/")
    if not sep:
        return "", model or ""
    return normalize_provider_name(head), rest


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def find_by_model(model: str) -> ProviderSpec | None:
    """Which provider does this model id name? The one answer to that question.

    An explicit prefix names the vendor outright, so it decides alone -- falling
    through to keywords would let one vendor claim another's model whenever the
    id happens to contain its name ("deepinfra/deepseek-ai/DeepSeek-V3" is
    DeepInfra's, and matching DeepSeek there would send DeepInfra's key to
    DEEPSEEK_API_KEY and rewrite the model id). A gateway prefix likewise names
    the gateway itself, not the upstream vendor behind it.

    Local providers answer to their prefix too, but are otherwise matched by
    api_base -- a bare id never resolves to one here.
    """
    prefix, _ = split_model_id(model)
    if prefix:
        for spec in PROVIDERS:
            if prefix in spec.route_names:
                return spec
        return None
    return find_by_keywords(model)


def find_by_keywords(model: str) -> ProviderSpec | None:
    """Match a vendor by keyword alone, ignoring any prefix on the model id.

    For gateway-routed ids the prefixes belong to the gateway and its upstream
    ("openrouter/moonshotai/kimi-k2.5"), neither of which is a spec name -- but
    the vendor's own quirks still apply, so the keyword is all there is to go on.
    Only safe where the spec is not used to place credentials.
    """
    for spec in PROVIDERS:
        if spec.is_gateway or spec.is_local:
            continue
        if spec.matches_keywords(model):
            return spec
    return None


def find_gateway(
    provider_name: str | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
) -> ProviderSpec | None:
    """Detect gateway/local provider.

    Priority:
      1. provider_name — if it maps to a gateway/local spec, use it directly.
      2. api_key prefix — e.g. "sk-or-" → OpenRouter.
      3. api_base keyword — e.g. "aihubmix" in URL → AiHubMix.

    A standard provider with a custom api_base (e.g. DeepSeek behind a proxy)
    will NOT be mistaken for vLLM — the old fallback is gone.
    """
    # 1. Direct match by config key
    if provider_name:
        spec = find_by_name(provider_name)
        if spec and (spec.is_gateway or spec.is_local):
            return spec

    # 2. Auto-detect by api_key prefix / api_base keyword
    for spec in PROVIDERS:
        if spec.detect_by_key_prefix and api_key and api_key.startswith(spec.detect_by_key_prefix):
            return spec
        if spec.detect_by_base_keyword and api_base and spec.detect_by_base_keyword in api_base:
            return spec

    return None


def find_by_name(name: str | None) -> ProviderSpec | None:
    """Find a provider spec by config field name, e.g. "dashscope".

    Accepts a provider's former name too: callers all over the codebase look
    specs up by whatever string a config or a command line handed them, so
    canonicalizing here is the only way a rename reaches all of them.
    """
    name = normalize_provider_name(canonical_provider_name(name))
    for spec in PROVIDERS:
        if normalize_provider_name(spec.name) == name:
            return spec
    return None


def canonical_provider_name(name: str | None) -> str:
    """Map a provider's former name to its current one, e.g. zhipu -> zai.

    Renaming a provider otherwise splits users in two: saved configs and typed
    commands keep the old name, while everything in the code speaks the new one.
    Unknown names come back normalized, not unchanged: a caller writing the
    LiteLLM spelling of a vendor Raven has no spec for still has to match the
    config section, which is written in the underscored form.
    """
    normalized = normalize_provider_name(name)
    for spec in PROVIDERS:
        if normalized in {normalize_provider_name(a) for a in spec.name_aliases}:
            return spec.name
    return normalized
