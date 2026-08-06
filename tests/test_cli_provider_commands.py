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


def _fake_chatgpt_authenticator(monkeypatch: pytest.MonkeyPatch, token: str | None) -> list[str]:
    """Stand in for LiteLLM's ChatGPT driver, which owns this flow.

    Patched on the module the command imports from, so the command's own wiring --
    import litellm first, then ask its authenticator -- is what runs.
    """
    calls: list[str] = []

    class _Authenticator:
        def get_access_token(self) -> str | None:
            calls.append("get_access_token")

            return token

    import sys
    from types import ModuleType

    module = ModuleType("litellm.llms.chatgpt.authenticator")
    module.Authenticator = _Authenticator  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm.llms.chatgpt.authenticator", module)

    common = ModuleType("litellm.llms.chatgpt.common_utils")
    common.CHATGPT_DEVICE_VERIFY_URL = "https://auth.openai.com/codex/device"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm.llms.chatgpt.common_utils", common)

    return calls


def test_provider_login_openai_codex_success(monkeypatch: pytest.MonkeyPatch, opened_urls: list[str]) -> None:
    """The login is one call into the driver that owns the device flow."""
    calls = _fake_chatgpt_authenticator(monkeypatch, "fake-access-token")

    r = runner.invoke(app, ["provider", "login", "openai-codex"])

    assert r.exit_code == 0
    assert calls == ["get_access_token"]
    assert "Authenticated with OpenAI Codex" in r.stdout
    assert opened_urls == ["https://auth.openai.com/codex/device"]


def test_provider_login_openai_codex_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    opened_urls: list[str],
) -> None:
    """No token back means the flow did not complete."""
    _fake_chatgpt_authenticator(monkeypatch, None)

    r = runner.invoke(app, ["provider", "login", "openai-codex"])

    assert r.exit_code == 1
    assert "Authentication failed" in r.stdout


def test_provider_login_openai_codex_opens_nothing_without_a_display(
    monkeypatch: pytest.MonkeyPatch,
    opened_urls: list[str],
) -> None:
    _fake_chatgpt_authenticator(monkeypatch, "fake-access-token")
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    r = runner.invoke(app, ["provider", "login", "openai-codex"])

    assert r.exit_code == 0
    assert opened_urls == []


@pytest.fixture
def opened_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture browser hand-offs; without this the suite opens real tabs."""
    urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: urls.append(url) or True)
    return urls


def test_provider_login_github_copilot_success(
    monkeypatch: pytest.MonkeyPatch,
    opened_urls: list[str],
) -> None:
    """github-copilot login triggers an acompletion → mock it returning OK."""

    async def fake_acompletion(**_):
        return None  # device-flow path: a successful call means auth completed

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    r = runner.invoke(app, ["provider", "login", "github-copilot"])
    assert r.exit_code == 0
    assert "Authenticated with GitHub Copilot" in r.stdout
    # LiteLLM prints the code but opens nothing, so the command opens the page
    # the code goes into -- the other two families already hand off a browser.
    assert opened_urls == ["https://github.com/login/device"]


def test_provider_login_github_copilot_headless_opens_nothing(
    monkeypatch: pytest.MonkeyPatch,
    opened_urls: list[str],
) -> None:
    async def fake_acompletion(**_):
        return None

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    r = runner.invoke(app, ["provider", "login", "github-copilot"])
    assert r.exit_code == 0
    assert opened_urls == []


def test_provider_login_github_copilot_error_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    opened_urls: list[str],
) -> None:
    """If litellm.acompletion raises, the command exits 1."""

    async def boom(**_):
        raise RuntimeError("device-flow failed")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", boom)

    r = runner.invoke(app, ["provider", "login", "github-copilot"])
    assert r.exit_code == 1
    assert "Authentication error" in r.stdout


@pytest.mark.parametrize(
    ("provider", "region", "label"),
    [
        ("minimax-global", "global", "MiniMax Global"),
        ("minimax-cn", "cn", "MiniMax CN"),
    ],
)
def test_provider_login_minimax_success(
    provider: str,
    region: str,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    seen: dict[str, object] = {}

    def fake_login(actual_region: str, **kwargs):
        seen["region"] = actual_region
        seen.update(kwargs)
        return SimpleNamespace(access="access")

    monkeypatch.setattr("raven.providers.minimax_oauth.login", fake_login)
    r = runner.invoke(app, ["provider", "login", provider])

    assert r.exit_code == 0
    assert seen["region"] == region
    assert f"Authenticated with {label}" in r.stdout


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
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "chatgpt"))
    token_file = tmp_path / "chatgpt" / "auth.json"
    token_file.parent.mkdir()
    token_file.write_text('{"access_token":"X","refresh_token":"R"}')

    r = runner.invoke(app, ["provider", "reset", "openai_codex", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert not token_file.exists()


def test_reset_oauth_idempotent_when_no_token_file(
    tmp_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "chatgpt"))
    r = runner.invoke(app, ["provider", "reset", "openai_codex", "--yes"])
    assert r.exit_code == 0, r.stdout


def test_reset_clears_minimax_oauth_token(
    tmp_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from raven.providers.minimax_oauth import MiniMaxOAuthToken, save_token

    token_file = tmp_path / "minimax_global.json"
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

    r = runner.invoke(app, ["provider", "reset", "minimax_global", "--yes"])

    assert r.exit_code == 0, r.stdout
    assert not token_file.exists()


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


def test_provider_login_openai_codex_says_so_when_it_would_do_nothing(
    monkeypatch: pytest.MonkeyPatch,
    opened_urls: list[str],
) -> None:
    """Asking the driver for a token returns the stored one when it is still good.
    Announcing a device flow and opening a browser tab first made a no-op look
    like a sign-in, and left no way to tell that switching accounts had failed."""
    calls = _fake_chatgpt_authenticator(monkeypatch, "fake-access-token")
    monkeypatch.setattr(
        "raven.providers.chatgpt_token.access_token_and_account",
        lambda: ("live", "acct"),
    )

    r = runner.invoke(app, ["provider", "login", "openai-codex"])

    assert r.exit_code == 0
    assert "Already signed in" in r.stdout
    # Named, not paraphrased: `provider remove` does not exist (the subcommand is
    # `reset`), and the wrong one was pinned here while the message printed it.
    assert "provider reset openai-codex" in r.stdout
    assert calls == [], "the driver was asked for a token anyway"
    assert opened_urls == [], "a browser tab was opened for a sign-in that did not happen"


def test_provider_login_openai_codex_signs_in_over_a_credential_that_stopped_working(
    monkeypatch: pytest.MonkeyPatch,
    opened_urls: list[str],
) -> None:
    """A revoked refresh token is still a stored credential, and the request path
    answers a revocation by sending the user to this command. Reporting "already
    signed in" for it made the two answers point at each other with no way out."""
    calls = _fake_chatgpt_authenticator(monkeypatch, "fresh-token")
    monkeypatch.setattr(
        "raven.providers.chatgpt_token.access_token_and_account",
        lambda: (_ for _ in ()).throw(RuntimeError("no longer valid")),
    )

    r = runner.invoke(app, ["provider", "login", "openai-codex"])

    assert r.exit_code == 0
    assert "Already signed in" not in r.stdout
    assert calls == ["get_access_token"], "the sign-in the user asked for never started"
