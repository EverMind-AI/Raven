"""Where a model id becomes a model id plus the credential that serves it.

A pin like ``context.curator_model`` is a model id and nothing else, so
resolving it has to answer a second question: which configured provider
serves that vendor. Answering it in one place is what stops a subsystem
from sending one vendor's key to another's endpoint -- either the pool
returns a complete pair, or it returns nothing and the caller follows the
conversation instead.
"""

from __future__ import annotations

import os

import pytest

from raven.config.schema import Config
from raven.providers.binding import ModelBinding, active_binding, resolve, use_binding


@pytest.fixture(autouse=True)
def _restore_env():
    """Building a keyed provider writes the vendor's env var; keep that here."""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


def _config(model: str = "claude-opus-4-5", **keys: str) -> Config:
    cfg = Config()
    cfg.agents.defaults.model = model
    cfg.agents.defaults.provider = "auto"
    for name, key in keys.items():
        section = cfg.providers.get(name)
        assert section is not None, name
        section.api_key = key
    return cfg


def _pool(model: str = "claude-opus-4-5", **kwargs):
    from raven.providers.pool import ProviderPool

    return ProviderPool(_config(model, **kwargs))


# ---------------------------------------------------------------------------
# Binding a model
# ---------------------------------------------------------------------------


def test_a_model_gets_its_own_vendors_key() -> None:
    """The point of the pool: the credential follows the model id, not whatever
    provider the caller happened to be holding.
    """
    pool = _pool(anthropic="sk-ant", gemini="AIza")

    assert pool.bind("gemini-2.5-flash").provider.api_key == "AIza"
    assert pool.bind("claude-sonnet-4-5").provider.api_key == "sk-ant"


def test_the_same_pair_is_built_once() -> None:
    """Building a provider imports LiteLLM and writes vendor env vars, so a
    session flipping between two models must not pay for it per turn.
    """
    pool = _pool(anthropic="sk-ant")
    first = pool.bind("claude-opus-4-5")

    assert pool.bind("claude-opus-4-5") is first


def test_two_models_of_one_vendor_are_two_bindings() -> None:
    pool = _pool(anthropic="sk-ant")

    assert pool.bind("claude-opus-4-5") is not pool.bind("claude-sonnet-4-5")


def test_an_explicit_provider_name_wins_over_derivation() -> None:
    """The picker sends the provider alongside the model; a gateway serving
    another vendor's id would be mis-derived without it.
    """
    pool = _pool("anthropic/claude-opus-4-5", openrouter="sk-or")
    binding = pool.bind("anthropic/claude-opus-4-5", "openrouter")

    assert binding.provider.api_key == "sk-or"


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def test_a_pin_with_credentials_becomes_a_pair() -> None:
    pool = _pool(anthropic="sk-ant", gemini="AIza")
    pin = pool.bind_pin("gemini-2.5-flash")

    assert pin is not None
    assert pin.model == "gemini-2.5-flash"
    assert pin.provider.api_key == "AIza"


def test_a_pin_without_credentials_is_not_a_pair() -> None:
    """The case that used to 401 every call: a model id whose vendor has no key.
    None tells the caller to follow the conversation instead of sending this id
    on the conversation's credential.
    """
    pool = _pool(anthropic="sk-ant")

    assert pool.bind_pin("gemini-2.5-flash") is None
    assert pool.bind_pin("openai/gpt-5-mini") is None


def test_an_unset_pin_is_not_a_pair() -> None:
    pool = _pool(anthropic="sk-ant")

    assert pool.bind_pin(None) is None
    assert pool.bind_pin("") is None


def test_a_pin_whose_vendor_cannot_be_derived_is_not_a_pair() -> None:
    """With no vendor there is no section to check, so there is no way to tell a
    working pin from a mis-paired one. Following the conversation is at least a
    pair.
    """
    pool = _pool(anthropic="sk-ant")

    assert pool.bind_pin("some-unknown-local-model") is None


def test_a_declared_but_empty_section_is_not_credentials() -> None:
    """Every vendor exists as an empty section whether or not it was configured,
    so presence proves nothing.
    """
    pool = _pool(anthropic="sk-ant")
    assert pool.config.providers.get("gemini") is not None

    assert pool.bind_pin("gemini-2.5-flash") is None


# ---------------------------------------------------------------------------
# The binding context
# ---------------------------------------------------------------------------


class _Stub:
    def get_default_model(self) -> str:
        return "stub/default"


