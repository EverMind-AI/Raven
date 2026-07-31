"""Curated "common models" shortlist per provider slug.

Hand-maintained on purpose. Provider ``/v1/models`` endpoints return the full
catalog (OpenRouter alone ships 300+ models) with no "popular"/"common" flag,
so a small, recognizable default set has to be curated rather than derived.

The TUI ``/model`` picker shows this shortlist *after* whatever the user has
configured in ``config.providers.<slug>.models``; users can always type any
model id by hand (``model.add_model``), so this list only needs to cover the
common case, not every model.

Model ids drift as providers ship releases — update this list as needed.
Providers not listed here fall back to their configured list.
"""

from __future__ import annotations

from functools import lru_cache

COMMON_MODELS: dict[str, list[str]] = {
    # Carries the "openrouter/" prefix like this provider's default_model does.
    # Bare OpenRouter ids start with the upstream vendor ("anthropic/...",
    # "deepseek/..."), which auto-detection reads as a request for that vendor
    # direct -- so a model picked from this list would quietly leave OpenRouter
    # as soon as the user also held that vendor's key.
    "openrouter": [
        "openrouter/anthropic/claude-opus-4.8",
        "openrouter/anthropic/claude-opus-4.7",
        "openrouter/anthropic/claude-sonnet-5",
        "openrouter/anthropic/claude-fable-5",
        "openrouter/openai/gpt-5.5",
        "openrouter/openai/gpt-5.4-mini",
        "openrouter/google/gemini-3.5-flash",
        "openrouter/google/gemini-3-flash-preview",
        "openrouter/x-ai/grok-4.3",
        "openrouter/meta-llama/llama-4-maverick",
        "openrouter/mistralai/mistral-medium-3-5",
        "openrouter/deepseek/deepseek-v4-flash",
        "openrouter/deepseek/deepseek-v4-pro",
        "openrouter/xiaomi/mimo-v2.5",
        "openrouter/minimax/minimax-m3",
        "openrouter/z-ai/glm-5.2",
        "openrouter/tencent/hy3",
        "openrouter/moonshotai/kimi-k2.6",
        "openrouter/qwen/qwen3.7-max",
    ],
    "openai": [
        "openai/gpt-5.5",
        "openai/gpt-5.5-pro",
        "openai/gpt-5.4",
        "openai/gpt-5.4-mini",
        "openai/gpt-5.4-nano",
        "openai/gpt-5.3-codex",
        "openai/gpt-4.1",
        "openai/gpt-4o-mini",
    ],
    "anthropic": [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-4-7",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-haiku-4-5",
        "anthropic/claude-fable-5",
    ],
    "gemini": [
        "gemini/gemini-3.5-flash",
        "gemini/gemini-2.5-pro",
        "gemini/gemini-2.5-flash",
        "gemini/gemini-2.5-flash-lite",
        "gemini/gemini-3.1-pro-preview",
        "gemini/gemini-3.1-flash-lite",
        "gemini/gemini-3-flash-preview",
    ],
    "groq": [
        "groq/openai/gpt-oss-120b",
        "groq/openai/gpt-oss-20b",
        "groq/llama-3.3-70b-versatile",
        "groq/llama-3.1-8b-instant",
        "groq/qwen/qwen3.6-27b",
    ],
    "deepseek": [
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    ],
    "minimax_global": [
        "minimax-global/MiniMax-M3",
        "minimax-global/MiniMax-M2.7",
        "minimax-global/MiniMax-M2.7-highspeed",
    ],
    "minimax_cn": [
        "minimax-cn/MiniMax-M3",
        "minimax-cn/MiniMax-M2.7",
        "minimax-cn/MiniMax-M2.7-highspeed",
    ],
    "zai": [
        "zai/glm-5.2",
        "zai/glm-5.1",
        "zai/glm-5",
        "zai/glm-4.7",
        "zai/glm-4.6",
        "zai/glm-4.5-air",
        "zai/glm-4.5",
        "zai/glm-4.7-flash",
        "zai/glm-4.5-flash",
    ],
    "dashscope": [
        "dashscope/qwen-plus",
        "dashscope/qwen-max",
        "dashscope/qwen-flash",
        "dashscope/qwen-turbo",
        "dashscope/qwen3.5-plus",
        "dashscope/qwen3.6-plus",
        "dashscope/qwen3.7-max",
        "dashscope/qwq-plus",
        "dashscope/qwen3-coder-plus",
        "dashscope/qwen3-coder-flash",
        "dashscope/qwen3-vl-plus",
    ],
}


