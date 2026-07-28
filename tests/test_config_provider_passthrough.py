"""Providers Raven carries no spec for are usable from config alone.

LiteLLM supports far more vendors than Raven has ProviderSpecs. A key under the
vendor's name plus a "<vendor>/<model>" model id is enough: matching resolves the
provider and the env bridge places the key in whatever variable LiteLLM reads.
"""

from __future__ import annotations

import pytest

from raven.config.schema import Config, ProvidersConfig
from raven.providers.litellm_provider import LiteLLMProvider


def _config(**providers: dict[str, str]) -> Config:
    return Config.model_validate({"providers": providers})


def test_unknown_provider_key_survives_validation() -> None:
    cfg = _config(mistral={"apiKey": "K-MISTRAL"})

    mistral = cfg.providers.get("mistral")
    assert mistral is not None
    assert mistral.api_key == "K-MISTRAL"


def test_get_reads_declared_fields_too() -> None:
    cfg = _config(openai={"apiKey": "K-OPENAI"})

    assert cfg.providers.get("openai").api_key == "K-OPENAI"
    assert cfg.providers.get("nope") is None


def test_explicit_prefix_resolves_an_unregistered_provider() -> None:
    cfg = _config(mistral={"apiKey": "K-MISTRAL"}, openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "mistral/mistral-large-latest"

    assert cfg.get_provider_name() == "mistral"
    assert cfg.get_api_key() == "K-MISTRAL"


def test_unregistered_provider_without_a_key_falls_back() -> None:
    cfg = _config(mistral={}, openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "mistral/mistral-large-latest"

    assert cfg.get_provider_name() == "openrouter"


def test_configured_names_spans_declared_and_extra_providers() -> None:
    cfg = _config(openai={"apiKey": "K"}, mistral={"apiKey": "K"}, deepseek={})

    names = cfg.providers.configured_names()
    assert "openai" in names and "mistral" in names
    assert "deepseek" not in names


def test_env_bridge_uses_the_variable_litellm_expects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    LiteLLMProvider(api_key="K-MISTRAL", default_model="mistral/mistral-large-latest")

    import os

    assert os.environ["MISTRAL_API_KEY"] == "K-MISTRAL"


def test_env_bridge_is_a_noop_for_a_model_litellm_cannot_place(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bare name with no vendor prefix: nothing to derive, and nothing to crash on.
    LiteLLMProvider(api_key="K", default_model="some-unprefixed-model")


def test_declared_provider_fields_are_untouched_by_extras() -> None:
    # Old configs list every provider by name; extras must not shadow them.
    cfg = ProvidersConfig.model_validate({"openai": {"apiKey": "A"}, "mistral": {"apiKey": "B"}})

    assert cfg.openai.api_key == "A"
    assert cfg.get("mistral").api_key == "B"
