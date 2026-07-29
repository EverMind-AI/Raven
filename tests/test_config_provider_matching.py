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


def test_gateway_shortlist_models_stay_on_the_gateway() -> None:
    # OpenRouter ids name the upstream vendor ("anthropic/..."); without the
    # gateway prefix a picked model silently leaves OpenRouter for that vendor.
    from raven.providers.common_models import common_models_for

    cfg = _config(anthropic={"apiKey": "K-ANT"}, openrouter={"apiKey": "sk-or-K"})
    for model in common_models_for("openrouter"):
        cfg.agents.defaults.model = model
        assert cfg.get_provider_name() == "openrouter", model


def test_a_model_id_written_before_the_zai_rename_still_resolves() -> None:
    # The rename has to cover saved model ids too, not just the credential key:
    # "zhipu/glm-4.6" is what an existing config holds.
    from raven.providers.litellm_provider import LiteLLMProvider

    assert LiteLLMProvider(default_model="zhipu/glm-4.6")._resolve_model("zhipu/glm-4.6") == "zai/glm-4.6"


def test_a_config_written_before_the_zai_rename_still_resolves() -> None:
    cfg = _config(zhipu={"apiKey": "K-ZAI"})
    cfg.agents.defaults.model = "zai/glm-4.6"

    assert cfg.get_provider_name() == "zai"
    assert cfg.get_api_key() == "K-ZAI"


def test_forced_provider_outranks_the_preferred_gateway() -> None:
    cfg = _config(openai={"apiKey": "K-OPENAI"}, openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "gpt-4o"
    cfg.agents.defaults.provider = "openai"
    cfg.agents.defaults.gateway = "openrouter"

    assert cfg.get_provider_name() == "openai"
