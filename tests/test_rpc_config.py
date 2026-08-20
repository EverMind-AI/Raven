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

from raven.rpc.errors import (
    ConfigFieldReadonlyError,
    ConfigValidationError,
    ModelNotAvailableError,
)
from raven.rpc.methods.config import (
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

    def has_session_binding(self, session_key: str) -> bool:
        return session_key in self.session_bindings

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
    import raven.rpc.methods.config as config_mod

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


async def test_config_set_model_without_a_provider_is_refused(fake_home: Path) -> None:
    """The boundary where the rule can actually be enforced. A model id does not
    name whose credential serves it -- `openrouter` serving
    `anthropic/claude-haiku-4-5` and `anthropic` serving `claude-haiku-4-5` are
    both real and bill different accounts -- and a prefix is LiteLLM routing
    syntax, not evidence about a key. This used to derive one."""
    _pin(fake_home, "anthropic", {"anthropic": {"api_key": "sk-ant"}})

    with pytest.raises(ConfigValidationError, match="needs a provider"):
        await config_set(
            {"key": "model", "value": "anthropic/claude-opus-4-8"},
            agent_loop_factory=lambda: None,
        )

    # And nothing was written: a refused switch leaves the config alone.
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"].get("model") != "anthropic/claude-opus-4-8"


async def test_config_set_model_with_a_provider_writes_the_pair(fake_home: Path) -> None:
    _pin(fake_home, "anthropic", {"anthropic": {"api_key": "sk-ant"}})

    result = await config_set(
        {"key": "model", "value": "claude-opus-4-8", "provider": "anthropic"},
        agent_loop_factory=lambda: None,
    )

    assert result["applied"] is True
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["agents"]["defaults"]["provider"] == "anthropic"
    assert cfg["agents"]["defaults"]["model"] == "anthropic/claude-opus-4-8"


async def test_config_set_model_is_scoped_to_the_session_that_asked(fake_home: Path, monkeypatch) -> None:
    """A session switching its own model must not move anyone else's, and must
    not rewrite the default a new session starts on.
    """
    import raven.rpc.methods.config as config_mod

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
    import raven.rpc.methods.config as config_mod

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
    import raven.rpc.methods.config as config_mod

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
                "provider": "broken",
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
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.config import register_config_methods

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


async def test_a_session_switch_is_written_to_the_session_record(fake_home: Path, monkeypatch, tmp_path) -> None:
    """The in-memory override dies with the process, so the record is the only
    place the choice survives -- and a write nobody reads is worse than none.
    """
    import raven.rpc.methods.config as config_mod
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
    import raven.rpc.methods.config as config_mod

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
    import raven.rpc.methods.config as config_mod

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


# ----------------------------------------------------------------------------
# Scope is never widened
# ----------------------------------------------------------------------------


async def test_a_session_scope_without_a_session_id_is_refused_not_widened(fake_home: Path, monkeypatch) -> None:
    """An explicit ``scope="session"`` with no session must not fall through to
    the default branch.

    The TUI sends ``session_id: ctx.sid``, which is null until the first
    ``session.create`` resolves and after a failed one. Widening the scope
    there rewrites ``agents.defaults.model`` on disk and moves every session
    that never chose its own model -- from a request that asked for the
    opposite.
    """
    import raven.rpc.methods.config as config_mod

    (fake_home / ".raven").mkdir()
    (fake_home / ".raven" / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    )

    loop = _FakeLoop("old-prov", "anthropic/claude-sonnet-4-5")
    monkeypatch.setattr(config_mod, "make_provider", lambda _cfg: SimpleNamespace(name="new-prov"))
    monkeypatch.setattr(
        config_mod,
        "load_runtime_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(model="", provider="auto"))),
    )

    for absent in (None, ""):
        with pytest.raises(ConfigValidationError):
            await config_set(
                {
                    "key": "model",
                    "value": "anthropic/claude-opus-4-8",
                    "scope": "session",
                    "session_id": absent,
                },
                agent_loop_factory=lambda: loop,
            )

    assert loop.switches == [], "a refused switch must not move the default binding"
    assert loop.session_bindings == {}
    on_disk = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert on_disk["agents"]["defaults"]["model"] == "anthropic/claude-sonnet-4-5"


async def test_a_default_scope_with_a_session_id_still_writes_the_default(fake_home: Path, monkeypatch) -> None:
    """``/model X --default`` sends both ``scope="default"`` and the caller's
    session id, and the scope has to win.

    Without the scope conjunct the session id alone decides, and the switch
    silently becomes an override on the asking session -- the file is never
    written, so nothing a new session starts on ever changes.
    """
    import raven.rpc.methods.config as config_mod

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
            "scope": "default",
            "session_id": "tui:a",
        },
        agent_loop_factory=lambda: loop,
    )

    assert result["scope"] == "default"
    assert loop.switches == [(new_provider, "anthropic/claude-opus-4-8")]
    assert "tui:a" not in loop.session_bindings, "a default switch is not a session override"
    on_disk = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert on_disk["agents"]["defaults"]["model"] == "anthropic/claude-opus-4-8"


