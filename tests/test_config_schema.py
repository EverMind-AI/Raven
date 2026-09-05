"""Tests for ``raven.config.schema.AgentDefaults.context_window_tokens`` and
``ProviderConfig.endpoints``.

The context-window tests: None (or 0) means "figure it out" against the
model's real window; a positive value pins it. See
``raven.providers.rates.effective_context_window`` for the ladder that reads
this field.

The endpoints tests: ``ProviderEndpoint`` is the S1 data shape for a provider
section holding several url/key/header groups. See
``raven.providers.endpoints.provider_endpoints`` for the read point that
resolves it against the older flat/``api_key_list`` shapes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from raven.config.schema import AgentDefaults, ProviderConfig, ProviderEndpoint


def test_context_window_tokens_defaults_to_none() -> None:
    assert AgentDefaults().context_window_tokens is None


def test_context_window_tokens_explicit_value_round_trips() -> None:
    defaults = AgentDefaults(context_window_tokens=200_000)
    assert defaults.context_window_tokens == 200_000


def test_context_window_tokens_camel_alias_round_trips() -> None:
    defaults = AgentDefaults.model_validate({"contextWindowTokens": 200_000})
    assert defaults.context_window_tokens == 200_000
    assert defaults.model_dump(by_alias=True)["contextWindowTokens"] == 200_000


def test_provider_endpoints_defaults_to_empty_list() -> None:
    assert ProviderConfig().endpoints == []


def test_provider_endpoint_round_trips_with_camel_alias() -> None:
    endpoint = ProviderEndpoint.model_validate(
        {"label": "us-east", "apiKey": "sk-1", "apiBase": "https://a.example", "extraHeaders": {"X-Region": "us"}}
    )
    assert endpoint.label == "us-east"
    assert endpoint.api_key == "sk-1"
    assert endpoint.api_base == "https://a.example"
    assert endpoint.extra_headers == {"X-Region": "us"}

    dumped = endpoint.model_dump(by_alias=True)
    assert dumped["apiKey"] == "sk-1"
    assert dumped["apiBase"] == "https://a.example"
    assert dumped["extraHeaders"] == {"X-Region": "us"}


def test_provider_endpoint_defaults() -> None:
    endpoint = ProviderEndpoint(label="only")
    assert endpoint.api_key == ""
    assert endpoint.api_base is None
    assert endpoint.extra_headers is None


def test_provider_config_endpoints_round_trip_with_camel_alias() -> None:
    section = ProviderConfig.model_validate(
        {"endpoints": [{"label": "primary", "apiKey": "sk-1"}, {"label": "backup", "apiKey": "sk-2"}]}
    )
    assert [e.label for e in section.endpoints] == ["primary", "backup"]

    dumped = section.model_dump(by_alias=True)
    assert dumped["endpoints"][0]["apiKey"] == "sk-1"
    assert dumped["endpoints"][1]["label"] == "backup"


def test_endpoint_strategy_defaults_to_sticky() -> None:
    assert ProviderConfig().endpoint_strategy == "sticky"


def test_endpoint_strategy_accepts_round_robin_with_camel_alias() -> None:
    section = ProviderConfig.model_validate({"endpointStrategy": "round_robin"})
    assert section.endpoint_strategy == "round_robin"


def test_endpoint_strategy_rejects_an_unknown_value() -> None:
    with pytest.raises(ValidationError):
        ProviderConfig.model_validate({"endpointStrategy": "random"})


def test_empty_endpoint_label_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ProviderEndpoint(label="")


def test_duplicate_endpoint_labels_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate endpoint label"):
        ProviderConfig.model_validate(
            {"endpoints": [{"label": "primary", "apiKey": "sk-1"}, {"label": "primary", "apiKey": "sk-2"}]}
        )


def test_distinct_endpoint_labels_are_accepted() -> None:
    section = ProviderConfig.model_validate(
        {"endpoints": [{"label": "primary", "apiKey": "sk-1"}, {"label": "backup", "apiKey": "sk-2"}]}
    )
    assert [e.label for e in section.endpoints] == ["primary", "backup"]


def test_ask_user_timeout_is_configurable_per_surface() -> None:
    """The wait belongs to the surface: a chat channel and a rendered page sit
    out a silent user very differently, and 600s was hardcoded for both."""
    from raven.config.schema import AskUserToolConfig, ToolsConfig

    assert ToolsConfig().ask_user.timeout == 600
    assert AskUserToolConfig(timeout=90).timeout == 90


def test_ask_user_timeout_must_be_positive() -> None:
    from raven.config.schema import AskUserToolConfig

    with pytest.raises(ValidationError):
        AskUserToolConfig(timeout=0)
