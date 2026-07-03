"""Catalog-shape tests for the LLM provider registry and backend classes.

These pin the *current* shape so that adding / removing a provider spec or a
concrete backend class trips a test instead of silently drifting.
"""

from __future__ import annotations

from raven.providers.base import LLMProvider
from raven.providers.registry import PROVIDERS

# The Confluence "Providers" page claims 19 providers. This pins the current
# registry so any drift (add/remove a ProviderSpec) is caught here.
EXPECTED_PROVIDER_NAMES = {
    "custom",
    "azure_openai",
    "openrouter",
    "aihubmix",
    "siliconflow",
    "volcengine",
    "anthropic",
    "openai",
    "openai_codex",
    "github_copilot",
    "deepseek",
    "gemini",
    "zhipu",
    "dashscope",
    "moonshot",
    "minimax",
    "vllm",
    "ollama",
    "groq",
}


def test_registry_has_exactly_19_providers() -> None:
    assert len(PROVIDERS) == 19
    assert len(EXPECTED_PROVIDER_NAMES) == 19


def test_registry_provider_name_set_is_pinned() -> None:
    assert {spec.name for spec in PROVIDERS} == EXPECTED_PROVIDER_NAMES


def test_provider_names_are_unique() -> None:
    names = [spec.name for spec in PROVIDERS]
    assert len(names) == len(set(names))


def _concrete_provider_subclasses() -> set[type]:
    """All non-abstract LLMProvider subclasses defined in raven.providers."""
    # Import each backend module so its subclass is registered on LLMProvider.
    import raven.providers.azure_openai_provider  # noqa: F401
    import raven.providers.custom_provider  # noqa: F401
    import raven.providers.litellm_provider  # noqa: F401
    import raven.providers.openai_codex_provider  # noqa: F401

    seen: set[type] = set()
    stack = list(LLMProvider.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if getattr(cls, "__abstractmethods__", frozenset()):
            continue
        if cls.__module__.startswith("raven.providers"):
            seen.add(cls)
    return seen


def test_exactly_four_concrete_backend_classes() -> None:
    # Only 3 are runtime-dispatched via cli/_helpers.py `_get_provider`
    # (LiteLLM / AzureOpenAI / OpenAICodex); CustomProvider is legacy and not
    # wired. This asserts class existence only, not the dispatch wiring.
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.custom_provider import CustomProvider
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.providers.openai_codex_provider import OpenAICodexProvider

    expected = {
        LiteLLMProvider,
        AzureOpenAIProvider,
        OpenAICodexProvider,
        CustomProvider,
    }
    assert _concrete_provider_subclasses() == expected
    for cls in expected:
        assert issubclass(cls, LLMProvider)
