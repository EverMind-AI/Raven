"""Tests for ``config.get`` / ``config.set`` RPC handlers (specs §3.6).

v0.1 hot-changeable whitelist (per specs §3.6):
    - ``agent.thinking_budget``
    - ``agent.temperature``
    - ``tui.theme``
    - ``tui.show_token_usage``

Writes to non-whitelisted keys → -32010 ``config_field_readonly``.
Writes that fail Pydantic-style validation → -32011 ``config_validation_error``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.tui_rpc.errors import (
    ConfigFieldReadonlyError,
    ConfigValidationError,
    ModelNotAvailableError,
)
from raven.tui_rpc.methods.config import (
    CONFIG_WRITABLE_KEYS,
    config_get,
    config_set,
)


@pytest.fixture
def fake_home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


# ----------------------------------------------------------------------------
# config.get
# ----------------------------------------------------------------------------


async def test_config_get_no_keys_returns_all_writable(fake_home: Path) -> None:
    result = await config_get({})
    assert "config" in result
    cfg = result["config"]
    # All 4 whitelisted keys present (defaults), no extras.
    assert set(cfg.keys()) == set(CONFIG_WRITABLE_KEYS)


async def test_config_get_specific_keys_returns_subset(fake_home: Path) -> None:
    result = await config_get({"keys": ["tui.theme", "agent.temperature"]})
    assert set(result["config"].keys()) == {"tui.theme", "agent.temperature"}


async def test_config_get_unknown_keys_silently_omitted(fake_home: Path) -> None:
    result = await config_get({"keys": ["nope.invalid", "tui.theme"]})
    # Unknown key silently absent — spec §3.6 says no error.
    assert "nope.invalid" not in result["config"]
    assert "tui.theme" in result["config"]


async def test_config_get_reads_persisted_values(fake_home: Path) -> None:
    (fake_home / ".raven").mkdir()
    (fake_home / ".raven" / "config.json").write_text(json.dumps({"tui": {"theme": "solarized-dark"}}))
    result = await config_get({"keys": ["tui.theme"]})
    assert result["config"]["tui.theme"] == "solarized-dark"


# ----------------------------------------------------------------------------
# config.set
# ----------------------------------------------------------------------------


async def test_config_set_whitelisted_returns_applied(fake_home: Path) -> None:
    result = await config_set({"key": "tui.theme", "value": "dark"})
    assert result["applied"] is True
    assert "previous" in result


async def test_config_set_non_whitelisted_raises_readonly(fake_home: Path) -> None:
    with pytest.raises(ConfigFieldReadonlyError):
        await config_set({"key": "secret.api_key", "value": "x"})


async def test_config_set_invalid_theme_raises_validation(fake_home: Path) -> None:
    with pytest.raises(ConfigValidationError):
        await config_set({"key": "tui.theme", "value": "@@@nope@@@"})


async def test_config_set_invalid_temperature_raises_validation(fake_home: Path) -> None:
    # Temperature must be a number in [0, 2]; passing a string fails.
    with pytest.raises(ConfigValidationError):
        await config_set({"key": "agent.temperature", "value": "hot"})
    # Out-of-range numeric also rejected.
    with pytest.raises(ConfigValidationError):
        await config_set({"key": "agent.temperature", "value": 99})


async def test_config_set_persists_to_config_json(fake_home: Path) -> None:
    await config_set({"key": "tui.theme", "value": "dracula"})
    cfg_path = fake_home / ".raven" / "config.json"
    assert cfg_path.exists()
    payload = json.loads(cfg_path.read_text())
    assert payload["tui"]["theme"] == "dracula"


async def test_config_set_previous_value_returned(fake_home: Path) -> None:
    # First write — previous is None.
    res1 = await config_set({"key": "tui.show_token_usage", "value": True})
    assert res1["applied"] is True
    assert res1["previous"] is None
    # Second write — previous reflects the first write's value.
    res2 = await config_set({"key": "tui.show_token_usage", "value": False})
    assert res2["applied"] is True
    assert res2["previous"] is True


async def test_config_set_creates_config_when_missing(fake_home: Path) -> None:
    """When ~/.raven/config.json doesn't exist yet, set must create it."""
    assert not (fake_home / ".raven" / "config.json").exists()
    await config_set({"key": "tui.theme", "value": "ok"})
    assert (fake_home / ".raven" / "config.json").exists()


