"""Tests for the ``model.*`` RPC handlers (TUI ``/model`` v1 backend).

The five handlers wrap ``raven.config.update_providers`` write/read helpers
plus the provider registry. Config is sandboxed by redirecting ``Path.home()``
to a tmp dir (same mechanism as ``test_tui_rpc_config`` / ``test_tui_rpc_setup``)
so the real user config is never touched. No network is hit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.tui_rpc.errors import ConfigValidationError, NotSupportedInV01Error
from raven.tui_rpc.methods.model import (
    model_add_model,
    model_disconnect,
    model_options,
    model_remove_model,
    model_save_key,
)


@pytest.fixture
def fake_home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _write_config(home: Path, payload: dict) -> None:
    cfg_dir = home / ".raven"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _entry(result: dict, slug: str) -> dict:
    for entry in result["providers"]:
        if entry["slug"] == slug:
            return entry
    raise AssertionError(f"provider {slug!r} not in options result")


# ----------------------------------------------------------------------------
# model.options
# ----------------------------------------------------------------------------


async def test_options_authed_provider_lists_models(fake_home: Path) -> None:
    _write_config(
        fake_home,
        {
            "agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}},
            "providers": {
                "anthropic": {
                    "apiKey": "sk-ant-xxx",
                    "models": ["claude-opus-4-8", "claude-sonnet-4-5"],
                }
            },
        },
    )
    result = await model_options({})
    entry = _entry(result, "anthropic")
    assert entry["authenticated"] is True
    assert entry["models"] == ["claude-opus-4-8", "claude-sonnet-4-5"]
    assert entry["total_models"] == 2
    assert entry["auth_type"] == "api_key"
    assert entry["key_env"] == "ANTHROPIC_API_KEY"


async def test_options_unauthed_provider_marked(fake_home: Path) -> None:
    _write_config(
        fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}}
    )
    result = await model_options({})
    entry = _entry(result, "openai")
    assert entry["authenticated"] is False
    assert entry["models"] == []
    assert entry["total_models"] == 0


async def test_options_current_provider_marked(fake_home: Path) -> None:
    _write_config(
        fake_home,
        {
            "agents": {
                "defaults": {
                    "model": "anthropic/claude-sonnet-4-5",
                    "provider": "anthropic",
                }
            }
        },
    )
    result = await model_options({})
    assert result["model"] == "anthropic/claude-sonnet-4-5"
    assert result["provider"] == "anthropic"
    assert _entry(result, "anthropic")["is_current"] is True
    assert _entry(result, "openai")["is_current"] is False


async def test_options_current_provider_derived_from_model(fake_home: Path) -> None:
    _write_config(
        fake_home,
        {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}},
    )
    result = await model_options({})
    assert result["provider"] == "anthropic"
    assert _entry(result, "anthropic")["is_current"] is True


async def test_options_oauth_provider_warning_and_auth_type(fake_home: Path) -> None:
    _write_config(
        fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}}
    )
    result = await model_options({})
    entry = _entry(result, "openai_codex")
    assert entry["auth_type"] == "oauth"
    assert entry["authenticated"] is False
    assert entry["warning"]
    assert "provider login" in entry["warning"]


async def test_options_needs_api_base_flag(fake_home: Path) -> None:
    _write_config(
        fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}}
    )
    result = await model_options({})
    assert _entry(result, "custom")["needs_api_base"] is True
    assert _entry(result, "azure_openai")["needs_api_base"] is True
    assert _entry(result, "anthropic")["needs_api_base"] is False


# ----------------------------------------------------------------------------
# model.save_key
# ----------------------------------------------------------------------------


async def test_save_key_happy_path_writes_key(fake_home: Path) -> None:
    result = await model_save_key({"slug": "anthropic", "api_key": "sk-ant-new"})
    entry = result["provider"]
    assert entry["slug"] == "anthropic"
    assert entry["authenticated"] is True

    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["providers"]["anthropic"]["apiKey"] == "sk-ant-new"


async def test_save_key_custom_accepts_api_base(fake_home: Path) -> None:
    result = await model_save_key(
        {
            "slug": "custom",
            "api_key": "key123",
            "api_base": "https://example.test/v1",
        }
    )
    assert result["provider"]["slug"] == "custom"
    cfg = json.loads((fake_home / ".raven" / "config.json").read_text())
    assert cfg["providers"]["custom"]["apiBase"] == "https://example.test/v1"


async def test_save_key_oauth_rejected(fake_home: Path) -> None:
    with pytest.raises(NotSupportedInV01Error):
        await model_save_key({"slug": "openai_codex", "api_key": "x"})


async def test_save_key_missing_params_rejected(fake_home: Path) -> None:
    with pytest.raises(ConfigValidationError):
        await model_save_key({"slug": "anthropic"})


# ----------------------------------------------------------------------------
# model.disconnect
# ----------------------------------------------------------------------------


async def test_disconnect_clears_creds(fake_home: Path) -> None:
    await model_save_key({"slug": "anthropic", "api_key": "sk-ant-xxx"})
    result = await model_disconnect({"slug": "anthropic"})
    assert result == {"disconnected": True}

    options = await model_options({})
    assert _entry(options, "anthropic")["authenticated"] is False


# ----------------------------------------------------------------------------
# model.add_model / model.remove_model
# ----------------------------------------------------------------------------


async def test_add_model_reflected_in_options(fake_home: Path) -> None:
    await model_save_key({"slug": "anthropic", "api_key": "sk-ant-xxx"})
    result = await model_add_model({"slug": "anthropic", "model": "claude-opus-4-8"})
    assert "claude-opus-4-8" in result["provider"]["models"]

    options = await model_options({})
    assert "claude-opus-4-8" in _entry(options, "anthropic")["models"]


async def test_remove_model_reflected_in_options(fake_home: Path) -> None:
    await model_save_key({"slug": "anthropic", "api_key": "sk-ant-xxx"})
    await model_add_model({"slug": "anthropic", "model": "claude-opus-4-8"})
    result = await model_remove_model(
        {"slug": "anthropic", "model": "claude-opus-4-8"}
    )
    assert "claude-opus-4-8" not in result["provider"]["models"]

    options = await model_options({})
    assert "claude-opus-4-8" not in _entry(options, "anthropic")["models"]


async def test_add_model_unknown_provider_rejected(fake_home: Path) -> None:
    with pytest.raises(ConfigValidationError):
        await model_add_model({"slug": "no_such_provider", "model": "x"})


# ----------------------------------------------------------------------------
# Dispatcher wiring
# ----------------------------------------------------------------------------


async def test_model_methods_registered_via_helper(fake_home: Path) -> None:
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods.model import register_model_methods

    _write_config(
        fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}}
    )
    d = Dispatcher()
    register_model_methods(d)
    resp = await d.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "model.options", "params": {}}
    )
    assert "error" not in resp
    assert resp["result"]["model"] == "anthropic/claude-sonnet-4-5"

    resp = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "model.save_key",
            "params": {"slug": "openai_codex", "api_key": "x"},
        }
    )
    assert resp["error"]["code"] == -32012


# ----------------------------------------------------------------------------
# Regressions (code review)
# ----------------------------------------------------------------------------


async def test_options_accepts_session_id(fake_home: Path) -> None:
    # The picker calls model.options with {session_id: "tui:default"}; the param
    # model must accept it (strict models reject unknown keys otherwise).
    _write_config(
        fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}}
    )
    result = await model_options({"session_id": "tui:default"})
    assert "providers" in result


async def test_save_key_custom_without_api_base_rejected(fake_home: Path) -> None:
    with pytest.raises(ConfigValidationError):
        await model_save_key({"slug": "custom", "api_key": "x"})
