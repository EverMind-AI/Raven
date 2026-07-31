"""Catalog-shape tests for the LLM provider registry and backend classes.

These pin the *current* shape so that adding / removing a provider spec or a
concrete backend class trips a test instead of silently drifting.
"""

from __future__ import annotations

import pytest

from raven.providers.base import LLMProvider
from raven.providers.common_models import common_models_for
from raven.providers.registry import PROVIDERS, find_by_name

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
    "zai",
    "dashscope",
    "moonshot",
    "minimax",
    "minimax_global",
    "minimax_cn",
    "hosted_vllm",
    "ollama_chat",
    "groq",
}


def test_registry_has_exactly_21_providers() -> None:
    assert len(PROVIDERS) == 21
    assert len(EXPECTED_PROVIDER_NAMES) == 21


def test_registry_provider_name_set_is_pinned() -> None:
    assert {spec.name for spec in PROVIDERS} == EXPECTED_PROVIDER_NAMES


def test_provider_names_are_unique() -> None:
    names = [spec.name for spec in PROVIDERS]
    assert len(names) == len(set(names))


def test_a_specs_route_prefix_is_a_provider_litellm_knows() -> None:
    """The prefix decides where LiteLLM sends the request.

    Defaulting it to our own name only works while that name is one LiteLLM
    carries; a name it does not know would route every request nowhere.
    """
    import litellm

    known = {str(getattr(p, "value", p)) for p in litellm.provider_list}
    for spec in PROVIDERS:
        if spec.model_prefix:
            assert spec.model_prefix in known, spec.name


def test_via_driver_names_another_vendor_that_litellm_actually_speaks() -> None:
    """A borrowed driver must be somebody else's, and must exist.

    Naming your own driver is a no-op that invites the two to drift apart;
    naming a vendor LiteLLM has never heard of routes nowhere. Neither can be
    caught by reading the field -- only by checking it against LiteLLM.
    """
    from raven.providers.litellm_setup import import_litellm

    known = {str(getattr(p, "value", p)) for p in import_litellm().provider_list}
    for spec in PROVIDERS:
        if not spec.via_driver:
            continue
        assert spec.via_driver not in spec.route_names, f"{spec.name}: via_driver is one of its own names"
        assert spec.via_driver in known, f"{spec.name}: LiteLLM does not know driver {spec.via_driver!r}"


def test_a_providers_name_is_litellms_spelling_whenever_litellm_has_one() -> None:
    """One name per provider: ours is LiteLLM's wherever LiteLLM has one.

    This is what removed the reconciliation layer. A spec whose own name is
    absent from LiteLLM while an alias of it is present means the rename went
    the wrong way, and every prefix comparison downstream inherits two answers.
    """
    from raven.providers.litellm_setup import import_litellm

    known = {str(getattr(p, "value", p)) for p in import_litellm().provider_list}
    for spec in PROVIDERS:
        stale = [a for a in spec.name_aliases if a in known and spec.name not in known]
        assert not stale, f"{spec.name}: LiteLLM knows {stale} but not {spec.name!r} -- adopt LiteLLM's spelling"


def test_registry_and_schema_declare_the_same_providers() -> None:
    # A provider needs both a ProviderSpec (env keys, prefixes, detection) and a
    # ProvidersConfig field (where its credentials live). Miss either half and it
    # is unconfigurable or unreachable, with nothing else failing.
    from raven.config.schema import ProvidersConfig

    assert {spec.name for spec in PROVIDERS} == set(ProvidersConfig.model_fields)


# Direct providers seeded in the model picker (issue #100). Each must expose a
# non-empty default_model drawn from its curated shortlist, so the onboarding
# fallback and the picker stay in sync and no provider defaults to empty.
_SEEDED_DIRECT_PROVIDERS = [
    "deepseek",
    "openai",
    "anthropic",
    "gemini",
    "zai",
    "dashscope",
    "groq",
    "minimax_global",
    "minimax_cn",
]


@pytest.mark.parametrize("slug", _SEEDED_DIRECT_PROVIDERS)
def test_seeded_provider_default_model_in_shortlist(slug: str) -> None:
    default = find_by_name(slug).default_model
    assert default, f"{slug} has no default_model"
    assert default in common_models_for(slug)


def _concrete_provider_subclasses() -> set[type]:
    """All non-abstract LLMProvider subclasses defined in raven.providers."""
    # Import each backend module so its subclass is registered on LLMProvider.
    import raven.providers.azure_openai_provider  # noqa: F401
    import raven.providers.litellm_provider  # noqa: F401
    import raven.providers.minimax_oauth_provider  # noqa: F401
    import raven.providers.openai_codex_provider  # noqa: F401
    import raven.providers.per_model_provider  # noqa: F401

    seen: set[type] = set()
    stack = list(LLMProvider.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if getattr(cls, "__abstractmethods__", frozenset()):
            continue
        # LazyProvider is a proxy over a real backend, not a backend itself, and
        # is not imported above -- so its presence via __subclasses__ depends on
        # ambient imports from other tests. Skip it to keep the set deterministic.
        if cls.__module__ == "raven.providers.lazy":
            continue
        if cls.__module__.startswith("raven.providers"):
            seen.add(cls)
    return seen


def test_exactly_five_concrete_backend_classes() -> None:
    # This asserts class existence only, not the dispatch wiring.
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.providers.minimax_oauth_provider import MiniMaxOAuthProvider
    from raven.providers.openai_codex_provider import OpenAICodexProvider
    from raven.providers.per_model_provider import PerModelProvider

    expected = {
        LiteLLMProvider,
        AzureOpenAIProvider,
        OpenAICodexProvider,
        MiniMaxOAuthProvider,
        PerModelProvider,
    }
    assert _concrete_provider_subclasses() == expected
    for cls in expected:
        assert issubclass(cls, LLMProvider)


def test_every_gateway_prefixes_the_models_it_routes() -> None:
    """The prefix is what sends the request to the gateway rather than the vendor.

    Reading the raw registry field instead of the resolved prefix dropped it for
    any gateway whose name is already LiteLLM's, so a stored "anthropic/claude-x"
    went out unprefixed and LiteLLM handed the gateway's key to Anthropic.
    """
    from raven.providers.litellm_provider import LiteLLMProvider

    for spec in PROVIDERS:
        if not spec.is_gateway:
            continue
        provider = LiteLLMProvider(api_key="K", provider_name=spec.name, default_model="probe-model")
        resolved = provider._resolve_model("probe-model")
        assert resolved.startswith(f"{spec.model_prefix}/"), f"{spec.name}: {resolved}"
