"""Unit tests for ``raven.cli._helpers``.

Currently focused on ``send_probe`` — the shared LLM probe used by
``onboard`` Step 3 and ``doctor --probe``. Provider and config are
stubbed so the test never touches network or disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.cli import _helpers
from raven.cli._helpers import send_probe


@pytest.fixture
def stub_load_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_config`` is lazy-imported inside ``send_probe`` — patch at source."""
    monkeypatch.setattr(
        "raven.config.loader.load_config",
        lambda: object(),
    )


def test_send_probe_success(monkeypatch: pytest.MonkeyPatch, stub_load_config: None) -> None:
    """Happy path: provider returns a normal response → tuple shape correct."""

    class _FakeProvider:
        async def chat_with_retry(self, **_kwargs):
            return SimpleNamespace(
                finish_reason="stop",
                content="Hello world",
                usage={"total_tokens": 42},
            )

    monkeypatch.setattr(_helpers, "make_provider", lambda _config: _FakeProvider())

    text, tokens, elapsed = send_probe()

    assert text == "Hello world"
    assert tokens == 42
    assert elapsed >= 0


def test_send_probe_provider_error_raises(monkeypatch: pytest.MonkeyPatch, stub_load_config: None) -> None:
    """``finish_reason='error'`` → ``send_probe`` raises ``RuntimeError``."""

    class _ErrProvider:
        async def chat_with_retry(self, **_kwargs):
            return SimpleNamespace(
                finish_reason="error",
                content="AuthenticationError: bad key",
                usage=None,
            )

    monkeypatch.setattr(_helpers, "make_provider", lambda _config: _ErrProvider())

    with pytest.raises(RuntimeError, match="bad key"):
        send_probe()


def test_send_probe_timeout_raises(monkeypatch: pytest.MonkeyPatch, stub_load_config: None) -> None:
    """Slow provider trips ``asyncio.TimeoutError`` when ``timeout_s`` elapses."""

    class _SlowProvider:
        async def chat_with_retry(self, **_kwargs):
            await asyncio.sleep(5)
            return SimpleNamespace(finish_reason="stop", content="", usage=None)

    monkeypatch.setattr(_helpers, "make_provider", lambda _config: _SlowProvider())

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        send_probe(timeout_s=1)


# ---------------------------------------------------------------------------
# make_provider — custom routes through LiteLLM (so it gets streaming)
# ---------------------------------------------------------------------------


def test_make_provider_custom_routes_through_litellm(tmp_path: Path) -> None:
    from raven.config.loader import load_config
    from raven.providers.litellm_provider import LiteLLMProvider

    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "my-model", "provider": "custom"}},
                "providers": {"custom": {"apiKey": "sk-x", "apiBase": "http://localhost:9000/v1"}},
            }
        ),
        encoding="utf-8",
    )
    provider = _helpers.make_provider(load_config(p))
    assert isinstance(provider, LiteLLMProvider)


# ---------------------------------------------------------------------------
# check_provider_credentials — fail-fast without importing litellm
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, *, api_key: str | None) -> Path:
    provider: dict = {"apiBase": "http://localhost:9000/v1"}
    if api_key is not None:
        provider["apiKey"] = api_key
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "my-model", "provider": "custom"}},
                "providers": {"custom": provider},
            }
        ),
        encoding="utf-8",
    )
    return p


def test_check_provider_credentials_raises_when_no_key(tmp_path: Path) -> None:
    """Raises rather than printing and exiting: three entry points ask this, and
    only one of them is a terminal. The sentence travels with the exception so
    each renders it in its own idiom."""
    from raven.config.loader import load_config
    from raven.providers.auth import MissingCredentialsError

    with pytest.raises(MissingCredentialsError) as excinfo:
        _helpers.check_provider_credentials(load_config(_write_config(tmp_path, api_key=None)))

    assert "API key" in excinfo.value.summary
    assert excinfo.value.provider


def test_check_provider_credentials_passes_with_key(tmp_path: Path) -> None:
    from raven.config.loader import load_config

    _helpers.check_provider_credentials(load_config(_write_config(tmp_path, api_key="sk-x")))  # no raise


