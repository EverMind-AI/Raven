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

COMMON_MODELS: dict[str, list[str]] = {
    "openrouter": [
        "anthropic/claude-opus-4.8",
        "anthropic/claude-opus-4.7",
        "anthropic/claude-sonnet-5",
        "anthropic/claude-fable-5",
        "openai/gpt-5.5",
        "openai/gpt-5.4-mini",
        "google/gemini-3.5-flash",
        "google/gemini-3-flash-preview",
        "x-ai/grok-4.3",
        "meta-llama/llama-4-maverick",
        "mistralai/mistral-medium-3-5",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "xiaomi/mimo-v2.5",
        "minimax/minimax-m3",
        "z-ai/glm-5.2",
        "tencent/hy3",
        "moonshotai/kimi-k2.6",
        "qwen/qwen3.7-max",
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
    "zhipu": [
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
