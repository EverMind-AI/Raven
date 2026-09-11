"""ResolvingProvider: dispatches each call to the vendor the model resolves to."""

from __future__ import annotations

import pytest

from raven.config.schema import Config
from raven.providers.factory import make_resolving_provider
from raven.providers.resolving_provider import ResolvingProvider


def _config(default_model: str = "deepseek/deepseek-v3") -> Config:
    cfg = Config()
    cfg.agents.defaults.model = default_model
    cfg.providers.deepseek.api_key = "KD"
    cfg.providers.anthropic.api_key = "KA"
    return cfg


def test_pick_routes_by_vendor():
    p = ResolvingProvider(_config())
    a = p._pick("anthropic/claude-opus-4-5")
    d = p._pick("deepseek/deepseek-v3")
    assert a is not d


def test_pick_memoizes_per_vendor():
    p = ResolvingProvider(_config())
    assert p._pick("anthropic/claude-opus-4-5") is p._pick("anthropic/claude-opus-4-5")


def test_pick_routes_a_keyword_aliased_model_to_its_canonical_vendor():
    """Regression guard. A provider's registry name and the prefix users write
    often differ (`zhipu/...` -> `zai`, `kimi/...` -> `moonshot`). Resolution is
    Config's job; anything here that second-guesses it by comparing the prefix to
    the vendor name sends the model to the WRONG vendor's baked-in credentials."""
    cfg = _config()
    cfg.providers.zai.api_key = "KZ"
    p = ResolvingProvider(cfg)

    assert cfg.get_provider_name("zhipu/glm-4.6") == "zai"
    assert p._pick("zhipu/glm-4.6") is not p._pick(None)


def _picker_config() -> Config:
    """The shape the web model picker produces: a gateway configured for its own
    catalog, plus OpenRouter carrying curated models whose names start with
    another vendor's prefix (`openai/...`, `anthropic/...`)."""
    cfg = Config()
    cfg.agents.defaults.model = "openrouter/anthropic/claude-sonnet-4-5"
    cfg.providers.custom.api_key = "KC"
    cfg.providers.custom.api_base = "http://gateway.local/v1"
    cfg.providers.custom.models = ["MiniMax-M3"]
    cfg.providers.openrouter.api_key = "sk-or-v1-x"
    cfg.providers.openrouter.models = [
        "openai/gpt-5.6-terra",
        "anthropic/claude-opus-5",
        "moonshotai/kimi-k3",
    ]
    return cfg


@pytest.mark.parametrize(
    "model",
    ["openai/gpt-5.6-terra", "anthropic/claude-opus-5", "moonshotai/kimi-k3"],
)
def test_a_curated_model_routes_to_the_provider_that_lists_it(model: str):
    """A provider's curated `models` is the user stating which vendor serves what,
    so it must outrank the prefix/keyword guesses below it. Without this the
    keyword ladder finds no configured vendor for `openai/...` (OpenAI has no key)
    and the last rung hands the model to the first configured gateway -- sending
    an OpenRouter model to an unrelated endpoint's base_url and api_key."""
    cfg = _picker_config()
    assert cfg.get_provider_name(model) == "openrouter"
    assert cfg.get_api_base(model) == "https://openrouter.ai/api/v1"


def test_routing_agrees_with_the_accept_check_on_every_offered_model():
    """`serving_provider_for_model` gates what the picker may store and
    `get_provider_name` routes the turn. When they disagree a model validates as
    servable and is then sent to a different vendor's credentials."""
    cfg = _picker_config()
    offered = list(cfg.providers.custom.models) + list(cfg.providers.openrouter.models)
    for model in offered:
        assert cfg.serving_provider_for_model(model) == cfg.get_provider_name(model), model


def test_a_curated_model_does_not_outrank_a_pinned_provider():
    cfg = _picker_config()
    cfg.agents.defaults.provider = "custom"
    assert cfg.get_provider_name("openai/gpt-5.6-terra") == "custom"


def test_an_unconfigured_provider_does_not_claim_its_curated_models():
    cfg = _picker_config()
    cfg.providers.openrouter.api_key = ""
    assert cfg.get_provider_name("openai/gpt-5.6-terra") != "openrouter"


def test_pick_still_returns_a_provider_when_nothing_is_configured():
    """`get_provider_name` returns None only when no provider is configured at
    all; that is the one case the None branch exists for."""
    bare = Config()
    assert bare.get_provider_name("no-such-vendor/mystery") is None
    assert ResolvingProvider(bare)._pick("no-such-vendor/mystery") is not None


def test_default_model_is_the_config_default():
    assert ResolvingProvider(_config()).get_default_model() == "deepseek/deepseek-v3"


@pytest.mark.asyncio
async def test_chat_with_retry_delegates_to_the_models_vendor():
    p = ResolvingProvider(_config())
    picked = p._pick("anthropic/claude-opus-4-5")
    calls: list[str | None] = []

    async def _spy(messages, tools=None, model=None, **kw):
        calls.append(model)
        return "ok"

    picked.chat_with_retry = _spy
    assert await p.chat_with_retry([], model="anthropic/claude-opus-4-5") == "ok"
    assert calls == ["anthropic/claude-opus-4-5"]


def test_make_resolving_provider_returns_one():
    assert isinstance(make_resolving_provider(_config()), ResolvingProvider)


def test_factory_can_defer_first_run_credentials_to_the_model_call():
    from raven.config.schema import Config
    from raven.providers.auth import MissingCredentialsError

    config = Config()
    with pytest.raises(MissingCredentialsError):
        make_resolving_provider(config)

    assert isinstance(make_resolving_provider(config, allow_unconfigured=True), ResolvingProvider)