def test_make_lazy_provider_returns_lazy_without_building(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``make_lazy_provider`` returns a LazyProvider that answers ``get_default_model``
    from config without building the real (litellm-importing) provider."""
    from raven.config.loader import load_config
    from raven.providers.lazy import LazyProvider

    # Stub the real build so prewarm never imports litellm.
    monkeypatch.setattr(_helpers, "make_provider", lambda _c: SimpleNamespace(name="real"))

    provider = _helpers.make_lazy_provider(load_config(_write_config(tmp_path, api_key="sk-x")))

    assert isinstance(provider, LazyProvider)
    assert provider.get_default_model() == "my-model"


def test_make_lazy_provider_carries_the_first_endpoint_label_for_a_multi_endpoint_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session footer reads ``active_endpoint_label`` before the first call, when
    the real (rotor-wrapping) provider has not been built yet. For a section
    with several endpoints, ``make_lazy_provider`` must hand the lazy wrapper
    the first entry's label so that read answers something instead of
    always ``None``."""
    from raven.config.schema import Config
    from raven.providers.lazy import LazyProvider

    monkeypatch.setattr(_helpers, "make_provider", lambda _c: SimpleNamespace(name="real"))
    # Real prewarm races a background thread against this assertion -- disable
    # it so the test observes the pre-materialization state deterministically.
    monkeypatch.setattr(LazyProvider, "prewarm", lambda self: None)
    config = Config.model_validate(
        {
            "providers": {
                "custom": {
                    "endpoints": [
                        {"label": "first", "apiKey": "k1", "apiBase": "https://first.example"},
                        {"label": "second", "apiKey": "k2", "apiBase": "https://second.example"},
                    ]
                }
            },
            "agents": {"defaults": {"model": "my-model", "provider": "custom"}},
        }
    )

    provider = _helpers.make_lazy_provider(config)

    assert isinstance(provider, LazyProvider)
    assert provider.active_endpoint_label == "first"


def test_make_lazy_provider_has_no_endpoint_label_for_a_single_endpoint_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A section with only one endpoint (flat or explicit) never rotates, so
    there is nothing for the footer to name -- ``active_endpoint_label`` is
    ``None`` rather than a label nobody will ever see it change from."""
    from raven.providers.lazy import LazyProvider

    monkeypatch.setattr(_helpers, "make_provider", lambda _c: SimpleNamespace(name="real"))
    from raven.config.loader import load_config

    provider = _helpers.make_lazy_provider(load_config(_write_config(tmp_path, api_key="sk-x")))

    assert isinstance(provider, LazyProvider)
    assert provider.active_endpoint_label is None


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai_codex", "openai-codex/gpt-5.3-codex", "OpenAICodexProvider"),
        ("minimax_global", "minimax-global/MiniMax-M3", "MiniMaxOAuthProvider"),
        ("azure_openai", "azure_openai/my-deployment", "AzureOpenAIProvider"),
        ("deepseek", "deepseek/deepseek-chat", "LiteLLMProvider"),
    ],
)
def test_which_client_serves_a_provider_is_read_from_the_registry(
    provider: str,
    model: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory dispatches on the spec, so a family is added by declaring it.

    The name comparisons this replaces had drifted: a Codex model id spelled the
    old way needed its own check beside the resolved provider name.
    """
    from raven.cli._helpers import make_provider
    from raven.config.schema import Config

    config = Config.model_validate(
        {
            "providers": {provider: {"apiKey": "k", "apiBase": "https://example.test"}},
            "agents": {"defaults": {"model": model, "provider": provider}},
        }
    )
    monkeypatch.setattr("raven.cli._helpers.check_provider_credentials", lambda _config: None)

    assert type(make_provider(config)).__name__ == expected


def _config_for(tmp_path: Path, provider: str, model: str, section: dict) -> Path:
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": model, "provider": provider}},
                "providers": {provider: section},
            }
        ),
        encoding="utf-8",
    )
    return p


def test_an_empty_config_section_is_not_a_rejection_for_an_oauth_provider(monkeypatch, tmp_path: Path) -> None:
    """A signed-in OAuth provider passes with nothing in its config section.

    Its credential is a token file, so an empty section is the normal shape and
    must not read as "no API key". Asserted through behaviour: a source scan
    passed while the branch was dead.
    """
    from raven.config.loader import load_config

    monkeypatch.setattr("raven.providers.chatgpt_token.stored_credentials", lambda: {"access_token": "t"})
    cfg = _config_for(tmp_path, "openai_codex", "openai-codex/gpt-5.6-sol", {})

    _helpers.check_provider_credentials(load_config(cfg))  # no raise: the token is not in config


def test_an_oauth_provider_that_was_never_signed_in_is_told_to_sign_in(monkeypatch, tmp_path: Path, capsys) -> None:
    """Missing credentials name the fix, rather than surfacing at the first call.

    This gate used to return early for Codex without checking anything, so an
    agent configured for it started and then failed on the first request with
    whatever the backend said about an absent token.
    """
    from raven.config.loader import load_config
    from raven.providers.auth import MissingCredentialsError

    monkeypatch.setattr("raven.providers.chatgpt_token.stored_credentials", lambda: None)
    cfg = _config_for(tmp_path, "openai_codex", "openai-codex/gpt-5.6-sol", {})

    with pytest.raises(MissingCredentialsError) as excinfo:
        _helpers.check_provider_credentials(load_config(cfg))

    # Carried on the exception rather than printed: the TUI reaches this same
    # check and a stdout line there goes to a log nobody is reading.
    assert "raven provider login openai-codex" in excinfo.value.summary


