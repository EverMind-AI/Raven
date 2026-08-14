"""``settings.set`` — the GUI dialog's whitelisted hot-writable keys."""

from __future__ import annotations

import json

import pytest

from raven.tui_rpc.errors import ConfigValidationError
from raven.tui_rpc.methods import console as rpc_console

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    return path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


async def test_bool_key_writes_through(cfg):
    r = await rpc_console.settings_set({"key": "channels.sendProgress", "value": True})
    assert r["applied"] is True
    assert _read(cfg)["channels"]["sendProgress"] is True


async def test_bool_key_rejects_non_bool(cfg):
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "channels.sendProgress", "value": "yes"})


@pytest.mark.parametrize(
    "key,value",
    [
        ("tools.restrictToWorkspace", False),
        ("tools.sandbox.backend", "none"),
        ("tools.web.proxy", "http://attacker.example:8080"),
    ],
)
async def test_the_containment_controls_are_not_writable_here(cfg, key, value):
    """These three decide what a caller who reaches this endpoint can then do.

    ``settings.set`` is reachable from any RPC client with no confirmation step,
    so the first two -- the workspace sandbox and the exec sandbox -- must not be
    switchable through it, and the third would route every WebSearch and
    WebFetch, API keys and all, through a host of the caller's choosing. Editing
    the config file is the friction, and it is deliberate.
    """
    with pytest.raises(ConfigValidationError, match="not writable"):
        await rpc_console.settings_set({"key": key, "value": value})
    assert key.split(".")[-1] not in str(_read(cfg)), "nothing may have been written"


async def test_int_key_bounds(cfg):
    r = await rpc_console.settings_set({"key": "tools.exec.timeout", "value": 120})
    assert r["applied"] is True
    assert _read(cfg)["tools"]["exec"]["timeout"] == 120
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "tools.exec.timeout", "value": 0})
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "tools.exec.timeout", "value": True})


async def test_enum_key(cfg):
    r = await rpc_console.settings_set({"key": "agents.defaults.reasoningEffort", "value": "high"})
    assert r["applied"] is True
    assert _read(cfg)["agents"]["defaults"]["reasoningEffort"] == "high"
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "agents.defaults.reasoningEffort", "value": "extreme"})


async def test_memory_backend_not_writable(cfg):
    """The backend is not a user choice - long-term memory is EverOS."""
    with pytest.raises(ConfigValidationError, match="not writable"):
        await rpc_console.settings_set({"key": "memory.backend", "value": None})


@pytest.fixture()
def everos_toml(tmp_path, monkeypatch):
    path = tmp_path / "everos.toml"
    monkeypatch.setattr("raven.config.update_everos._EVEROS_CONFIG", path)
    return path


async def test_everos_get_masks_key(everos_toml):
    everos_toml.write_text(
        '[embedding]\nmodel = "openai/text-embedding-3-large"\napi_key = "sk-secret"\n'
        '[llm]\nmodel = "<pick-a-model>"\n',
        encoding="utf-8",
    )
    r = await rpc_console.settings_everos({})
    emb = r["sections"]["embedding"]
    assert emb["model"] == "openai/text-embedding-3-large"
    assert emb["api_key_set"] is True
    assert "sk-secret" not in str(r)
    assert r["sections"]["llm"]["model"] == ""


async def test_everos_set_merges_section(everos_toml):
    await rpc_console.settings_everos_set({"section": "embedding", "fields": {"model": "m1", "api_key": "k1"}})
    await rpc_console.settings_everos_set({"section": "embedding", "fields": {"base_url": "https://x/v1"}})
    import tomllib

    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["embedding"] == {"model": "m1", "api_key": "k1", "base_url": "https://x/v1"}


async def test_everos_set_rejects_bad_input(everos_toml):
    with pytest.raises(ConfigValidationError, match="unknown everos section"):
        await rpc_console.settings_everos_set({"section": "memory", "fields": {"model": "m"}})
    with pytest.raises(ConfigValidationError, match="not writable"):
        await rpc_console.settings_everos_set({"section": "llm", "fields": {"dimensions": "1024"}})
    with pytest.raises(ConfigValidationError, match="non-empty"):
        await rpc_console.settings_everos_set({"section": "llm", "fields": {"model": "  "}})


async def test_everos_clear_optional_only(everos_toml):
    everos_toml.write_text('[rerank]\nmodel = "r1"\n[llm]\nmodel = "m1"\n', encoding="utf-8")
    r = await rpc_console.settings_everos_set({"section": "rerank", "clear": True})
    assert r["applied"] is True
    import tomllib

    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert "rerank" not in data
    with pytest.raises(ConfigValidationError, match="required"):
        await rpc_console.settings_everos_set({"section": "llm", "clear": True})


async def test_secret_key_writes_string(cfg):
    r = await rpc_console.settings_set({"key": "tools.web.search.apiKey", "value": "sk-x"})
    assert r["applied"] is True
    assert _read(cfg)["tools"]["web"]["search"]["apiKey"] == "sk-x"


async def test_unknown_key_rejected(cfg):
    with pytest.raises(ConfigValidationError, match="not writable"):
        await rpc_console.settings_set({"key": "providers.openai.apiKey", "value": "x"})


async def test_previous_value_returned(cfg):
    await rpc_console.settings_set({"key": "channels.sendProgress", "value": False})
    r = await rpc_console.settings_set({"key": "channels.sendProgress", "value": True})
    assert r["previous"] is False
