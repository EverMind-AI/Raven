"""Curated "common models" shortlist per provider slug.

Hand-maintained on purpose. Provider ``/v1/models`` endpoints return the full
catalog (OpenRouter alone ships 300+ models) with no "popular"/"common" flag,
so a small, recognizable default set has to be curated rather than derived.

The TUI ``/model`` picker shows this shortlist *after* whatever the user has
configured in ``config.providers.<slug>.models``; users can always type any
model id by hand (``model.add_model``), so this list only needs to cover the
common case, not every model.

Model ids drift as providers ship releases — update this list as needed. Only
``openrouter`` (the default provider) is seeded today; other providers fall
back to their configured list until curated here.
"""

from __future__ import annotations

COMMON_MODELS: dict[str, list[str]] = {
    "openrouter": [
        "anthropic/claude-opus-4.8",
        "anthropic/claude-sonnet-5",
        "anthropic/claude-fable-5",
        "openai/gpt-5.5",
        "openai/gpt-5.5-pro",
        "openai/gpt-5.4-mini",
        "google/gemini-3.5-flash",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        "x-ai/grok-4.3",
        "qwen/qwen3.7-max",
        "moonshotai/kimi-k2-thinking",
        "meta-llama/llama-4-maverick",
        "mistralai/mistral-medium-3-5",
    ],
}


def common_models_for(slug: str) -> list[str]:
    """Return a copy of the curated common-model shortlist for ``slug``."""
    return list(COMMON_MODELS.get(slug, []))