def test_the_credential_check_wants_both_halves_of_an_azure_endpoint(tmp_path: Path) -> None:
    from raven.config.loader import load_config
    from raven.providers.auth import MissingCredentialsError

    # A key without an address is the half Azure cannot work with, and the generic
    # "no API key" message would not say which half is missing.
    cfg = _config_for(tmp_path, "azure_openai", "my-deployment", {"apiKey": "az-key"})

    with pytest.raises(MissingCredentialsError):
        _helpers.check_provider_credentials(load_config(cfg))


# ---------------------------------------------------------------------------
# make_provider — several endpoints under one section (S3)
# ---------------------------------------------------------------------------


def test_make_provider_builds_a_rotor_over_several_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """More than one endpoint fans out behind an ``EndpointRotorProvider``,
    one inner ``LiteLLMProvider`` per entry."""
    from raven.config.schema import Config
    from raven.providers.endpoint_rotor import EndpointRotorProvider
    from raven.providers.litellm_provider import LiteLLMProvider

    config = Config.model_validate(
        {
            "providers": {
                "custom": {
                    "endpoints": [
                        {"label": "a", "apiKey": "k1", "apiBase": "https://a.example"},
                        {"label": "b", "apiKey": "k2", "apiBase": "https://b.example"},
                    ]
                }
            },
            "agents": {"defaults": {"model": "my-model", "provider": "custom"}},
        }
    )
    monkeypatch.setattr("raven.cli._helpers.check_provider_credentials", lambda _config: None)

    provider = _helpers.make_provider(config)

    assert isinstance(provider, EndpointRotorProvider)
    assert len(provider._inners) == 2
    assert all(isinstance(inner, LiteLLMProvider) for inner in provider._inners)


def test_make_provider_a_single_endpoint_entry_still_returns_a_plain_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One entry in ``endpoints`` takes the unchanged single-provider path,
    same as the flat ``apiKey``/``apiBase`` fields -- no rotor for one."""
    from raven.config.schema import Config
    from raven.providers.litellm_provider import LiteLLMProvider

    config = Config.model_validate(
        {
            "providers": {"custom": {"endpoints": [{"label": "only", "apiKey": "k1"}]}},
            "agents": {"defaults": {"model": "my-model", "provider": "custom"}},
        }
    )
    monkeypatch.setattr("raven.cli._helpers.check_provider_credentials", lambda _config: None)

    provider = _helpers.make_provider(config)

    assert type(provider) is LiteLLMProvider


def test_make_provider_single_endpoint_entry_credentials_are_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A section declaring exactly one entry under ``endpoints`` has nothing in
    the flat ``apiKey``/``apiBase``/``extraHeaders`` fields -- reading those
    instead of the endpoint (the shape this section actually used) silently
    sent an empty key. The single-endpoint path must read the endpoint."""
    from raven.config.schema import Config
    from raven.providers.litellm_provider import LiteLLMProvider

    config = Config.model_validate(
        {
            "providers": {
                "custom": {
                    "endpoints": [
                        {
                            "label": "only",
                            "apiKey": "k-only",
                            "apiBase": "https://only.example",
                            "extraHeaders": {"X-Only": "1"},
                        }
                    ]
                }
            },
            "agents": {"defaults": {"model": "my-model", "provider": "custom"}},
        }
    )
    monkeypatch.setattr("raven.cli._helpers.check_provider_credentials", lambda _config: None)

    provider = _helpers.make_provider(config)

    assert type(provider) is LiteLLMProvider
    # The reverse-case shape a prior review caught live: this must not be empty.
    assert provider.api_key == "k-only"
    assert provider.api_base == "https://only.example"
    assert provider.extra_headers == {"X-Only": "1"}


