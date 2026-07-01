"""Tests for ``setup.status`` RPC handler (specs §3.9, design §3a.1).

provider_configured is true only when the onboarding gate's criterion is met:
``agents.defaults.model`` is set AND a provider signal exists (a non-``auto``
``agents.defaults.provider`` or a ``providers.<name>.apiKey``). Either alone
is not enough to drive a turn, so the UI parks on the setup panel. On any read
/ parse failure the handler returns the v0.1 fallback
``{"provider_configured": true}`` so the hermes UI never gets blocked just
because the config file is in an unexpected shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods.setup import register_setup_methods, setup_status


@pytest.fixture
def fake_home(monkeypatch, tmp_path) -> Path:
    """Redirect ``Path.home()`` to a tmp dir so tests don't touch the user's config."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


async def test_setup_status_provider_configured_true(fake_home: Path) -> None:
    cfg_dir = fake_home / ".raven"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"provider": "anthropic", "model": "anthropic/claude-sonnet-4-5"}}})
    )
    result = await setup_status({})
    assert result == {"provider_configured": True}


async def test_setup_status_provider_without_model_returns_false(fake_home: Path) -> None:
    # A provider but no default model can't drive a turn → not configured.
    cfg_dir = fake_home / ".raven"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"agents": {"defaults": {"provider": "anthropic"}}}))
    result = await setup_status({})
    assert result == {"provider_configured": False}


async def test_setup_status_missing_config_falls_back_true(fake_home: Path) -> None:
    # No file at all → v0.1 fallback true (don't block hermes UI startup).
    result = await setup_status({})
    assert result == {"provider_configured": True}


async def test_setup_status_malformed_config_falls_back_true(fake_home: Path) -> None:
    cfg_dir = fake_home / ".raven"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("not-valid-json{{{")
    result = await setup_status({})
    assert result == {"provider_configured": True}


async def test_setup_status_provider_auto_returns_false(fake_home: Path) -> None:
    cfg_dir = fake_home / ".raven"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"agents": {"defaults": {"provider": "auto"}}}))
    result = await setup_status({})
    assert result == {"provider_configured": False}


async def test_setup_status_registered_via_helper(fake_home: Path) -> None:
    cfg_dir = fake_home / ".raven"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"provider": "openai", "model": "openai/gpt-4o-mini"}}})
    )
    d = Dispatcher()
    register_setup_methods(d)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "setup.status", "params": {}})
    assert resp["result"]["provider_configured"] is True
