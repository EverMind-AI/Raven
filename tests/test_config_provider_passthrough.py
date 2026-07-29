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


def test_a_provider_without_a_spec_writes_no_credentials_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Specs still name the one variable their vendor reads. What is gone is
    # *deriving* variables from LiteLLM's "missing keys" for a vendor we carry no
    # spec for -- that list is every variable the vendor wants, so the key landed
    # in AWS_SECRET_ACCESS_KEY for bedrock and an endpoint var for cloudflare.
    import os

    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "CLOUDFLARE_API_KEY", "CLOUDFLARE_API_BASE"):
        monkeypatch.delenv(var, raising=False)

    LiteLLMProvider(api_key="K-SECRET", default_model="bedrock/amazon.nova-pro-v1:0")
    LiteLLMProvider(api_key="K-SECRET", default_model="cloudflare/@cf/meta/llama-3-8b-instruct")

    assert os.environ.get("AWS_ACCESS_KEY_ID") is None
    assert os.environ.get("AWS_SECRET_ACCESS_KEY") is None
    assert os.environ.get("CLOUDFLARE_API_BASE") is None


def test_the_cli_configures_a_provider_that_only_litellm_knows(tmp_path) -> None:
    # Otherwise the whole point of accepting these providers is unreachable
    # except by hand-editing config.json, which is what the CLI exists to avoid.
    import json

    from raven.config.update_providers import get_provider_config, set_provider_fields

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"providers": {}}))

    set_provider_fields("mistral", {"api_key": "K-MISTRAL"}, config_path=path)

    assert get_provider_config("mistral", config_path=path, redact_secrets=False)["api_key"] == "K-MISTRAL"
    assert list(json.loads(path.read_text())["providers"]) == ["mistral"]


def test_testing_an_unregistered_provider_reports_instead_of_crashing(tmp_path) -> None:
    # test_provider reads several spec attributes; a vendor with no spec must not
    # trip any of them.
    import json

    from raven.config.update_providers import set_provider_fields, test_provider

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"providers": {}}))
    set_provider_fields(
        "mistral",
        {"api_key": "K", "api_base": "https://api.mistral.ai/v1"},
        config_path=path,
    )

    result = test_provider("mistral", config_path=path, timeout_s=1)

    assert result["status"] != "unknown_provider"
    assert isinstance(result["ok"], bool)


def test_the_cli_rejects_a_vendor_nobody_has_heard_of(tmp_path) -> None:
    # A typo should say so, not quietly write a section nothing will ever read.
    import json

    from raven.config.update_providers import set_provider_fields

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"providers": {}}))

    with pytest.raises(KeyError, match="notaprovider"):
        set_provider_fields("notaprovider", {"api_key": "K"}, config_path=path)


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
