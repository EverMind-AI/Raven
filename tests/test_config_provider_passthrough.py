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


def test_forcing_an_unregistered_provider_resolves_its_key() -> None:
    # `provider: <name>` short-circuits matching, and that branch used to read
    # the field directly -- which hands back a raw dict for an extra.
    cfg = _config(mistral={"apiKey": "K-MISTRAL"})
    cfg.agents.defaults.provider = "mistral"
    cfg.agents.defaults.model = "mistral/mistral-large-latest"

    assert cfg.get_api_key() == "K-MISTRAL"


def test_building_a_provider_writes_no_credentials_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # The key travels as an api_key kwarg on every call. Deriving env vars from
    # LiteLLM's "missing keys" instead spilled it into whatever a vendor happens
    # to want -- AWS_SECRET_ACCESS_KEY for bedrock, an endpoint var for cloudflare.
    import os

    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "CLOUDFLARE_API_KEY", "CLOUDFLARE_API_BASE"):
        monkeypatch.delenv(var, raising=False)

    LiteLLMProvider(api_key="K-SECRET", default_model="bedrock/amazon.nova-pro-v1:0")
    LiteLLMProvider(api_key="K-SECRET", default_model="cloudflare/@cf/meta/llama-3-8b-instruct")

    assert os.environ.get("AWS_ACCESS_KEY_ID") is None
    assert os.environ.get("AWS_SECRET_ACCESS_KEY") is None
    assert os.environ.get("CLOUDFLARE_API_BASE") is None


def test_callers_read_declared_fields_and_extras_through_get() -> None:
    cfg = _config(openai={"apiKey": "A"}, mistral={"apiKey": "B"})

    assert cfg.providers.get("openai").api_key == "A"
    assert cfg.providers.get("mistral").api_key == "B"
    assert cfg.providers.get("nope") is None


def test_make_provider_passes_configured_model_overrides_through() -> None:
    # The wiring from config to provider is one line and easy to leave out,
    # which would make every configured override silently do nothing.
    from raven.cli._helpers import make_provider

    cfg = _config(openai={"apiKey": "K"})
    cfg.agents.defaults.model = "openai/gpt-4o"
    cfg.agents.defaults.model_overrides = {"gpt-4o": {"top_p": 0.5}}

    assert make_provider(cfg).model_overrides == {"gpt-4o": {"top_p": 0.5}}


def test_declared_provider_fields_are_untouched_by_extras() -> None:
    # Old configs list every provider by name; extras must not shadow them.
    cfg = ProvidersConfig.model_validate({"openai": {"apiKey": "A"}, "mistral": {"apiKey": "B"}})

    assert cfg.openai.api_key == "A"
    assert cfg.get("mistral").api_key == "B"
