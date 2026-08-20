"""LLM provider abstraction module."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.base import LLMProvider, LLMResponse
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.providers.openai_codex_provider import OpenAICodexProvider

__all__ = ["LLMProvider", "LLMResponse", "LiteLLMProvider", "OpenAICodexProvider", "AzureOpenAIProvider"]

# Lazy re-exports (PEP 562): importing a provider submodule must not eagerly pull
# ``litellm_provider`` -> litellm, which dominates CLI cold start.
_LAZY_EXPORTS = {
    "LLMProvider": "raven.providers.base",
    "LLMResponse": "raven.providers.base",
    "LiteLLMProvider": "raven.providers.litellm_provider",
    "OpenAICodexProvider": "raven.providers.openai_codex_provider",
    "AzureOpenAIProvider": "raven.providers.azure_openai_provider",
}


def __getattr__(name: str) -> object:
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)


def __dir__() -> list[str]:
    return sorted(__all__)
