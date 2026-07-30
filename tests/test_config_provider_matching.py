"""Auto-detection: which provider serves a given model.

``provider: auto`` resolves in order -- explicit "<provider>/<model>" prefix, the
model's own vendor by keyword, then a credentialed fallback.
``provider: <name>`` skips all of it.
"""

from __future__ import annotations

import json

import pytest

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


def test_gateway_shortlist_models_stay_on_their_gateway() -> None:
    # Gateway ids name the upstream vendor ("anthropic/..."); without the
    # gateway's own prefix a picked model silently leaves it for that vendor as
    # soon as the user holds a key there. Checked for every gateway that ships a
    # shortlist, not just the one that first got this wrong.
    from raven.providers.common_models import common_models_for
    from raven.providers.registry import PROVIDERS

    competing = {"anthropic": {"apiKey": "K-ANT"}, "openai": {"apiKey": "K-OPENAI"}}
    checked = 0
    for spec in PROVIDERS:
        shortlist = common_models_for(spec.name)
        if not spec.is_gateway or not shortlist:
            continue
        cfg = _config(**competing, **{spec.name: {"apiKey": "K-GATEWAY"}})
        for model in shortlist:
            cfg.agents.defaults.model = model
            assert cfg.get_provider_name() == spec.name, f"{spec.name}: {model}"
            checked += 1
    # Without this the test passes by checking nothing the day a shortlist empties.
    assert checked, "no gateway shortlist was exercised"


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
    runtime = Config.model_validate({"providers": stored})
    runtime.agents.defaults.model = "zai/glm-4.6"
    assert runtime.get_api_key() == "WRITTEN"
    assert get_provider_config("zai", config_path=path, redact_secrets=False)["api_key"] == "WRITTEN"


def test_reading_a_never_migrated_config_finds_the_credentials(tmp_path) -> None:
    # Diagnostics must agree with the runtime: a file still saying "zhipu" is
    # live config, not an unconfigured provider.
    import json

    from raven.config.update_providers import get_provider_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"providers": {"zhipu": {"apiKey": "OLD"}}}))

    assert get_provider_config("zai", config_path=path, redact_secrets=False)["api_key"] == "OLD"


def test_a_half_migrated_config_keeps_fields_only_the_old_name_had() -> None:
    # Credentials under the old name, a model list under the new one: taking
    # either section whole would drop the other's fields on the next write.
    cfg = _config(
        zhipu={"apiKey": "K", "apiBase": "https://custom.example/v1"},
        zai={"models": ["zai/glm-4.6"]},
    )

    section = cfg.providers.get("zai")
    assert section.api_key == "K"
    assert section.api_base == "https://custom.example/v1"
    assert section.models == ["zai/glm-4.6"]


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