class TestCredentialsAreLiveWithASupplier:
    """The gateway's default lane rotates its keys without a restart: each pick
    compares the providers fingerprint and rebuilds the per-vendor adapters
    when it moved -- the resolver-side twin of the pool's binding cache."""

    def _write(self, path, key: str) -> None:
        import json

        path.write_text(
            json.dumps(
                {
                    "agents": {"defaults": {"model": "anthropic/claude-opus-4-5", "provider": "anthropic"}},
                    "providers": {"anthropic": {"apiKey": key}},
                }
            ),
            encoding="utf-8",
        )

    def _resolver(self, tmp_path, monkeypatch):
        from raven.core.config_stack import load_runtime_config

        cfg_path = tmp_path / "config.json"
        self._write(cfg_path, "sk-v1")
        monkeypatch.setattr("raven.home._current_config_path", cfg_path)
        config = load_runtime_config(None, None)
        return make_resolving_provider(config, config_supplier=lambda: load_runtime_config(None, None)), cfg_path

    def test_a_rotated_key_rebuilds_the_adapter_on_the_next_pick(self, tmp_path, monkeypatch):
        resolver, cfg_path = self._resolver(tmp_path, monkeypatch)
        before = resolver._pick("anthropic/claude-opus-4-5")

        self._write(cfg_path, "sk-v2")
        after = resolver._pick("anthropic/claude-opus-4-5")

        assert after is not before, "the adapter must be rebuilt from the rotated credentials"
        assert resolver._config.providers.anthropic.api_key == "sk-v2"

    def test_an_unchanged_file_keeps_the_adapter(self, tmp_path, monkeypatch):
        resolver, _ = self._resolver(tmp_path, monkeypatch)
        assert resolver._pick("anthropic/claude-opus-4-5") is resolver._pick("anthropic/claude-opus-4-5")

    def test_a_torn_config_keeps_the_adapters_it_has(self, tmp_path, monkeypatch):
        resolver, cfg_path = self._resolver(tmp_path, monkeypatch)
        before = resolver._pick("anthropic/claude-opus-4-5")

        cfg_path.write_text("{ half a rewri", encoding="utf-8")

        assert resolver._pick("anthropic/claude-opus-4-5") is before

    def test_without_a_supplier_nothing_moves(self, tmp_path, monkeypatch):
        from raven.config.loader import load_config

        cfg_path = tmp_path / "config.json"
        self._write(cfg_path, "sk-v1")
        monkeypatch.setattr("raven.home._current_config_path", cfg_path)
        resolver = make_resolving_provider(load_config(cfg_path))
        before = resolver._pick("anthropic/claude-opus-4-5")

        self._write(cfg_path, "sk-v2")

        assert resolver._pick("anthropic/claude-opus-4-5") is before

    def _write_two_providers(self, path, key: str, provider: str) -> None:
        import json

        path.write_text(
            json.dumps(
                {
                    "agents": {"defaults": {"model": "claude-opus-4-5", "provider": provider}},
                    "providers": {
                        "anthropic": {"apiKey": key},
                        "openrouter": {"apiKey": "sk-or-boot"},
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_a_dormant_routing_edit_does_not_ride_in_on_a_key_rotation(self, tmp_path, monkeypatch):
        # The gate watches credentials only; the swap must not import a routing
        # change with them. Boot forces anthropic; a mid-session edit flips
        # agents.defaults.provider to openrouter but touches no credential, so
        # the fingerprint is unchanged and routing stays anthropic (dormant).
        # Rotating the anthropic key then moves the fingerprint -- and must NOT
        # let the dormant openrouter routing hitchhike in.
        from raven.core.config_stack import load_runtime_config

        cfg_path = tmp_path / "config.json"
        self._write_two_providers(cfg_path, "sk-v1", "anthropic")
        monkeypatch.setattr("raven.home._current_config_path", cfg_path)
        config = load_runtime_config(None, None)
        resolver = make_resolving_provider(config, config_supplier=lambda: load_runtime_config(None, None))

        assert resolver._config.get_provider_name("claude-opus-4-5") == "anthropic"

        self._write_two_providers(cfg_path, "sk-v1", "openrouter")  # routing edit, no credential change
        assert resolver._config.get_provider_name("claude-opus-4-5") == "anthropic", "the edit is dormant"

        self._write_two_providers(cfg_path, "sk-v2", "openrouter")  # key rotation moves the fingerprint
        resolver._pick("claude-opus-4-5")
        assert resolver._config.providers.anthropic.api_key == "sk-v2", "credentials did refresh"
        assert resolver._config.get_provider_name("claude-opus-4-5") == "anthropic", (
            "the dormant routing edit must not ride in on a credentials refresh"
        )


def test_the_caching_probe_reaches_the_vendor_adapter():
    """The gateway's loop asks this router whether a request may carry
    ``cache_control``; the base default (False) would silently switch the
    cache optimizer off for every vendor behind it."""
    cfg = _config("openrouter/claude-opus-5")
    cfg.providers.openrouter.api_key = "KO"
    cfg.providers.openrouter.model_protocols = {"openrouter/claude-opus-5": "anthropic"}
    p = ResolvingProvider(cfg)
    assert p.supports_prompt_caching("openrouter/claude-opus-5") is True
    assert p.supports_prompt_caching("deepseek/deepseek-v3") is False
