"""CLI tests for ``raven status``.

The command reads the active config + prints provider status. Tests use a
sandboxed tmp config via ``set_config_path``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path

runner = CliRunner()


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    yield cfg
    set_config_path(None)  # type: ignore[arg-type]


def test_status_help_works() -> None:
    """``raven status --help`` lists the command without error."""
    r = runner.invoke(app, ["status", "--help"])
    assert r.exit_code == 0
    assert "Show Raven status" in r.stdout


def test_status_without_config_still_runs(tmp_config: Path) -> None:
    """Status runs even when no config file exists (load_config returns defaults)."""
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0
    assert "Raven Status" in r.stdout
    assert "Config:" in r.stdout
    assert "Workspace:" in r.stdout


def test_status_with_existing_config_shows_model(tmp_config: Path) -> None:
    """When the config file exists, the active model + provider rows are listed."""
    from raven.config.loader import save_config
    from raven.config.schema import Config

    cfg_obj = Config()
    cfg_obj.providers.anthropic.api_key = "test-anthropic-key"
    save_config(cfg_obj)

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0
    assert "Model:" in r.stdout
    assert "Anthropic" in r.stdout


def test_status_marks_oauth_providers_distinctly(tmp_config: Path, isolated_oauth_home: Path) -> None:
    """OAuth-based providers (openai_codex, github_copilot) display ``OAuth`` flag."""
    from raven.config.loader import save_config
    from raven.config.schema import Config

    codex_auth = isolated_oauth_home / ".raven" / "oauth" / "chatgpt" / "auth.json"
    codex_auth.parent.mkdir(parents=True, exist_ok=True)
    codex_auth.write_text('{"access_token": "test-oauth-token"}', encoding="utf-8")
    save_config(Config())

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0
    # OpenAI Codex is a pure-OAuth provider in the registry; the token file
    # above simulates a completed login so the row is shown as configured.
    assert "OAuth" in r.stdout


# ---------------------------------------------------------------------------
# Unconfigured providers fold into one summary line, and the Config line
# resolves three states (ok / invalid / missing) instead of a checkmark
# whenever the file merely exists.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_oauth_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every OAuth token family at this test's tmp dir so a real login
    on the developer machine cannot leak into the logged-in/out premise."""
    home = tmp_path / "home"
    oauth = home / ".raven" / "oauth"
    oauth.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(oauth / "chatgpt"))
    monkeypatch.setenv("GITHUB_COPILOT_TOKEN_DIR", str(oauth / "github_copilot"))
    monkeypatch.setenv("MINIMAX_OAUTH_TOKEN_DIR", str(oauth))
    return home


@pytest.fixture
def tmp_home_one_provider(tmp_config: Path, isolated_oauth_home: Path) -> Path:
    """Config with exactly one provider set; no OAuth token on disk."""
    from raven.config.loader import save_config
    from raven.config.schema import Config

    cfg = Config()
    cfg.providers.openrouter.api_key = "sk-or-test-key"
    save_config(cfg)
    return isolated_oauth_home


def test_status_folds_unconfigured_providers(tmp_home_one_provider: Path) -> None:
    out = runner.invoke(app, ["status"]).stdout
    assert "not set" not in out
    assert re.search(r"\d+ providers not configured", out)


def test_status_oauth_no_false_checkmark(tmp_home_one_provider: Path) -> None:
    out = runner.invoke(app, ["status"]).stdout
    assert "✓ (OAuth)" not in out


def test_status_config_line_flags_invalid_json(tmp_config: Path) -> None:
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.write_text('{"providers": {},}', encoding="utf-8")

    out = runner.invoke(app, ["status"]).stdout
    line = out[out.index("Config:") : out.index("Workspace:")]
    assert "✓" not in line
