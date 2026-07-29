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


def test_writing_under_the_old_provider_name_lands_on_the_current_key(tmp_path) -> None:
    # `raven provider set zhipu ...` is muscle memory and lives in every guide
    # written before the rename. Taking the name at face value would split the
    # file into two sections, and the runtime only reads one of them.
    import json

    from raven.config.update_providers import get_provider_config, set_provider_fields

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"providers": {"zhipu": {"apiKey": "OLD"}}}))

    set_provider_fields("zhipu", {"api_key": "WRITTEN"}, config_path=path)

    stored = json.loads(path.read_text())["providers"]
    assert list(stored) == ["zai"]
    assert Config.model_validate({"providers": stored}).get_api_key() == "WRITTEN"
    assert get_provider_config("zai", config_path=path, redact_secrets=False)["api_key"] == "WRITTEN"


def test_reading_a_never_migrated_config_finds_the_credentials(tmp_path) -> None:
    # Diagnostics must agree with the runtime: a file still saying "zhipu" is
    # live config, not an unconfigured provider.
    import json

    from raven.config.update_providers import get_provider_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"providers": {"zhipu": {"apiKey": "OLD"}}}))

    assert get_provider_config("zai", config_path=path, redact_secrets=False)["api_key"] == "OLD"


def test_a_half_migrated_config_serves_the_current_key() -> None:
    # Both keys present: the declared field is the live one, and the leftover
    # must not shadow it with a stale credential.
    cfg = _config(zhipu={"apiKey": "STALE"}, zai={"apiKey": "CURRENT"})

    assert cfg.providers.get("zhipu").api_key == "CURRENT"
    assert cfg.providers.get("zai").api_key == "CURRENT"


def test_an_explicit_prefix_does_not_leak_to_a_vendor_named_later_in_the_id() -> None:
    # "deepinfra/deepseek-ai/DeepSeek-V3" is DeepInfra's. Matching DeepSeek on
    # the substring would send DeepInfra's key to DEEPSEEK_API_KEY and rewrite
    # the model id to deepseek/...
    from raven.providers.registry import find_by_model

    assert find_by_model("deepinfra/deepseek-ai/DeepSeek-V3") is None


def test_a_model_prefixed_with_the_old_name_reports_the_new_provider() -> None:
    cfg = _config(zhipu={"apiKey": "K"})
    cfg.agents.defaults.model = "zhipu/glm-4.6"

    assert cfg.get_provider_name() == "zai"


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