async def test_config_set_missing_key_param_raises_validation(fake_home: Path) -> None:
    with pytest.raises(ConfigValidationError):
        await config_set({"value": "x"})
    with pytest.raises(ConfigValidationError):
        await config_set({"key": "tui.theme"})


# ----------------------------------------------------------------------------
# config.set key="model" — the live-loop switch branch
# ----------------------------------------------------------------------------


class _FakeLoop:
    """Stand-in for AgentLoop's half of the switch contract.

    Records the ``set_provider`` calls: assigning ``provider``/``model``
    directly would leave the subagent manager, the context engine and the
    consolidator on the old provider, so the handler must go through the
    method, not the attributes.
    """

    def __init__(self, provider: object, model: str) -> None:
        self.provider = provider
        self.model = model
        self.switches: list[tuple[object, str]] = []
        self.session_bindings: dict[str, object] = {}
        self.provider_pool = None

    def session_model(self, session_key: str) -> str:
        binding = self.session_bindings.get(session_key)
        return binding.model if binding is not None else self.model

    def set_session_binding(self, session_key: str, binding: object) -> None:
        self.session_bindings[session_key] = binding

    def set_default_binding(self, binding: object) -> None:
        self.provider = binding.provider
        self.model = binding.model
        self.switches.append((binding.provider, binding.model))

    def set_provider(self, provider: object, model: str) -> None:
        self.provider = provider
        self.model = model
        self.switches.append((provider, model))


async def test_config_set_model_reassigns_loop_and_persists(fake_home: Path, monkeypatch) -> None:
    import raven.tui_rpc.methods.config as config_mod

    loop = _FakeLoop("old-prov", "old-model")
    new_provider = SimpleNamespace(name="new-prov")

    monkeypatch.setattr(config_mod, "make_provider", lambda _cfg: new_provider)
    monkeypatch.setattr(
        config_mod,
        "load_runtime_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(model="", provider="auto"))),
    )

    result = await config_set(
        {
            "key": "model",
            "value": "anthropic/claude-opus-4-8",
            "provider": "anthropic",
            "scope": "default",
        },
        agent_loop_factory=lambda: loop,
    )

    assert result["applied"] is True
    assert result["value"] == "anthropic/claude-opus-4-8"
    assert result["scope"] == "default"
    assert loop.model == "anthropic/claude-opus-4-8"
    assert loop.provider is new_provider
    # Routed through set_provider, so everything holding the old provider
    # (subagents, context-engine segments, consolidator) gets told too --
    # including the context window, which the loop re-resolves at adoption
    # (pinned in test_agent_loop_model_switch, where the adopt path lives).
    assert loop.switches == [(new_provider, "anthropic/claude-opus-4-8")]

    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["model"] == "anthropic/claude-opus-4-8"
    assert cfg["agents"]["defaults"]["provider"] == "anthropic"


async def test_config_set_model_bare_derives_provider(fake_home: Path) -> None:
    # A bare `/model <name>` carries no provider; _set_model must derive it from
    # the model so a previously-forced provider does not silently mis-route.
    # Configured, because an id naming a vendor with no section is deliberately
    # left on `auto`: pinning it would stop routing before the fallback that lets
    # a gateway serve that vendor's models.
    _pin(fake_home, "auto", {"anthropic": {"api_key": "sk-ant"}})

    result = await config_set(
        {"key": "model", "value": "anthropic/claude-opus-4-8"},
        agent_loop_factory=lambda: None,
    )
    assert result["applied"] is True
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["model"] == "anthropic/claude-opus-4-8"
    assert cfg["agents"]["defaults"]["provider"] == "anthropic"


