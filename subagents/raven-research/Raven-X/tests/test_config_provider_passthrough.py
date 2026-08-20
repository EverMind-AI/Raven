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


def test_an_unregistered_provider_counts_as_configured(tmp_path) -> None:
    """The startup gate has to see it, or the wizard runs forever.

    Runtime routing already worked, but list_providers only enumerated declared
    fields -- so the gate read "no provider configured", forced the wizard, and
    the wizard declines to configure exactly this kind of vendor.
    """
    import json

    from raven.config.loader import set_config_path
    from raven.config.update_providers import list_providers

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"providers": {"mistral": {"apiKey": "K"}}}))
    set_config_path(path)

    rows = {row["name"]: row["configured"] for row in list_providers()}
    assert rows.get("mistral") is True


def test_a_hyphenated_vendor_name_reads_back(tmp_path) -> None:
    # LiteLLM names some vendors with a hyphen, which is the spelling the write
    # path accepts. Matching normalizes to underscores, so looking up only one
    # form left a section that was written correctly unreachable.
    for section, model in (
        ("nano-gpt", "nano-gpt/gpt-4o"),
        ("nano-gpt", "nano_gpt/gpt-4o"),
        ("nano_gpt", "nano-gpt/gpt-4o"),
    ):
        cfg = Config.model_validate({"providers": {section: {"apiKey": "K"}}})
        cfg.agents.defaults.model = model
        assert cfg.get_api_key() == "K", f"{section} / {model}"


def test_a_local_deployment_answers_to_litellms_spelling_and_its_own_former_name() -> None:
    # A local deployment's section name IS LiteLLM's spelling for it, so the id
    # and the section agree; the pre-rename spelling still resolves so saved
    # configs keep working. A configured gateway must not intercept either.
    for model, expected in (
        ("hosted_vllm/my-model", "hosted_vllm"),
        ("vllm/my-model", "hosted_vllm"),
        ("ollama_chat/llama3.2", "ollama_chat"),
        ("ollama/llama3.2", "ollama_chat"),
    ):
        cfg = Config.model_validate(
            {
                "providers": {"openrouter": {"apiKey": "sk-or-K"}, "hosted_vllm": {"apiBase": "http://x/v1"}}
                if expected == "hosted_vllm"
                else {"openrouter": {"apiKey": "sk-or-K"}, "ollama_chat": {"apiBase": "http://x:11434"}}
            }
        )
        cfg.agents.defaults.model = model
        assert cfg.get_provider_name() == expected, model


def test_an_empty_section_does_not_answer_for_a_provider() -> None:
    # Every declared provider exists as an empty section, so presence proves
    # nothing. An empty one used to win over the credentials the user had really
    # written under another of that provider's names.
    cfg = Config.model_validate({"providers": {"hosted_vllm": {}, "openrouter": {"apiKey": "sk-or-K"}}})
    cfg.agents.defaults.model = "hosted_vllm/my-model"
    assert cfg.get_provider_name() != "hosted_vllm"


def test_an_unrecognizable_provider_key_does_not_break_listing(tmp_path) -> None:
    """A typo must not cost the user their ability to start Raven.

    Keeping unknown keys means they reach the enumeration, and raising there took
    down `provider list` and the startup gate together -- leaving no way in, not
    even the wizard that would fix the file.
    """
    import json

    from raven.config.loader import set_config_path
    from raven.config.update_providers import list_providers

    for typo in ("openRouter", "azureOpenAI", "totally-bogus"):
        path = tmp_path / f"{typo}.json"
        path.write_text(json.dumps({"providers": {typo: {"apiKey": "K"}, "openai": {"apiKey": "K"}}}))
        set_config_path(path)

        rows = {row["name"]: row["configured"] for row in list_providers()}
        assert rows["openai"] is True, typo
        assert typo not in rows, typo


def test_listing_survives_litellm_being_unavailable(tmp_path) -> None:
    """Reporting must not fail on "we cannot check".

    Name validation asks LiteLLM whether it knows a vendor. Answering "no" when
    LiteLLM cannot be consulted would call a correct name wrong, so it raises --
    and listing has to tolerate that, or the startup gate goes down with it.
    """
    import json
    from unittest.mock import patch

    from raven.config.loader import set_config_path
    from raven.config.update_providers import list_providers

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"providers": {"mistral": {"apiKey": "K"}, "openai": {"apiKey": "K"}}}))
    set_config_path(path)

    with patch("raven.providers.litellm_setup.import_litellm", side_effect=RuntimeError("broken")):
        rows = {row["name"]: row["configured"] for row in list_providers()}

    assert rows["openai"] is True


@pytest.mark.parametrize(
    "spelling",
    ["azure_openai", "azureOpenai", "azure-openai", "AzureOpenai", "AZURE_OPENAI"],
)
def test_a_declared_provider_is_found_under_any_spelling(spelling: str) -> None:
    """Declared fields follow the same spelling rule as passthrough sections.

    A declared field exists as an empty section whether or not it was configured,
    so a differently-spelled key landing in extras used to lose to that empty
    field -- the key the user really wrote read back as unset, silently.
    """
    cfg = Config.model_validate({"providers": {spelling: {"apiKey": "sk-probe"}}})
    section = cfg.providers.get("azure_openai")
    assert section is not None and section.api_key == "sk-probe"