async def test_a_default_switch_reports_whether_it_moved_the_asking_session(fake_home: Path, monkeypatch) -> None:
    """The scope alone cannot tell a client whether to repaint the status bar.

    A session that never chose a model reads the default, so a default-scoped
    switch moves it; a session with its own binding stays where it is. The
    client cannot see the difference, so the server answers it.
    """
    import raven.rpc.methods.config as config_mod

    monkeypatch.setattr(config_mod, "make_provider", lambda _cfg: SimpleNamespace(name="new-prov"))
    monkeypatch.setattr(
        config_mod,
        "load_runtime_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(model="", provider="auto"))),
    )

    loop = _FakeLoop("old-prov", "old-model")
    loop.session_bindings["tui:chose"] = SimpleNamespace(provider="own-prov", model="own/model")

    params = {"key": "model", "value": "anthropic/claude-opus-4-8", "provider": "anthropic", "scope": "default"}

    followed = await config_set({**params, "session_id": "tui:followed"}, agent_loop_factory=lambda: loop)
    assert followed["applies_to_session"] is True

    chose = await config_set({**params, "session_id": "tui:chose"}, agent_loop_factory=lambda: loop)
    assert chose["applies_to_session"] is False


async def test_a_session_switch_always_applies_to_its_own_session(fake_home: Path, monkeypatch) -> None:
    import raven.rpc.methods.config as config_mod

    monkeypatch.setattr(config_mod, "make_provider", lambda _cfg: SimpleNamespace(name="new-prov"))
    monkeypatch.setattr(
        config_mod,
        "load_runtime_config",
        lambda *a, **k: SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(model="", provider="auto"))),
    )

    result = await config_set(
        {"key": "model", "value": "anthropic/claude-opus-4-8", "provider": "anthropic", "session_id": "tui:a"},
        agent_loop_factory=lambda: _FakeLoop("old-prov", "old-model"),
    )
    assert result["applies_to_session"] is True


async def test_a_model_switch_before_the_first_message_writes_no_session_file(tmp_path, monkeypatch) -> None:
    """``session.create`` is lazy: it mints a key and writes nothing until the
    session's first real save. Persisting the model here used to manufacture a
    zero-message record, which ``/sessions list`` then showed as an untitled
    row for every switch made before saying anything."""
    from raven.rpc.methods.config import _remember_session_model
    from raven.session.manager import SessionManager

    sessions = SessionManager(tmp_path)
    loop = SimpleNamespace(sessions=sessions)

    _remember_session_model(loop, "tui:fresh", "vendor-a/model", "anthropic")

    assert sessions.exists("tui:fresh") is False
    assert [s for s in sessions.list_sessions() if s.get("session_key") == "tui:fresh"] == []
    # In memory it is remembered, so the session's first real save carries it.
    assert sessions.get_or_create("tui:fresh").metadata["model"] == "vendor-a/model"


async def test_a_model_switch_on_a_saved_session_is_persisted_at_once(tmp_path) -> None:
    from raven.rpc.methods.config import _remember_session_model
    from raven.session.manager import SessionManager

    sessions = SessionManager(tmp_path)
    record = sessions.get_or_create("tui:saved")
    sessions.save(record)
    loop = SimpleNamespace(sessions=sessions)

    _remember_session_model(loop, "tui:saved", "vendor-b/model", "openrouter")

    reread = SessionManager(tmp_path).peek("tui:saved")
    assert reread.metadata["model"] == "vendor-b/model"
    assert reread.metadata["provider"] == "openrouter"
