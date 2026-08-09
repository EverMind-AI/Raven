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
        json.dumps(
            {
                "agents": {"defaults": {"provider": "anthropic", "model": "anthropic/claude-sonnet-4-5"}},
                "providers": {"anthropic": {"apiKey": "sk-ant"}},
            }
        )
    )
    result = await setup_status({})
    assert result == {"provider_configured": True}


async def test_a_pinned_provider_name_is_not_credentials(fake_home: Path) -> None:
    """The name says which section to ask about, not that it holds anything.

    ``agents.defaults.provider`` was waved through on its own, as a signal from
    configs predating per-provider sections. It is now written on every model
    change, so that branch would have let a pinned name stand for credentials
    nobody has -- an empty config would pass the gate and the first turn would
    fail with whatever the backend said about a missing key.
    """
    cfg_dir = fake_home / ".raven"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"provider": "anthropic", "model": "anthropic/claude-sonnet-4-5"}}})
    )

    assert await setup_status({}) == {"provider_configured": False}


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


async def test_setup_status_minimax_oauth_token_returns_true(
    fake_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from raven.providers.minimax_oauth import MiniMaxOAuthToken, save_token

    cfg_dir = fake_home / ".raven"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"provider": "auto", "model": "minimax-global/MiniMax-M3"}}})
    )
    monkeypatch.setenv("MINIMAX_OAUTH_TOKEN_DIR", str(tmp_path))
    save_token(
        "global",
        MiniMaxOAuthToken(
            "access",
            "refresh",
            4_000_000_000_000,
            "https://api.minimax.io/anthropic/v1",
        ),
    )

    assert await setup_status({}) == {"provider_configured": True}


async def test_setup_status_ignores_minimax_token_for_other_model(
    fake_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from raven.providers.minimax_oauth import MiniMaxOAuthToken, save_token

    cfg_dir = fake_home / ".raven"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"provider": "auto", "model": "anthropic/claude-sonnet-4-5"}}})
    )
    monkeypatch.setenv("MINIMAX_OAUTH_TOKEN_DIR", str(tmp_path))
    save_token(
        "global",
        MiniMaxOAuthToken(
            "access",
            "refresh",
            4_000_000_000_000,
            "https://api.minimax.io/anthropic/v1",
        ),
    )

    assert await setup_status({}) == {"provider_configured": False}


async def test_setup_status_registered_via_helper(fake_home: Path) -> None:
    cfg_dir = fake_home / ".raven"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {"provider": "openai", "model": "openai/gpt-4o-mini"}},
                "providers": {"openai": {"apiKey": "sk-openai"}},
            }
        )
    )
    d = Dispatcher()
    register_setup_methods(d)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "setup.status", "params": {}})
    assert resp["result"]["provider_configured"] is True


def test_minimax_oauth_is_detected_from_either_spelling_of_the_prefix(monkeypatch) -> None:
    """The region is read off the model-id prefix, which arrives underscored.

    The check used to compare against hyphenated literals, so a saved
    "minimax_global/..." matched nothing and a logged-in user read as unconfigured.
    """
    import raven.tui_rpc.methods.setup as setup

    seen: list[str] = []
    monkeypatch.setattr(
        "raven.providers.minimax_oauth.load_token",
        lambda region: seen.append(region) or object(),
    )
    for model in ("minimax_global/abab6.5", "minimax-global/abab6.5"):
        seen.clear()
        payload = {"agents": {"defaults": {"model": model}}}
        assert setup._detect_provider_configured(payload) is True
        assert seen == ["global"], model
