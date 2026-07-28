"""Auto-detection: which provider serves a given model.

``provider: auto`` resolves in order -- explicit "<provider>/<model>" prefix, the
preferred gateway, the model's own vendor by keyword, then a credentialed
fallback. ``provider: <name>`` skips all of it.
"""

from __future__ import annotations

from raven.config.schema import Config


def _config(**providers: dict[str, str]) -> Config:
    return Config.model_validate({"providers": providers})


def test_keyword_match_prefers_the_models_own_vendor() -> None:
    cfg = _config(openai={"apiKey": "K-OPENAI"}, openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "gpt-4o"

    assert cfg.get_provider_name() == "openai"


def test_gateway_fallback_when_the_vendor_has_no_key() -> None:
    cfg = _config(openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "gpt-4o"

    assert cfg.get_provider_name() == "openrouter"


def test_preferred_gateway_claims_bare_model_names() -> None:
    cfg = _config(openai={"apiKey": "K-OPENAI"}, openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "gpt-4o"
    cfg.agents.defaults.gateway = "openrouter"

    assert cfg.get_provider_name() == "openrouter"


def test_explicit_prefix_beats_the_preferred_gateway() -> None:
    # The point of a soft preference: one model can still go direct.
    cfg = _config(openai={"apiKey": "K-OPENAI"}, openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "openai/gpt-4o"
    cfg.agents.defaults.gateway = "openrouter"

    assert cfg.get_provider_name() == "openai"


def test_preferred_gateway_without_credentials_is_skipped() -> None:
    cfg = _config(openai={"apiKey": "K-OPENAI"})
    cfg.agents.defaults.model = "gpt-4o"
    cfg.agents.defaults.gateway = "openrouter"

    assert cfg.get_provider_name() == "openai"


def test_forced_provider_outranks_the_preferred_gateway() -> None:
    cfg = _config(openai={"apiKey": "K-OPENAI"}, openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "gpt-4o"
    cfg.agents.defaults.provider = "openai"
    cfg.agents.defaults.gateway = "openrouter"

    assert cfg.get_provider_name() == "openai"
