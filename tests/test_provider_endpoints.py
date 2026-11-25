"""Tests for raven.providers.endpoints -- the one read point over a provider
section's three ways of holding url/key/header material.

The three shapes (``endpoints``, Gemini's ``api_key_list``, the flat fields)
do not mix: whichever is most specific wins outright, not merged with the
others. These assert that precedence and the empty-section fallback.
"""

from __future__ import annotations

from raven.config.schema import GeminiProviderConfig, ProviderConfig, ProviderEndpoint
from raven.providers.endpoints import ResolvedEndpoint, provider_endpoints, provider_endpoints_for_request


def test_flat_fields_synthesize_a_single_default_endpoint() -> None:
    section = ProviderConfig(api_key="sk-flat", api_base="https://flat.example", extra_headers={"X-A": "1"})

    assert provider_endpoints(section) == [
        ResolvedEndpoint(
            label="default", api_key="sk-flat", api_base="https://flat.example", extra_headers={"X-A": "1"}
        )
    ]


def test_empty_section_resolves_to_one_empty_endpoint_not_an_empty_list() -> None:
    assert provider_endpoints(ProviderConfig()) == [
        ResolvedEndpoint(label="default", api_key="", api_base=None, extra_headers=None)
    ]


def test_endpoints_list_is_used_verbatim() -> None:
    section = ProviderConfig(
        endpoints=[
            ProviderEndpoint(label="primary", api_key="sk-1", api_base="https://a.example"),
            ProviderEndpoint(label="backup", api_key="sk-2", api_base="https://b.example", extra_headers={"X-B": "2"}),
        ]
    )

    assert provider_endpoints(section) == [
        ResolvedEndpoint(label="primary", api_key="sk-1", api_base="https://a.example", extra_headers=None),
        ResolvedEndpoint(label="backup", api_key="sk-2", api_base="https://b.example", extra_headers={"X-B": "2"}),
    ]


def test_endpoints_list_takes_priority_over_flat_key_but_inherits_missing_base_and_headers() -> None:
    """``api_key`` never inherits -- a stale flat key must not outlive the
    endpoint meant to replace it. ``api_base``/``extra_headers`` do, the same
    way an ``api_key_list`` entry already shares the flat address: an
    ``endpoint add`` that only ever set ``--label``/``--api-key`` still needs
    somewhere to send the request.
    """
    section = ProviderConfig(
        api_key="sk-flat",
        api_base="https://flat.example",
        extra_headers={"X-Flat": "1"},
        endpoints=[ProviderEndpoint(label="only", api_key="sk-1")],
    )

    resolved = provider_endpoints(section)

    assert resolved == [
        ResolvedEndpoint(label="only", api_key="sk-1", api_base="https://flat.example", extra_headers={"X-Flat": "1"})
    ]


def test_endpoint_with_no_key_does_not_inherit_the_flat_key() -> None:
    """The one field that never inherits, even though the others do."""
    section = ProviderConfig(
        api_key="sk-flat",
        api_base="https://flat.example",
        endpoints=[ProviderEndpoint(label="only")],
    )

    resolved = provider_endpoints(section)

    assert resolved == [ResolvedEndpoint(label="only", api_key="", api_base="https://flat.example", extra_headers=None)]


def test_endpoints_own_base_and_headers_win_over_the_flat_ones() -> None:
    """Inheritance only fills a gap; an endpoint that names its own wins."""
    section = ProviderConfig(
        api_base="https://flat.example",
        extra_headers={"X-Flat": "1"},
        endpoints=[
            ProviderEndpoint(label="only", api_key="sk-1", api_base="https://own.example", extra_headers={"X-Own": "2"})
        ],
    )

    resolved = provider_endpoints(section)

    assert resolved == [
        ResolvedEndpoint(label="only", api_key="sk-1", api_base="https://own.example", extra_headers={"X-Own": "2"})
    ]


def test_endpoints_without_their_own_base_all_inherit_the_flat_one() -> None:
    """``endpoint add`` with no ``--api-base`` must still be able to run --
    every entry missing one falls back to the section's flat address, not just
    the first."""
    section = ProviderConfig(
        api_base="http://10.0.0.5:8000/v1",
        endpoints=[
            ProviderEndpoint(label="a", api_key="k1"),
            ProviderEndpoint(label="b", api_key="k2"),
        ],
    )

    resolved = provider_endpoints(section)

    assert [e.api_base for e in resolved] == ["http://10.0.0.5:8000/v1", "http://10.0.0.5:8000/v1"]


def test_gemini_api_key_list_yields_one_endpoint_per_key() -> None:
    section = GeminiProviderConfig(api_key_list=["k1", "k2", "k3"], api_base="https://gemini.example")

    assert provider_endpoints(section) == [
        ResolvedEndpoint(label="key-1", api_key="k1", api_base="https://gemini.example", extra_headers=None),
        ResolvedEndpoint(label="key-2", api_key="k2", api_base="https://gemini.example", extra_headers=None),
        ResolvedEndpoint(label="key-3", api_key="k3", api_base="https://gemini.example", extra_headers=None),
    ]


