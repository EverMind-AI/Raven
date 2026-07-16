"""CLI tests for ``raven provider``.

The ``provider login <name>`` command dispatches to registered OAuth handlers
(``openai-codex`` and ``github-copilot``). Real OAuth flow requires browser
+ network; these tests mock the underlying SDK calls.
"""

from __future__ import annotations

import json
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


def test_provider_help_works() -> None:
    """``raven provider --help`` lists the subcommands."""
    r = runner.invoke(app, ["provider", "--help"])
    assert r.exit_code == 0
    assert "login" in r.stdout


def test_provider_login_help_lists_argument() -> None:
    """``raven provider login --help`` surfaces the PROVIDER argument."""
    r = runner.invoke(app, ["provider", "login", "--help"])
    assert r.exit_code == 0
    assert "PROVIDER" in r.stdout
    assert "openai-codex" in r.stdout or "github-copilot" in r.stdout


def test_provider_login_unknown_provider_exits_1() -> None:
    """An unknown OAuth provider exits 1 and prints the supported list."""
    r = runner.invoke(app, ["provider", "login", "no-such-provider"])
    assert r.exit_code == 1
    assert "Unknown OAuth provider" in r.stdout
    # At least one real OAuth provider listed
    assert "openai-codex" in r.stdout or "github-copilot" in r.stdout


