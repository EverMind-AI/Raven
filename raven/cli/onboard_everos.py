"""EverOS long-term memory cluster of the onboard wizard (Step 4).

Split out of ``onboard_commands`` because that module had grown past 5000
lines; this file owns EverOS role configuration (llm / embedding / rerank /
multimodal) end to end. Shared wizard UI state (``console``, ``_BACK``,
``_QMARK``, questionary helpers, ...) still lives in ``onboard_commands`` --
this module reaches it via the ``oc`` module reference (not a value import) so
that test monkeypatches on ``onboard_commands`` attributes keep working
whichever module a caller patches through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import typer

from raven.cli import _onboard_shared as oc
from raven.core.plugin_stack import everos_plugin_installed, everos_plugin_missing_note
from raven.i18n import t


def _set_memory_backend(backend: Optional[str]) -> None:
    """Set ``memory.backend`` (``"everos"`` / ``None``) via the ops layer."""
    from raven.config.update import set_memory_backend

    set_memory_backend(backend)


def _everos_section(section: str) -> dict[str, Any]:
    from raven.config.update_everos import everos_section

    return everos_section(section)


def _everos_role_configured(section: str) -> bool:
    from raven.config.update_everos import everos_role_configured

    return everos_role_configured(section)


def _memory_enabled() -> bool:
    """True iff EverOS memory is both selected AND usable on disk.

    "Usable" means the llm role is configured -- that is the whole requirement.
    embedding is advised but optional: without it the adapter searches lexically
    instead of semantically, which is weaker memory rather than none, and gating
    on it here would tell a user who skipped it that memory is off (and skip the
    import step along with it).
    """
    data = oc._load_raw_config()
    if (data.get("memory") or {}).get("backend") != "everos":
        return False
    slice_ = _recorded_memory_slice(data)
    if slice_.get("owned") is False:
        # A server the user runs. Its models live in a toml raven promised not
        # to read, and no root is recorded for it, so the address is the whole
        # of what raven can know -- liveness is the runtime's question.
        return bool(slice_.get("base_url"))
    return _everos_role_configured("llm")


def _recorded_memory_slice(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """``plugins.config["everos-memory"]`` as raw JSON."""
    data = oc._load_raw_config() if data is None else data
    plugins = data.get("plugins") or {}
    slice_ = (plugins.get("config") or {}).get("everos-memory") if isinstance(plugins, dict) else None
    return slice_ if isinstance(slice_, dict) else {}


def _configured_target_url() -> str:
    """Where a raven-managed server is meant to listen.

    A recorded intent rather than a constant. Without one, "the running address
    differs from the target" could not distinguish a port an earlier raven left
    behind -- which should be converged -- from one the user chose, which
    should not, and the wizard moved both while calling the second a legacy
    port.
    """
    from raven_everos.server import DEFAULT_EVEROS_BASE_URL

    port = _recorded_memory_slice().get("port")
    if isinstance(port, int) and port > 0:
        return f"http://localhost:{port}"
    # Deliberately not falling back to the recorded address. That address is
    # where the service *is*; convergence compares the two, so reading one as
    # the other makes every pre-upgrade install look like it is already where
    # it belongs and the "keep it or move it" question never fires -- leaving
    # the upgrade quietly parked on the old port with the standard one never
    # mentioned. No intent recorded means the default is the target, and the
    # user gets asked.
    return DEFAULT_EVEROS_BASE_URL


# Providers whose main model can be reused as the EverOS memory LLM: they
# speak the OpenAI chat-completions protocol that EverOS's bare OpenAI client
# requires. OAuth providers (github_copilot / openai_codex) and non-OpenAI
# wire protocols (anthropic / gemini) are excluded.
_OPENAI_COMPATIBLE_PROVIDERS = {"openrouter", "openai", "deepseek", "custom"}

# Fallback OpenAI-compatible base URLs for providers whose registry
# ``default_api_base`` is empty (they rely on the SDK's built-in default,
# which EverOS's bare client doesn't know). EverOS needs an explicit base_url.
_PROVIDER_BASE_URL_FALLBACK = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


def _resolve_model_provider(model: str) -> Optional[str]:
    """Best-effort: which configured provider does ``model`` belong to?

    Prefixed models (``openrouter/...`` / ``openai/gpt-4o``) read off the head.
    A custom endpoint stores its model as a BARE id (e.g. ``qwen-max``) with no
    prefix, so an unrecognized head falls back to ``"custom"`` when a custom
    provider is actually configured with a key. Returns ``None`` when no match.
    """
    from raven.providers.registry import split_model_id

    if not model:
        return None
    head, _ = split_model_id(model)
    if head:
        from raven.config.update_providers import provider_field_specs

        try:
            provider_field_specs(head)
            return head
        except KeyError:
            pass
    # No usable prefix → could be a bare custom-endpoint model.
    from raven.config.schema import ProviderConfig
    from raven.providers.auth import credential_status

    custom = (oc._load_raw_config().get("providers") or {}).get("custom") or {}
    if credential_status("custom", ProviderConfig.model_validate(custom)).ok:
        return "custom"
    # A bare id that still matches a known provider head (rare; e.g. a direct
    # provider's bare default before prefixing) — accept the head if known.
    return head if head in _OPENAI_COMPATIBLE_PROVIDERS else None


def _model_is_openai_compatible(model: Optional[str]) -> bool:
    """Heuristic: can the main chat model's provider be reused for memory LLM?

    EverOS's memory LLM uses a bare OpenAI client, so the main model is
    reusable only when its provider speaks the OpenAI chat protocol. Custom
    endpoints are OpenAI-compatible by definition (the wizard only offers
    ``custom`` for OpenAI-compatible endpoints).
    """
    if not model:
        return False
    return _resolve_model_provider(model) in _OPENAI_COMPATIBLE_PROVIDERS


def _resolve_reuse_llm_creds(main_model: str) -> dict[str, Optional[str]]:
    """Map a litellm-style main model to bare EverOS LLM settings.

    EverOS sends ``EVEROS_LLM__MODEL`` to ``base_url`` via a bare OpenAI
    client, so:
      - strip the provider's litellm prefix to the bare model id the upstream
        endpoint expects (``openrouter/anthropic/claude-x`` → ``anthropic/claude-x``;
        a custom endpoint's bare id is used as-is);
      - resolve the provider's real ``base_url`` (configured ``apiBase`` →
        registry ``default_api_base`` → a known fallback);
      - carry the provider's stored api_key.
    """
    from raven.providers.registry import find_by_name, normalize_provider_name, split_model_id

    provider = _resolve_model_provider(main_model) or split_model_id(main_model)[0]
    spec = find_by_name(provider)
    # Through the ops library, so a section still stored under the provider's
    # pre-rename name is found -- a raw lookup by the resolved name is not.
    from raven.config.update_providers import get_provider_config

    # No `if spec` gate: LiteLLM-only vendors have no spec of ours yet their
    # section holds real credentials, and gating on the spec silently handed the
    # probe an empty api_key while the main model was working fine.
    try:
        _resolved = get_provider_config(provider, redact_secrets=False)
    except KeyError:
        _resolved = {}
    prov_cfg = {"apiKey": _resolved.get("api_key"), "apiBase": _resolved.get("api_base")} if _resolved else {}

    # Strip the routing prefix to the bare model id the upstream endpoint
    # expects: litellm consumes it, the raw OpenAI client must not see it. Only
    # a prefix naming this provider is stripped -- a custom endpoint stores a
    # bare id already, and anything else is part of the vendor's own model id.
    bare_model = main_model
    head, rest = split_model_id(main_model)
    known_prefixes = set(spec.route_names) if spec else {normalize_provider_name(provider)}
    if spec:
        known_prefixes.add(normalize_provider_name(spec.model_prefix))
    if head and head in known_prefixes:
        bare_model = rest

    base_url = (
        prov_cfg.get("apiBase")
        or (getattr(spec, "default_api_base", "") if spec else "")
        or _PROVIDER_BASE_URL_FALLBACK.get(provider)
    )
    return {
        "model": bare_model,
        "api_key": prov_cfg.get("apiKey"),
        "base_url": base_url,
    }


def _prompt_text(label: str, *, secret: bool = False, default: str = "", allow_back: bool = False) -> Any:
    """Prompt for free text. With ``allow_back``, an empty submit returns
    ``oc._BACK`` (and a hint is shown); otherwise returns the stripped string."""
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    placeholder = oc._back_placeholder(allow_back)
    if secret:
        value = questionary.password(label, placeholder=placeholder, style=RAVEN_STYLE, qmark=oc._QMARK).ask()
    else:
        value = questionary.text(
            label, default=default, placeholder=placeholder, style=RAVEN_STYLE, qmark=oc._QMARK
        ).ask()
    if value is None:
        raise typer.Exit(1)
    value = value.strip()
    if allow_back and value == "":
        return oc._BACK
    return value


def _probe_everos_chat(model: Optional[str], *, api_key: Optional[str], base_url: Optional[str]) -> tuple[bool, str]:
    """Real capability probe for a memory-LLM endpoint: ``POST
    {base_url}/chat/completions`` once and confirm a choice comes back. Unlike a
    bare ``GET /models`` connectivity check, this exercises the picked model, so
    an endpoint that lists models but doesn't serve the chosen id fails here
    instead of reporting a false green. Provider-agnostic; never raises."""
    import httpx

    if not base_url:
        return False, "no base_url configured"
    url = base_url.rstrip("/") + ("/chat/completions" if "/v1" in base_url else "/v1/chat/completions")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        return False, f"probe failed: {exc}"
    choices = data.get("choices") if isinstance(data, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return True, "ok"
    return False, "endpoint returned no completion"


def _verify_everos_llm(
    label: str,
    *,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    non_interactive: bool,
    warnings: list[str],
    continue_hint: Optional[str] = None,
) -> bool:
    """Probe the memory LLM with a real chat completion, offering retry/continue on failure."""
    oc.console.print(t("  [dim]⏳ Verifying {label}…[/dim]", label=label))
    ok, detail = _probe_everos_chat(model, api_key=api_key, base_url=base_url)
    if ok:
        oc.console.print(t("  [green]✓ {label} connected.[/green]", label=label))
        return True
    oc.console.print(t("  [yellow]✗ Couldn't verify {label}: {detail}[/yellow]", label=label, detail=detail))
    if continue_hint:
        cont_label = t("Continue anyway ({hint})", hint=t(continue_hint))
    else:
        cont_label = t("Continue anyway")
    choice = oc._failure_choice(
        [
            (t("Re-enter"), "rekey"),
            (cont_label, "continue"),
        ],
        non_interactive=non_interactive,
    )
    if choice == "rekey":
        return False
    warnings.append(label)
    return True


def _verify_rerank(
    label: str,
    *,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    rerank_provider: Optional[str],
    non_interactive: bool,
    warnings: list[str],
    continue_hint: Optional[str] = None,
) -> bool:
    """Probe a rerank endpoint with a provider-specific request, offering retry/continue on failure."""
    oc.console.print(t("  [dim]⏳ Verifying {label}…[/dim]", label=label))
    ok, detail = _probe_rerank(model, api_key=api_key, base_url=base_url, rerank_provider=rerank_provider)
    if ok:
        oc.console.print(t("  [green]✓ {label} connected.[/green]", label=label))
        return True
    oc.console.print(t("  [yellow]✗ Couldn't verify {label}: {detail}[/yellow]", label=label, detail=detail))
    if continue_hint:
        cont_label = t("Continue anyway ({hint})", hint=t(continue_hint))
    else:
        cont_label = t("Continue anyway")
    choice = oc._failure_choice(
        [
            (t("Re-enter"), "rekey"),
            (cont_label, "continue"),
        ],
        non_interactive=non_interactive,
    )
    if choice == "rekey":
        return False
    warnings.append(label)
    return True


def _probe_rerank(
    model: Optional[str],
    *,
    api_key: Optional[str],
    base_url: Optional[str],
    rerank_provider: Optional[str],
) -> tuple[bool, str]:
    """Real capability probe for a rerank endpoint. Dispatches by provider
    protocol (vllm / deepinfra / dashscope). Never raises."""
    import httpx

    if not base_url or not model:
        return False, "no base_url or model configured"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    headers["Content-Type"] = "application/json"

    try:
        if rerank_provider == "deepinfra":
            url = f"{base_url.rstrip('/')}/{model}"
            body: dict = {"queries": ["ping"], "documents": ["pong"]}
        elif rerank_provider == "dashscope":
            url = f"{base_url.rstrip('/')}/api/v1/services/rerank/text-rerank/text-rerank"
            body = {
                "model": model,
                "input": {"query": "ping", "documents": ["pong"]},
                "parameters": {"return_documents": False, "top_n": 1},
            }
        else:  # vllm / OpenAI-compat
            url = f"{base_url.rstrip('/')}/rerank"
            body = {"model": model, "query": "ping", "documents": ["pong"]}

        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=body, headers=headers)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        return False, f"probe failed: {exc}"

    if rerank_provider == "deepinfra":
        scores = data.get("scores")
        if isinstance(scores, list) and scores:
            return True, "ok"
        return False, "endpoint returned no scores"
    if rerank_provider == "dashscope":
        output = data.get("output")
        results = output.get("results") if isinstance(output, dict) else None
        if isinstance(results, list) and results:
            return True, "ok"
        return False, "endpoint returned no results"
    # vllm
    results = data.get("results")
    if isinstance(results, list) and results:
        return True, "ok"
    return False, "endpoint returned no results"


_REQUIRED_EMBEDDING_DIM = 1024


def _probe_embedding_dim(url: str, headers: dict, model: str) -> int | str:
    """Try embedding with ``dimensions=1024``; fall back to native dim.

    Returns the effective dimension (int) on success, or an error
    description (str) on failure.
    """
    import httpx

    def _try_embed(client: httpx.Client, body: dict) -> int | str:
        try:
            resp = client.post(url, json=body, headers=headers)
            if resp.status_code != 200:
                return f"HTTP {resp.status_code}"
            items = resp.json().get("data", [])
            if not items:
                return "empty response"
            first = items[0]
            if not isinstance(first, dict):
                return "unexpected response format"
            return len(first.get("embedding", []))
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            return str(exc)

    with httpx.Client(timeout=15) as client:
        result = _try_embed(
            client, {"model": model, "input": ["dimension check"], "dimensions": _REQUIRED_EMBEDDING_DIM}
        )
        if result == _REQUIRED_EMBEDDING_DIM:
            return result
        return _try_embed(client, {"model": model, "input": ["dimension check"]})


def _verify_embedding_dim(
    *,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    non_interactive: bool,
) -> bool:
    """Send a test embedding request and verify the vector dimension is 1024.

    Returns True to proceed, False to re-prompt.
    """
    if not base_url or not model:
        return True

    url = base_url.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    while True:
        oc.console.print(t("  [dim]⏳ Checking embedding dimension…[/dim]"))
        result = _probe_embedding_dim(url, headers, model)

        if result == _REQUIRED_EMBEDDING_DIM:
            oc.console.print(t("  [green]✓ Supports {result}-dim.[/green]", result=result))
            return True

        if isinstance(result, int) and result < _REQUIRED_EMBEDDING_DIM:
            oc.console.print(
                t(
                    "  [red]✗ Dimension too small: model outputs {result}-dim, EverOS requires >= {_REQUIRED_EMBEDDING_DIM}. Please pick another model.[/red]",
                    result=result,
                    _REQUIRED_EMBEDDING_DIM=_REQUIRED_EMBEDDING_DIM,
                )
            )
            return False

        if isinstance(result, int) and result > _REQUIRED_EMBEDDING_DIM:
            oc.console.print(
                t(
                    "  [red]✗ Model outputs {result}-dim and does not support the dimensions parameter to truncate to {_REQUIRED_EMBEDDING_DIM}. Please pick another model.[/red]",
                    result=result,
                    _REQUIRED_EMBEDDING_DIM=_REQUIRED_EMBEDDING_DIM,
                )
            )
            return False

        oc.console.print(t("  [yellow]✗ Couldn't verify dimension: {result}[/yellow]", result=result))
        if non_interactive:
            return False
        choice = oc._failure_choice(
            [
                (t("Retry"), "retry"),
                (t("Re-enter"), "rekey"),
            ],
            non_interactive=False,
        )
        if choice == "rekey":
            return False


# Curated OpenAI-compatible endpoints for EverOS memory models. Picking one
# pre-fills its base_url (mirrors the main provider step); everything else is
# reachable via "reuse an existing endpoint" or "custom" (type a base_url).
# These are the providers' documented OpenAI-compatible /v1 endpoints.
_EVEROS_PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "supports": {"llm", "embedding", "multimodal"},
    },
    {
        "name": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "supports": {"llm", "embedding", "rerank", "multimodal"},
        "rerank_provider": "vllm",
    },
    {
        "name": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "supports": {"llm"},
    },
    {
        "name": "deepinfra",
        "label": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_provider": "deepinfra",
        "rerank_base_url": "https://api.deepinfra.com/v1/inference",
    },
    {
        "name": "siliconflow",
        "label": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_provider": "vllm",
    },
    {
        "name": "dashscope",
        "label": "DashScope (Alibaba)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_provider": "dashscope",
        "rerank_base_url": "https://dashscope.aliyuncs.com",
    },
]


def _match_provider_by_url(base_url: Optional[str]) -> Optional[str]:
    """Reverse-lookup a curated provider name from its base_url."""
    if not base_url:
        return None
    normalized = base_url.rstrip("/")
    for prov in _EVEROS_PROVIDERS:
        if prov["base_url"].rstrip("/") == normalized:
            return prov["name"]
    return None


# Per-role config: menu/verify label, model-id example, whether optional, and
# whether to run a connectivity probe after configuring (rerank/multimodal use
# non-chat endpoints whose /models probe isn't a reliable health check).
_EVEROS_ROLES: dict[str, dict[str, Any]] = {
    "llm": {
        "label": "Memory LLM",
        "example": "gpt-4.1-mini",
        "optional": False,
        "verify": True,
        "purpose": "Reads each conversation to judge what matters and extract the key points.",
        # Worded as a floor rather than a default: this id is what the field
        # starts on, but a key that cannot reach it is no reason to stop -- the
        # point is the capability level, and the fetched list is there to pick
        # an equivalent from.
        "recommendation": "Capability floor: [bold]gpt-4.1-mini[/bold] -- weaker models degrade extraction",
        "continue_hint": "memory extraction may fail",
    },
    "embedding": {
        "label": "Memory embedding",
        "example": "Qwen/Qwen3-Embedding-4B",
        # Optional in the sense that memory still functions without it: the
        # adapter drops to KEYWORD search, which needs no vectors. Strongly
        # advised all the same -- lexical recall misses a memory the moment the
        # user phrases the question differently.
        "optional": True,
        "verify": True,
        "purpose": "Turns text into vectors so memories are found by meaning, not just keywords.",
        "tag": "[accent](optional, strongly advised)[/accent]",
        "cost": "Without it: rephrase a question and it may miss a memory you have;\n  recall can only match keywords.",
        "recommendation": "Recommended: [bold]Qwen/Qwen3-Embedding-4B[/bold] -- must be [bold yellow]1024-dim[/bold yellow],\n"
        "  Chinese + English",
        "continue_hint": "semantic recall will be unavailable",
        "skip_note": "  [yellow]! Skipped: recall will match keywords, not meaning.[/yellow]\n"
        "  [dim]Phrase a question differently and it may miss a memory you have.\n"
        "  Configure it later, then run `everos cascade backfill`.[/dim]",
    },
    "rerank": {
        "label": "Memory rerank",
        "example": "Qwen/Qwen3-Reranker-4B",
        "optional": True,
        "verify": True,
        "purpose": "Re-ranks what semantic search found so the best match comes first, at a small\n  latency cost.",
        "tag": "[accent](optional, advised)[/accent]",
        "recommendation": "Recommended: [bold]Qwen/Qwen3-Reranker-4B[/bold]",
        "continue_hint": "rerank quality may degrade",
        "skip_note": "  [dim]Skipped rerank; memory retrieval still works.[/dim]",
    },
    "multimodal": {
        "label": "Memory multimodal",
        "example": "google/gemini-3-flash-preview",
        "optional": True,
        "verify": True,
        "purpose": "Lets Raven understand and recall images / PDFs / audio as memory.",
        "cost": "Without it: those files stay out of memory. Having such files is not the same\n"
        "  as needing them remembered -- configure it when you do.",
        "recommendation": "Recommended: [bold]google/gemini-3-flash-preview[/bold]",
        "skip_note": "  [dim]Skipped; nothing else is affected -- configure it if you come to need\n  multimodal memory.[/dim]",
    },
}


_EMBEDDING_MODEL_PATTERNS = ("embed", "bge", "e5-", "gte-")
_MULTIMODAL_MODEL_PATTERNS = ("vision", "4o", "gemini", "pixtral", "qwen-vl", "qwen2-vl", "qwen2.5-vl")


def _fetch_everos_models(
    base_url: Optional[str],
    api_key: Optional[str],
    *,
    section: str = "llm",
    provider_name: Optional[str] = None,
) -> Optional[list[str]]:
    """Fetch available model ids from a provider endpoint. Never raises.

    For ``section="embedding"``, delegates to per-provider logic because
    each provider exposes embedding models differently.
    """
    if not base_url:
        return None
    if section == "embedding":
        return _fetch_embedding_models(base_url, api_key, provider_name)
    if section == "rerank":
        return _fetch_rerank_models(base_url, api_key, provider_name)
    if section == "multimodal":
        return _fetch_multimodal_models(base_url, api_key, provider_name)
    return _fetch_openai_models(base_url, api_key)


def _fetch_openai_models(
    base_url: str,
    api_key: Optional[str],
    *,
    params: Optional[dict[str, str]] = None,
) -> Optional[list[str]]:
    """``GET {base_url}/models`` with OpenAI-style response parsing."""
    import httpx

    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
    return sorted(ids) or None


def _fetch_deepinfra_models(
    api_key: Optional[str],
    reported_type: str,
    *,
    name_contains: Optional[str] = None,
) -> Optional[list[str]]:
    """Fetch DeepInfra models filtered by ``reported_type`` and optional name substring."""
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get("https://api.deepinfra.com/models/list", headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    items = data if isinstance(data, list) else []
    ids = [
        m.get("model_name")
        for m in items
        if isinstance(m, dict)
        and m.get("reported_type") == reported_type
        and m.get("model_name")
        and (name_contains is None or name_contains in m.get("model_name", ""))
    ]
    return sorted(ids) or None


def _fetch_embedding_models(
    base_url: str,
    api_key: Optional[str],
    provider_name: Optional[str],
) -> Optional[list[str]]:
    """Provider-specific embedding model listing."""
    if provider_name == "openrouter":
        return _fetch_openai_models(base_url.rstrip("/") + "/embeddings", api_key)

    if provider_name == "siliconflow":
        return _fetch_openai_models(base_url, api_key, params={"type": "text", "sub_type": "embedding"})

    if provider_name == "deepinfra":
        return _fetch_deepinfra_models(api_key, "embeddings")

    # OpenAI, DashScope, custom — GET /models + name-based filter.
    ids = _fetch_openai_models(base_url, api_key)
    if ids is None:
        return None
    filtered = [i for i in ids if any(p in i.lower() for p in _EMBEDDING_MODEL_PATTERNS)]
    return filtered or None


def _fetch_rerank_models(
    base_url: str,
    api_key: Optional[str],
    provider_name: Optional[str],
) -> Optional[list[str]]:
    """Provider-specific rerank model listing."""
    if provider_name == "deepinfra":
        # The deepinfra provider hardcodes a Qwen3-Reranker chat template,
        # so only Qwen3-Reranker models are compatible.
        return _fetch_deepinfra_models(api_key, "reranker", name_contains="Qwen3-Reranker")

    if provider_name == "siliconflow":
        return _fetch_openai_models(base_url, api_key, params={"sub_type": "reranker"})

    if provider_name == "dashscope":
        return ["gte-rerank-v2"]

    if provider_name == "openrouter":
        return _fetch_openai_models(base_url, api_key, params={"output_modalities": "rerank"})

    # vllm / custom — no standard rerank listing.
    return None


def _fetch_multimodal_models(
    base_url: str,
    api_key: Optional[str],
    provider_name: Optional[str],
) -> Optional[list[str]]:
    """Provider-specific multimodal (vision) model listing."""
    if provider_name == "openrouter":
        return _fetch_openai_models(base_url, api_key, params={"input_modalities": "image"})

    # OpenAI, custom — GET /models + name-based filter.
    ids = _fetch_openai_models(base_url, api_key)
    if ids is None:
        return None
    filtered = [i for i in ids if any(p in i.lower() for p in _MULTIMODAL_MODEL_PATTERNS)]
    return filtered or None


def _match_everos_default(example: str, models: list[str]) -> str:
    """The catalog entry to start the model field on, or ``""`` for none.

    The example (e.g. ``gpt-4.1-mini``) is a bare model name, while ``models``
    may carry provider prefixes (``openai/gpt-4.1-mini``), so a match is the
    first id equal to ``example`` or ending in ``/example``.

    A catalog with no such entry pre-fills nothing. Falling back to the bare
    example put an id the catalog had just denied under the cursor, and Enter
    submits the default: picking a provider that does not serve the recommended
    model cost a verification round trip to learn what the list already said.
    """
    lower = example.lower()
    suffix = f"/{lower}"
    for mid in models:
        if mid.lower() == lower or mid.lower().endswith(suffix):
            return mid
    return ""


# Where a reused key came from. The provider list is tagged with it so the user
# can see which providers cost nothing to pick, and the same origin names the
# note printed when the key is taken -- a key that arrives without being typed
# has to say where it came from.
_KEY_REUSE_TAG: dict[str, str] = {
    "main": "{label} (main model provider, reuse Key)",
    "llm": "{label} (memory LLM provider, reuse Key)",
    "config": "{label} (configured, reuse Key)",
}
_KEY_REUSE_NOTE: dict[str, str] = {
    "main": "  [dim]API key and endpoint reused from main chat model.[/dim]",
    "llm": "  [dim]API key and endpoint reused from memory LLM.[/dim]",
    "config": "  [dim]API key and endpoint reused from this provider's Raven configuration.[/dim]",
}


def _reusable_creds(
    section: str, prov: dict[str, Any], main_model: Optional[str], llm_section: dict[str, Any]
) -> tuple[Optional[dict[str, str]], str]:
    """The credentials raven already holds for ``prov``, and which store lent them.

    ``(None, "")`` when nothing is on file -- the only case that still costs a
    key entry. A key and the address it was issued for are one group and travel
    together: a section can hold its key in an ``endpoints`` entry pointing at a
    private gateway, and pairing that key with the curated vendor URL would send
    a private credential to the public endpoint.

    Two stores. The memory LLM's own section answers first for the roles
    configured after it, because a key typed into that step lives there and
    nowhere else. Otherwise ``borrow_provider_credentials`` reads the provider
    section, which is the one place that knows the precedence a section can be
    written in -- ``endpoints``, then ``api_key_list``, then the flat pair. The
    origin only names which store answered, and separates the main chat model's
    provider from any other configured one for the note the user sees.
    """
    provider = prov["name"]
    if section != "llm" and _match_provider_by_url(llm_section.get("base_url")) == provider:
        key = llm_section.get("api_key")
        if key:
            return {"api_key": str(key), "base_url": str(llm_section.get("base_url") or "")}, "llm"
    from raven.config.update_everos import borrow_provider_credentials

    try:
        lent = borrow_provider_credentials(provider)
    except (KeyError, ValueError):
        return None, ""
    # rerank reaches a different service path on the same vendor, and a key
    # issued for some other address cannot speak for that path. Declining beats
    # both alternatives: pointing the borrowed key at the curated rerank URL is
    # the mismatch this function exists to prevent, and deriving a rerank path
    # from a private base URL is a guess about someone else's deployment.
    if section == "rerank" and prov.get("rerank_base_url"):
        lent_url = str(lent.get("base_url") or "")
        if lent_url and lent_url.rstrip("/") != str(prov["base_url"]).rstrip("/"):
            return None, ""
    origin = "main" if main_model and _resolve_model_provider(main_model) == provider else "config"
    return lent, origin


def _everos_pick_model(
    *,
    base_url: Optional[str],
    api_key: Optional[str],
    example: str,
    allow_back: bool,
    section: str = "llm",
    provider_name: Optional[str] = None,
    recommendation: Optional[str] = None,
) -> Any:
    """Pick a model id for an EverOS endpoint: fetch ``/models`` for a
    fuzzy-searchable list, else fall back to free text. Empty submit = back.

    The field starts on ``example`` -- the role's own recommended model --
    matched against the fetched list so a provider-prefixed id is offered whole.
    Reusing a provider does not carry that provider's model over: the roles want
    different models, and the memory LLM is not the main chat model.
    """
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    oc.console.print(t("  [dim]⏳ Loading models…[/dim]"))
    models = _fetch_everos_models(base_url, api_key, section=section, provider_name=provider_name)
    if recommendation:
        oc.console.print(f"  [dim]{t(recommendation)}[/dim]")
    if models:
        default_model = _match_everos_default(example, models)
        # An empty field where a value is expected reads as a broken prompt, so
        # say who left it empty and why. The recommendation printed just above
        # is a capability floor, and this endpoint not carrying that exact id
        # says nothing about whether it carries an equal.
        if not default_model:
            oc.console.print(
                t(
                    "  [dim]This endpoint does not list [bold]{example}[/bold] -- pick one of its own\n"
                    "  at that level from the list below.[/dim]",
                    example=example,
                ),
                highlight=False,
            )
        question = questionary.autocomplete(
            t("Model ({a0} available — type to filter):", a0=len(models)),
            choices=models,
            default=default_model,
            ignore_case=True,
            match_middle=True,
            placeholder=oc._back_placeholder(allow_back),
            style=RAVEN_STYLE,
            qmark=oc._QMARK,
        )
        # Trigger the completion popup immediately so the user sees
        # all available models without typing first.
        app = question.application

        def _show_completions() -> None:
            buf = app.current_buffer
            buf.start_completion()

        app.pre_run_callables.append(_show_completions)
        chosen = question.ask()
    else:
        oc.console.print(t("  [dim]Couldn't list models from this endpoint — type the id manually.[/dim]"))
        chosen = questionary.text(
            t("Model id (e.g. {example}):", example=example),
            default="",
            placeholder=oc._back_placeholder(allow_back),
            style=RAVEN_STYLE,
            qmark=oc._QMARK,
        ).ask()
    if chosen is None:
        raise typer.Exit(1)
    chosen = chosen.strip()
    if allow_back and chosen == "":
        return oc._BACK
    if not chosen:
        raise typer.Exit(1)
    return chosen


def _everos_pick_creds_and_model(
    *,
    section: str,
    example: str,
    main_model: Optional[str],
    non_interactive: bool,
    recommendation: Optional[str] = None,
) -> Any:
    """Mirror the main provider step for one EverOS model: pick a source
    (curated provider / custom) → API key → model. Returns a dict with
    ``model`` / ``api_key`` / ``base_url`` (plus ``provider`` for rerank), or
    ``oc._BACK`` when the user backs out of the source picker. Empty submit on any
    field rewinds one step."""
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    llm_section = _everos_section("llm")

    # Which provider the cursor lands on. For the LLM role, the main chat
    # model's. For the others, whichever provider the LLM step just configured —
    # the same key usually serves both and only the model differs. Landing there
    # is all this decides: every provider raven already holds a key for is
    # reusable, so moving the cursor off the default costs nothing either.
    if section == "llm":
        default_provider = _resolve_model_provider(main_model or "")
    else:
        default_provider = _match_provider_by_url(llm_section.get("base_url"))

    while True:  # source picker — a field-level back rewinds here
        offered = [p for p in _EVEROS_PROVIDERS if section in p.get("supports", set())]
        held = {p["name"]: _reusable_creds(section, p, main_model, llm_section) for p in offered}
        choices: list[Any] = []
        default_choice = None
        for prov in offered:
            # Tagged by what raven actually holds, not by which entry is
            # highlighted: a default with no key on file still asks for one, and
            # a label promising reuse there would be a lie.
            _creds, origin = held[prov["name"]]
            label = t(_KEY_REUSE_TAG[origin], label=t(prov["label"])) if origin else t(prov["label"])
            choice = questionary.Choice(label, value=("provider", prov))
            choices.append(choice)
            if prov["name"] == default_provider:
                default_choice = choice.value
        choices.append(
            questionary.Choice(
                t("Other (custom OpenAI-compatible endpoint)"),
                value=("custom",),
            )
        )
        choices.append(questionary.Separator())
        choices.append(questionary.Choice(t("Back"), value=oc._BACK))

        src = questionary.select(
            t("Pick a provider (or reuse / custom):"),
            choices=choices,
            default=default_choice,
            style=RAVEN_STYLE,
            qmark=oc._QMARK,
        ).ask()
        if src is None:
            raise typer.Exit(1)
        if src is oc._BACK:
            return oc._BACK
        kind = src[0]

        # Resolve (api_key, base_url) from the chosen source.
        chosen_provider: Optional[str] = None
        if kind == "provider":
            chosen_provider = src[1]["name"]
            creds, origin = held[chosen_provider]
            if creds:
                # The lent address, not the curated one: a borrowed key is only
                # valid against the endpoint it was issued for.
                api_key = creds["api_key"]
                base_url = creds.get("base_url") or src[1]["base_url"]
                oc.console.print(t(_KEY_REUSE_NOTE[origin]))
            else:
                base_url = src[1]["base_url"]
                api_key = oc._prompt_api_key(chosen_provider, allow_back=True)
                if api_key is oc._BACK:
                    continue
        else:  # custom
            base_url = _prompt_text(t("Base URL (must include /v1):"), allow_back=True)
            if base_url is oc._BACK:
                continue
            api_key = _prompt_text(t("API key (hidden):"), secret=True, allow_back=True)
            if api_key is oc._BACK:
                continue

        # Guard against a source that resolved to an empty key / endpoint —
        # set_everos_section drops None values, which would otherwise persist a
        # section with a model but no usable endpoint.
        if not (api_key and base_url):
            oc.console.print(t("  [yellow]✗ Missing API key or Base URL for this source — pick another.[/yellow]"))
            continue

        # rerank: resolve service type + override base_url when needed.
        rerank_provider: Optional[str] = None
        if section == "rerank":
            chosen_prov_dict = src[1] if kind == "provider" else None
            if chosen_prov_dict and chosen_prov_dict.get("rerank_provider"):
                rerank_provider = chosen_prov_dict["rerank_provider"]
                if chosen_prov_dict.get("rerank_base_url"):
                    base_url = chosen_prov_dict["rerank_base_url"]
            else:
                rerank_provider = questionary.select(
                    t("Rerank service type:"),
                    choices=[
                        questionary.Choice("deepinfra", value="deepinfra"),
                        questionary.Choice("vllm", value="vllm"),
                        questionary.Choice("dashscope", value="dashscope"),
                        questionary.Choice(t("Back"), value=oc._BACK),
                    ],
                    style=RAVEN_STYLE,
                    qmark=oc._QMARK,
                ).ask()
                if rerank_provider is None:
                    raise typer.Exit(1)
                if rerank_provider is oc._BACK:
                    continue

        model = _everos_pick_model(
            base_url=base_url,
            api_key=api_key,
            example=example,
            allow_back=True,
            section=section,
            provider_name=chosen_provider,
            recommendation=recommendation,
        )
        if model is oc._BACK:
            continue

        result: dict[str, Any] = {"model": model, "api_key": api_key, "base_url": base_url}
        if rerank_provider:
            result["provider"] = rerank_provider
        return result


def _config_everos_role(
    *, section: str, main_model: Optional[str], non_interactive: bool, warnings: list[str], skip_test: bool = False
) -> Any:
    """Configure one EverOS memory role (llm / embedding / rerank / multimodal)
    with the unified provider→key→model flow, reuse shortcuts, and a back loop.

    Returns ``None`` normally; returns ``oc._ABORT_EVEROS`` when the user gives up a
    required role (the caller then disables EverOS, leaving no long-term memory)."""
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_everos import clear_everos_section, set_everos_section

    role = _EVEROS_ROLES[section]
    label = t(role["label"])
    purpose = t(role["purpose"])
    optional = role["optional"]
    verify_label = label

    # Tell the user what this model is for, and what skipping it costs, before
    # asking them to configure it. Header sits on the 2-space info column (bold
    # accent); purpose and cost nest under it, matching the layout used
    # everywhere else.
    #
    # The cost line is dim rather than a warning colour on purpose: this is
    # pre-decision information, and colouring it would cry wolf before the user
    # has chosen anything. The warning comes after, from ``skip_note``.
    #
    # Roles that want to be configured say so in their own ``tag`` -- calling all
    # three merely "optional" flattens the difference between losing semantic
    # recall entirely and losing a little ranking accuracy.
    tag_markup = t(role["tag"]) if role.get("tag") else t("[dim](optional)[/dim]")
    lines = [f"  [bold][accent]{label}[/accent][/bold]" + (f" {tag_markup}" if optional else "")]
    lines.append(f"  [dim]{purpose}[/dim]")
    if role.get("cost"):
        lines.append(f"  [dim]{t(role['cost'])}[/dim]")
    oc.console.print()
    # highlight=False so Rich's default highlighter doesn't tint the dim prose
    # (parens/numbers/words) and make an informational hint read like an error.
    oc.console.print("\n".join(lines), highlight=False)

    while True:  # role-menu loop — a back-out of the source picker returns here
        current = _everos_section(section).get("model") if _everos_role_configured(section) else None
        if current:
            choices = [
                questionary.Choice(t("Keep current: {current}", current=current), value="keep"),
                questionary.Choice(t("Reconfigure"), value="redo"),
            ]
            if optional:
                choices.append(questionary.Choice(t("Skip"), value="off"))
            action = questionary.select(
                t("Already configured — what now?"),
                choices=choices,
                style=RAVEN_STYLE,
                qmark=oc._QMARK,
            ).ask()
            if action is None:
                raise typer.Exit(1)
            if action == "keep":
                return
            if action == "off":
                clear_everos_section(section)
                oc.console.print(t("  [dim]{label} skipped.[/dim]", label=label))
                return
        elif optional:
            action = questionary.select(
                t("Configure it?"),
                choices=[
                    questionary.Choice(t("Configure"), value="redo"),
                    questionary.Choice(t("Skip"), value="skip"),
                ],
                style=RAVEN_STYLE,
                qmark=oc._QMARK,
            ).ask()
            if action is None:
                raise typer.Exit(1)
            if action == "skip":
                # Printed verbatim rather than wrapped in [dim]: skipping rerank
                # costs ordering, skipping embedding costs semantic recall
                # entirely, and one of those deserves to be seen.
                note = role.get("skip_note")
                oc.console.print(t(note) if note else t("  [dim]Skipped {label}.[/dim]", label=label), highlight=False)
                return
        # A required role with nothing configured falls straight into the picker.

        result = _everos_pick_creds_and_model(
            section=section,
            example=role["example"],
            main_model=main_model,
            non_interactive=non_interactive,
            recommendation=role.get("recommendation"),
        )
        if result is oc._BACK:
            if optional or _everos_role_configured(section):
                # Optional roles offer Skip; a required role already configured
                # falls back to its keep/reconfigure menu. Either way, re-show
                # the role menu rather than forcing the give-up exit.
                continue
            # A required role with nothing configured has no Skip, so backing out
            # of the picker would loop forever. Offer a bounded exit -- keep
            # trying, or leave without long-term memory. Stated in full and in
            # colour: this is the only place the wizard can lose memory
            # altogether, and "no cross-session memory" is a consequence a user
            # should not discover weeks later by noticing the agent forgets
            # everything.
            oc.console.print()
            oc.console.print(
                t(
                    "  [yellow]⚠ {label} is required for long-term memory.[/yellow]\n  [dim]Without it Raven has no memory across sessions: every conversation starts\n  from nothing, with no recollection of your preferences or of what was done before.[/dim]",
                    label=label,
                ),
                highlight=False,
            )
            action = questionary.select(
                t("What would you like to do?"),
                choices=[
                    questionary.Choice(t("Pick a provider / model"), value="retry"),
                    questionary.Choice(
                        t("Give up (no long-term memory)"),
                        value="abort",
                    ),
                ],
                style=RAVEN_STYLE,
                qmark=oc._QMARK,
            ).ask()
            if action is None:
                raise typer.Exit(1)
            if action == "retry":
                continue
            return oc._ABORT_EVEROS

        if role["verify"] and skip_test:
            oc.console.print(
                t("  [dim]Skipping the {verify_label} test call (--skip-test).[/dim]", verify_label=verify_label)
            )
            ok = True
        elif section == "llm":
            ok = _verify_everos_llm(
                verify_label,
                model=result["model"],
                api_key=result["api_key"],
                base_url=result["base_url"],
                non_interactive=non_interactive,
                warnings=warnings,
                continue_hint=role.get("continue_hint"),
            )
        elif section == "embedding":
            ok = _verify_embedding_dim(
                model=result["model"],
                api_key=result["api_key"],
                base_url=result["base_url"],
                non_interactive=non_interactive,
            )
        elif section == "rerank":
            ok = _verify_rerank(
                verify_label,
                model=result["model"],
                api_key=result["api_key"],
                base_url=result["base_url"],
                rerank_provider=result.get("provider"),
                non_interactive=non_interactive,
                warnings=warnings,
                continue_hint=role.get("continue_hint"),
            )
        elif section == "multimodal":
            ok = _verify_everos_llm(
                verify_label,
                model=result["model"],
                api_key=result["api_key"],
                base_url=result["base_url"],
                non_interactive=non_interactive,
                warnings=warnings,
                continue_hint=role.get("continue_hint"),
            )
        else:
            ok = True
        if not ok:
            continue

        set_everos_section(section, result)
        oc.console.print(t("  [green]✓ {label} configured.[/green]", label=label))
        return


def _lock_holder(root: Path | str):
    """The process serving ``root``, or None. Indirected so callers can stub it."""
    from raven_everos.server import lock_holder

    return lock_holder(root)


def _stop_for_reload(root: Path | str) -> bool:
    """Stop our own server for ``root`` so a rewritten config is actually read.

    EverOS builds its LLM client in the API lifespan, at startup, so a process
    already running keeps the models it booted with. Without this the wizard
    wrote new models to disk and the closing ``ensure_everos_server`` found the
    address answering and returned -- the reconfiguration was inert until some
    unrelated restart, with nothing on screen saying so.

    Not a decision to put to the user: they asked to reconfigure and filled in
    the models a moment ago, and applying them is what that means. Only a server
    raven can identify as serving this root is touched.
    """
    from raven_everos.server import StopOutcome, stop_pid

    holder = _lock_holder(root)
    if holder is None:
        return False
    oc.console.print(t("  [dim]Restarting the service so it picks up the new configuration...[/dim]"))
    # The pid the lock named, not the one the pidfile remembers. Asking the
    # pidfile here would report "not ours" about the very process just
    # identified, which is the state the lock lookup exists to get out of.
    outcome = stop_pid(holder.pid)
    if outcome is not StopOutcome.STOPPED:
        reason = {
            StopOutcome.SIGNAL_FAILED: t("the stop signal could not be delivered"),
            StopOutcome.STILL_DRAINING: t("it is still finishing memory work"),
        }.get(outcome, t("it did not stop"))
        oc.console.print(
            t(
                "  [yellow]! The service is still running ({reason}), so it keeps the models it started with. The new ones take effect the next time it starts.[/yellow]",
                reason=reason,
            ),
            highlight=False,
        )
        return False
    return True


def _port_is_free(port: int) -> bool:
    """Whether a local TCP port can still be bound.

    Out-of-range ports answer False without touching the socket: ``bind``
    raises ``OverflowError`` (not ``OSError``) above 65535, and port 0
    silently binds an ephemeral port, so neither may reach it.
    """
    import socket

    if not (0 < port <= 65535):
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _ask_managed_port(root: Path | str, *, default: int | None = None, force_prompt: bool = False) -> int:
    """Where a raven-managed server should listen.

    Silent while the intended port is free, which is nearly always. 18791 is a
    recommendation rather than a fixed address, but turning that into a screen
    on every fresh install asks for a decision almost nobody has to make. The
    case that had no way forward is the occupied one: the start failed and the
    wizard had nothing to offer, so that is where the question belongs.

    ``default`` overrides what to check, for callers whose intended port is not
    raven's recorded one -- the reuse lane checks the address the root itself
    declares. Without it the offered default is whatever is already recorded,
    not the shipped constant, so a second run does not quietly undo a port the
    user moved to by inviting them to press Enter on the old one.

    A typed answer is checked the same way the initial port was: an occupied or
    out-of-range one asks again, so the question cannot hand the start a port
    it knows is bad. An empty answer keeps the offered default -- accepting it
    is the user's call, and a start into it still meets the failure menu.

    ``force_prompt`` skips the silent pass-through. Change port is an explicit
    request to move, and the start may have failed for reasons no port fixes --
    silently deciding the port is fine and rerunning the identical start makes
    the menu item look broken.
    """
    from raven_everos.server import DEFAULT_EVEROS_BASE_URL

    # Creating a root is the other question, and here a recorded address is the
    # best answer available: ignoring it is what "start the everos server at
    # its configured address, not the default" was about.
    if default is None:
        slice_ = _recorded_memory_slice()
        recorded = slice_.get("port")
        if not (isinstance(recorded, int) and recorded > 0):
            recorded = urlparse(str(slice_.get("base_url") or "")).port
        current = recorded or urlparse(DEFAULT_EVEROS_BASE_URL).port or 18791
    else:
        current = int(default)
    if not force_prompt:
        if _port_is_free(current):
            return current
        # A bind test cannot tell a stranger from our own service. When the
        # holder of this root's lock is the thing listening there, the port is
        # not taken -- it is ours, and the step is about to restart into it.
        holder = _lock_holder(root)
        if holder is not None and holder.port == current:
            return current
        oc.console.print(t("  [yellow]! Port {current} is already in use by something else.[/yellow]", current=current))
    while True:
        answer = _prompt_text(
            t("Memory service port:"),
            default=str(current),
        )
        text = str(answer)
        if not text:
            oc.console.print(t("  [dim]Keeping {current}.[/dim]", current=current))
            return current
        if not text.isdigit():
            oc.console.print(t("  [dim]Not a port ({answer}).[/dim]", answer=answer))
            continue
        port = int(text)
        if not (0 < port <= 65535):
            oc.console.print(t("  [dim]Not a valid port ({port}); range is 1-65535.[/dim]", port=port))
            continue
        if _port_is_free(port):
            return port
        holder = _lock_holder(root)
        if holder is not None and holder.port == port:
            return port
        oc.console.print(t("  [yellow]! Port {port} is also in use.[/yellow]", port=port))


def _retry_or_skip_address() -> str:
    """After a bad address: type another, or give up on memory for now.

    Both ways this is reached -- a port with a typo in it, a server not started
    yet -- are fixed by one more line of input, so ending the step over either
    would send the user back through the whole wizard for a digit. Giving up
    stays available and is what the caller turns into "memory off"; what is not
    on offer is being redirected into a managed setup nobody asked for.
    """
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    choice = questionary.select(
        t("What now?"),
        choices=[
            questionary.Choice(t("Enter a different address"), value="retry"),
            questionary.Choice(
                t("Skip"),
                value="skip",
            ),
        ],
        style=RAVEN_STYLE,
        qmark=oc._QMARK,
    ).ask()
    if choice is None:
        raise typer.Exit(1)
    return str(choice)


def _use_self_managed_everos() -> bool:
    """Point raven at an EverOS the user runs. Returns False if it is unreachable.

    The whole path is two prompts and one probe. Nothing is scanned: an address
    raven guessed is an address the user did not confirm, and this is precisely
    the setup where guessing wrong means writing to, or taking the lock on,
    something that is not raven's.

    No port default. Offering one -- and the only one raven could offer is its
    own -- invites acceptance by reflex, which would point the self-managed path
    at the managed path's address. Someone who runs their own EverOS knows the
    port; someone who does not should be on the other branch.

    Only the address is recorded. Without a root there is no path on disk for a
    later change to write to by accident, which turns the read-only promise from
    a convention into something the code cannot break.
    """
    from raven.config.update import set_plugin_config_fields
    from raven_everos.server import ProbeVerdict, probe_health

    while True:
        host = _prompt_text(t("Host (e.g. 127.0.0.1):"), default="localhost")
        port = _prompt_text(t("Port:"))
        if not port.isdigit():
            oc.console.print(t("  [red]x Not a port: {port}[/red]", port=port))
            if _retry_or_skip_address() == "retry":
                continue
            return False

        base_url = f"http://{host}:{port}"
        oc.console.print(t("  [dim]Checking {base_url}...[/dim]", base_url=base_url))
        result = probe_health(base_url)
        if result is ProbeVerdict.OK:
            break
        oc.console.print(
            t("  [red]x No EverOS answered at {base_url} ({a1}).[/red]", base_url=base_url, a1=result.value),
            highlight=False,
        )
        if _retry_or_skip_address() == "retry":
            continue
        return False

    set_plugin_config_fields(
        "everos-memory",
        {"owned": False, "base_url": base_url},
        # A merging write cannot express "this no longer applies", and a root
        # left behind from a previous managed setup is still exported as
        # EVEROS_ROOT -- pointing raven at a directory it just promised to stop
        # touching. Not recording a path is what makes the promise structural.
        remove=("root", "port"),
    )
    _set_memory_backend("everos")
    caps = " ".join(_capability_lines(base_url))
    oc.console.print(
        t(
            "  [green]v Raven will use the EverOS at {base_url}.[/green]\n      capability  {caps}\n  [dim]You run it: Raven never edits its config and never starts or stops it.\n  If it is down when a session begins, that session has no long-term memory.[/dim]",
            base_url=base_url,
            caps=caps,
        ),
        highlight=False,
    )
    return True


def _capability_lines(base_url: str) -> list[str]:
    """One line per capability the running server actually built.

    "Running" stopped implying "working" in everos 1.2.1: a server whose
    embedding provider failed to build still answers 200 and quietly degrades to
    keyword-only recall.
    """
    from raven_everos.health import (
        DEGRADING_SECTIONS,
        REQUIRED_SECTIONS,
        capability_available,
        probe_capabilities,
    )

    report = probe_capabilities(base_url)
    if not report.reports_capabilities:
        return []
    lines = []
    for section in (*REQUIRED_SECTIONS, *DEGRADING_SECTIONS):
        state = capability_available(report.capabilities, section)
        mark = "[green]✓[/green]" if state is True else "[red]✗[/red]" if state is False else "[dim]?[/dim]"
        lines.append(f"{mark} {section}")
    return lines


def _restart_here(root: Any, target: str) -> bool:
    """Start the service at ``target`` and record the address it now serves.

    Convergence is stop -> write -> start, and the last step has to belong to
    the same function as the first two. Leaving it to "whatever runs later"
    meant that a user who picked ``Keep it enabled`` -- the first option, and the
    answer an existing install gives -- left the wizard with the service stopped:
    it had been shut down to move ports and the branch that would have restarted
    it was never reached.
    """
    import asyncio

    from raven_everos.server import ensure_everos_server

    _set_base_url(target)
    oc.console.print(t("  [dim]Starting the service at {target}...[/dim]", target=target))
    try:
        asyncio.run(ensure_everos_server(target))
    except RuntimeError as exc:
        oc.console.print(
            t("  [red]x Could not start it at {target}: {exc}[/red]", target=target, exc=exc),
            highlight=False,
        )
        return False
    oc.console.print(t("  [green]v EverOS service is running at {target}.[/green]", target=target))
    return True


def _set_base_url(base_url: str) -> None:
    """Cache the address in raven's config, and record the port as the intent.

    ``base_url`` is a cache -- ``<root>/everos.toml`` is what the server reads,
    and this follows it so the runtime does not have to scan for a root on
    every session. ``port`` is the other half: where a managed server is
    *meant* to listen, which is what convergence compares against.

    Written together on purpose. Left to separate callers, the intent was
    recorded by exactly one branch, so every ordinary converge produced an
    address with no intent beside it and the comparison fell back to the
    shipped constant -- reintroducing the behaviour the intent was added to
    prevent.
    """
    from urllib.parse import urlparse

    from raven.config.update import set_plugin_config_fields

    fields: dict[str, Any] = {"base_url": base_url}
    port = urlparse(base_url).port
    if port:
        fields["port"] = int(port)
    set_plugin_config_fields("everos-memory", fields)


def _with_port(target: str, port: int) -> str:
    """``target`` with its port swapped for ``port``, host and scheme kept."""
    parsed = urlparse(target)
    return parsed._replace(netloc=f"{parsed.hostname or 'localhost'}:{port}").geturl()


def _adopt_root(root: Any) -> None:
    """Record ``root`` as raven's own, retracting an address that was not raven's.

    Ownership is the lane the user picked, not something discovery can observe:
    asking raven to run everos makes the directory raven's whatever an earlier
    run recorded there.

    The retraction is the other half. A merging write cannot say "this address
    no longer applies", and the address recorded for a server the user runs is
    exactly what :func:`_ask_managed_port` falls back to -- so leaving it behind
    parks raven's own service on the user's port, one screen after taking the
    job over. A managed port the user deliberately moved to is kept.
    """
    from raven.config.update import set_plugin_config_fields

    theirs = _recorded_memory_slice().get("owned") is False
    set_plugin_config_fields(
        "everos-memory",
        {"root": str(root), "owned": True},
        remove=("base_url", "port") if theirs else (),
    )


def _memory_source_menu() -> str:
    """The one question this step asks: who runs EverOS.

    Everything else follows from the answer -- ownership above all, which is why
    no later screen has to infer it from a directory that happens to exist.
    Skipping is an answer here rather than something reachable only by walking
    into a lane and backing out of it.
    """
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    choice = questionary.select(
        t("Where should long-term memory come from?"),
        choices=[
            questionary.Choice(t("Let Raven run EverOS for me"), value="managed"),
            questionary.Choice(
                t("I run my own EverOS -- connect to it"),
                value="self",
            ),
            questionary.Choice(t("Skip for now"), value="skip"),
        ],
        style=RAVEN_STYLE,
        qmark=oc._QMARK,
    ).ask()
    if choice is None:
        raise typer.Exit(1)
    return str(choice)


def _found_root_menu(state: Any) -> str:
    """Take the found root as it is, reconfigure it, or go back.

    Asked before anything is written or signalled. The wizard used to converge
    the address first and ask afterwards, so a user who only wanted to confirm
    an existing setup had the service stopped and restarted on a different port
    before the question appeared.
    """
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    where = state.declared_url or t("(no address declared)")
    if state.serving:
        status = t("running at {where}", where=where)
    elif state.busy_elsewhere:
        status = t("its data is in use, but not at that address")
    else:
        status = t("not running ({where} declared)", where=where)
    oc.console.print()
    oc.console.print(
        t(
            "  [green]v Found a memory directory Raven can take over[/green]\n      memory dir  {a0}\n      state       {status}",
            a0=state.root,
            status=status,
        ),
        highlight=False,
    )
    choice = questionary.select(
        t("What would you like to do?"),
        choices=[
            questionary.Choice(t("Use it as it is"), value="reuse"),
            questionary.Choice(
                t("Reconfigure it (port and models)"),
                value="redo",
            ),
            questionary.Choice(t("Back"), value="back"),
        ],
        style=RAVEN_STYLE,
        qmark=oc._QMARK,
    ).ask()
    if choice is None:
        raise typer.Exit(1)
    return str(choice)


def _use_found_root(state: Any) -> None:
    """Serve memory from ``state.root`` at whatever address it is already on.

    Nothing is stopped, moved or reconfigured: the user asked to use this
    directory as it is, and the address its own server answers on is the answer.

    The declared address is still checked before starting into it: a listener
    that does not answer /health reads as "not running" to discovery, so the
    occupied-port question that the fresh-build lane asks is asked here too.
    A start that fails anyway -- free at the check, taken by bind -- offers
    retry, a different port, or skipping memory for this session.

    One memory directory admits one engine, so a root whose data is held by
    something raven cannot reach over HTTP has no way forward here. That one is
    reported -- with the pid and the command line -- instead of being worked
    around by starting a second instance that could only fail on the lock.
    """
    if state.serving:
        _set_base_url(str(state.declared_url))
        _set_memory_backend("everos")
        _report_everos_capabilities()
        return

    if state.busy_elsewhere:
        holder = _lock_holder(state.root)
        if holder is not None and holder.port:
            found_at = f"http://localhost:{holder.port}"
            oc.console.print(
                t("  [dim]It is answering at {found_at} (pid {a1}).[/dim]", found_at=found_at, a1=holder.pid),
                highlight=False,
            )
            _set_base_url(found_at)
            _set_memory_backend("everos")
            _report_everos_capabilities()
            return
        detail = f"  [dim]{holder.cmdline}[/dim]\n" if holder is not None else ""
        pid_line = t("pid {pid} holds", pid=holder.pid) if holder is not None else t("something holds")
        oc.console.print(
            t(
                "  [yellow]! {pid_line} {a1} but serves no HTTP.[/yellow]\n{detail}  [dim]Stop it and re-run `raven onboard`.[/dim]",
                pid_line=pid_line,
                a1=state.root,
                detail=detail,
            ),
            highlight=False,
        )
        return

    target = state.declared_url or _configured_target_url()
    target = _with_port(target, _ask_managed_port(state.root, default=urlparse(target).port))
    while True:
        if _restart_here(state.root, target):
            _set_memory_backend("everos")
            _report_everos_capabilities()
            return
        questionary = oc._require_questionary()
        from raven.cli._styles import RAVEN_STYLE

        action = questionary.select(
            t("What to do?"),
            choices=[
                questionary.Choice(t("Retry"), value="retry"),
                questionary.Choice(t("Change port"), value="change"),
                questionary.Choice(t("Skip"), value="skip"),
            ],
            style=RAVEN_STYLE,
            qmark=oc._QMARK,
        ).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "skip":
            return
        if action == "change":
            target = _with_port(target, _ask_managed_port(state.root, default=urlparse(target).port, force_prompt=True))


def _step4_memory(
    *, skip: bool, non_interactive: bool, main_model: Optional[str], warnings: list[str], skip_test: bool = False
) -> object:
    """Step 4 -- EverOS long-term memory.

    Three lanes, asked once: raven runs everos, the user runs it, or neither
    happens today. Which lane decides ownership, so no later screen has to read
    it back off a directory that happens to exist -- the mistake behind a managed
    reconfigure overwriting an address raven had promised not to touch.

    The managed lane is the one that must always land somewhere usable: it takes
    over whatever memory directory it finds, or builds its own. The self-managed
    lane writes only after a probe answers, and a refused address returns to the
    lane question with nothing written. Skipping leaves the config as it is.

    ``None`` means no long-term memory this session, not a fallback to something
    simpler. ``_memory_enabled`` gates on the llm role alone, so a seeded but
    modelless config reads as "not configured yet"; embedding and rerank are
    offered but never gate, since skipping them costs recall quality rather than
    memory itself.
    """
    oc._step_header(4, t("EverOS long-term memory"))

    import sys

    if sys.platform == "win32":
        oc.console.print(
            t(
                "  [yellow]⚠ EverOS memory engine does not support native Windows.[/yellow]\n"
                "  [dim]Run Raven inside WSL for full memory support.[/dim]\n"
                "  [dim]Skipping memory configuration.[/dim]"
            )
        )
        _set_memory_backend(None)
        return None

    if not everos_plugin_installed():
        # Nothing is written. Every lane below configures, starts or probes a
        # service this install does not carry, and turning the backend off here
        # would answer for the user a question only installing the plugin
        # settles.
        oc.console.print(
            t(
                "  [yellow]⚠ Long-term memory cannot be configured in this installation.[/yellow]\n  [dim]{note}[/dim]",
                note=everos_plugin_missing_note(),
            ),
            highlight=False,
        )
        return None

    if skip or non_interactive:
        # Never configured the required models here → disable backend-driven
        # memory so runtime doesn't activate EverOS without an llm/embedding.
        # (``_memory_enabled`` already gates on both required models, so an
        # already-enabled+configured setup is preserved.)
        if not _memory_enabled():
            _set_memory_backend(None)
        oc.console.print(
            t(
                "  [dim]Long-term memory stays off.[/dim]\n"
                "  [dim]Run `raven onboard` again whenever you want to configure it.[/dim]"
            )
        )
        return None

    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven_everos import roots

    while True:
        source = _memory_source_menu()

        if source == "skip":
            # Same rule as ``--skip-memory``: a configured setup is left exactly
            # as it is, and a seeded-but-modelless one resolves to off so the
            # runtime does not activate everos with no models behind it.
            if not _memory_enabled():
                _set_memory_backend(None)
            oc.console.print(
                t(
                    "  [dim]Long-term memory left as it is.[/dim]\n"
                    "  [dim]Run `raven onboard` again whenever you want to configure it.[/dim]"
                )
            )
            return None

        if source == "self":
            if _use_self_managed_everos():
                return None
            # A refused address is a server not started or a port mistyped, not
            # a change of mind: back to the one question this step asks. Nothing
            # was written, so the setup that was working a moment ago still is.
            continue

        oc.console.print(t("  [dim]Looking for a memory directory Raven can take over...[/dim]"))
        from raven.config.update_everos import applicable_legacy_root, default_everos_root, recorded_slice

        # The wizard owns the candidate list: it reads the config record and the
        # host defaults and hands them to the cargo-side describer, which
        # imports nothing from the host.
        recorded = recorded_slice().get("root")
        found = roots.pick(
            roots.discover(
                recorded_root=Path(str(recorded)).expanduser() if recorded else None,
                fallback_roots=(default_everos_root(), applicable_legacy_root()),
            )
        )
        if found is None:
            break

        action = _found_root_menu(found)
        if action == "back":
            # Nothing is recorded until an answer other than "back", so this
            # leaves the config untouched.
            continue
        _adopt_root(found.root)
        if action == "reuse":
            _use_found_root(found)
            return None
        break

    if not _everos_role_configured("llm"):
        # embedding and rerank are both skippable, so what each one buys has to
        # be on screen before the first prompt -- otherwise the roles read as
        # three questions of equal weight.
        # Wrapped by hand: rich re-wraps at the terminal width and drops the
        # two-space indent on continuation lines, which reads as a stray
        # left-flush sentence under an indented block.
        oc.console.print(
            t(
                "  [dim]Raven's long-term memory comes from EverOS. What it can do grows with\n"
                "  what you configure:[/dim]\n"
                "  [dim]    memory LLM only    conversations become memories; recall matches keywords[/dim]\n"
                "  [dim]  + memory embedding   recall matches meaning, not wording (strongly advised)[/dim]\n"
                "  [dim]  + memory rerank      recall ordering gets sharper[/dim]"
            ),
            highlight=False,
        )

    # Ensure the EverOS home directory has its config templates (everos.toml
    # + ome.toml) BEFORE writing model sections — set_everos_section merges
    # into the template so default sections (memory/sqlite/lancedb/api) are
    # preserved. Also creates ome.toml which the runtime requires.
    from raven.config.update_everos import configure_everos_env, ensure_everos_home, owned_everos_root

    # owned_everos_root, not everos_root: after a user declined to share theirs,
    # the recorded root is still theirs, and building there would adopt it.
    root = owned_everos_root()
    _adopt_root(root)
    configure_everos_env(root)
    ensure_everos_home(root)

    # Only when the default is taken. A port screen on every fresh install
    # would be a decision almost nobody has to make; the case that had no way
    # forward is the occupied one, where the start simply failed and the wizard
    # had nowhere to send the user.
    _set_base_url(f"http://localhost:{_ask_managed_port(root)}")

    # Configure required models FIRST, then flip the backend on — so a Ctrl+C
    # mid-configuration leaves backend at its prior (disabled) value rather
    # than an enabled-but-modelless state.
    for _role in ("llm", "embedding", "rerank", "multimodal"):
        # Each role prints one leading blank before its own header, so no extra
        # separator here — avoids the double blank line between roles.
        outcome = _config_everos_role(
            section=_role,
            main_model=main_model,
            non_interactive=non_interactive,
            warnings=warnings,
            skip_test=skip_test,
        )
        if outcome is oc._ABORT_EVEROS:
            _set_memory_backend(None)
            oc.console.print(
                t(
                    "  [yellow]⚠ Gave up long-term memory: Raven will not remember anything "
                    "between sessions.[/yellow]\n"
                    "  [dim]Run `raven onboard` again whenever you want to configure it.[/dim]"
                )
            )
            return None

    # Verify EverOS server is reachable (auto-starts if needed)
    import asyncio

    from raven.config.raven import load_raven_config
    from raven_everos.health import configured_base_url
    from raven_everos.server import ensure_everos_server

    # The configured address, not the default: the memory backend connects to
    # whatever ``plugins.config`` names, so probing 18791 on a setup that moved
    # everos elsewhere reports on a server nobody uses -- and then spawns a
    # second instance that cannot hold the OME lock.
    base_url = configured_base_url(load_raven_config())

    # The models were just written; a process already running booted with the
    # old ones and will not re-read them.
    _stop_for_reload(root)

    oc.console.print()
    oc.console.print(t("  [dim]Starting EverOS service...[/dim]"))
    # A failed start is not a decision to abandon long-term memory. The models
    # are already on disk at this point, so "defer" keeps the whole
    # configuration and lets the runtime start the service on the next session;
    # only the explicit third choice turns memory off.
    while True:
        try:
            asyncio.run(ensure_everos_server(base_url))
            oc.console.print(t("  [green]✓ EverOS service is running.[/green]"))
            break
        except RuntimeError as exc:
            oc.console.print(t("  [red]✗ EverOS service failed to start: {exc}[/red]", exc=exc))
            action = questionary.select(
                t("What to do?"),
                choices=[
                    questionary.Choice(t("Retry"), value="retry"),
                    questionary.Choice(t("Change port"), value="change"),
                    questionary.Choice(
                        t("Leave it for later (settings kept, Raven retries next start)"),
                        value="defer",
                    ),
                    questionary.Choice(
                        t("Turn long-term memory off"),
                        value="disable",
                    ),
                ],
                style=RAVEN_STYLE,
                qmark=oc._QMARK,
            ).ask()
            if action is None:
                raise typer.Exit(1) from exc
            if action == "retry":
                continue
            if action == "change":
                base_url = _with_port(
                    base_url, _ask_managed_port(root, default=urlparse(base_url).port, force_prompt=True)
                )
                _set_base_url(base_url)
                continue
            if action == "defer":
                _set_memory_backend("everos")
                oc.console.print(
                    t(
                        "  [yellow]! Memory settings kept. Raven will try to start the "
                        "service again on the next session.[/yellow]"
                    )
                )
                return None
            _set_memory_backend(None)
            oc.console.print(
                t(
                    "  [yellow]! Long-term memory turned off. Run `raven onboard` "
                    "again whenever you want it back.[/yellow]"
                )
            )
            return None
    _report_everos_capabilities()
    _set_memory_backend("everos")
    return None


def _report_everos_capabilities() -> None:
    """Say what the running server can actually do, not just that it answers.

    ``ensure_everos_server`` proves the process is up and nothing more. Since
    everos 1.2.1 a server whose embedding provider failed to build still answers
    200 and degrades to keyword-only search, so stopping at "running" would
    print a tick over an install that cannot recall anything. The roles were
    each verified against their provider earlier in this step; what is new here
    is whether everos itself could build them from what got written to
    ``everos.toml``.

    Silent on a server too old to report capabilities -- reading that silence as
    "unavailable" would condemn a working install.
    """
    from raven.config.raven import load_raven_config
    from raven_everos.health import (
        DEGRADING_SECTIONS,
        REQUIRED_SECTIONS,
        configured_base_url,
        probe_capabilities,
    )

    # The configured address, not the default: probing the wrong port reports on
    # a server nobody is using, and reads as "not running".
    report = probe_capabilities(configured_base_url(load_raven_config()))
    if not report.reports_capabilities:
        return
    configured = [s for s in (*REQUIRED_SECTIONS, *DEGRADING_SECTIONS) if _everos_role_configured(s)]
    broken = [s for s in configured if report.available(s) is False]
    if not broken:
        names = " and ".join(configured)
        oc.console.print(
            t("  [green]✓ {names} {a1} available.[/green]", names=names, a1="is" if len(configured) == 1 else "are")
        )
        return
    names = " and ".join(broken)
    oc.console.print(
        t(
            "  [yellow]⚠ {names} is configured but EverOS could not build it.[/yellow]\n  [dim]Memory runs degraded until this is fixed.[/dim]\n  [dim]Check: {a1}[/dim]",
            names=names,
            a1=_everos_server_log_hint(),
        )
    )


def _everos_server_log_hint() -> str:
    from raven_everos.server import server_log_path

    return str(server_log_path())