def test_a_forced_provider_skips_matching_entirely() -> None:
    cfg = _config(openai={"apiKey": "K-OPENAI"}, openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "claude-sonnet-5"
    cfg.agents.defaults.provider = "openai"

    assert cfg.get_provider_name() == "openai"


def test_a_gateway_prefixed_model_never_borrows_another_vendors_key() -> None:
    # LiteLLM routes by the prefix, so this request goes to OpenRouter. Handing
    # back Anthropic's key would post that key to OpenRouter.
    cfg = _config(anthropic={"apiKey": "K-ANTHROPIC"})
    cfg.agents.defaults.model = "openrouter/anthropic/claude-sonnet-4-5"

    assert cfg.get_provider_name() is None
    assert cfg.get_api_key() is None


def test_a_gateway_prefixed_model_still_works_with_the_gateways_key() -> None:
    cfg = _config(openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "openrouter/anthropic/claude-sonnet-4-5"

    assert cfg.get_provider_name() == "openrouter"


def test_a_bare_vendor_id_may_still_be_served_by_a_gateway() -> None:
    # The long-standing way to reach a vendor through OpenRouter; narrowing the
    # gateway case must not take it away.
    cfg = _config(openrouter={"apiKey": "sk-or-K"})
    cfg.agents.defaults.model = "anthropic/claude-x"

    assert cfg.get_provider_name() == "openrouter"


def test_specs_are_findable_by_a_former_name() -> None:
    # Call sites all over the codebase look specs up by whatever string a config
    # or command line handed them, so the rename has to land in the lookup.
    from raven.providers.registry import find_by_name

    assert find_by_name("zhipu") is find_by_name("zai")


def test_a_gateway_prefixed_model_resolves_to_the_gateway_spec() -> None:
    # The picker and the wizard derive the current provider from the model id.
    from raven.providers.registry import find_by_model

    spec = find_by_model("openrouter/anthropic/claude-sonnet-4-5")
    assert spec is not None and spec.name == "openrouter"


@pytest.mark.parametrize(
    ("spelling", "field"),
    [
        ("azureOpenai", "azure_openai"),
        ("azure-openai", "azure_openai"),
        ("AzureOpenai", "azure_openai"),
        ("nanoGpt", "nano_gpt"),
        ("nano-gpt", "nano_gpt"),
    ],
)
def test_the_management_surface_reads_a_section_under_any_spelling(spelling: str, field: str, tmp_path) -> None:
    """`provider get` must see what the runtime sees.

    The two used different comparisons, so a section stored under one spelling
    was invisible here while the runtime read it happily.
    """
    from raven.config.update_providers import get_provider_config

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"providers": {spelling: {"apiKey": "REAL-KEY"}}}))
    assert get_provider_config(field, redact_secrets=False, config_path=cfg)["api_key"] == "REAL-KEY"


@pytest.mark.parametrize(
    ("spelling", "field"),
    [
        ("azureOpenai", "azure_openai"),
        ("azure-openai", "azure_openai"),
        ("nanoGpt", "nano_gpt"),
    ],
)
def test_writing_a_provider_never_leaves_a_second_section_or_drops_its_key(spelling: str, field: str, tmp_path) -> None:
    """A write must consolidate, not add a rival section.

    Retiring only the spellings one comparison recognised left the other behind.
    The declared field is then present and empty, and merging it over the stored
    section erased the credential the user had actually written.
    """
    from raven.config.update_providers import set_provider_fields

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"providers": {spelling: {"apiKey": "REAL-KEY"}}}))
    set_provider_fields(field, {"api_base": "https://example.invalid/v1"}, config_path=cfg)

    stored = json.loads(cfg.read_text())["providers"]
    assert len(stored) == 1, f"expected one section, got {sorted(stored)}"
    resolved = Config.model_validate({"providers": stored}).providers.get(field)
    assert resolved is not None
    assert resolved.api_key == "REAL-KEY", "the write erased the key it was not asked to touch"
    assert resolved.api_base == "https://example.invalid/v1"


def test_a_placeholder_section_does_not_erase_a_key_stored_under_another_spelling() -> None:
    """A config that already holds both spellings must keep the real credential.

    Older builds could leave two sections for one provider. The declared field is
    always present, so merging it over the other verbatim let its empty apiKey
    win -- the credential was in the file and unreachable.
    """
    config = Config.model_validate(
        {"providers": {"azureOpenai": {"apiKey": "REAL-KEY"}, "azure_openai": {"apiKey": ""}}}
    )
    section = config.providers.get("azure_openai")
    assert section is not None and section.api_key == "REAL-KEY"


def test_the_current_name_still_wins_a_real_conflict() -> None:
    """Preferring a set value must not demote the current name when both are set."""
    config = Config.model_validate(
        {"providers": {"zhipu": {"apiKey": "OLD", "apiBase": "https://old/v1"}, "zai": {"apiKey": "NEW"}}}
    )
    section = config.providers.get("zai")
    assert section is not None
    assert section.api_key == "NEW", "the current name must win where both hold a value"
    assert section.api_base == "https://old/v1", "a field only the old section had must survive"