def common_models_for(slug: str) -> list[str]:
    """Return a copy of the curated common-model shortlist for ``slug``."""
    return list(COMMON_MODELS.get(slug, []))


def _is_priced_template(model: str) -> bool:
    """Is this a price-book row rather than something callable?

    The catalogue doubles as a price list, so it carries entries no request can
    name: "ft:gpt-4o-..." states what a model fine-tuned from that base costs,
    and "container" bills a code-interpreter session. Sorted alphabetically they
    led OpenAI's candidates, so the first dozen rows a user saw were unusable.
    """
    bare = model.split("/", 1)[-1]
    return bare.startswith("ft:") or bare == "container"


@lru_cache(maxsize=1)
def _cached_chat_models_by_provider() -> dict[str, tuple[str, ...]]:
    """LiteLLM's own catalogue, indexed by the provider it belongs to.

    Built once and cached: reading it imports LiteLLM, which costs about two
    seconds, so callers must be somewhere a user is already waiting.

    Only ``mode == "chat"`` survives. The catalogue also carries embedding and
    speech models, and offering those where a chat model is asked for produces a
    selection that fails on first use.
    """
    import os
    from collections import defaultdict

    # Read the copy shipped inside litellm rather than the one it fetches from
    # GitHub on import: the remote answer is a different set (90 OpenAI models
    # against 98) and it arrives over a 5-second-timeout request, so without this
    # the candidate list depends on the network and costs a round trip.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    from raven.providers.litellm_setup import import_litellm

    catalogue = import_litellm().model_cost

    by_provider: dict[str, list[str]] = defaultdict(list)
    for model, info in catalogue.items():
        if not isinstance(info, dict) or info.get("mode") != "chat":
            continue
        if _is_priced_template(model):
            continue
        provider = info.get("litellm_provider")
        if provider:
            by_provider[provider].append(model)
    return {provider: tuple(sorted(models)) for provider, models in by_provider.items()}


def _litellm_chat_models_by_provider() -> dict[str, tuple[str, ...]]:
    """The cached index, except that a failure is not what gets cached.

    `lru_cache` remembers whatever the call returned, so caching an empty result
    from a transient import failure would leave the picker permanently empty for
    the life of the process with no way to retry.
    """
    try:
        index = _cached_chat_models_by_provider()
    except Exception:
        _cached_chat_models_by_provider.cache_clear()
        return {}
    if not index:
        _cached_chat_models_by_provider.cache_clear()
    return index


def litellm_models_for(slug: str) -> list[str]:
    """Chat models LiteLLM knows for this provider, as ids that actually route.

    Looked up by every name the provider answers to, not just its own: LiteLLM
    files vLLM under "hosted_vllm" and Ollama under "ollama", so a lookup by the
    section name alone finds nothing for exactly the providers whose curated
    shortlist is empty.

    Ids come back spelled the way LiteLLM would have to receive them. The
    catalogue is inconsistent -- Moonshot's entries carry their prefix, VolcEngine's
    do not -- and offering a bare id would route it by keyword rather than to the
    provider the user picked.
    """
    from raven.providers.registry import find_by_name, normalize_provider_name

    spec = find_by_name(slug)
    if spec is None:
        return []
    if normalize_provider_name(spec.model_prefix) not in spec.route_names:
        # The wire prefix names somebody else -- this provider is reached through
        # another vendor's driver. Prefixing candidates with it would hand the
        # user ids that resolve to the driver's owner instead of to the provider
        # they picked. Today the catalogue has no rows for those five, so the
        # loop below would be empty anyway; the guard states the rule rather than
        # relying on what LiteLLM happens to contain.
        return []
    index = _litellm_chat_models_by_provider()
    prefix = spec.model_prefix
    out: list[str] = []
    seen: set[str] = set()
    for route_name in sorted(spec.route_names):
        for model in index.get(route_name, ()):
            bare = model.split("/", 1)[1] if "/" in model else model
            full = f"{prefix}/{bare}" if prefix else bare
            if full not in seen:
                seen.add(full)
                out.append(full)
    return out
