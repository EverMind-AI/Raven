"""EverOS long-term memory cluster of the onboard wizard (Step 4).

Split out of ``onboard_commands`` because that module had grown past 5000
lines; this file owns EverOS role configuration (llm / embedding / rerank /
multimodal) end to end. Shared wizard UI state (``console``, ``_t``, ``_BACK``,
``_QMARK``, questionary helpers, ...) still lives in ``onboard_commands`` --
this module reaches it via the ``oc`` module reference (not a value import) so
that test monkeypatches on ``onboard_commands`` attributes keep working
whichever module a caller patches through.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from raven.cli import onboard_commands as oc


def _set_memory_backend(backend: Optional[str]) -> None:
    """Set ``memory.backend`` (``"everos"`` / ``None``) via the ops layer."""
    from raven.config.update import set_memory_backend

    set_memory_backend(backend)


def _init_extension_block_defaults() -> None:
    """Seed the memory / plugins / skillForge extension defaults via the ops layer."""
    from raven.config.update import init_extension_block_defaults

    init_extension_block_defaults()


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
    return _everos_role_configured("llm")


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
    continue_hint: Optional[tuple[str, str]] = None,
) -> bool:
    """Probe the memory LLM with a real chat completion, offering retry/continue on failure."""
    oc.console.print(oc._t(f"  [dim]⏳ Verifying {label}…[/dim]", f"  [dim]⏳ 正在验证 {label}…[/dim]"))
    ok, detail = _probe_everos_chat(model, api_key=api_key, base_url=base_url)
    if ok:
        oc.console.print(oc._t(f"  [green]✓ {label} connected.[/green]", f"  [green]✓ {label} 连接成功。[/green]"))
        return True
    oc.console.print(
        oc._t(
            f"  [yellow]✗ Couldn't verify {label}: {detail}[/yellow]",
            f"  [yellow]✗ 验证失败 {label}:{detail}[/yellow]",
        )
    )
    if continue_hint:
        cont_label = oc._t(f"Continue anyway ({continue_hint[0]})", f"仍然继续({continue_hint[1]})")
    else:
        cont_label = oc._t("Continue anyway", "仍然继续")
    choice = oc._failure_choice(
        [
            (oc._t("Re-enter", "重新填写"), "rekey"),
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
    continue_hint: Optional[tuple[str, str]] = None,
) -> bool:
    """Probe a rerank endpoint with a provider-specific request, offering retry/continue on failure."""
    oc.console.print(oc._t(f"  [dim]⏳ Verifying {label}…[/dim]", f"  [dim]⏳ 正在验证 {label}…[/dim]"))
    ok, detail = _probe_rerank(model, api_key=api_key, base_url=base_url, rerank_provider=rerank_provider)
    if ok:
        oc.console.print(oc._t(f"  [green]✓ {label} connected.[/green]", f"  [green]✓ {label} 连接成功。[/green]"))
        return True
    oc.console.print(
        oc._t(
            f"  [yellow]✗ Couldn't verify {label}: {detail}[/yellow]",
            f"  [yellow]✗ 验证失败 {label}:{detail}[/yellow]",
        )
    )
    if continue_hint:
        cont_label = oc._t(f"Continue anyway ({continue_hint[0]})", f"仍然继续({continue_hint[1]})")
    else:
        cont_label = oc._t("Continue anyway", "仍然继续")
    choice = oc._failure_choice(
        [
            (oc._t("Re-enter", "重新填写"), "rekey"),
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
        oc.console.print(
            oc._t(
                "  [dim]⏳ Checking embedding dimension…[/dim]",
                "  [dim]⏳ 正在检测 embedding 维度…[/dim]",
            )
        )
        result = _probe_embedding_dim(url, headers, model)

        if result == _REQUIRED_EMBEDDING_DIM:
            oc.console.print(
                oc._t(
                    f"  [green]✓ Supports {result}-dim.[/green]",
                    f"  [green]✓ 支持 {result} 维。[/green]",
                )
            )
            return True

        if isinstance(result, int) and result < _REQUIRED_EMBEDDING_DIM:
            oc.console.print(
                oc._t(
                    f"  [red]✗ Dimension too small: model outputs {result}-dim, "
                    f"EverOS requires >= {_REQUIRED_EMBEDDING_DIM}. Please pick another model.[/red]",
                    f"  [red]✗ 维度不足：模型输出 {result} 维，"
                    f"EverOS 要求 >= {_REQUIRED_EMBEDDING_DIM} 维，请重新选择。[/red]",
                )
            )
            return False

        if isinstance(result, int) and result > _REQUIRED_EMBEDDING_DIM:
            oc.console.print(
                oc._t(
                    f"  [red]✗ Model outputs {result}-dim and does not support the "
                    f"dimensions parameter to truncate to {_REQUIRED_EMBEDDING_DIM}. "
                    "Please pick another model.[/red]",
                    f"  [red]✗ 模型输出 {result} 维，且不支持 dimensions 参数"
                    f"截断到 {_REQUIRED_EMBEDDING_DIM} 维，请重新选择。[/red]",
                )
            )
            return False

        oc.console.print(
            oc._t(
                f"  [yellow]✗ Couldn't verify dimension: {result}[/yellow]",
                f"  [yellow]✗ 无法验证维度：{result}[/yellow]",
            )
        )
        if non_interactive:
            return False
        choice = oc._failure_choice(
            [
                (oc._t("Retry", "重试"), "retry"),
                (oc._t("Re-enter", "重新选择"), "rekey"),
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
        "label_zh": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "supports": {"llm", "embedding", "multimodal"},
    },
    {
        "name": "openrouter",
        "label": "OpenRouter",
        "label_zh": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "supports": {"llm", "embedding", "rerank", "multimodal"},
        "rerank_provider": "vllm",
    },
    {
        "name": "deepseek",
        "label": "DeepSeek",
        "label_zh": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "supports": {"llm"},
    },
    {
        "name": "deepinfra",
        "label": "DeepInfra",
        "label_zh": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_provider": "deepinfra",
        "rerank_base_url": "https://api.deepinfra.com/v1/inference",
    },
    {
        "name": "siliconflow",
        "label": "SiliconFlow",
        "label_zh": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_provider": "vllm",
    },
    {
        "name": "dashscope",
        "label": "DashScope (Alibaba)",
        "label_zh": "阿里百炼 DashScope",
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
        "label": ("Memory LLM", "记忆 LLM"),
        "example": "gpt-4.1-mini",
        "optional": False,
        "verify": True,
        "purpose": (
            "Reads each conversation to judge what matters and extract the key points.",
            "从对话中判断信息边界、抽取要点。",
        ),
        # Worded as a floor rather than a default: the field is pre-filled with
        # the user's own main model, because a recommended id is only reachable
        # if their key carries it. This tells them how to judge their own.
        "recommendation": (
            "Capability floor: [bold]gpt-4.1-mini[/bold] -- weaker models degrade extraction",
            "能力下限参考 [bold]gpt-4.1-mini[/bold]：低于这个水平会明显影响提取质量",
        ),
        "continue_hint": ("memory extraction may fail", "记忆抽取可能失败"),
    },
    "embedding": {
        "label": ("Memory embedding", "记忆 embedding"),
        "example": "Qwen/Qwen3-Embedding-4B",
        # Optional in the sense that memory still functions without it: the
        # adapter drops to KEYWORD search, which needs no vectors. Strongly
        # advised all the same -- lexical recall misses a memory the moment the
        # user phrases the question differently.
        "optional": True,
        "verify": True,
        "purpose": (
            "Turns text into vectors so memories are found by meaning, not just keywords.",
            "把文字转成向量，让记忆能按「意思」检索，而不只是按关键词。",
        ),
        "tag": (
            "[accent](optional, strongly advised)[/accent]",
            "[accent]（可选，强烈建议配置）[/accent]",
        ),
        "cost": (
            "Without it: rephrase a question and it may miss a memory you have;\n  recall can only match keywords.",
            "不配置：换个说法提问就可能找不到已有记忆，记忆召回时只能使用关键词检索。",
        ),
        "recommendation": (
            "Recommended: [bold]Qwen/Qwen3-Embedding-4B[/bold] -- must be [bold yellow]1024-dim[/bold yellow],\n"
            "  Chinese + English",
            "推荐 [bold]Qwen/Qwen3-Embedding-4B[/bold]，需 [bold yellow]1024 维[/bold yellow]且支持中英文的模型",
        ),
        "continue_hint": ("semantic recall will be unavailable", "语义召回将不可用"),
        "skip_note": (
            "  [yellow]! Skipped: recall will match keywords, not meaning.[/yellow]\n"
            "  [dim]Phrase a question differently and it may miss a memory you have.\n"
            "  Configure it later, then run `everos cascade backfill`.[/dim]",
            "  [yellow]⚠ 已跳过：召回将按关键词匹配，而非按语义。[/yellow]\n"
            "  [dim]换一种说法提问，就可能找不到已有的记忆。\n"
            "  日后配好后运行 everos cascade backfill 可为已存记忆补上向量。[/dim]",
        ),
    },
    "rerank": {
        "label": ("Memory rerank", "记忆 rerank"),
        "example": "Qwen/Qwen3-Reranker-4B",
        "optional": True,
        "verify": True,
        "purpose": (
            "Re-ranks what semantic search found so the best match comes first, at a small\n  latency cost.",
            "在语义召回一批候选后再精排一遍，让最相关的排在最前，会略增延迟。",
        ),
        "tag": (
            "[accent](optional, advised)[/accent]",
            "[accent]（可选，建议配置）[/accent]",
        ),
        "recommendation": (
            "Recommended: [bold]Qwen/Qwen3-Reranker-4B[/bold]",
            "推荐 [bold]Qwen/Qwen3-Reranker-4B[/bold]",
        ),
        "continue_hint": ("rerank quality may degrade", "rerank 精度可能下降"),
        "skip_note": (
            "  [dim]Skipped rerank; memory retrieval still works.[/dim]",
            "  [dim]已跳过 rerank，记忆检索仍可用。[/dim]",
        ),
    },
    "multimodal": {
        "label": ("Memory multimodal", "记忆多模态"),
        "example": "google/gemini-3-flash-preview",
        "optional": True,
        "verify": True,
        "purpose": (
            "Lets Raven understand and recall images / PDFs / audio as memory.",
            "让 Raven 把图片 / PDF / 音频也作为记忆来理解和检索。",
        ),
        "cost": (
            "Without it: those files stay out of memory. Having such files is not the same\n"
            "  as needing them remembered -- configure it when you do.",
            "不配置：这类文件不进入记忆；有这类文件并不等于需要，确有此需求时再配即可。",
        ),
        "recommendation": (
            "Recommended: [bold]google/gemini-3-flash-preview[/bold]",
            "推荐 [bold]google/gemini-3-flash-preview[/bold]",
        ),
        "skip_note": (
            "  [dim]Skipped; nothing else is affected -- configure it if you come to need\n  multimodal memory.[/dim]",
            "  [dim]已跳过；其余功能不受影响，日后确有把多模态内容纳入记忆的需求时再配即可。[/dim]",
        ),
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
    """Find the best match for ``example`` in the fetched model list.

    The example (e.g. ``gpt-4.1-mini``) is a bare model name, while
    ``models`` may carry provider prefixes (``openai/gpt-4.1-mini``).
    Returns the first model whose id ends with ``/example`` or equals
    ``example`` exactly; falls back to the bare example string so the
    autocomplete input is pre-filled even if no exact match exists.
    """
    lower = example.lower()
    suffix = f"/{lower}"
    for mid in models:
        if mid.lower() == lower or mid.lower().endswith(suffix):
            return mid
    return example


def _preferred_memory_model(section: str, main_model: Optional[str], chosen_provider: Optional[str]) -> Optional[str]:
    """The main chat model, when it is a sensible pre-fill for this role.

    Only the llm role -- an embedding / rerank / multimodal endpoint does not
    serve a chat model. Only when the picked provider is the main model's own: no
    other provider carries that id, and pre-filling one it cannot serve turns
    Enter into a verification failure. A custom endpoint has no resolved provider
    and is left alone for the same reason.
    """
    if section != "llm" or not main_model or chosen_provider is None:
        return None
    if chosen_provider != _resolve_model_provider(main_model):
        return None
    return _resolve_reuse_llm_creds(main_model).get("model")


def _everos_pick_model(
    *,
    base_url: Optional[str],
    api_key: Optional[str],
    example: str,
    allow_back: bool,
    section: str = "llm",
    provider_name: Optional[str] = None,
    recommendation: Optional[tuple[str, str]] = None,
    preferred: Optional[str] = None,
) -> Any:
    """Pick a model id for an EverOS endpoint: fetch ``/models`` for a
    fuzzy-searchable list, else fall back to free text. Empty submit = back.

    ``preferred`` pre-fills a model the user is already known to have access to
    -- their main chat model. It wins over ``example`` because a recommended
    model is only a recommendation if the user's key can reach it, and many keys
    cannot; ``example`` then reads as the capability floor rather than the
    default (see ``recommendation``).
    """
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    oc.console.print(oc._t("  [dim]⏳ Loading models…[/dim]", "  [dim]⏳ 正在拉取模型列表…[/dim]"))
    models = _fetch_everos_models(base_url, api_key, section=section, provider_name=provider_name)
    if preferred:
        oc.console.print(
            oc._t(
                f"  [dim]Pre-filled with your main model [bold]{preferred}[/bold] -- press Enter to accept.[/dim]",
                f"  [dim]已填入你的主模型 [bold]{preferred}[/bold]，直接回车即可。[/dim]",
            )
        )
    if recommendation:
        oc.console.print(f"  [dim]{oc._t(*recommendation)}[/dim]")
    if models:
        default_model = preferred or _match_everos_default(example, models)
        question = questionary.autocomplete(
            oc._t(
                f"Model ({len(models)} available — type to filter):",
                f"模型(共 {len(models)} 个 — 输入可筛选):",
            ),
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
        oc.console.print(
            oc._t(
                "  [dim]Couldn't list models from this endpoint — type the id manually.[/dim]",
                "  [dim]该端点拉不到模型列表 — 请手动输入模型 id。[/dim]",
            )
        )
        chosen = questionary.text(
            oc._t(f"Model id (e.g. {example}):", f"模型 id（如 {example}）："),
            default=preferred or "",
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
    recommendation: Optional[tuple[str, str]] = None,
) -> Any:
    """Mirror the main provider step for one EverOS model: pick a source
    (curated provider / custom) → API key → model. Returns a dict with
    ``model`` / ``api_key`` / ``base_url`` (plus ``provider`` for rerank), or
    ``oc._BACK`` when the user backs out of the source picker. Empty submit on any
    field rewinds one step."""
    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    llm_section = _everos_section("llm")

    # For the LLM role, default to the main chat model's provider.
    # For other roles (embedding/rerank/multimodal), default to whichever
    # provider the LLM step just configured — the user likely has the
    # same API key and only needs to pick a different model.
    if section == "llm":
        default_provider = _resolve_model_provider(main_model or "")
        reuse_source = "main"
    else:
        default_provider = _match_provider_by_url(llm_section.get("base_url"))
        reuse_source = "llm"

    while True:  # source picker — a field-level back rewinds here
        choices: list[Any] = []
        default_choice = None
        for prov in _EVEROS_PROVIDERS:
            if section not in prov.get("supports", set()):
                continue
            is_default = default_provider is not None and prov["name"] == default_provider
            if is_default:
                if reuse_source == "main":
                    label = oc._t(
                        f"{prov['label']} (main model provider, reuse Key)",
                        f"{prov['label_zh']}（主模型服务商，复用 Key）",
                    )
                else:
                    label = oc._t(
                        f"{prov['label']} (memory LLM provider, reuse Key)",
                        f"{prov['label_zh']}（记忆 LLM 服务商，复用 Key）",
                    )
            else:
                label = oc._t(prov["label"], prov["label_zh"])
            choice = questionary.Choice(label, value=("provider", prov))
            choices.append(choice)
            if is_default:
                default_choice = choice.value
        choices.append(
            questionary.Choice(
                oc._t("Other (custom OpenAI-compatible endpoint)", "其他(自定义 OpenAI 兼容端点)"),
                value=("custom",),
            )
        )
        choices.append(questionary.Separator())
        choices.append(questionary.Choice(oc._t("Back", "返回"), value=oc._BACK))

        src = questionary.select(
            oc._t("Pick a provider (or reuse / custom):", "选择服务商(或复用 / 自定义):"),
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
            base_url = src[1]["base_url"]
            prefilled_key: Optional[str] = None
            if default_provider == src[1]["name"]:
                if reuse_source == "main":
                    prefilled_key = _resolve_reuse_llm_creds(main_model or "").get("api_key")
                else:
                    prefilled_key = llm_section.get("api_key")
            if prefilled_key:
                if reuse_source == "main":
                    oc.console.print(
                        oc._t(
                            "  [dim]API key reused from main chat model.[/dim]",
                            "  [dim]已复用主对话模型的 API Key。[/dim]",
                        )
                    )
                else:
                    oc.console.print(
                        oc._t(
                            "  [dim]API key reused from memory LLM.[/dim]",
                            "  [dim]已复用记忆 LLM 的 API Key。[/dim]",
                        )
                    )
                api_key = prefilled_key
            else:
                api_key = oc._prompt_api_key(src[1]["name"], allow_back=True)
                if api_key is oc._BACK:
                    continue
        else:  # custom
            base_url = _prompt_text(oc._t("Base URL (must include /v1):", "Base URL(需包含 /v1):"), allow_back=True)
            if base_url is oc._BACK:
                continue
            api_key = _prompt_text(oc._t("API key (hidden):", "API Key(隐藏输入):"), secret=True, allow_back=True)
            if api_key is oc._BACK:
                continue

        # Guard against a source that resolved to an empty key / endpoint —
        # set_everos_section drops None values, which would otherwise persist a
        # section with a model but no usable endpoint.
        if not (api_key and base_url):
            oc.console.print(
                oc._t(
                    "  [yellow]✗ Missing API key or Base URL for this source — pick another.[/yellow]",
                    "  [yellow]✗ 该来源缺少 API Key 或 Base URL — 请换一个。[/yellow]",
                )
            )
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
                    oc._t("Rerank service type:", "rerank 服务类型:"),
                    choices=[
                        questionary.Choice("deepinfra", value="deepinfra"),
                        questionary.Choice("vllm", value="vllm"),
                        questionary.Choice("dashscope", value="dashscope"),
                        questionary.Choice(oc._t("Back", "返回"), value=oc._BACK),
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
            preferred=_preferred_memory_model(section, main_model, chosen_provider),
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
    label_en, label_zh = role["label"]
    purpose_en, purpose_zh = role["purpose"]
    optional = role["optional"]
    verify_label = oc._t(label_en, label_zh)

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
    tag_markup = oc._t(*role["tag"]) if role.get("tag") else oc._t("[dim](optional)[/dim]", "[dim]（可选）[/dim]")
    lines = [f"  [bold][accent]{oc._t(label_en, label_zh)}[/accent][/bold]" + (f" {tag_markup}" if optional else "")]
    lines.append(f"  [dim]{oc._t(purpose_en, purpose_zh)}[/dim]")
    if role.get("cost"):
        lines.append(f"  [dim]{oc._t(*role['cost'])}[/dim]")
    oc.console.print()
    # highlight=False so Rich's default highlighter doesn't tint the dim prose
    # (parens/numbers/words) and make an informational hint read like an error.
    oc.console.print("\n".join(lines), highlight=False)

    while True:  # role-menu loop — a back-out of the source picker returns here
        current = _everos_section(section).get("model") if _everos_role_configured(section) else None
        if current:
            choices = [
                questionary.Choice(oc._t(f"Keep current: {current}", f"沿用当前:{current}"), value="keep"),
                questionary.Choice(oc._t("Reconfigure", "重新配置"), value="redo"),
            ]
            if optional:
                choices.append(questionary.Choice(oc._t("Skip", "跳过"), value="off"))
            action = questionary.select(
                oc._t("Already configured — what now?", "已配置,怎么处理?"),
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
                oc.console.print(oc._t(f"  [dim]{label_en} skipped.[/dim]", f"  [dim]已跳过 {label_zh}。[/dim]"))
                return
        elif optional:
            action = questionary.select(
                oc._t("Configure it?", "要配置吗?"),
                choices=[
                    questionary.Choice(oc._t("Configure", "配置"), value="redo"),
                    questionary.Choice(oc._t("Skip", "跳过"), value="skip"),
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
                note_en, note_zh = role.get(
                    "skip_note", (f"  [dim]Skipped {label_en}.[/dim]", f"  [dim]已跳过 {label_zh}。[/dim]")
                )
                oc.console.print(oc._t(note_en, note_zh), highlight=False)
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
                oc._t(
                    f"  [yellow]⚠ {label_en} is required for long-term memory.[/yellow]\n"
                    "  [dim]Without it Raven has no memory across sessions: every conversation starts\n"
                    "  from nothing, with no recollection of your preferences or of what was done before.[/dim]",
                    f"  [yellow]⚠ {label_zh} 是长期记忆的必需项。[/yellow]\n"
                    "  [dim]放弃后 Raven 没有任何跨会话记忆：每次对话都从零开始，不记得你的偏好，\n"
                    "  也不记得之前做过什么。[/dim]",
                ),
                highlight=False,
            )
            action = questionary.select(
                oc._t("What would you like to do?", "想做什么？"),
                choices=[
                    questionary.Choice(oc._t("Pick a provider / model", "选择服务商 / 模型"), value="retry"),
                    questionary.Choice(
                        oc._t("Give up (no long-term memory)", "放弃（不启用长期记忆）"),
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
                oc._t(
                    f"  [dim]Skipping the {verify_label} test call (--skip-test).[/dim]",
                    f"  [dim]已跳过 {verify_label} 的测试调用(--skip-test)。[/dim]",
                )
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
        oc.console.print(
            oc._t(
                f"  [green]✓ {label_en} configured.[/green]",
                f"  [green]✓ 已配置 {label_zh}。[/green]",
            )
        )
        return


def _step4_memory(
    *, skip: bool, non_interactive: bool, main_model: Optional[str], warnings: list[str], skip_test: bool = False
) -> object:
    """Step 4 -- EverOS long-term memory (model sub-screens).

    The bootstrap seeds ``memory.backend="everos"`` (schema default) and everos
    is the only memory backend, so this step does not ask whether to enable it:
    it either confirms the seed by configuring the llm role, or resolves it back
    to ``None`` on skip / non-interactive / give-up. ``None`` means no long-term
    memory at all, not a fallback to something simpler.

    ``_memory_enabled`` gates on the llm role alone, so a fresh modelless seed
    reads as "not configured yet" and the keep/reconfigure menu only appears once
    that model is actually on disk. embedding and rerank are offered here but
    never gate: skipping them costs recall quality, not memory itself.
    """
    oc._step_header(4, oc._t("EverOS long-term memory", "EverOS 长期记忆"))

    import sys

    if sys.platform == "win32":
        oc.console.print(
            oc._t(
                "  [yellow]⚠ EverOS memory engine does not support native Windows.[/yellow]\n"
                "  [dim]Run Raven inside WSL for full memory support.[/dim]\n"
                "  [dim]Skipping memory configuration.[/dim]",
                "  [yellow]⚠ EverOS 记忆引擎暂不支持 Windows 原生环境。[/yellow]\n"
                "  [dim]在 WSL 中运行 Raven 可获得完整记忆支持。[/dim]\n"
                "  [dim]已跳过记忆配置。[/dim]",
            )
        )
        _set_memory_backend(None)
        return None

    if skip or non_interactive:
        # Never configured the required models here → disable backend-driven
        # memory so runtime doesn't activate EverOS without an llm/embedding.
        # (``_memory_enabled`` already gates on both required models, so an
        # already-enabled+configured setup is preserved.)
        if not _memory_enabled():
            _set_memory_backend(None)
        oc.console.print(
            oc._t(
                "  [dim]Long-term memory stays off.[/dim]\n"
                "  [dim]Run `raven onboard` again whenever you want to configure it.[/dim]",
                "  [dim]长期记忆保持关闭。[/dim]\n  [dim]随时可以重新运行 raven onboard 配置。[/dim]",
            )
        )
        return None

    questionary = oc._require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if _memory_enabled():
        action = questionary.select(
            oc._t(
                "EverOS long-term memory is already enabled. What would you like to do?",
                "EverOS 长期记忆已启用。想做什么?",
            ),
            choices=[
                questionary.Choice(oc._t("Keep it enabled", "保持启用"), value="keep"),
                questionary.Choice(oc._t("Reconfigure", "重新配置"), value="redo"),
            ],
            style=RAVEN_STYLE,
            qmark=oc._QMARK,
        ).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "keep":
            return None  # backend already "everos" + models on disk; leave as-is
    else:
        # No enable/decline question: everos is the only memory backend, so the
        # step goes straight into configuring it. Leaving is still possible --
        # backing out of the required roles reaches the give-up prompt, which
        # spells out what is lost.
        # Wrapped by hand: rich re-wraps at the terminal width and drops the
        # two-space indent on continuation lines, which reads as a stray
        # left-flush sentence under an indented block.
        oc.console.print(
            oc._t(
                "  [dim]Raven's long-term memory comes from EverOS. What it can do grows with\n"
                "  what you configure:[/dim]\n"
                "  [dim]    memory LLM only    conversations become memories; recall matches keywords[/dim]\n"
                "  [dim]  + memory embedding   recall matches meaning, not wording (strongly advised)[/dim]\n"
                "  [dim]  + memory rerank      recall ordering gets sharper[/dim]",
                "  [dim]Raven 拥有 EverOS 提供的强大长期记忆能力，能力随配置递进：[/dim]\n"
                "  [dim]    仅记忆 LLM       对话会被提炼成记忆存下来，召回按关键词匹配[/dim]\n"
                "  [dim]  + 记忆 embedding   召回按语义匹配，换个问法也能找到（强烈建议配）[/dim]\n"
                "  [dim]  + 记忆 rerank      召回结果排序更准[/dim]",
            ),
            highlight=False,
        )

    # Ensure the EverOS home directory has its config templates (everos.toml
    # + ome.toml) BEFORE writing model sections — set_everos_section merges
    # into the template so default sections (memory/sqlite/lancedb/api) are
    # preserved. Also creates ome.toml which the runtime requires.
    from raven.config.update_everos import configure_everos_env, ensure_everos_home

    configure_everos_env()
    ensure_everos_home()

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
                oc._t(
                    "  [yellow]⚠ Gave up long-term memory: Raven will not remember anything "
                    "between sessions.[/yellow]\n"
                    "  [dim]Run `raven onboard` again whenever you want to configure it.[/dim]",
                    "  [yellow]⚠ 已放弃长期记忆，Raven 不会记住任何跨会话内容。[/yellow]\n"
                    "  [dim]随时可以重新运行 raven onboard 配置。[/dim]",
                )
            )
            return None

    # Verify EverOS server is reachable (auto-starts if needed)
    import asyncio

    from raven.config.raven import load_raven_config
    from raven.plugin.memory.everos._health import configured_base_url
    from raven.plugin.memory.everos._server import ensure_everos_server

    # The configured address, not the default: the memory backend connects to
    # whatever ``plugins.config`` names, so probing 18791 on a setup that moved
    # everos elsewhere reports on a server nobody uses -- and then spawns a
    # second instance that cannot hold the OME lock.
    base_url = configured_base_url(load_raven_config())

    oc.console.print()
    oc.console.print(
        oc._t(
            "  [dim]Starting EverOS service...[/dim]",
            "  [dim]正在启动 EverOS 服务...[/dim]",
        )
    )
    # A failed start is not a decision to abandon long-term memory. The models
    # are already on disk at this point, so "defer" keeps the whole
    # configuration and lets the runtime start the service on the next session;
    # only the explicit third choice turns memory off.
    while True:
        try:
            asyncio.run(ensure_everos_server(base_url))
            oc.console.print(
                oc._t(
                    "  [green]✓ EverOS service is running.[/green]",
                    "  [green]✓ EverOS 服务已启动。[/green]",
                )
            )
            break
        except RuntimeError as exc:
            oc.console.print(
                oc._t(
                    f"  [red]✗ EverOS service failed to start: {exc}[/red]",
                    f"  [red]✗ EverOS 服务启动失败：{exc}[/red]",
                )
            )
            action = questionary.select(
                oc._t("What to do?", "怎么办？"),
                choices=[
                    questionary.Choice(oc._t("Retry", "重试"), value="retry"),
                    questionary.Choice(
                        oc._t(
                            "Leave it for later (settings kept, Raven retries next start)",
                            "暂时跳过（保留配置，下次启动 Raven 时会再试）",
                        ),
                        value="defer",
                    ),
                    questionary.Choice(
                        oc._t("Turn long-term memory off", "关闭长期记忆"),
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
            if action == "defer":
                _set_memory_backend("everos")
                oc.console.print(
                    oc._t(
                        "  [yellow]! Memory settings kept. Raven will try to start the "
                        "service again on the next session.[/yellow]",
                        "  [yellow]⚠ 已保留记忆配置。下次会话启动时 Raven 会再尝试启动服务。[/yellow]",
                    )
                )
                return None
            _set_memory_backend(None)
            oc.console.print(
                oc._t(
                    "  [yellow]! Long-term memory turned off. Run `raven onboard` "
                    "again whenever you want it back.[/yellow]",
                    "  [yellow]⚠ 已关闭长期记忆。随时可以重新运行 raven onboard 开启。[/yellow]",
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
    from raven.plugin.memory.everos._health import (
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
            oc._t(
                f"  [green]✓ {names} {'is' if len(configured) == 1 else 'are'} available.[/green]",
                f"  [green]✓ {names} 均可用。[/green]",
            )
        )
        return
    names = " and ".join(broken)
    oc.console.print(
        oc._t(
            f"  [yellow]⚠ {names} is configured but EverOS could not build it.[/yellow]\n"
            "  [dim]Memory runs degraded until this is fixed.[/dim]\n"
            f"  [dim]Check: {_everos_server_log_hint()}[/dim]",
            f"  [yellow]⚠ {names} 已配置，但 EverOS 未能构建成功。[/yellow]\n"
            "  [dim]在此修复前，记忆能力将处于降级状态。[/dim]\n"
            f"  [dim]请查看：{_everos_server_log_hint()}[/dim]",
        )
    )


def _everos_server_log_hint() -> str:
    from raven.plugin.memory.everos._server import server_log_path

    return str(server_log_path())