def test_a_binding_needs_a_model() -> None:
    with pytest.raises(ValueError):
        ModelBinding(_Stub(), "")


def test_nothing_is_bound_outside_a_turn() -> None:
    assert active_binding() is None


def test_a_binding_is_visible_for_its_block_and_no_longer() -> None:
    binding = ModelBinding(_Stub(), "vendor/model")
    with use_binding(binding):
        assert active_binding() is binding
    assert active_binding() is None


def test_a_binding_is_restored_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError):
        with use_binding(ModelBinding(_Stub(), "vendor/model")):
            raise RuntimeError("boom")
    assert active_binding() is None


def test_bindings_nest() -> None:
    outer = ModelBinding(_Stub(), "outer/model")
    inner = ModelBinding(_Stub(), "inner/model")
    with use_binding(outer):
        with use_binding(inner):
            assert active_binding() is inner
        assert active_binding() is outer


def test_resolve_prefers_a_pin_then_the_turn_then_the_fallback() -> None:
    pin = ModelBinding(_Stub(), "pin/model")
    turn = ModelBinding(_Stub(), "turn/model")
    fallback = ModelBinding(_Stub(), "fallback/model")

    assert resolve(None, fallback) is fallback
    with use_binding(turn):
        assert resolve(None, fallback) is turn
        assert resolve(pin, fallback) is pin, "a configured subsystem does not follow the turn"


@pytest.mark.asyncio
async def test_a_detached_task_keeps_the_binding_it_was_created_under() -> None:
    """This is what makes a spawned subagent finish on the model it started on:
    ``create_task`` copies the context, so a later switch cannot reach it.
    """
    import asyncio

    started = ModelBinding(_Stub(), "started/model")
    seen: list[str | None] = []
    release = asyncio.Event()

    async def _detached() -> None:
        await release.wait()
        binding = active_binding()
        seen.append(binding.model if binding else None)

    with use_binding(started):
        task = asyncio.create_task(_detached())

    # Outside the block now, and a different binding is current.
    with use_binding(ModelBinding(_Stub(), "switched/model")):
        release.set()
        await task

    assert seen == ["started/model"]


def test_a_config_supplier_is_re_read_and_drops_stale_bindings() -> None:
    """A credential fixed after start (an OAuth re-login, an edited file) has
    to be visible without a restart, which a snapshot would prevent.
    """
    from raven.providers.pool import ProviderPool

    broken = _config(anthropic="")
    fixed = _config(anthropic="sk-ant")
    current = [broken]
    pool = ProviderPool(lambda: current[0])

    assert pool.bind_pin("claude-opus-4-5") is None, "no key yet"

    current[0] = fixed
    pin = pool.bind_pin("claude-opus-4-5")
    assert pin is not None
    assert pin.provider.api_key == "sk-ant"


def test_a_gateway_serves_a_pin_its_upstream_vendor_has_no_key_for() -> None:
    """A gateway bills its own key for whatever id it is handed, so asking
    whether the upstream vendor has a key of its own would drop a pin that
    works -- which is how a working setup got broken once already.
    """
    cfg = _config("anthropic/claude-opus-4-5", openrouter="sk-or")
    cfg.agents.defaults.provider = "openrouter"

    from raven.providers.pool import ProviderPool

    pin = ProviderPool(cfg).bind_pin("gemini-2.5-flash")

    assert pin is not None
    assert pin.model == "gemini-2.5-flash"
    assert pin.provider.api_key == "sk-or"


def test_an_unbuildable_pin_is_reported_not_raised(monkeypatch) -> None:
    """``bind_pin`` is called from the factory at construction; a vendor whose
    provider cannot be built must leave the subsystem following the
    conversation, not stop the agent from starting.
    """
    import raven.cli._helpers as helpers

    pool = _pool(anthropic="sk-ant", gemini="AIza")

    def _boom(_cfg):
        raise RuntimeError("cannot build")

    monkeypatch.setattr(helpers, "make_provider", _boom)
    assert pool.bind_pin("gemini-2.5-flash") is None


def test_a_credential_added_after_boot_is_visible_without_a_restart() -> None:
    """The production wiring hands the pool a loader, not the boot config: a
    user who connects a provider in the picker and then picks its model must
    not be told the model is unavailable.
    """
    from raven.providers.pool import ProviderPool

    cfg = _config()
    pool = ProviderPool(lambda: cfg)

    assert pool.bind_pin("gemini-2.5-flash") is None, "no key yet"

    cfg.providers.gemini.api_key = "AIza-added-after-boot"
    pin = pool.bind_pin("gemini-2.5-flash")

    assert pin is not None
    assert pin.provider.api_key == "AIza-added-after-boot"