def test_gemini_api_key_list_shares_the_flat_api_base_and_headers() -> None:
    section = GeminiProviderConfig(
        api_key_list=["k1", "k2"], api_base="https://gemini.example", extra_headers={"X-G": "1"}
    )

    resolved = provider_endpoints(section)

    assert all(e.api_base == "https://gemini.example" for e in resolved)
    assert all(e.extra_headers == {"X-G": "1"} for e in resolved)


def test_gemini_api_key_list_takes_priority_over_flat_api_key_without_duplication() -> None:
    section = GeminiProviderConfig(api_key="sk-flat", api_key_list=["k1", "k2"])

    resolved = provider_endpoints(section)

    assert [e.api_key for e in resolved] == ["k1", "k2"]
    assert "sk-flat" not in [e.api_key for e in resolved]


def test_gemini_without_api_key_list_falls_back_to_flat_fields() -> None:
    section = GeminiProviderConfig(api_key="sk-flat", api_base="https://gemini.example")

    assert provider_endpoints(section) == [
        ResolvedEndpoint(label="default", api_key="sk-flat", api_base="https://gemini.example", extra_headers=None)
    ]


def test_gemini_endpoints_list_takes_priority_over_api_key_list() -> None:
    section = GeminiProviderConfig(
        api_key_list=["k1", "k2"],
        endpoints=[ProviderEndpoint(label="only", api_key="sk-endpoint")],
    )

    resolved = provider_endpoints(section)

    assert [e.label for e in resolved] == ["only"]
    assert [e.api_key for e in resolved] == ["sk-endpoint"]


def test_explicit_endpoints_keep_their_keys_when_an_env_reference_is_also_named(monkeypatch) -> None:
    """``apiKeyEnv`` stands in for the flat key only.

    A section that grew an explicit endpoint keeps that endpoint's key: the
    environment variable is a leftover from before, and it must not shadow the
    key the section declares.
    """
    monkeypatch.setenv("PROVIDER_KEY", "sk-from-env")
    section = ProviderConfig(
        api_key_env="PROVIDER_KEY",
        endpoints=[ProviderEndpoint(label="only", api_key="sk-explicit")],
    )

    resolved = provider_endpoints_for_request(section)

    assert [e.api_key for e in resolved] == ["sk-explicit"]


def test_an_api_key_list_keeps_its_keys_when_an_env_reference_is_also_named(monkeypatch) -> None:
    monkeypatch.setenv("PROVIDER_KEY", "sk-from-env")
    section = GeminiProviderConfig(api_key_env="PROVIDER_KEY", api_key_list=["k1", "k2"])

    resolved = provider_endpoints_for_request(section)

    assert [e.api_key for e in resolved] == ["k1", "k2"]


def test_an_env_reference_that_names_an_unset_variable_does_not_fail_a_declared_section() -> None:
    """The variable is never read for a section that declares its own keys.

    Reading it would raise a missing-credentials error at request time for a
    section that has nothing to ask the environment for.
    """
    section = ProviderConfig(
        api_key_env="PROVIDER_KEY_THAT_IS_NOT_SET",
        endpoints=[ProviderEndpoint(label="only", api_key="sk-explicit")],
    )

    resolved = provider_endpoints_for_request(section)

    assert [e.api_key for e in resolved] == ["sk-explicit"]


def test_a_raw_config_mapping_names_the_variable_too(monkeypatch) -> None:
    """A section read straight from config.json is a mapping, not a schema object.

    Raw mappings spell the field camelCase, and both spellings are read.
    """
    monkeypatch.setenv("PROVIDER_KEY", "sk-from-env")

    resolved = provider_endpoints_for_request({"apiKeyEnv": "PROVIDER_KEY"})

    assert [e.api_key for e in resolved] == ["sk-from-env"]


def test_a_raw_config_mapping_keeps_its_api_key_list_when_an_env_reference_is_also_named(monkeypatch) -> None:
    """``apiKeyEnv`` stands in for the flat key only, a raw mapping included.

    The file spells the list ``apiKeyList``; reading only the schema spelling
    let the guard miss it and the environment value replace every declared key.
    """
    monkeypatch.setenv("PROVIDER_KEY", "sk-from-env")

    resolved = provider_endpoints_for_request({"apiKeyEnv": "PROVIDER_KEY", "apiKeyList": ["k1", "k2"]})

    assert [e.api_key for e in resolved] == ["k1", "k2"]


def test_a_raw_config_mapping_reads_an_endpoint_api_key(monkeypatch) -> None:
    """An endpoint read straight from config.json spells its key ``apiKey``.

    The declared endpoint already keeps the environment value out; its own key
    is the only key there is, and reading only the schema spelling lost it.
    """
    monkeypatch.setenv("PROVIDER_KEY", "sk-from-env")

    resolved = provider_endpoints_for_request(
        {"apiKeyEnv": "PROVIDER_KEY", "endpoints": [{"label": "only", "apiKey": "sk-explicit"}]}
    )

    assert [e.api_key for e in resolved] == ["sk-explicit"]