async def test_config_set_model_is_scoped_to_the_session_that_asked(fake_home: Path, monkeypatch) -> None:
    """A session switching its own model must not move anyone else's, and must
    not rewrite the default a new session starts on.
    """
    import raven.tui_rpc.methods.config as config_mod

    (fake_home / ".raven").mkdir()
    (fake_home / ".raven" / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    )

    new_provider = SimpleNamespace(name="new-prov")
    loop = _FakeLoop("old-prov", "anthropic/claude-sonnet-4-5")

    monkeypatch.setattr(config_mod, "make_provider", lambda _cfg: new_provider)
    monkeypatch.setattr(
        config_mod,
        "load_runtime_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(model="", provider="auto"))),
    )

    result = await config_set(
        {
            "key": "model",
            "value": "anthropic/claude-opus-4-8",
            "provider": "anthropic",
            "session_id": "tui:a",
        },
        agent_loop_factory=lambda: loop,
    )

    assert result["scope"] == "session"
    assert result["session_id"] == "tui:a"
    assert loop.session_bindings["tui:a"].model == "anthropic/claude-opus-4-8"
    assert "tui:b" not in loop.session_bindings, "another session must not move"
    assert loop.switches == [], "a session switch is not a default change"

    on_disk = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert on_disk["agents"]["defaults"]["model"] == "anthropic/claude-sonnet-4-5", (
        "a new session must still start on the configured default"
    )


async def test_config_set_model_is_not_refused_mid_turn(fake_home: Path, monkeypatch) -> None:
    """The running turn holds the binding it started on, so the switch lands on
    the session's next turn rather than being rejected.
    """
    import raven.tui_rpc.methods.config as config_mod

    new_provider = SimpleNamespace(name="new-prov")
    loop = _FakeLoop("old-prov", "old-model")
    monkeypatch.setattr(config_mod, "make_provider", lambda _cfg: new_provider)
    monkeypatch.setattr(
        config_mod,
        "load_runtime_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(model="", provider="auto"))),
    )

    result = await config_set(
        {"key": "model", "value": "anthropic/claude-opus-4-8", "provider": "anthropic", "session_id": "tui:busy"},
        agent_loop_factory=lambda: loop,
    )
    assert result["applied"] is True


async def test_config_set_model_unconstructable_preserves_previous(fake_home: Path, monkeypatch) -> None:
    import raven.tui_rpc.methods.config as config_mod

    (fake_home / ".raven").mkdir()
    (fake_home / ".raven" / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    )

    def _boom(_cfg):
        raise RuntimeError("no api key")

    monkeypatch.setattr(config_mod, "make_provider", _boom)
    monkeypatch.setattr(
        config_mod,
        "load_runtime_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(model="", provider="auto"))),
    )

    loop = _FakeLoop("keep-prov", "anthropic/claude-sonnet-4-5")
    with pytest.raises(ModelNotAvailableError):
        await config_set(
            {
                "key": "model",
                "value": "broken/model",
                "session_id": "tui:default",
            },
            agent_loop_factory=lambda: loop,
        )

    # Loop untouched and on-disk model preserved.
    assert loop.model == "anthropic/claude-sonnet-4-5"
    assert loop.provider == "keep-prov"
    assert loop.switches == []
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["model"] == "anthropic/claude-sonnet-4-5"


# ----------------------------------------------------------------------------
# Dispatcher wiring
# ----------------------------------------------------------------------------


async def test_config_methods_registered_via_helper(fake_home: Path) -> None:
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods.config import register_config_methods

    d = Dispatcher()
    register_config_methods(d)
    resp = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "config.set",
            "params": {"key": "tui.theme", "value": "ok"},
        }
    )
    assert "error" not in resp
    assert resp["result"]["applied"] is True

    resp = await d.dispatch({"jsonrpc": "2.0", "id": 2, "method": "config.get", "params": {"keys": ["tui.theme"]}})
    assert resp["result"]["config"]["tui.theme"] == "ok"

    # readonly → JSON-RPC error -32010
    resp = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "config.set",
            "params": {"key": "secret.api_key", "value": "x"},
        }
    )
    assert resp["error"]["code"] == -32010


async def test_config_set_refuses_malformed_config_and_preserves_file(fake_home: Path) -> None:
    # REGRESSION: TUI config.set must not clobber a malformed config down to one
    # key (same data-loss failure mode as the CLI write path).
    cfg = fake_home / ".raven" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    original = '{\n  "tui": {"theme": "dark"},\n  // comment => invalid JSON\n}\n'
    cfg.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        await config_set({"key": "tui.theme", "value": "dracula"})
    assert cfg.read_text(encoding="utf-8") == original  # NOT clobbered


