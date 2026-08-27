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

from raven.cli import provider_commands
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


def _fake_chatgpt_authenticator(
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    *,
    on_login=None,
) -> list[str]:
    """Stand in for LiteLLM's ChatGPT driver, which owns this flow.

    Patched on the module the command imports from, so the command's own wiring --
    import litellm first, then ask its authenticator -- is what runs. ``on_login``
    stands in for the credential the real driver writes on its way to a token.
    """
    calls: list[str] = []

    class _Authenticator:
        def get_access_token(self) -> str | None:
            calls.append("get_access_token")
            if on_login is not None:
                on_login()

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


@pytest.mark.parametrize(
    ("slug", "written"),
    [
        ("openai-codex", ("chatgpt/auth.json",)),
        ("github-copilot", ("github_copilot/access-token", "github_copilot/api-key.json")),
    ],
)
def test_a_sign_in_leaves_its_credential_readable_only_by_its_owner(
    slug: str,
    written: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_urls: list[str],
) -> None:
    """The drivers that write these are LiteLLM's, and they use a plain ``open()``:
    under a normal umask the credential lands world-readable. The mode is set here
    explicitly rather than left to the runner's umask, so this asks about the
    command and not about the machine.
    """
    import stat

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for env in ("CHATGPT_TOKEN_DIR", "GITHUB_COPILOT_TOKEN_DIR", "MINIMAX_OAUTH_TOKEN_DIR"):
        monkeypatch.delenv(env, raising=False)

    paths = [tmp_path / ".raven" / "oauth" / name for name in written]

    def _write_them_wide_open() -> None:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("credential", encoding="utf-8")
            path.chmod(0o644)

    if slug == "openai-codex":
        _fake_chatgpt_authenticator(monkeypatch, "fake-access-token", on_login=_write_them_wide_open)
    else:
        # The dispatch reads this dict, so replacing the entry is what stands in for
        # the whole copilot flow.
        monkeypatch.setitem(
            provider_commands._LOGIN_HANDLERS,
            "github_copilot",
            _write_them_wide_open,
        )

    r = runner.invoke(app, ["provider", "login", slug])

    assert r.exit_code == 0, r.stdout
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, f"{path.name} is readable by anyone"


def test_the_oauth_directory_is_owner_only_even_if_it_already_existed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not every writer in here is ours to fix -- LiteLLM's create their files
    under the umask, and a family added later will too. The directory is what
    holds for them, so an existing loose one is tightened rather than trusted."""
    import stat

    from raven.config.paths import get_oauth_dir

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    loose = tmp_path / ".raven" / "oauth"
    loose.mkdir(parents=True)
    loose.chmod(0o755)

    assert stat.S_IMODE(get_oauth_dir().stat().st_mode) == 0o700


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
    """Capture browser hand-offs; without this the suite opens real tabs.

    A display is declared too. Whether the login opens a page depends on having
    somewhere to open it, and CI runs headless Linux -- so every assertion here
    would otherwise be answered by the runner rather than by the code: the ones
    expecting a page opened fail, and the ones expecting none pass for the wrong
    reason. The two headless cases drop it again.
    """
    monkeypatch.setenv("DISPLAY", ":0")
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


def test_the_three_bool_flag_forms_are_parsed(monkeypatch) -> None:
    """Tested against the parser rather than a provider, because none declares a bool.

    Gemini's ``vertex`` was the only one and it has been removed. The parser
    keeps the forms -- it mirrors ``_parse_channel_flags``, where bool fields are
    common -- so this exercises them directly instead of asserting through a
    field that would have to be invented to keep the test alive.
    """
    from raven.cli import provider_commands

    # Patched where it is looked up: the parser imports it inside the function,
    # so the name on `provider_commands` is never the one that gets called.
    monkeypatch.setattr(
        "raven.config.update_providers.provider_field_specs",
        lambda name: {"dry_run": {"type": "bool", "default": False, "is_secret": False, "description": ""}},
    )
    parse = provider_commands._parse_provider_flags
    # A written value comes back as written; the schema coerces it later.
    assert parse(["--dry-run", "true"], "gemini") == {"dry_run": "true"}
    assert parse(["--dry-run"], "gemini") == {"dry_run": True}
    assert parse(["--no-dry-run"], "gemini") == {"dry_run": False}


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


def test_resetting_the_provider_behind_the_default_model_says_so(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The model id survives the reset and still names this provider, so the next
    command finds a default nothing can answer. Said before it happens and again
    after, because the second half is what the user acts on."""
    monkeypatch.setattr(
        "raven.config.update_providers.serves_default_model",
        lambda name, **_: True,
    )
    monkeypatch.setattr("raven.config.update_providers.reset_provider", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "raven.config.update_providers.get_provider_config",
        lambda *_a, **_k: {"api_key": "sk-x"},
    )

    r = runner.invoke(app, ["provider", "reset", "openrouter", "-y"])

    assert r.exit_code == 0
    assert "no longer works" in r.stdout, r.stdout
    assert "/model" in r.stdout