def test_a_re_reading_supplier_still_reuses_bindings() -> None:
    """Invalidating on config identity would clear the cache on every call for
    a supplier that re-reads, which defeats the pool. Only a credential change
    may drop it.
    """
    from raven.providers.pool import ProviderPool

    def _fresh():
        # A new object every call, same contents -- what a file loader does.
        return _config(anthropic="sk-ant")

    pool = ProviderPool(_fresh)
    first = pool.bind("claude-opus-4-5")

    assert pool.bind("claude-opus-4-5") is first


def test_a_gateway_pin_that_cannot_be_built_is_reported_not_raised(monkeypatch) -> None:
    """The gateway branch is reached from the factory at construction too, so
    it needs the same guard as the direct-vendor branch -- otherwise a missing
    gateway key stops the agent from starting.
    """
    import raven.cli._helpers as helpers

    cfg = _config("anthropic/claude-opus-4-5", openrouter="sk-or")
    cfg.agents.defaults.provider = "openrouter"

    from raven.providers.pool import ProviderPool

    pool = ProviderPool(cfg)

    def _boom(_cfg):
        raise RuntimeError("no gateway key")

    monkeypatch.setattr(helpers, "make_provider", _boom)
    assert pool.bind_pin("gemini-2.5-flash") is None


# ---------------------------------------------------------------------------
# A pin configured as a pair
# ---------------------------------------------------------------------------


def test_a_configured_provider_beats_the_gateway_guess() -> None:
    """The id alone cannot say which credential was meant.

    On a gateway, ``anthropic/claude-...`` served by the gateway and
    ``claude-...`` served by Anthropic direct are both valid, name different
    credentials and different bills. Guessing picks one; the configured pair
    says which.
    """
    cfg = _config("anthropic/claude-opus-4-5", openrouter="sk-or", anthropic="sk-ant")
    cfg.agents.defaults.provider = "openrouter"

    from raven.providers.pool import ProviderPool

    pin = ProviderPool(cfg).bind_pin("claude-haiku-4-5", "anthropic")

    assert pin is not None
    assert pin.model == "claude-haiku-4-5"
    assert pin.provider.api_key == "sk-ant", "the configured provider serves it, not the gateway"


def test_a_configured_provider_beats_the_vendor_the_id_names() -> None:
    """The other direction: a gateway reselling a vendor's model. Deriving from
    the id would send it to Anthropic direct on a key the user may not hold.
    """
    cfg = _config("claude-opus-4-5", openrouter="sk-or", anthropic="sk-ant")

    from raven.providers.pool import ProviderPool

    pin = ProviderPool(cfg).bind_pin("anthropic/claude-haiku-4-5", "openrouter")

    assert pin is not None
    assert pin.provider.api_key == "sk-or", "the gateway serves it, not the vendor in the id"


def test_a_configured_provider_without_credentials_is_not_a_pair() -> None:
    """Explicitly configured and still unusable is a config error, and the
    subsystem follows the conversation rather than borrowing another key.
    """
    cfg = _config("claude-opus-4-5", anthropic="sk-ant")

    from raven.providers.pool import ProviderPool

    assert ProviderPool(cfg).bind_pin("gemini-2.5-flash", "gemini") is None


def test_an_unpaired_pin_still_derives_its_vendor() -> None:
    """Configs written before the provider field existed keep working."""
    cfg = _config("claude-opus-4-5", anthropic="sk-ant", gemini="AIza")

    from raven.providers.pool import ProviderPool

    pin = ProviderPool(cfg).bind_pin("gemini-2.5-flash")

    assert pin is not None
    assert pin.provider.api_key == "AIza"


def test_a_pin_that_cannot_be_built_at_all_does_not_stop_the_agent(monkeypatch) -> None:
    """``bind_pin`` runs in the context-engine factory at construction, and
    building a provider imports a vendor module -- so the failures are not only
    the credential ones the narrower guard covered.
    """
    import raven.cli._helpers as helpers

    cfg = _config("claude-opus-4-5", anthropic="sk-ant")

    from raven.providers.pool import ProviderPool

    def _boom(_cfg):
        raise ModuleNotFoundError("no module named 'litellm'")

    monkeypatch.setattr(helpers, "make_provider", _boom)
    assert ProviderPool(cfg).bind_pin("claude-haiku-4-5", "anthropic") is None
