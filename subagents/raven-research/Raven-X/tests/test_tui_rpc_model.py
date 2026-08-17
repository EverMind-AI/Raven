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

from raven.providers.common_models import common_models_for
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
    # Configured models rank first; the curated shortlist follows (deduped).
    assert entry["models"][:2] == ["claude-opus-4-8", "claude-sonnet-4-5"]
    assert entry["total_models"] == 2 + len(common_models_for("anthropic"))
    assert entry["auth_type"] == "api_key"
    assert entry["key_env"] == "ANTHROPIC_API_KEY"


async def test_options_unauthed_provider_marked(fake_home: Path) -> None:
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    result = await model_options({})
    entry = _entry(result, "openai")
    assert entry["authenticated"] is False
    # Curated shortlist is shown regardless of auth (as openrouter always has),
    # so the picker is never empty; the unauthed state is conveyed separately.
    assert entry["models"] == common_models_for("openai")
    assert entry["total_models"] == len(common_models_for("openai"))


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
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    result = await model_options({})
    entry = _entry(result, "openai_codex")
    assert entry["auth_type"] == "oauth"
    assert entry["authenticated"] is False
    assert entry["warning"]
    assert "provider login" in entry["warning"]


async def test_options_needs_api_base_flag(fake_home: Path) -> None:
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
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
    result = await model_remove_model({"slug": "anthropic", "model": "claude-opus-4-8"})
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

    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    d = Dispatcher()
    register_model_methods(d)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "model.options", "params": {}})
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
    _write_config(fake_home, {"agents": {"defaults": {"model": "anthropic/claude-sonnet-4-5"}}})
    result = await model_options({"session_id": "tui:default"})
    assert "providers" in result


async def test_save_key_custom_without_api_base_rejected(fake_home: Path) -> None:
    with pytest.raises(ConfigValidationError):
        await model_save_key({"slug": "custom", "api_key": "x"})


# ----------------------------------------------------------------------------
# common-model shortlist (curated defaults shown in the picker)
# ----------------------------------------------------------------------------


async def test_options_openrouter_seeds_common_models(fake_home: Path) -> None:
    # A provider with a key but no explicitly configured models still lists the
    # curated "common" shortlist, so the picker is never empty.
    _write_config(
        fake_home,
        {
            "agents": {"defaults": {"model": "openrouter/anthropic/claude-opus-4.8"}},
            "providers": {"openrouter": {"apiKey": "sk-or-xxx", "models": []}},
        },
    )
    entry = _entry(await model_options({}), "openrouter")
    assert entry["models"] == common_models_for("openrouter")
    assert entry["total_models"] == len(common_models_for("openrouter"))


async def test_options_config_models_rank_before_common_and_dedup(fake_home: Path) -> None:
    # Configured models come first; the common shortlist follows with duplicates
    # removed (a configured id already in the shortlist is not listed twice).
    dup = common_models_for("openrouter")[0]
    _write_config(
        fake_home,
        {
            "agents": {"defaults": {"model": dup}},
            "providers": {"openrouter": {"apiKey": "sk-or-xxx", "models": ["my/custom-model", dup]}},
        },
    )
    models = _entry(await model_options({}), "openrouter")["models"]
    assert models[:2] == ["my/custom-model", dup]
    assert models.count(dup) == 1
    assert set(common_models_for("openrouter")).issubset(set(models))


# Direct providers seeded for issue #100 (keyed provider, empty config models,
# used to show an empty picker). ``prefix`` is the litellm-routing form each id
# must carry so a picked id drops straight into ``agents.defaults.model``.
_SEEDED_DIRECT_PROVIDERS = [
    ("deepseek", "deepseek/"),
    ("openai", "openai/"),
    ("anthropic", "anthropic/"),
    ("gemini", "gemini/"),
    ("zhipu", "zai/"),
    ("groq", "groq/"),
    ("dashscope", "dashscope/"),
]


@pytest.mark.parametrize("slug, prefix", _SEEDED_DIRECT_PROVIDERS)
def test_common_models_seeded_for_direct_providers(slug: str, prefix: str) -> None:
    models = common_models_for(slug)
    assert models, f"{slug} common-model shortlist is empty"
    assert all(m.startswith(prefix) for m in models), models
    assert len(models) == len(set(models)), "duplicate ids in shortlist"


@pytest.mark.parametrize("slug, prefix", _SEEDED_DIRECT_PROVIDERS)
async def test_options_direct_provider_lists_common_models_when_unconfigured(
    fake_home: Path, slug: str, prefix: str
) -> None:
    _write_config(
        fake_home,
        {
            "agents": {"defaults": {"model": "openrouter/anthropic/claude-opus-4.8"}},
            "providers": {slug: {"apiKey": "sk-test-xxxxxxx", "models": []}},
        },
    )
    entry = _entry(await model_options({}), slug)
    assert entry["total_models"] > 0
    assert entry["models"] == common_models_for(slug)
