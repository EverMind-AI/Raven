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
    cfg.agents.defaults.preferred_gateway = "openrouter"

    assert cfg.get_provider_name() == "openrouter"


def test_explicit_prefix_beats_the_preferred_gateway() -> None:
    # The point of a soft preference: one model can still go direct.
    cfg = _config(openai={"apiKey": "K-OPENAI"}, openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "openai/gpt-4o"
    cfg.agents.defaults.preferred_gateway = "openrouter"

    assert cfg.get_provider_name() == "openai"


def test_preferred_gateway_without_credentials_is_skipped() -> None:
    cfg = _config(openai={"apiKey": "K-OPENAI"})
    cfg.agents.defaults.model = "gpt-4o"
    cfg.agents.defaults.preferred_gateway = "openrouter"

    assert cfg.get_provider_name() == "openai"


def test_gateway_shortlist_models_stay_on_their_gateway() -> None:
    # Gateway ids name the upstream vendor ("anthropic/..."); without the
    # gateway's own prefix a picked model silently leaves it for that vendor as
    # soon as the user holds a key there. Checked for every gateway that ships a
    # shortlist, not just the one that first got this wrong.
    from raven.providers.common_models import common_models_for
    from raven.providers.registry import PROVIDERS

    competing = {"anthropic": {"apiKey": "K-ANT"}, "openai": {"apiKey": "K-OPENAI"}}
    for spec in PROVIDERS:
        shortlist = common_models_for(spec.name)
        if not spec.is_gateway or not shortlist:
            continue
        cfg = _config(**competing, **{spec.name: {"apiKey": "K-GATEWAY"}})
        for model in shortlist:
            cfg.agents.defaults.model = model
            assert cfg.get_provider_name() == spec.name, f"{spec.name}: {model}"


def test_a_model_id_written_before_the_zai_rename_still_resolves() -> None:
    # The rename has to cover saved model ids too, not just the credential key:
    # "zhipu/glm-4.6" is what an existing config holds.
    from raven.providers.litellm_provider import LiteLLMProvider

    assert LiteLLMProvider(default_model="zhipu/glm-4.6")._resolve_model("zhipu/glm-4.6") == "zai/glm-4.6"


def test_the_old_provider_name_still_works_on_the_write_path() -> None:
    # `raven provider set zhipu ...` is muscle memory and lives in every guide
    # written before the rename.
    from raven.config.update_providers import provider_field_specs

    assert provider_field_specs("zhipu") == provider_field_specs("zai")


def test_a_half_migrated_config_serves_the_current_key() -> None:
    # Both keys present: the declared field is the live one, and the leftover
    # must not shadow it with a stale credential.
    cfg = _config(zhipu={"apiKey": "STALE"}, zai={"apiKey": "CURRENT"})

    assert cfg.providers.get("zhipu").api_key == "CURRENT"
    assert cfg.providers.get("zai").api_key == "CURRENT"


def test_forcing_the_old_provider_name_resolves_to_the_new_one() -> None:
    cfg = _config(zhipu={"apiKey": "K-ZAI"})
    cfg.agents.defaults.provider = "zhipu"
    cfg.agents.defaults.model = "zai/glm-4.6"

    assert cfg.get_provider_name() == "zai"


def test_a_config_written_before_the_zai_rename_still_resolves() -> None:
    cfg = _config(zhipu={"apiKey": "K-ZAI"})
    cfg.agents.defaults.model = "zai/glm-4.6"

    assert cfg.get_provider_name() == "zai"
    assert cfg.get_api_key() == "K-ZAI"


def test_forced_provider_outranks_the_preferred_gateway() -> None:
    cfg = _config(openai={"apiKey": "K-OPENAI"}, openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "gpt-4o"
    cfg.agents.defaults.provider = "openai"
    cfg.agents.defaults.preferred_gateway = "openrouter"

    assert cfg.get_provider_name() == "openai"
