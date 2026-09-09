"""Build an Raven LLMProvider from a hermes-style config dict.

Supports three provider routes:
- openrouter: prefixes model with `openrouter/`
- custom     (LAN OpenAI-compatible, e.g. vLLM): prefixes with `openai/`,
  forces a non-empty api_key, and bypasses HTTP(S)_PROXY for the target host.
- (default) : anything else — pass through.
"""

from __future__ import annotations

import os
from typing import Any

from .proxy import bypass_proxy_for_url


def make_provider(cfg: dict[str, Any], model_override: str | None = None):
    """Construct a LiteLLMProvider + return (provider, model_name).

    cfg is typically what ``load_config_from_hermes_home()`` returned.
    ``model_override`` forces a specific model and re-infers OpenRouter
    routing if the override begins with "openrouter/" (e.g. for a
    separate judge model).
    """
    # Local import so this module is cheap to import when only helpers are needed.
    from raven.providers.litellm_provider import LiteLLMProvider

    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        model, base_url, provider_name = model_cfg, None, None
    else:
        model = model_cfg.get("default") or "gpt-4o-mini"
        base_url = model_cfg.get("base_url")
        provider_name = model_cfg.get("provider")

    if model_override:
        model = model_override
        if model.startswith("openrouter/"):
            provider_name = "openrouter"
            base_url = base_url or "https://openrouter.ai/api/v1"

    base_url = base_url or os.environ.get("OPENROUTER_BASE_URL")
    or_key_raw = os.environ.get("OPENROUTER_API_KEY") or ""
    # OPENROUTER_API_KEY may carry a comma-separated rotation list for OC;
    # single-key consumers (simulator, planner) take the first.
    or_key = or_key_raw.split(",", 1)[0].strip() if or_key_raw else ""
    api_key = or_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""

    if provider_name and provider_name.lower() == "openrouter":
        if "/" in model and not model.startswith("openrouter/"):
            model = f"openrouter/{model}"
    elif provider_name and provider_name.lower() == "custom":
        if not model.startswith("openai/"):
            model = f"openai/{model}"
        # Custom LAN endpoint must NOT inherit OPENROUTER/OPENAI keys from env:
        # find_gateway() detects "sk-or-" prefix → misroutes to OpenRouter.
        api_key = "dummy"
        if base_url:
            bypass_proxy_for_url(base_url)

    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=base_url,
        default_model=model,
        provider_name=provider_name,
    )
    return provider, model


__all__ = ["make_provider"]