def test_provider_login_openai_codex_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Login starts an interactive flow even when a cached token exists."""
    from types import SimpleNamespace

    fake_token = SimpleNamespace(access="fake-access-token", account_id="user@example.com")
    login_calls = 0

    def fake_login(**_):
        nonlocal login_calls
        login_calls += 1
        return fake_token

    fake_module = SimpleNamespace(
        get_token=lambda: fake_token,
        login_oauth_interactive=fake_login,
    )
    monkeypatch.setitem(__import__("sys").modules, "oauth_cli_kit", fake_module)

    r = runner.invoke(app, ["provider", "login", "openai-codex"])
    assert r.exit_code == 0
    assert login_calls == 1
    assert "Authenticated with OpenAI Codex" in r.stdout


def test_provider_login_openai_codex_failure_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """If oauth_cli_kit returns no access token, the command exits 1."""
    from types import SimpleNamespace

    empty_token = SimpleNamespace(access=None, account_id=None)

    fake_module = SimpleNamespace(
        get_token=lambda: empty_token,
        login_oauth_interactive=lambda **_: empty_token,
    )
    monkeypatch.setitem(__import__("sys").modules, "oauth_cli_kit", fake_module)

    r = runner.invoke(app, ["provider", "login", "openai-codex"])
    assert r.exit_code == 1
    assert "Authentication failed" in r.stdout


def test_provider_login_openai_codex_disables_browser_without_linux_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    empty_token = SimpleNamespace(access=None, account_id=None)
    authenticated_token = SimpleNamespace(access="fake-access-token", account_id="user@example.com")
    login_kwargs: dict[str, object] = {}

    def fake_login(**kwargs):
        login_kwargs.update(kwargs)
        return authenticated_token

    fake_module = SimpleNamespace(
        get_token=lambda: empty_token,
        login_oauth_interactive=fake_login,
    )
    monkeypatch.setitem(__import__("sys").modules, "oauth_cli_kit", fake_module)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    r = runner.invoke(app, ["provider", "login", "openai-codex"])

    assert r.exit_code == 0
    assert login_kwargs["open_browser"] is False


def test_provider_login_github_copilot_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """github-copilot login triggers an acompletion → mock it returning OK."""

    async def fake_acompletion(**_):
        return None  # device-flow path: a successful call means auth completed

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    r = runner.invoke(app, ["provider", "login", "github-copilot"])
    assert r.exit_code == 0
    assert "Authenticated with GitHub Copilot" in r.stdout


def test_provider_login_github_copilot_error_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """If litellm.acompletion raises, the command exits 1."""

    async def boom(**_):
        raise RuntimeError("device-flow failed")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", boom)

    r = runner.invoke(app, ["provider", "login", "github-copilot"])
    assert r.exit_code == 1
    assert "Authentication error" in r.stdout


def test_provider_help_lists_all_subcommands() -> None:
    r = runner.invoke(app, ["provider", "--help"])
    assert r.exit_code == 0
    for cmd in ("login", "list", "get", "set", "test", "reset", "show"):
        assert cmd in r.stdout


def test_list_shows_every_provider(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "list"])
    assert r.exit_code == 0, r.stdout
    assert "openrouter" in r.stdout
    assert "github_copilot" in r.stdout
    assert "ollama" in r.stdout


def test_set_and_get_round_trip(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "set", "openrouter", "--api-key", "test-key"])
    assert r.exit_code == 0, r.stdout
    assert "updated" in r.stdout

    r = runner.invoke(app, ["provider", "get", "openrouter"])
    assert r.exit_code == 0, r.stdout
    assert "****set****" in r.stdout
    assert "test-key" not in r.stdout


def test_get_with_show_secrets_returns_plaintext(tmp_config: Path) -> None:
    runner.invoke(app, ["provider", "set", "openrouter", "--api-key", "test-key"])
    r = runner.invoke(app, ["provider", "get", "openrouter", "--show-secrets"])
    assert r.exit_code == 0
    assert "test-key" in r.stdout


def test_set_oauth_provider_via_api_key_rejected(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "set", "github_copilot", "--api-key", "X"])
    assert r.exit_code != 0
    assert "login" in r.output.lower()


def test_set_complex_provider_azure(tmp_config: Path) -> None:
    r = runner.invoke(
        app,
        [
            "provider",
            "set",
            "azure_openai",
            "--api-key",
            "X",
            "--api-base",
            "https://example.openai.azure.com",
        ],
    )
    assert r.exit_code == 0, r.stdout
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    section = data["providers"]["azure_openai"]
    assert section["apiKey"] == "X"
    assert section["apiBase"] == "https://example.openai.azure.com"


def test_unknown_field_points_to_show(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "set", "openrouter", "--not-a-field", "X"])
    assert r.exit_code != 0
    assert "provider show" in r.output


def test_show_lists_all_flags(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "show", "openrouter"])
    assert r.exit_code == 0
    assert "--api-key" in r.stdout
    assert "--api-base" in r.stdout


def test_show_gemini_includes_extra_flags(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "show", "gemini"])
    assert r.exit_code == 0
    assert "--vertex" in r.stdout
    assert "--api-key-list" in r.stdout


def test_reset_clears_all_fields(tmp_config: Path) -> None:
    runner.invoke(app, ["provider", "set", "openrouter", "--api-key", "X"])
    r = runner.invoke(app, ["provider", "reset", "openrouter", "--yes"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["provider", "get", "openrouter"])
    assert "(empty)" in r.stdout


def test_reset_clears_oauth_token_file(
    tmp_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "codex.json"
    token_file.write_text('{"access":"X","refresh":"R","expires":0}')
    monkeypatch.setenv("OAUTH_CLI_KIT_TOKEN_PATH", str(token_file))

    r = runner.invoke(app, ["provider", "reset", "openai_codex", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert not token_file.exists()


def test_reset_oauth_idempotent_when_no_token_file(
    tmp_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OAUTH_CLI_KIT_TOKEN_PATH", str(tmp_path / "nonexistent.json"))
    r = runner.invoke(app, ["provider", "reset", "openai_codex", "--yes"])
    assert r.exit_code == 0, r.stdout


def test_get_unknown_provider_exits_1(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "get", "no-such-provider"])
    assert r.exit_code == 1
    assert "Unknown provider" in r.output


def test_show_unknown_provider_exits_1(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "show", "no-such-provider"])
    assert r.exit_code == 1
    assert "Unknown provider" in r.output


def test_set_empty_flags_prints_schema_table(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "set", "openrouter"])
    assert r.exit_code == 0
    assert "--api-key" in r.output
    assert "Tip" in r.output or "--api-base" in r.output


def test_set_with_equals_form(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "set", "openrouter", "--api-key=sk-equals"])
    assert r.exit_code == 0, r.output
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["providers"]["openrouter"]["apiKey"] == "sk-equals"


def test_set_with_no_vertex_bool_negative(tmp_config: Path) -> None:
    runner.invoke(app, ["provider", "set", "gemini", "--vertex", "true"])
    r = runner.invoke(app, ["provider", "set", "gemini", "--no-vertex"])
    assert r.exit_code == 0, r.output
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["providers"]["gemini"]["vertex"] is False


def test_reset_without_yes_aborts_on_no(tmp_config: Path) -> None:
    runner.invoke(app, ["provider", "set", "openrouter", "--api-key", "X"])
    r = runner.invoke(app, ["provider", "reset", "openrouter"], input="n\n")
    assert r.exit_code == 0
    assert "Aborted" in r.output
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["providers"]["openrouter"]["apiKey"] == "X"


def test_test_command_success_renders_models_count(
    tmp_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from raven.config import update_providers

    def fake_probe(name: str, *, timeout_s: int = 10) -> dict:
        assert name == "openrouter"
        return {
            "ok": True,
            "status": "valid",
            "elapsed_ms": 234,
            "http_status": 200,
            "models_count": 412,
            "error": None,
        }

    monkeypatch.setattr(update_providers, "test_provider", fake_probe)

    r = runner.invoke(app, ["provider", "test", "openrouter"])
    assert r.exit_code == 0, r.output
    assert "412 models" in r.output
    assert "234ms" in r.output


def test_test_command_failure_renders_hint(
    tmp_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from raven.config import update_providers

    def fake_probe(name: str, *, timeout_s: int = 10) -> dict:
        return {
            "ok": False,
            "status": "invalid_key",
            "elapsed_ms": 50,
            "http_status": 401,
            "models_count": None,
            "error": "HTTP 401",
        }

    monkeypatch.setattr(update_providers, "test_provider", fake_probe)

    r = runner.invoke(app, ["provider", "test", "openrouter"])
    assert r.exit_code == 1
    assert "invalid_key" in r.output
    assert "provider set openrouter --api-key" in r.output


def test_test_command_unknown_provider_exits_1(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "test", "no-such-provider"])
    assert r.exit_code == 1
    assert "No registry entry" in r.output or "Unknown provider" in r.output


def test_provider_set_refuses_malformed_config_and_preserves_file(tmp_config: Path) -> None:
    # REGRESSION: provider set must not clobber a malformed config. ConfigReadError
    # is not a RuntimeError, so it bypasses the command's `except RuntimeError`
    # (which is for OAuth-refusal) and is handled uniformly at the CLI entrypoint.
    original = '{\n  "providers": {"openai": {"apiKey": "sk-o"}},\n  // comment => invalid JSON\n}\n'
    tmp_config.write_text(original, encoding="utf-8")
    result = runner.invoke(app, ["provider", "set", "openrouter", "--api-key", "sk-x"])
    assert result.exit_code != 0
    assert tmp_config.read_text(encoding="utf-8") == original  # NOT clobbered