def test_resetting_an_unrelated_provider_stays_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "raven.config.update_providers.serves_default_model",
        lambda name, **_: False,
    )
    monkeypatch.setattr("raven.config.update_providers.reset_provider", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "raven.config.update_providers.get_provider_config",
        lambda *_a, **_k: {"api_key": "sk-x"},
    )

    r = runner.invoke(app, ["provider", "reset", "openrouter", "-y"])

    assert r.exit_code == 0
    assert "no longer works" not in r.stdout


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        pytest.param("openai-codex", "raven provider login openai-codex", id="oauth-signs-in"),
        pytest.param("openrouter", "raven provider set openrouter --api-key", id="key-takes-a-key"),
        pytest.param("hosted-vllm", "raven provider set hosted-vllm --api-base", id="local-takes-an-address"),
        pytest.param(
            "azure-openai",
            "raven provider set azure-openai --api-key <KEY> --api-base",
            id="endpoint-takes-both",
        ),
    ],
)
def test_resetting_the_provider_behind_the_default_model_names_the_way_back(
    slug: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model id survives the reset and still names this provider, so the next
    command finds a default nothing can answer. The way back differs by credential
    kind: `provider login` exits 1 for anyone who is not an OAuth family, and a
    local deployment has no key to give -- so a single spelling sent four of them
    to a flag that does nothing.
    """
    monkeypatch.setattr(
        "raven.config.update_providers.serves_default_model",
        lambda name, **_: True,
    )
    monkeypatch.setattr("raven.config.update_providers.reset_provider", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "raven.config.update_providers.get_provider_config",
        lambda *_a, **_k: {},
    )

    r = runner.invoke(app, ["provider", "reset", slug, "-y"])

    assert r.exit_code == 0
    flat = " ".join(r.stdout.split())
    assert "no longer works" in flat, flat
    assert expected in flat, flat


# ---------------------------------------------------------------------------
# `provider use`: the third surface that can change the model
# ---------------------------------------------------------------------------


def test_use_sets_the_default_model_in_the_shared_spelling(tmp_config: Path) -> None:
    """The CLI writes what the wizard and the picker write.

    Three surfaces choose models and only two could; the third had to re-run the
    whole wizard. Now that all three write, they have to write the same string --
    that is the contract the storage step established.
    """
    from raven.providers.wire import stored_model_id

    r = runner.invoke(app, ["provider", "use", "claude-sonnet-5", "--provider", "anthropic"])
    assert r.exit_code == 0, r.output

    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    stored = data["agents"]["defaults"]["model"]
    assert stored == stored_model_id("anthropic", "claude-sonnet-5") == "anthropic/claude-sonnet-5"


def test_use_infers_the_provider_from_a_qualified_id(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "use", "deepseek/deepseek-chat", "--provider", "deepseek"])
    assert r.exit_code == 0, r.output
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["agents"]["defaults"]["model"] == "deepseek/deepseek-chat"


def test_use_warns_but_does_not_refuse_when_the_provider_has_no_credentials(tmp_config: Path) -> None:
    """Picking a model before configuring its provider is a normal order.

    Refusing would force the two steps into one sequence; the startup gate says
    the same thing again if the key is still missing by the time it matters.
    """
    r = runner.invoke(app, ["provider", "use", "deepseek/deepseek-chat", "--provider", "deepseek"])
    assert r.exit_code == 0, r.output
    assert "API key" in r.output


def test_use_refuses_an_id_that_names_no_provider(tmp_config: Path) -> None:
    """There is no honest guess to make. A bare id names nobody, and a prefixed
    one names a routing path rather than a credential -- both used to be filled
    in by detection, which decided whose key paid for the call. The refusal
    names the flag and points at the configured list."""
    r = runner.invoke(app, ["provider", "use", "some-unqualified-model"])

    assert r.exit_code == 1
    assert "--provider is required" in r.output
    assert "provider list" in r.output
    assert not tmp_config.exists(), "a refused switch must not write a config"


def test_use_moves_the_pin_instead_of_reporting_that_it_is_stuck(tmp_config: Path) -> None:
    """A write that changes nothing must not report success and stop there.

    `agents.defaults.provider` overrides what a model id names, so `provider use`
    wrote the model, printed a tick, and requests kept going to the pinned
    provider. It used to say so and tell the user to set the field to 'auto' --
    advice no command could follow, because none wrote that field. It writes it
    now, by the same rule the picker uses.
    """
    tmp_config.write_text(json.dumps({"agents": {"defaults": {"provider": "openai"}}}), encoding="utf-8")
    # Configured, because an unconfigured vendor is deliberately left on `auto`
    # so a gateway can serve it -- see test_use_does_not_pin_a_vendor_that_has_no_configuration.
    runner.invoke(app, ["provider", "set", "anthropic", "--api-key", "sk-ant"])

    r = runner.invoke(app, ["provider", "use", "anthropic/claude-sonnet-5", "--provider", "anthropic"])

    assert r.exit_code == 0, r.output
    defaults = json.loads(tmp_config.read_text(encoding="utf-8"))["agents"]["defaults"]
    assert defaults["provider"] == "anthropic"
    assert defaults["model"] == "anthropic/claude-sonnet-5"
    assert "pinned" not in r.output, "the note is about a state that can no longer happen"


def test_use_moves_the_pin_to_a_vendor_raven_has_no_spec_for(tmp_config: Path) -> None:
    """A passthrough vendor is named like any other. What used to happen here --
    handing routing back to `auto` because no spec claimed the id -- was the
    detection deciding for the user; naming it is the whole point."""
    tmp_config.write_text(json.dumps({"agents": {"defaults": {"provider": "openai"}}}), encoding="utf-8")

    r = runner.invoke(app, ["provider", "use", "mistral/mistral-large", "--provider", "mistral"])

    assert r.exit_code == 0, r.output
    defaults = json.loads(tmp_config.read_text(encoding="utf-8"))["agents"]["defaults"]
    assert defaults["provider"] == "mistral"
    assert defaults["model"] == "mistral/mistral-large"


def test_use_keeps_a_pin_that_serves_the_bare_id(tmp_config: Path) -> None:
    """A bare id names nobody, so the pin is the only evidence -- and it is kept
    only when the pinned provider actually serves the model."""
    tmp_config.write_text(json.dumps({"agents": {"defaults": {"provider": "deepseek"}}}), encoding="utf-8")
    runner.invoke(app, ["provider", "set", "deepseek", "--api-key", "sk-ds"])

    r = runner.invoke(app, ["provider", "use", "deepseek-chat", "--provider", "deepseek"])

    assert r.exit_code == 0, r.output
    defaults = json.loads(tmp_config.read_text(encoding="utf-8"))["agents"]["defaults"]
    assert defaults["provider"] == "deepseek"
    assert defaults["model"] == "deepseek/deepseek-chat"


def test_use_leaves_the_config_alone_when_it_cannot_tell(tmp_config: Path) -> None:
    """Refusing is the point: writing a guess would route one vendor's key to
    another, which is what the prefix rules exist to prevent."""
    before = json.dumps({"agents": {"defaults": {"provider": "deepseek", "model": "deepseek/deepseek-chat"}}})
    tmp_config.write_text(before, encoding="utf-8")

    r = runner.invoke(app, ["provider", "use", "some-model-nobody-serves"])

    assert r.exit_code == 1
    assert json.loads(tmp_config.read_text(encoding="utf-8")) == json.loads(before)


def test_use_says_so_when_an_azure_deployment_overrides_the_model_id(tmp_config: Path) -> None:
    """Azure's deployment decides the deployment; the model id then does nothing."""
    runner.invoke(app, ["provider", "set", "azure-openai", "--api-key", "k", "--api-base", "https://x/"])
    runner.invoke(app, ["provider", "set", "azure-openai", "--deployment", "prod-gpt4"])

    r = runner.invoke(app, ["provider", "use", "azure-openai/some-model", "--provider", "azure_openai"])
    assert r.exit_code == 0, r.output
    assert "deployment" in r.output and "prod-gpt4" in r.output


def test_use_pins_the_named_vendor_and_says_it_has_no_credentials(tmp_config: Path) -> None:
    """Naming a vendor you have not configured yet is a normal order to do
    things in, so it is written and reported rather than quietly redirected.

    What it must not do is what detection used to: an OpenRouter-only install
    running `provider use anthropic/...` had the pin silently handed elsewhere,
    which is convenient right up until the bill arrives from the wrong vendor.
    The startup gate says the same thing again if the key is still missing.
    """
    from raven.config.loader import load_config

    runner.invoke(app, ["provider", "set", "openrouter", "--api-key", "sk-or"])

    r = runner.invoke(app, ["provider", "use", "anthropic/claude-sonnet-4-5", "--provider", "anthropic"])
    assert r.exit_code == 0, r.output

    defaults = json.loads(tmp_config.read_text(encoding="utf-8"))["agents"]["defaults"]
    assert defaults["provider"] == "anthropic", "the named vendor is written down, configured or not"
    assert "no api key" in r.output.lower() or "api key" in r.output.lower()

    # And the pin is what routing answers with -- no silent redirection to the
    # provider that happens to hold a key.
    _, name = load_config()._match_provider(defaults["model"])
    assert name == "anthropic"


def test_use_still_pins_a_vendor_that_is_configured(tmp_config: Path) -> None:
    """The fallback is for the unconfigured case only -- a vendor the user has
    set up is still named outright, so its own key is the one used."""
    runner.invoke(app, ["provider", "set", "anthropic", "--api-key", "sk-ant"])

    r = runner.invoke(app, ["provider", "use", "anthropic/claude-sonnet-4-5", "--provider", "anthropic"])
    assert r.exit_code == 0, r.output
    assert json.loads(tmp_config.read_text(encoding="utf-8"))["agents"]["defaults"]["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# provider endpoint add / remove / list
# ---------------------------------------------------------------------------


def test_endpoint_help_lists_subcommands() -> None:
    r = runner.invoke(app, ["provider", "endpoint", "--help"])
    assert r.exit_code == 0
    assert "add" in r.stdout
    assert "remove" in r.stdout
    assert "list" in r.stdout


def test_endpoint_add_writes_the_section(tmp_config: Path) -> None:
    r = runner.invoke(
        app,
        ["provider", "endpoint", "add", "openrouter", "--label", "primary", "--api-key", "k1"],
    )
    assert r.exit_code == 0, r.output
    assert "primary" in r.stdout

    section = json.loads(tmp_config.read_text(encoding="utf-8"))["providers"]["openrouter"]
    assert section["endpoints"] == [{"label": "primary", "apiKey": "k1", "apiBase": None, "extraHeaders": None}]


def test_endpoint_add_with_api_base_and_headers(tmp_config: Path) -> None:
    r = runner.invoke(
        app,
        [
            "provider",
            "endpoint",
            "add",
            "openrouter",
            "--label",
            "eu",
            "--api-key",
            "k1",
            "--api-base",
            "https://eu.example.com",
            "--extra-headers",
            '{"X-Region": "eu"}',
        ],
    )
    assert r.exit_code == 0, r.output

    section = json.loads(tmp_config.read_text(encoding="utf-8"))["providers"]["openrouter"]
    assert section["endpoints"][0]["apiBase"] == "https://eu.example.com"
    assert section["endpoints"][0]["extraHeaders"] == {"X-Region": "eu"}


def test_endpoint_add_rejects_malformed_headers_json(tmp_config: Path) -> None:
    r = runner.invoke(
        app,
        ["provider", "endpoint", "add", "openrouter", "--label", "x", "--api-key", "k", "--extra-headers", "{not-json"],
    )
    assert r.exit_code != 0
    assert "JSON" in r.output


def test_endpoint_add_same_label_replaces(tmp_config: Path) -> None:
    runner.invoke(app, ["provider", "endpoint", "add", "openrouter", "--label", "primary", "--api-key", "k1"])
    r = runner.invoke(app, ["provider", "endpoint", "add", "openrouter", "--label", "primary", "--api-key", "k2"])
    assert r.exit_code == 0, r.output

    section = json.loads(tmp_config.read_text(encoding="utf-8"))["providers"]["openrouter"]
    assert len(section["endpoints"]) == 1
    assert section["endpoints"][0]["apiKey"] == "k2"


def test_endpoint_list_renders_a_validation_error_not_a_traceback(tmp_config: Path) -> None:
    """A hand-edited invalid section (duplicate label) must come back as the
    same rendered failure `provider set` settled on, not a bare traceback."""
    runner.invoke(app, ["provider", "endpoint", "add", "openrouter", "--label", "a", "--api-key", "k1"])
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    data["providers"]["openrouter"]["endpoints"].append({"label": "a", "apiKey": "dup"})
    tmp_config.write_text(json.dumps(data), encoding="utf-8")

    r = runner.invoke(app, ["provider", "endpoint", "list", "openrouter"])

    assert r.exit_code == 1
    assert "Validation failed" in r.output


def test_endpoint_add_on_an_oauth_provider_renders_the_refusal(tmp_config: Path) -> None:
    """The write path shares the factory's refusal; the CLI must render it as
    the same clean failure a bad provider name gets, not a bare traceback."""
    r = runner.invoke(
        app,
        ["provider", "endpoint", "add", "github_copilot", "--label", "x", "--api-key", "k"],
    )
    assert r.exit_code == 1
    assert "does not support multiple endpoints" in r.output


def test_endpoint_add_unknown_provider_exits_1(tmp_config: Path) -> None:
    r = runner.invoke(
        app,
        ["provider", "endpoint", "add", "no-such-provider", "--label", "x", "--api-key", "k"],
    )
    assert r.exit_code == 1
    assert "Unknown provider" in r.output


def test_endpoint_add_without_a_key_is_refused_for_a_key_based_provider(tmp_config: Path) -> None:
    """``--api-key`` is no longer required at the flag level -- the shape-aware
    refusal lives in the ops layer, shared with the RPC picker, so this must
    still exit non-zero with a readable reason rather than silently persist a
    keyless endpoint into the rotation."""
    r = runner.invoke(
        app,
        ["provider", "endpoint", "add", "openrouter", "--label", "x", "--api-base", "https://a.example/v1"],
    )
    assert r.exit_code == 1
    assert "api_key" in r.output


def test_endpoint_add_without_a_key_is_allowed_for_a_local_deployment(tmp_config: Path) -> None:
    r = runner.invoke(
        app,
        ["provider", "endpoint", "add", "hosted_vllm", "--label", "x", "--api-base", "http://10.0.0.5:8000/v1"],
    )
    assert r.exit_code == 0, r.output

    section = json.loads(tmp_config.read_text(encoding="utf-8"))["providers"]["hosted_vllm"]
    assert section["endpoints"][0]["apiKey"] == ""


def test_endpoint_remove_drops_the_label(tmp_config: Path) -> None:
    runner.invoke(app, ["provider", "endpoint", "add", "openrouter", "--label", "primary", "--api-key", "k1"])
    runner.invoke(app, ["provider", "endpoint", "add", "openrouter", "--label", "backup", "--api-key", "k2"])

    r = runner.invoke(app, ["provider", "endpoint", "remove", "openrouter", "--label", "primary"])
    assert r.exit_code == 0, r.output

    section = json.loads(tmp_config.read_text(encoding="utf-8"))["providers"]["openrouter"]
    assert [e["label"] for e in section["endpoints"]] == ["backup"]


def test_endpoint_remove_absent_label_is_noop(tmp_config: Path) -> None:
    runner.invoke(app, ["provider", "endpoint", "add", "openrouter", "--label", "primary", "--api-key", "k1"])

    r = runner.invoke(app, ["provider", "endpoint", "remove", "openrouter", "--label", "not-there"])
    assert r.exit_code == 0, r.output

    section = json.loads(tmp_config.read_text(encoding="utf-8"))["providers"]["openrouter"]
    assert [e["label"] for e in section["endpoints"]] == ["primary"]


def test_endpoint_remove_unknown_provider_exits_1(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "endpoint", "remove", "no-such-provider", "--label", "x"])
    assert r.exit_code == 1
    assert "Unknown provider" in r.output


def test_endpoint_list_redacts_api_key(tmp_config: Path) -> None:
    runner.invoke(app, ["provider", "endpoint", "add", "openrouter", "--label", "primary", "--api-key", "k1"])

    r = runner.invoke(app, ["provider", "endpoint", "list", "openrouter"])
    assert r.exit_code == 0, r.output
    assert "primary" in r.stdout
    assert "****set****" in r.stdout
    assert "k1" not in r.stdout


def test_endpoint_list_empty_when_none_configured(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "endpoint", "list", "openrouter"])
    assert r.exit_code == 0, r.output


def test_endpoint_list_unknown_provider_exits_1(tmp_config: Path) -> None:
    r = runner.invoke(app, ["provider", "endpoint", "list", "no-such-provider"])
    assert r.exit_code == 1
    assert "Unknown provider" in r.output