def test_make_provider_flat_config_is_equivalent_through_the_endpoint_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain flat ``apiKey``/``apiBase`` section (no ``endpoints`` field) is
    synthesized by ``provider_endpoints`` into a single endpoint and now goes
    through the same single-endpoint-material path as an explicit one. The
    result must match what the flat fields alone produced before this change."""
    from raven.config.schema import Config
    from raven.providers.litellm_provider import LiteLLMProvider

    config = Config.model_validate(
        {
            "providers": {
                "custom": {
                    "apiKey": "k-flat",
                    "apiBase": "https://flat.example",
                    "extraHeaders": {"X-Flat": "1"},
                }
            },
            "agents": {"defaults": {"model": "my-model", "provider": "custom"}},
        }
    )
    monkeypatch.setattr("raven.cli._helpers.check_provider_credentials", lambda _config: None)

    provider = _helpers.make_provider(config)

    assert type(provider) is LiteLLMProvider
    assert provider.api_key == "k-flat"
    assert provider.api_base == "https://flat.example"
    assert provider.extra_headers == {"X-Flat": "1"}


@pytest.mark.parametrize(
    ("provider", "model", "extra_section"),
    [
        ("openai_codex", "openai-codex/gpt-5.3-codex", {}),
        ("minimax_global", "minimax-global/MiniMax-M3", {}),
        ("azure_openai", "azure_openai/my-deployment", {}),
        ("github_copilot", "github_copilot/gpt-4o", {}),
    ],
)
def test_make_provider_rejects_endpoints_on_providers_that_cannot_rotate(
    provider: str,
    model: str,
    extra_section: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex / minimax_oauth / azure need more than a key and an address, and
    an OAuth section connects through one signed-in account -- ``endpoints``
    on any of them is a configuration error at construction time, not a
    silently-ignored field."""
    from raven.config.schema import Config
    from raven.providers.auth import MissingCredentialsError

    section = {
        "endpoints": [{"label": "a", "apiKey": "k1"}, {"label": "b", "apiKey": "k2"}],
        **extra_section,
    }
    config = Config.model_validate(
        {
            "providers": {provider: section},
            "agents": {"defaults": {"model": model, "provider": provider}},
        }
    )
    monkeypatch.setattr("raven.cli._helpers.check_provider_credentials", lambda _config: None)

    with pytest.raises(MissingCredentialsError, match="endpoints"):
        _helpers.make_provider(config)


# ---------------------------------------------------------------------------
# check_provider_credentials — onboard guidance
# ---------------------------------------------------------------------------


def test_no_api_key_error_mentions_onboard(tmp_path: Path) -> None:
    """Zero-provider config: the credentials gate every LLM-needing command
    runs through must point at ``raven onboard`` alongside the provider set
    command. Carried on the exception so the single outlet renders it."""
    from raven.config.loader import load_config
    from raven.providers.auth import MissingCredentialsError

    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")

    with pytest.raises(MissingCredentialsError) as excinfo:
        _helpers.check_provider_credentials(load_config(p))

    assert "raven onboard" in f"{excinfo.value.summary} {excinfo.value.remedy}"


def test_azure_missing_key_same_onboard_guidance(tmp_path: Path) -> None:
    """The azure branch flows through the same gate and must carry the same
    ``raven onboard`` guidance as the generic no-key branch, not its own
    hand-edit-config wording."""
    from raven.config.loader import load_config
    from raven.providers.auth import MissingCredentialsError

    cfg = _config_for(tmp_path, "azure_openai", "my-deployment", {"apiBase": "https://example.openai.azure.com"})

    with pytest.raises(MissingCredentialsError) as excinfo:
        _helpers.check_provider_credentials(load_config(cfg))

    assert "raven onboard" in f"{excinfo.value.summary} {excinfo.value.remedy}"


# ── the first-run verdict vs. a provider Raven carries no field for ──


def test_a_working_extra_provider_is_not_a_first_run(tmp_path: Path) -> None:
    """A credential under an undeclared provider key still counts as configured.

    ``ProvidersConfig`` allows extra keys and serves them from ``get`` -- a
    provider LiteLLM supports but Raven carries no field for is a supported
    shape. Deciding "is anything configured" from ``__dict__`` sees only the
    declared fields, so such a user was told nothing was configured yet and
    sent to the wizard. The model is left at the schema default here, which is
    the only case that reaches this branch at all.
    """
    from raven.config.loader import load_config
    from raven.providers.auth import MissingCredentialsError

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"providers": {"my-private-vllm": {"apiKey": "sk-real", "apiBase": "http://x/v1"}}}),
        encoding="utf-8",
    )

    with pytest.raises(MissingCredentialsError) as excinfo:
        _helpers.check_provider_credentials(load_config(cfg))

    # The specific verdict for the model's own vendor, not the first-run one.
    assert "no provider is configured yet" not in excinfo.value.summary
    assert "API key" in excinfo.value.summary


def test_nothing_configured_at_all_names_the_wizard(tmp_path: Path) -> None:
    """With no credential anywhere and no model chosen, the default model's
    vendor is not the thing to go fix -- the wizard is."""
    from raven.config.loader import load_config
    from raven.providers.auth import MissingCredentialsError

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"providers": {}}), encoding="utf-8")

    with pytest.raises(MissingCredentialsError) as excinfo:
        _helpers.check_provider_credentials(load_config(cfg))

    assert "no provider is configured yet" in excinfo.value.summary
    assert "raven onboard" in excinfo.value.summary