async def test_config_get_refuses_malformed_config(fake_home: Path) -> None:
    # The read path also surfaces a broken config (not a silent empty dict).
    cfg = fake_home / ".raven" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('{\n  "tui": {"theme": "dark"},\n  // bad\n}\n', encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        await config_get({"keys": ["tui.theme"]})


def _pin(home: Path, provider: str, providers: dict | None = None) -> None:
    """Write a config with a provider pinned, as the picker leaves it."""
    cfg = home / ".raven"
    cfg.mkdir(exist_ok=True)
    (cfg / "config.json").write_text(
        json.dumps(
            {
                "providers": providers if providers is not None else {"openai": {"api_key": "sk-openai"}},
                "agents": {"defaults": {"model": "gpt-4o", "provider": provider}},
            }
        ),
        encoding="utf-8",
    )
    import raven.config.loader as loader

    loader._current_config_path = None


async def test_a_bare_id_the_pinned_provider_does_not_serve_is_refused(fake_home: Path) -> None:
    """The pin decided routing for a model that names nobody, and it was wrong.

    Selecting anything from the picker pins its provider, so by the time someone
    types `/model <name>` there is almost always one. A bare id matching no
    provider's keywords is what a vendor Raven holds no spec for looks like, and
    keeping the pin sent that provider's key to a different vendor.

    Nothing here can tell whose model it is, so it says so rather than picking.
    """
    _pin(fake_home, "openai")

    with pytest.raises(ConfigValidationError) as excinfo:
        await config_set(
            {"key": "model", "value": "mistral-large-latest"},
            agent_loop_factory=lambda: None,
        )
    assert "mistral-large-latest" in str(excinfo.value)

    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["model"] == "gpt-4o", "a refused switch must not write"
    assert cfg["agents"]["defaults"]["provider"] == "openai"


async def test_a_bare_id_the_pinned_provider_does_serve_keeps_the_pin(fake_home: Path) -> None:
    """Refusing every bare id would break the case where the pin was right.

    A user who configured a spec-less vendor and types one of its model names is
    not being ambiguous -- that provider serves it. Asked of its own list and the
    catalogue rather than assumed either way.
    """
    _pin(fake_home, "mistral", {"mistral": {"api_key": "sk-mistral"}})

    result = await config_set(
        {"key": "model", "value": "mistral-large-latest"},
        agent_loop_factory=lambda: None,
    )
    assert result["applied"] is True
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["provider"] == "mistral"
    # Stored naming its provider, which is what every surface writes: the same
    # input through `raven provider use` produced the qualified form while this
    # one kept it bare, so the two disagreed about what the user had chosen.
    assert cfg["agents"]["defaults"]["model"] == "mistral/mistral-large-latest"


async def test_a_local_deployment_keeps_its_pin_for_any_bare_id(fake_home: Path) -> None:
    """Its server names whatever models it likes, and it holds no key to mis-route."""
    _pin(fake_home, "ollama_chat", {"ollama_chat": {"api_base": "http://gpu-box:11434"}})

    result = await config_set(
        {"key": "model", "value": "some-local-build"},
        agent_loop_factory=lambda: None,
    )
    assert result["applied"] is True
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["provider"] == "ollama_chat"


async def test_a_prefixed_id_no_spec_matches_still_hands_routing_back(fake_home: Path) -> None:
    """Unchanged: the prefix names the vendor, so the pin has no claim on it."""
    _pin(fake_home, "openai")

    result = await config_set(
        {"key": "model", "value": "mistral/mistral-large-latest"},
        agent_loop_factory=lambda: None,
    )
    assert result["applied"] is True
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["provider"] == "auto"


async def test_an_explicit_provider_is_never_second_guessed(fake_home: Path) -> None:
    """The picker sends one with every selection; that path must not reach the gate."""
    _pin(fake_home, "openai")

    result = await config_set(
        {"key": "model", "value": "some-deployment", "provider": "azure_openai"},
        agent_loop_factory=lambda: None,
    )
    assert result["applied"] is True
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["provider"] == "azure_openai"


async def test_a_bare_id_the_pinned_provider_lists_itself_keeps_the_pin(fake_home: Path) -> None:
    """The provider's own curated list is evidence too, and it stores ids bare.

    `model.add_model` writes what the user typed, so a hand-added id sits there
    unprefixed. Reading only the catalogue's prefixed spelling would refuse a model
    the user had already told us this provider serves.
    """
    _pin(
        fake_home,
        "mistral",
        {"mistral": {"api_key": "sk-mistral", "models": ["some-private-tune"]}},
    )

    result = await config_set(
        {"key": "model", "value": "some-private-tune"},
        agent_loop_factory=lambda: None,
    )
    assert result["applied"] is True
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["provider"] == "mistral"


async def test_a_session_switch_is_written_to_the_session_record(fake_home: Path, monkeypatch, tmp_path) -> None:
    """The in-memory override dies with the process, so the record is the only
    place the choice survives -- and a write nobody reads is worse than none.
    """
    import raven.tui_rpc.methods.config as config_mod
    from raven.session.manager import SessionManager

    new_provider = SimpleNamespace(name="new-prov")
    loop = _FakeLoop("old-prov", "old-model")
    loop.sessions = SessionManager(tmp_path)

    monkeypatch.setattr(config_mod, "make_provider", lambda _cfg: new_provider)
    monkeypatch.setattr(
        config_mod,
        "load_runtime_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(model="", provider="auto"))),
    )

    await config_set(
        {
            "key": "model",
            "value": "anthropic/claude-opus-4-8",
            "provider": "anthropic",
            "session_id": "tui:a",
        },
        agent_loop_factory=lambda: loop,
    )

    stored = loop.sessions.peek("tui:a")
    assert stored is not None
    assert stored.metadata["model"] == "anthropic/claude-opus-4-8"
    assert stored.metadata["provider"] == "anthropic"


async def test_a_session_switch_reports_the_model_it_replaced(fake_home: Path, monkeypatch) -> None:
    """``previous`` is the session's own model, not the global default."""
    import raven.tui_rpc.methods.config as config_mod

    loop = _FakeLoop("old-prov", "boot-model")
    loop.session_bindings["tui:a"] = SimpleNamespace(provider=object(), model="was-on-this")

    monkeypatch.setattr(config_mod, "make_provider", lambda _cfg: SimpleNamespace(name="new-prov"))
    monkeypatch.setattr(
        config_mod,
        "load_runtime_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(model="", provider="auto"))),
    )

    result = await config_set(
        {"key": "model", "value": "anthropic/claude-opus-4-8", "provider": "anthropic", "session_id": "tui:a"},
        agent_loop_factory=lambda: loop,
    )

    assert result["previous"] == "was-on-this"


async def test_an_unknown_scope_is_rejected(fake_home: Path) -> None:
    """A client typo must not silently degrade to a session switch."""
    with pytest.raises(ConfigValidationError):
        await config_set(
            {"key": "model", "value": "anthropic/claude-opus-4-8", "session_id": "tui:a", "scope": "globl"},
            agent_loop_factory=None,
        )


async def test_a_switch_goes_through_the_pool_when_the_loop_has_one(fake_home: Path, monkeypatch) -> None:
    """The pool is what makes a switch reuse a provider instead of rebuilding
    one per switch; without this the production path is never exercised.
    """
    import raven.tui_rpc.methods.config as config_mod

    asked: list[tuple[str, str | None]] = []
    pooled = SimpleNamespace(provider=SimpleNamespace(name="pooled"), model="anthropic/claude-opus-4-8")

    class _Pool:
        def bind(self, model: str, provider_name: str | None = None):
            asked.append((model, provider_name))
            return pooled

    loop = _FakeLoop("old-prov", "old-model")
    loop.provider_pool = _Pool()

    def _must_not_build(_cfg):
        raise AssertionError("a loop with a pool must not build its own provider")

    monkeypatch.setattr(config_mod, "make_provider", _must_not_build)
    monkeypatch.setattr(
        config_mod,
        "load_runtime_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(model="", provider="auto"))),
    )

    await config_set(
        {"key": "model", "value": "anthropic/claude-opus-4-8", "provider": "anthropic", "session_id": "tui:a"},
        agent_loop_factory=lambda: loop,
    )

    assert asked == [("anthropic/claude-opus-4-8", "anthropic")]
    assert loop.session_bindings["tui:a"] is pooled
