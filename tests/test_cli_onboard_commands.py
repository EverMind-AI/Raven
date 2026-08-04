"""CLI tests for ``raven onboard`` — the five-step wizard.

Most tests exercise ``--non-interactive`` so we can drive the wizard
deterministically without a real TTY. Interactive paths are covered by
stubbing the per-step helper functions directly (``_select_provider``,
``_prompt_api_key``, etc.) — that's cheaper and more readable than
patching :mod:`questionary` internals.

Network is mocked at the ops-library boundary
(``raven.config.update_providers.test_provider``) and at the step-3
chat boundary (``raven.cli.onboard_commands.send_probe``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from raven.cli import onboard_commands
from raven.cli.commands import app
from raven.config.loader import set_config_path

runner = CliRunner()


# --------------------------------------------------------------------------- async stub helpers
# ``_scancode_login`` drives ``asyncio.run(adapter.login(...))``. Tests stay
# synchronous (no running loop) and replace ``login`` with an async function
# returning a canned value, so ``asyncio.run`` is the only loop in play.


def _async_return(value: Any):
    """Build an async method stub that always returns ``value``."""

    async def _login(self, *args, **kwargs):  # noqa: ANN001
        return value

    return _login


def _async_iter(values):
    """Build an async method stub that returns successive ``values`` per call."""

    async def _login(self, *args, **kwargs):  # noqa: ANN001
        return next(values)

    return _login


def _must_not_call(name: str):
    """Build a stub that fails the test if invoked (guards 'never reached').

    Raises ``BaseException`` so a stray call inside a ``try/except Exception``
    (e.g. ``_scancode_login``'s login guard) still surfaces instead of being
    swallowed.
    """

    def _boom(*args, **kwargs):
        raise BaseException(f"{name} should not have been called")  # noqa: TRY002

    return _boom


@pytest.fixture(autouse=True)
def _restore_event_loop():
    """Keep ``asyncio.run`` side effects from leaking across tests.

    ``_scancode_login`` calls ``asyncio.run()``, which closes the loop and
    unsets the thread's current loop. Tests elsewhere that still use the legacy
    ``asyncio.get_event_loop()`` pattern then fail with "no current event loop".
    Hand each test a fresh loop and install another afterward.
    """
    asyncio.set_event_loop(asyncio.new_event_loop())
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect config_path + workspace_path under tmp_path; stub template sync.

    ``_bootstrap_empty_config`` uses lazy imports, so we patch the *source*
    modules (``raven.config.paths`` / ``raven.utils.helpers``) rather
    than the consumer.
    """
    cfg = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    set_config_path(cfg)
    monkeypatch.setattr(
        "raven.config.paths.get_workspace_path",
        lambda: workspace,
    )
    monkeypatch.setattr(
        "raven.utils.helpers.sync_workspace_templates",
        lambda _: None,
    )
    yield cfg
    set_config_path(None)  # type: ignore[arg-type]


@pytest.fixture
def stub_verify(monkeypatch: pytest.MonkeyPatch):
    """Default: provider verification succeeds with an empty catalog.

    An empty ``model_ids`` makes ``_pick_model`` fall back to
    ``spec.default_model``, which the non-interactive happy-path tests rely
    on. Tests that need a populated catalog should patch ``test_provider``
    directly with a richer payload.
    """

    def _ok(name: str, *args, **kwargs) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "valid",
            "models_count": 0,
            "model_ids": [],
            "elapsed_ms": 12,
        }

    monkeypatch.setattr("raven.config.update_providers.test_provider", _ok)
    return _ok


@pytest.fixture
def stub_step3(monkeypatch: pytest.MonkeyPatch):
    """Default: step 3 chat succeeds. Tests can override."""

    monkeypatch.setattr(
        onboard_commands,
        "send_probe",
        lambda: ("hi there", 24, 0.5),
    )


# --------------------------------------------------------------------------- help


def test_onboard_help_lists_all_flags() -> None:
    """``raven onboard --help`` exposes the full flag surface."""
    r = runner.invoke(app, ["onboard", "--help"])
    assert r.exit_code == 0, r.stdout
    out = r.stdout
    for flag in (
        "--provider",
        "--api-key",
        "--base-url",
        "--model",
        "--channel",
        "--skip-sandbox",
        "--skip-channel",
        "--skip-memory",
        "--skip-deep-research",
        "--skip-import",
        "--non-interactive",
        "--yes",
        "--reset",
    ):
        assert flag in out, f"missing flag in help: {flag}"


# --------------------------------------------------------------------------- curated provider list


def test_curated_providers_all_exist_in_registry() -> None:
    from raven.providers.registry import find_by_name

    for entry in onboard_commands._CURATED_PROVIDERS:
        assert find_by_name(entry["name"]) is not None, f"unknown provider: {entry['name']}"


def test_curated_providers_cover_the_seeded_picker_providers() -> None:
    """Every provider seeded in the model picker must be pickable in the wizard.

    The two lists drifted apart once already: zhipu / dashscope / groq carried a
    curated shortlist and a default_model, yet the wizard offered no way to
    choose them short of --provider or "Other".
    """
    from tests.test_provider_catalog import _SEEDED_DIRECT_PROVIDERS

    curated = {entry["name"] for entry in onboard_commands._CURATED_PROVIDERS}
    assert set(_SEEDED_DIRECT_PROVIDERS) <= curated


def test_curated_providers_do_not_restate_registry_flags() -> None:
    # is_oauth lives on the ProviderSpec; a copy here would be a second source
    # of truth that silently goes stale.
    for entry in onboard_commands._CURATED_PROVIDERS:
        assert "is_oauth" not in entry


# --------------------------------------------------------------------------- non-interactive happy path


def test_onboard_non_interactive_minimum_flags(tmp_env: Path, stub_verify, stub_step3) -> None:
    """Minimum non-interactive invocation runs all three steps and writes config."""
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-fake-test-key",
            "--skip-channel",
            "--yes",
        ],
    )
    assert r.exit_code == 0, r.stdout
    assert "Welcome to the Raven setup wizard" in r.stdout
    assert "Connected" in r.stdout
    assert "Setup complete" in r.stdout

    data = json.loads(tmp_env.read_text())
    assert data["providers"]["openai"]["apiKey"] == "sk-fake-test-key"
    assert data["agents"]["defaults"]["model"] == "openai/gpt-5.5"


def test_onboard_non_interactive_skips_optional_steps(
    tmp_env: Path, everos_isolated: Path, stub_verify, stub_step3
) -> None:
    """Non-interactive mode auto-skips sandbox / channel / memory steps.

    ``everos_isolated`` keeps ``_memory_enabled`` from reading the dev
    machine's real ``~/.everos/everos.toml``: the seeded backend="everos" is
    only kept when an llm model is configured, so an empty (isolated) EverOS
    config makes the skip-guard deterministically resolve it back to None.
    """
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-fake",
            "--yes",
        ],
    )
    assert r.exit_code == 0, r.stdout
    assert "Keeping run location: host" in r.stdout
    assert "Keeping native Markdown memory" in r.stdout
    assert "Setup complete" in r.stdout
    # Memory left unconfigured (no llm model) → backend resolves to None.
    data = json.loads(tmp_env.read_text())
    assert data.get("memory", {}).get("backend") != "everos"


def test_onboard_skip_channel_default(tmp_env: Path, stub_verify, stub_step3) -> None:
    """``--skip-channel`` produces the dim skip line in Step 3."""
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-fake",
            "--skip-channel",
            "--yes",
        ],
    )
    assert r.exit_code == 0
    assert "Skipped via --skip-channel" in r.stdout


def test_onboard_skip_import_default(tmp_env: Path, stub_verify, stub_step3) -> None:
    """``--skip-import`` produces the dim skip line in Step 5."""
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-fake",
            "--skip-channel",
            "--skip-import",
            "--yes",
        ],
    )
    assert r.exit_code == 0
    assert "Skipped via --skip-import" in r.stdout


def test_onboard_non_interactive_skips_import_step(tmp_env: Path, stub_verify, stub_step3) -> None:
    """Non-interactive mode auto-skips Step 5 even without ``--skip-import``."""
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-fake",
            "--skip-channel",
            "--yes",
        ],
    )
    assert r.exit_code == 0, r.stdout
    assert "Skipped (non-interactive)" in r.stdout
    assert "Setup complete" in r.stdout


# --------------------------------------------------------------------------- error paths


def test_onboard_non_interactive_missing_provider_fails(tmp_env: Path) -> None:
    """Without ``--provider`` non-interactive mode can't proceed."""
    r = runner.invoke(
        app,
        ["onboard", "--non-interactive", "--skip-channel", "--yes"],
    )
    assert r.exit_code != 0
    assert "--provider is required" in r.stdout


def test_onboard_non_interactive_custom_requires_base_url(
    tmp_env: Path,
) -> None:
    """``custom`` provider needs ``--base-url`` when non-interactive."""
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "custom",
            "--api-key",
            "sk-fake",
            "--skip-channel",
            "--yes",
        ],
    )
    assert r.exit_code != 0
    assert "--base-url is required" in r.stdout


def test_onboard_oauth_non_interactive_errors(tmp_env: Path) -> None:
    """OAuth providers can't run headless — wizard must surface that."""
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "github_copilot",
            "--skip-channel",
            "--yes",
        ],
    )
    assert r.exit_code != 0
    assert "OAuth providers require an interactive browser flow" in r.stdout


def test_onboard_non_tty_no_flag_fails(tmp_env: Path) -> None:
    """Without a TTY and without ``--non-interactive`` we give a clear hint.

    ``CliRunner`` captures stdout into a buffer, so ``isatty()`` already
    returns False here — no extra patching needed to trigger the bail.
    """
    r = runner.invoke(app, ["onboard"])
    assert r.exit_code == 2
    assert "Non-interactive terminal detected" in r.stdout


# --------------------------------------------------------------------------- existing-config handling


def test_onboard_existing_config_blocks_without_yes(tmp_env: Path, stub_verify, stub_step3) -> None:
    """Re-running over an existing populated config fails closed."""
    # Seed a populated config.
    runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-existing",
            "--skip-channel",
            "--yes",
        ],
    )

    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "anthropic",
            "--api-key",
            "sk-newer",
            "--skip-channel",
        ],
    )
    assert r.exit_code == 2
    assert "Existing config detected" in r.stdout
    # The original key must NOT have been overwritten.
    data = json.loads(tmp_env.read_text())
    assert data["providers"]["openai"]["apiKey"] == "sk-existing"


def test_onboard_reset_flag_forces_redo(tmp_env: Path, stub_verify, stub_step3) -> None:
    """``--reset`` bypasses the existing-config guard."""
    runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-old",
            "--skip-channel",
            "--yes",
        ],
    )
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-new",
            "--skip-channel",
            "--reset",
        ],
    )
    assert r.exit_code == 0, r.stdout
    data = json.loads(tmp_env.read_text())
    assert data["providers"]["openai"]["apiKey"] == "sk-new"


# --------------------------------------------------------------------------- verification / step3 failure paths


def test_onboard_provider_test_failure_warns_but_continues(
    tmp_env: Path, monkeypatch: pytest.MonkeyPatch, stub_step3
) -> None:
    """``test_provider`` failure should warn + continue in non-interactive mode."""

    def _fail(name: str, *args, **kwargs) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "invalid_key",
            "models_count": None,
            "elapsed_ms": 5,
            "error": "401 Unauthorized",
        }

    monkeypatch.setattr("raven.config.update_providers.test_provider", _fail)

    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-bad",
            "--skip-channel",
            "--yes",
        ],
    )
    assert r.exit_code == 0  # non-interactive falls through with warning
    assert "Auth failed" in r.stdout
    # The unmet connectivity check is summarized in the footer warning.
    assert "didn't pass a connectivity test" in r.stdout


def test_onboard_test_probe_failure_shows_warning_footer(
    tmp_env: Path, monkeypatch: pytest.MonkeyPatch, stub_verify
) -> None:
    """When the Step 1 test message raises, the footer must reflect the failure."""

    def _boom() -> tuple[str, int | None, float]:
        raise RuntimeError("AuthenticationError: bogus key")

    monkeypatch.setattr(onboard_commands, "send_probe", _boom)

    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-fake",
            "--skip-channel",
            "--yes",
        ],
    )
    assert r.exit_code == 0
    assert "Test failed" in r.stdout
    assert "Setup finished" in r.stdout
    assert "Setup complete" not in r.stdout
    assert "didn't pass a connectivity test" in r.stdout


# --------------------------------------------------------------------------- interactive (stubbed)


def test_onboard_interactive_uses_stubbed_pickers(
    tmp_env: Path, monkeypatch: pytest.MonkeyPatch, stub_verify, stub_step3
) -> None:
    """Interactive path: stub the per-step helpers and assert ops-lib is hit."""
    # CliRunner makes sys.stdout non-tty, so _check_tty_or_die would bail
    # before our stubs ever run. Skip it for this test.
    monkeypatch.setattr(onboard_commands, "_check_tty_or_die", lambda non_interactive: None)
    monkeypatch.setattr(onboard_commands, "_pick_language", lambda: None)
    monkeypatch.setattr(onboard_commands, "_select_provider", lambda: "anthropic")
    monkeypatch.setattr(onboard_commands, "_prompt_api_key", lambda provider, **kw: "sk-int-test")
    # Bypass the autocomplete picker — Step 1 catalog UI is exercised
    # separately by ``test_step1_picker_uses_catalog_when_available``.
    monkeypatch.setattr(
        onboard_commands,
        "_pick_model",
        lambda provider, spec, **_: spec.default_model,
    )
    # Optional steps 2-4 are covered separately; no-op them here so the
    # interactive Step 1 path can be asserted without driving every screen.
    monkeypatch.setattr(onboard_commands, "_step2_sandbox", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step3_channel", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step4_memory", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_deep_research", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_import", lambda **_: None)

    r = runner.invoke(app, ["onboard"])
    assert r.exit_code == 0, r.stdout

    data = json.loads(tmp_env.read_text())
    assert data["providers"]["anthropic"]["apiKey"] == "sk-int-test"
    assert data["agents"]["defaults"]["model"] == "anthropic/claude-sonnet-5"


# --------------------------------------------------------------------------- unit-level


def test_step1_writes_via_ops_lib(tmp_env: Path, monkeypatch: pytest.MonkeyPatch, stub_verify) -> None:
    """Step 1's write path must go through ``set_provider_fields``."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, fields: dict[str, Any], **_) -> dict[str, Any]:
        calls.append((name, dict(fields)))
        return {}

    monkeypatch.setattr("raven.config.update_providers.set_provider_fields", _spy)
    monkeypatch.setattr(onboard_commands, "send_probe", lambda: ("hi", 1, 0.1))

    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-spy",
            "--skip-channel",
            "--yes",
        ],
    )
    assert r.exit_code == 0, r.stdout
    assert calls, "set_provider_fields was never called"
    name, fields = calls[0]
    assert name == "openai"
    assert fields == {"api_key": "sk-spy"}


def test_styles_module_loads() -> None:
    """``_styles.py`` import must not crash and must export ``RAVEN_STYLE``."""
    from raven.cli._styles import RAVEN_STYLE  # noqa: F401

    assert RAVEN_STYLE is not None


# --------------------------------------------------------------------------- model picker


def test_step1_model_flag_overrides_picker(tmp_env: Path, stub_verify, stub_step3) -> None:
    """``--model X`` short-circuits the picker, even when a catalog exists."""
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openrouter",
            "--api-key",
            "sk-or-fake",
            "--model",
            "openrouter/openai/gpt-4o",
            "--skip-channel",
            "--yes",
        ],
    )
    assert r.exit_code == 0, r.stdout
    data = json.loads(tmp_env.read_text())
    assert data["agents"]["defaults"]["model"] == "openrouter/openai/gpt-4o"


def test_step1_falls_back_to_spec_default_in_non_interactive(tmp_env: Path, stub_verify, stub_step3) -> None:
    """Without --model + non-interactive → write whatever ProviderSpec says."""
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "anthropic",
            "--api-key",
            "sk-ant-fake",
            "--skip-channel",
            "--yes",
        ],
    )
    assert r.exit_code == 0, r.stdout
    data = json.loads(tmp_env.read_text())
    assert data["agents"]["defaults"]["model"] == "anthropic/claude-sonnet-5"


def test_step1_picker_uses_catalog_when_available(tmp_env: Path, monkeypatch: pytest.MonkeyPatch, stub_step3) -> None:
    """When ``/v1/models`` returns a list and we're interactive, the picker
    feeds that list to ``questionary.autocomplete`` and writes the choice."""

    captured_choices: dict[str, list[str]] = {}

    def _ok_with_catalog(name: str, *args, **kwargs) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "valid",
            "models_count": 3,
            "model_ids": ["claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5"],
            "elapsed_ms": 9,
        }

    monkeypatch.setattr("raven.config.update_providers.test_provider", _ok_with_catalog)
    monkeypatch.setattr(onboard_commands, "_check_tty_or_die", lambda non_interactive: None)
    monkeypatch.setattr(onboard_commands, "_pick_language", lambda: None)
    monkeypatch.setattr(onboard_commands, "_select_provider", lambda: "anthropic")
    monkeypatch.setattr(onboard_commands, "_prompt_api_key", lambda provider, **kw: "sk-ant-test")

    import questionary

    class _FakeQuestion:
        def __init__(self, answer: Any) -> None:
            self._answer = answer

        def ask(self) -> Any:
            return self._answer

    def _fake_autocomplete(message, choices, default=None, **kwargs):
        captured_choices["choices"] = list(choices)
        captured_choices["default"] = default
        return _FakeQuestion("claude-haiku-4-5")

    monkeypatch.setattr(questionary, "autocomplete", _fake_autocomplete)
    monkeypatch.setattr(onboard_commands, "_step2_sandbox", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step3_channel", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step4_memory", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_deep_research", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_import", lambda **_: None)

    r = runner.invoke(app, ["onboard"])
    assert r.exit_code == 0, r.stdout

    # Catalog feeds the picker, every id carrying the provider's route prefix.
    # The schema's pre-existing default (``anthropic/claude-opus-4-5``) is one of
    # them rather than a fourth entry: while bare and prefixed spellings coexisted
    # the same model appeared twice, once in each form.
    assert captured_choices["choices"] == [
        "anthropic/claude-haiku-4-5",
        "anthropic/claude-sonnet-4-5",
        "anthropic/claude-opus-4-5",
    ]
    assert captured_choices["default"] == "anthropic/claude-opus-4-5"
    # The pick made it into config, carrying the route prefix. The user may type
    # a bare id -- autocomplete accepts free text -- and a bare id is routed by
    # keyword and fallback rather than to the provider just configured, so the
    # wizard adds the prefix on the way out instead of persisting what was typed.
    data = json.loads(tmp_env.read_text())
    assert data["agents"]["defaults"]["model"] == "anthropic/claude-haiku-4-5"


def _capture_password_validate(monkeypatch: pytest.MonkeyPatch, answer: str) -> dict[str, Any]:
    """Stub ``questionary.password`` to record the ``validate`` callable the
    prompt installs and return ``answer`` from ``.ask()``."""
    import questionary

    captured: dict[str, Any] = {}

    class _FakeQuestion:
        def ask(self) -> Any:
            return answer

    def _fake_password(message: Any, *, validate: Any = None, **kwargs: Any) -> Any:
        captured["validate"] = validate
        return _FakeQuestion()

    monkeypatch.setattr(questionary, "password", _fake_password)
    return captured


def test_prompt_api_key_validator_rejects_whitespace_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """An all-whitespace (or empty) key must fail the field validator (which
    re-prompts) rather than pass the raw length check, strip to empty, and hit
    ``typer.Exit`` (which quit raven with no message)."""
    captured = _capture_password_validate(monkeypatch, "sk-realkey123")

    key = onboard_commands._prompt_api_key("deep_research")
    assert key == "sk-realkey123"

    validate = captured["validate"]
    assert validate("        ") is not True
    assert validate("") is not True
    assert validate("sk-12345678") is True


def test_prompt_api_key_empty_is_back_but_whitespace_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``allow_back``, only a truly-empty submit is the back/cancel signal;
    a whitespace-only entry is still rejected as an invalid key (re-prompt), not
    silently treated as back."""
    captured = _capture_password_validate(monkeypatch, "")

    result = onboard_commands._prompt_api_key("deep_research", allow_back=True)
    assert result is onboard_commands._BACK

    validate = captured["validate"]
    assert validate("") is True  # truly-empty submit rewinds/cancels
    assert validate("        ") is not True  # whitespace rejected, not back
    assert validate("sk-12345678") is True


def test_format_model_for_provider_prefix_rules() -> None:
    """The provider's model prefix is applied unless the id already carries one."""
    from raven.providers.registry import find_by_name

    openrouter = find_by_name("openrouter")
    deepseek = find_by_name("deepseek")
    openai = find_by_name("openai")

    # Gateway with prefix: bare id gets prefixed
    assert (
        onboard_commands._format_model_for_provider("openrouter", openrouter, "anthropic/claude-sonnet-4-5")
        == "openrouter/anthropic/claude-sonnet-4-5"
    )
    # Already prefixed by us → idempotent
    assert (
        onboard_commands._format_model_for_provider("openrouter", openrouter, "openrouter/anthropic/claude-sonnet-4-5")
        == "openrouter/anthropic/claude-sonnet-4-5"
    )
    # Standard provider: LiteLLM knows it under our own name, so that is the prefix
    assert onboard_commands._format_model_for_provider("openai", openai, "gpt-4o-mini") == "openai/gpt-4o-mini"
    assert onboard_commands._format_model_for_provider("openai", openai, "openai/gpt-4o-mini") == "openai/gpt-4o-mini"
    # skip_prefixes match → no double-prefix
    assert (
        onboard_commands._format_model_for_provider("deepseek", deepseek, "deepseek/deepseek-chat")
        == "deepseek/deepseek-chat"
    )
    assert (
        onboard_commands._format_model_for_provider("deepseek", deepseek, "deepseek-chat") == "deepseek/deepseek-chat"
    )


def test_model_routes_to_provider_heuristic() -> None:
    """Mirror of ``Config._match_provider``: prefix match wins, else keyword."""
    from raven.providers.registry import find_by_name

    openrouter = find_by_name("openrouter")
    anthropic = find_by_name("anthropic")
    openai = find_by_name("openai")

    # Prefix match (most explicit)
    assert onboard_commands._model_routes_to_provider("openrouter/anthropic/claude-sonnet-4-5", openrouter)
    # Wrong prefix → no match for anthropic (even though "claude" is in the string)
    assert not onboard_commands._model_routes_to_provider("openrouter/anthropic/claude-sonnet-4-5", anthropic)
    # Bare model: keyword match
    assert onboard_commands._model_routes_to_provider("claude-sonnet-4-5", anthropic)
    assert onboard_commands._model_routes_to_provider("gpt-4o-mini", openai)
    # No match
    assert not onboard_commands._model_routes_to_provider("gemini-2.5-flash", openai)
    # Empty / None inputs
    assert not onboard_commands._model_routes_to_provider("", anthropic)
    assert not onboard_commands._model_routes_to_provider("claude", None)


def test_registry_default_models_present() -> None:
    """Each curated provider must carry a ``default_model`` in its ``ProviderSpec``."""
    from raven.providers.registry import find_by_name

    for name in (
        "openrouter",
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "github_copilot",
        "openai_codex",
        "minimax_global",
        "minimax_cn",
    ):
        spec = find_by_name(name)
        assert spec is not None, f"missing provider in registry: {name}"
        assert spec.default_model, f"{name} has empty default_model"


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("minimax_global", "MiniMax-M3", "minimax-global/MiniMax-M3"),
        ("minimax_cn", "MiniMax-M3", "minimax-cn/MiniMax-M3"),
    ],
)
def test_minimax_catalog_models_keep_public_provider_prefix(provider: str, model: str, expected: str) -> None:
    from raven.providers.registry import find_by_name

    assert onboard_commands._format_model_for_provider(provider, find_by_name(provider), model) == expected


# --------------------------------------------------------------------------- fixtures (5-step)


@pytest.fixture
def everos_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect EverOS writes to a throwaway toml (never touches ~/.everos)."""
    import raven.config.update_everos as ue

    cfg = tmp_path / ".everos" / "everos.toml"
    monkeypatch.setattr(ue, "_EVEROS_CONFIG", cfg)
    return cfg


def _seed_provider(provider: str = "openai", key: str = "sk-seed", model: str = "openai/gpt-4o-mini") -> None:
    """Write a minimal populated config via the ops layer."""
    from raven.config.update import set_default_model
    from raven.config.update_providers import set_provider_fields

    set_provider_fields(provider, {"api_key": key})
    set_default_model(model)


# --------------------------------------------------------------------------- gate


def test_is_config_populated_requires_provider_and_model(tmp_env: Path) -> None:
    """Gate criterion: provider key + default model are BOTH required."""
    from raven.config.update import set_default_model
    from raven.config.update_providers import set_provider_fields

    assert onboard_commands._is_config_populated() is False
    set_provider_fields("openai", {"api_key": "sk-x"})
    # key alone is not enough (default model still the schema default? no — fresh file has none)
    data = json.loads(tmp_env.read_text()) if tmp_env.exists() else {}
    if not data.get("agents", {}).get("defaults", {}).get("model"):
        assert onboard_commands._is_config_populated() is False
    set_default_model("openai/gpt-4o-mini")
    assert onboard_commands._is_config_populated() is True


def test_is_config_populated_accepts_minimax_oauth_token(
    tmp_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from raven.config.update import set_default_model
    from raven.providers.minimax_oauth import MiniMaxOAuthToken, save_token

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
    set_default_model("minimax-global/MiniMax-M3")

    assert "minimax_global" in onboard_commands._configured_providers()
    assert onboard_commands._is_config_populated() is True


def test_ensure_configured_short_circuits_when_complete(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate returns True (no wizard) when config is already complete."""
    _seed_provider()
    ran: list[bool] = []
    monkeypatch.setattr(onboard_commands, "run_wizard", lambda **_: ran.append(True))
    assert onboard_commands.ensure_configured_or_onboard() is True
    assert ran == []  # wizard never invoked


def test_ensure_configured_runs_wizard_when_missing(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate runs the wizard when the required config is missing."""
    ran: list[bool] = []
    monkeypatch.setattr(onboard_commands, "run_wizard", lambda **_: ran.append(True))
    assert onboard_commands.ensure_configured_or_onboard() is False
    assert ran == [True]


# --------------------------------------------------------------------------- entry-point gate wiring


def test_agent_gate_triggers_when_missing(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`raven agent` (interactive, TTY, missing config) enters the wizard."""
    from raven.cli import agent_commands

    monkeypatch.setattr(agent_commands, "_stdout_isatty", lambda: True)
    gate_called: list[bool] = []

    def _gate(**_):
        gate_called.append(True)
        raise typer.Exit(0)  # stop before the heavy loop builds

    monkeypatch.setattr(onboard_commands, "ensure_configured_or_onboard", _gate)
    # Config is empty (tmp_env fresh) → _is_config_populated() is False.
    r = runner.invoke(app, ["agent"])
    assert gate_called == [True]
    assert r.exit_code == 0


def test_agent_gate_skips_when_populated(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`raven agent` with complete config does NOT enter the wizard."""
    from raven.cli import agent_commands

    _seed_provider()
    monkeypatch.setattr(agent_commands, "_stdout_isatty", lambda: True)
    gate_called: list[bool] = []
    monkeypatch.setattr(
        onboard_commands,
        "ensure_configured_or_onboard",
        lambda **_: gate_called.append(True),
    )

    # Stub the heavy loop so the command returns quickly after the gate check.
    def _boom(*a, **kw):
        raise typer.Exit(0)

    monkeypatch.setattr("raven.cli._helpers.load_runtime_config", _boom)
    runner.invoke(app, ["agent"])
    # Populated → _is_config_populated() True → gate body never runs.
    assert gate_called == []


def test_agent_gate_skips_oneshot_message(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`raven agent -m '...'` (one-shot) must NOT enter the wizard even on a
    TTY with missing config — scripted use fails loudly later instead."""
    from raven.cli import agent_commands

    monkeypatch.setattr(agent_commands, "_stdout_isatty", lambda: True)
    gate_called: list[bool] = []
    monkeypatch.setattr(
        onboard_commands,
        "ensure_configured_or_onboard",
        lambda **_: gate_called.append(True),
    )
    monkeypatch.setattr(
        "raven.cli._helpers.load_runtime_config",
        lambda *a, **kw: (_ for _ in ()).throw(typer.Exit(0)),
    )
    runner.invoke(app, ["agent", "-m", "hi"])
    assert gate_called == []


def test_agent_gate_skips_non_tty(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-TTY (piped) `raven agent` must not enter the wizard (would block)."""
    from raven.cli import agent_commands

    monkeypatch.setattr(agent_commands, "_stdout_isatty", lambda: False)
    gate_called: list[bool] = []
    monkeypatch.setattr(
        onboard_commands,
        "ensure_configured_or_onboard",
        lambda **_: gate_called.append(True),
    )
    monkeypatch.setattr(
        "raven.cli._helpers.load_runtime_config",
        lambda *a, **kw: (_ for _ in ()).throw(typer.Exit(0)),
    )
    runner.invoke(app, ["agent"])
    assert gate_called == []


def test_tui_gate_triggers_when_missing(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`raven tui` (TTY, missing config) enters the wizard before launching Node."""
    from raven.cli import tui_commands

    monkeypatch.setattr(tui_commands, "_stdout_isatty", lambda: True)
    gate_called: list[bool] = []

    def _gate(**_):
        gate_called.append(True)
        raise typer.Exit(0)  # stop before find_node / spawn

    monkeypatch.setattr(onboard_commands, "ensure_configured_or_onboard", _gate)
    r = runner.invoke(app, ["tui"])
    assert gate_called == [True]
    assert r.exit_code == 0


def test_tui_gate_skips_check_flag(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`raven tui --check` (no-TTY diagnostic) bypasses the wizard gate."""
    from raven.cli import tui_commands

    monkeypatch.setattr(tui_commands, "_stdout_isatty", lambda: True)
    gate_called: list[bool] = []
    monkeypatch.setattr(
        onboard_commands,
        "ensure_configured_or_onboard",
        lambda **_: gate_called.append(True),
    )
    # Stub find_node so --check exits fast without a real Node child.
    monkeypatch.setattr(tui_commands, "find_node", lambda: (None, None))
    runner.invoke(app, ["tui", "--check"])
    assert gate_called == []


# --------------------------------------------------------------------------- sandbox step


def test_sandbox_backend_persisted_via_ops(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Picking 'host' writes sandbox.backend=none through the ops layer."""
    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ("none"))
    onboard_commands._step2_sandbox(skip=False, non_interactive=False)
    data = json.loads(tmp_env.read_text())
    assert data["tools"]["sandbox"]["backend"] == "none"


def test_sandbox_boxlite_probe_failure_falls_back(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Boxlite probe failure → submenu → fall back to host."""
    import questionary

    answers = iter(["boxlite"])

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(answers)))
    monkeypatch.setattr(onboard_commands, "_probe_boxlite", lambda: (False, "missing"))
    # Failure submenu picks "fall back to host".
    monkeypatch.setattr(onboard_commands, "_failure_choice", lambda options, *, non_interactive: "host")
    onboard_commands._step2_sandbox(skip=False, non_interactive=False)
    data = json.loads(tmp_env.read_text())
    assert data["tools"]["sandbox"]["backend"] == "none"


def test_sandbox_keep_current_first_option(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-configured sandbox offers a 'keep current' first choice."""
    from raven.config.update import set_sandbox_backend

    set_sandbox_backend("boxlite")
    captured: dict[str, list] = {}
    import questionary

    class _FQ:
        def ask(self):
            return "keep"

    def _select(message, choices, **kw):
        captured["choices"] = [getattr(c, "value", c) for c in choices]
        return _FQ()

    monkeypatch.setattr(questionary, "select", _select)
    onboard_commands._step2_sandbox(skip=False, non_interactive=False)
    assert "keep" in captured["choices"]
    # 'keep' leaves the backend untouched.
    assert json.loads(tmp_env.read_text())["tools"]["sandbox"]["backend"] == "boxlite"


# --------------------------------------------------------------------------- memory step


def test_memory_disable_sets_backend_null(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Choosing 'don't enable' sets memory.backend=null and writes no EverOS toml."""
    import questionary

    class _FQ:
        def ask(self):
            return "off"

    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ())
    onboard_commands._step4_memory(skip=False, non_interactive=False, main_model="openai/gpt-4o-mini", warnings=[])
    data = json.loads(tmp_env.read_text())
    assert data["memory"]["backend"] is None
    assert not everos_isolated.exists()
    # Effective config (schema default is "everos") must resolve to disabled.
    from raven.config.raven import load_raven_config

    assert load_raven_config().memory.backend is None


def test_memory_enable_writes_everos_sections(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabling memory + LLM (custom source) + embedding (custom, same endpoint)
    writes the EverOS toml; rerank/multimodal skipped."""
    import tomllib

    import questionary

    _seed_provider("openrouter", "sk-or", "openrouter/anthropic/claude-sonnet-4-5")

    # _step4_memory select() calls, in order:
    #   1. enable memory                -> "on"
    #   2. LLM source picker            -> ("custom",)
    #   3. embedding source picker      -> ("custom",)
    #   4. rerank "Configure it?"       -> "skip"
    #   5. multimodal "Configure it?"   -> "skip"
    select_answers = iter(["on", ("custom",), ("custom",), "skip", "skip"])
    # text(): LLM base_url, LLM model, embed base_url, embed model.
    text_answers = iter(["https://llm/v1", "mem-llm", "https://llm/v1", "mem-embed"])
    # password(): LLM api key, embed api key.
    password_answers = iter(["k-llm", "k-embed"])

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ(next(text_answers)))
    monkeypatch.setattr(questionary, "password", lambda *a, **kw: _FQ(next(password_answers)))
    # No network: model list can't be fetched → free-text entry; probe succeeds.
    monkeypatch.setattr(onboard_commands, "_fetch_everos_models", lambda *a, **kw: None)
    monkeypatch.setattr(onboard_commands, "_probe_everos_chat", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(onboard_commands, "_verify_embedding_dim", lambda **kw: True)

    import raven.plugin.memory.everos._server as everos_server

    async def _fake_ensure_everos_server(*a: object, **kw: object) -> None:
        return None

    monkeypatch.setattr(everos_server, "ensure_everos_server", _fake_ensure_everos_server)

    onboard_commands._step4_memory(
        skip=False,
        non_interactive=False,
        main_model="openrouter/anthropic/claude-sonnet-4-5",
        warnings=[],
    )

    data = json.loads(tmp_env.read_text())
    assert data["memory"]["backend"] == "everos"
    # Effective config agrees (not just the raw JSON segment).
    from raven.config.raven import load_raven_config

    assert load_raven_config().memory.backend == "everos"
    with everos_isolated.open("rb") as f:
        everos = tomllib.load(f)
    assert everos["llm"]["model"] == "mem-llm"
    assert everos["llm"]["api_key"] == "k-llm"
    assert everos["llm"]["base_url"] == "https://llm/v1"
    assert everos["embedding"]["model"] == "mem-embed"
    assert everos["embedding"]["api_key"] == "k-embed"
    assert everos["embedding"]["base_url"] == "https://llm/v1"
    assert "rerank" not in everos
    assert "multimodal" not in everos


def test_memory_llm_reuse_pulls_provider_creds(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking the main model's provider auto-reuses its API key."""
    import tomllib

    from raven.config.update_providers import set_provider_fields

    set_provider_fields("openai", {"api_key": "sk-main", "api_base": "https://api.openai.com/v1"})

    import questionary

    class _FakeApp:
        pre_run_callables: list = []
        current_buffer = None

    class _FQ:
        def __init__(self, a):
            self._a = a
            self.application = _FakeApp()

        def ask(self):
            return self._a

    openai_prov = {"name": "openai", "label": "OpenAI", "label_zh": "OpenAI", "base_url": "https://api.openai.com/v1"}
    select_answers = iter([("provider", openai_prov)])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(questionary, "autocomplete", lambda *a, **kw: _FQ("gpt-4.1-mini"))
    monkeypatch.setattr(onboard_commands, "_probe_everos_chat", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(onboard_commands, "_fetch_everos_models", lambda *a, **kw: ["gpt-4.1-mini"])

    onboard_commands._config_everos_role(
        section="llm", main_model="openai/gpt-4o-mini", non_interactive=False, warnings=[]
    )
    with everos_isolated.open("rb") as f:
        everos = tomllib.load(f)
    assert everos["llm"]["model"] == "gpt-4.1-mini"
    assert everos["llm"]["api_key"] == "sk-main"
    assert everos["llm"]["base_url"] == "https://api.openai.com/v1"


def test_memory_rerank_reuse_llm_provider(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rerank picks the LLM's provider by default and reuses its key."""
    import tomllib

    from raven.config.update_everos import set_everos_section

    set_everos_section(
        "llm",
        {
            "model": "m",
            "api_key": "k-llm",
            "base_url": "https://api.deepinfra.com/v1/openai",
        },
    )

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    deepinfra_prov = next(p for p in onboard_commands._EVEROS_PROVIDERS if p["name"] == "deepinfra")
    # No service-type select needed — curated provider auto-resolves it.
    select_answers = iter(["redo", ("provider", deepinfra_prov)])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ("rerank-model"))
    monkeypatch.setattr(onboard_commands, "_fetch_everos_models", lambda *a, **kw: None)
    monkeypatch.setattr(onboard_commands, "_probe_rerank", lambda *a, **kw: (True, "ok"))

    onboard_commands._config_everos_role(
        section="rerank",
        main_model="openrouter/anthropic/claude-sonnet-4-5",
        non_interactive=False,
        warnings=[],
    )
    with everos_isolated.open("rb") as f:
        everos = tomllib.load(f)
    assert everos["rerank"]["provider"] == "deepinfra"
    assert everos["rerank"]["model"] == "rerank-model"
    assert everos["rerank"]["api_key"] == "k-llm"
    assert everos["rerank"]["base_url"] == "https://api.deepinfra.com/v1/inference"


def test_memory_seeded_role_is_not_configured(tmp_env: Path, everos_isolated: Path) -> None:
    """A seeded model with an empty api_key does not count as configured."""
    from raven.config.update_everos import set_everos_section

    assert onboard_commands._everos_role_configured("llm") is False
    set_everos_section("llm", {"model": "openai/gpt-4.1-mini", "api_key": ""})
    assert onboard_commands._everos_role_configured("llm") is False
    set_everos_section("llm", {"api_key": "sk-real"})
    assert onboard_commands._everos_role_configured("llm") is True


def test_memory_required_role_back_reaches_give_up_menu(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backing out of the picker must offer the give-up exit even when the
    shipped everos.toml template already seeded a model with an empty api_key —
    otherwise Back re-asks the provider picker forever."""
    from raven.config.update_everos import set_everos_section

    set_everos_section(
        "llm",
        {"model": "openai/gpt-4.1-mini", "api_key": "", "base_url": "https://openrouter.ai/api/v1"},
    )

    import questionary

    asked: list[str] = []

    class _FQ:
        def __init__(self, *a: object, **kw: object) -> None:
            values = [getattr(c, "value", None) for c in kw.get("choices", [])]  # type: ignore[union-attr]
            self._can_abort = "abort" in values

        def ask(self) -> object:
            asked.append("give-up" if self._can_abort else "picker")
            assert len(asked) <= 4, f"Back re-asked the picker instead of offering an exit: {asked}"
            return "abort" if self._can_abort else onboard_commands._BACK

    monkeypatch.setattr(questionary, "select", _FQ)

    out = onboard_commands._config_everos_role(section="llm", main_model=None, non_interactive=False, warnings=[])
    assert out is onboard_commands._ABORT_EVEROS
    assert asked == ["picker", "give-up"]


def test_model_openai_compatible_heuristic(tmp_env: Path) -> None:
    """Compat heuristic gates whether the memory LLM can reuse the main model."""
    f = onboard_commands._model_is_openai_compatible
    assert f("openai/gpt-4o-mini")
    assert f("openrouter/anthropic/claude-sonnet-4-5")
    assert f("deepseek/deepseek-chat")
    assert not f("anthropic/claude-sonnet-4-5")
    assert not f("gemini/gemini-2.5-flash")
    assert not f(None)
    # A bare id with no configured custom provider isn't recognized.
    assert not f("qwen-max")


def test_custom_model_reuse_is_compatible(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom endpoint's bare model is reusable; the provider picker
    defaults to the matching provider and reuses its key."""
    from raven.config.update_providers import set_provider_fields

    set_provider_fields("custom", {"api_key": "sk-cust", "api_base": "https://my-llm/v1"})
    assert onboard_commands._model_is_openai_compatible("qwen-max")

    creds = onboard_commands._resolve_reuse_llm_creds("qwen-max")
    assert creds["model"] == "qwen-max"
    assert creds["api_key"] == "sk-cust"
    assert creds["base_url"] == "https://my-llm/v1"


# --------------------------------------------------------------------------- scancode channels


def test_channel_uses_interactive_login_real_specs() -> None:
    """Scancode channels (WhatsApp / WeChat) report interactive_login; others don't."""
    f = onboard_commands._channel_uses_interactive_login
    assert f("whatsapp") is True
    assert f("weixin") is True
    assert f("telegram") is False


def test_channel_order_overseas_common_before_domestic() -> None:
    """Curated picker order: US/global-common → China-common → uncommon tail.

    (Reordered from the old domestic-first layout.)
    """
    names = onboard_commands._ordered_channel_names()
    # US/global-common lead the list, ahead of the China-common group.
    for overseas in ("telegram", "discord", "slack", "whatsapp"):
        for domestic in ("weixin", "wecom", "feishu", "dingtalk", "qq"):
            assert names.index(overseas) < names.index(domestic)
    # China-common still come before the less-common tail (matrix / email).
    for domestic in ("weixin", "feishu"):
        for tail in ("matrix", "email"):
            assert names.index(domestic) < names.index(tail)


def test_scancode_login_success_enables_channel(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful scancode login enables the channel and asks no schema fields."""
    # Stub the adapter's async login to succeed.
    monkeypatch.setattr(
        "raven.channels.adapters.weixin.channel.WeixinChannel.login",
        _async_return(True),
    )
    # Guard: the reflected-schema prompt must NOT be used for scancode channels.
    monkeypatch.setattr(onboard_commands, "_prompt_channel_fields", _must_not_call("_prompt_channel_fields"))

    onboard_commands._scancode_login("weixin")
    data = json.loads(tmp_env.read_text())
    assert data["channels"]["weixin"]["enabled"] is True


def test_scancode_login_retry_then_success(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Login fails once → 'retry' submenu choice → second attempt succeeds."""
    results = iter([False, True])
    monkeypatch.setattr(
        "raven.channels.adapters.weixin.channel.WeixinChannel.login",
        _async_iter(results),
    )
    # Failure submenu: choose retry first; second login succeeds so menu isn't
    # reached again.
    monkeypatch.setattr(
        onboard_commands,
        "_failure_choice",
        lambda options, *, non_interactive: "retry",
    )
    onboard_commands._scancode_login("weixin")
    data = json.loads(tmp_env.read_text())
    assert data["channels"]["weixin"]["enabled"] is True


def test_scancode_login_skip_reverts_enable(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """'skip' on a failed scan reverts the enable so the channel isn't shown as
    connected (config section is kept for a later `raven channels login`)."""
    monkeypatch.setattr(
        "raven.channels.adapters.weixin.channel.WeixinChannel.login",
        _async_return(False),
    )
    monkeypatch.setattr(
        onboard_commands,
        "_failure_choice",
        lambda options, *, non_interactive: "skip",
    )
    onboard_commands._scancode_login("weixin")
    data = json.loads(tmp_env.read_text())
    # Not logged in → disabled, so it never falsely shows as connected.
    assert data["channels"]["weixin"]["enabled"] is False


def test_add_one_channel_routes_scancode(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_add_one_channel` sends a scancode channel to login, NOT schema prompts."""
    monkeypatch.setattr(onboard_commands, "_select_provider", lambda: "weixin")
    monkeypatch.setattr(onboard_commands, "_select_channel", lambda: "weixin")
    routed: list[str] = []
    monkeypatch.setattr(onboard_commands, "_scancode_login", lambda c, **kw: routed.append(c))
    monkeypatch.setattr(onboard_commands, "_prompt_channel_fields", _must_not_call("_prompt_channel_fields"))
    onboard_commands._add_one_channel()
    assert routed == ["weixin"]


def test_scancode_login_node_missing_skip(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WhatsApp with no Node/npm shows the install menu (NOT the QR menu); skip
    reverts the enable; the adapter's login is never called."""
    monkeypatch.setattr(onboard_commands, "_node_runtime_missing", lambda c: True)
    # The Node-missing menu is distinct from the QR menu — assert its options
    # (no 're-show QR') and that login is never reached.
    captured: dict[str, list] = {}

    def _fc(options, *, non_interactive):
        captured["labels"] = [label for label, _ in options]
        return "skip"

    monkeypatch.setattr(onboard_commands, "_failure_choice", _fc)
    monkeypatch.setattr(
        "raven.channels.adapters.whatsapp.channel.WhatsAppChannel.login",
        _must_not_call("WhatsAppChannel.login"),
    )
    onboard_commands._scancode_login("whatsapp")
    data = json.loads(tmp_env.read_text())
    # Not logged in → reverted to disabled.
    assert data["channels"]["whatsapp"]["enabled"] is False
    # Install-then-retry menu, not "Re-show QR code".
    assert any("install" in lbl.lower() for lbl in captured["labels"])
    assert not any("qr" in lbl.lower() for lbl in captured["labels"])


def test_scancode_login_node_missing_retry_then_present(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Node-missing → 'retry' re-checks; once npm appears, login runs."""
    missing = iter([True, False])  # first check missing, then present
    monkeypatch.setattr(onboard_commands, "_node_runtime_missing", lambda c: next(missing))
    monkeypatch.setattr(
        onboard_commands,
        "_failure_choice",
        lambda options, *, non_interactive: "retry",
    )
    monkeypatch.setattr(
        "raven.channels.adapters.whatsapp.channel.WhatsAppChannel.login",
        _async_return(True),
    )
    onboard_commands._scancode_login("whatsapp")
    data = json.loads(tmp_env.read_text())
    assert data["channels"]["whatsapp"]["enabled"] is True


# --------------------------------------------------------------------------- multi-provider add/remove


def test_provider_remove_clears_key(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing a provider clears its api_key (disable, not hard-delete)."""
    from raven.config.update_providers import set_provider_fields

    set_provider_fields("openai", {"api_key": "sk-a"})
    set_provider_fields("anthropic", {"api_key": "sk-b"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    # pick anthropic → remove → back
    select_answers = iter(["anthropic", "remove", onboard_commands._BACK])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))

    onboard_commands._manage_existing_providers(non_interactive=False)
    data = json.loads(tmp_env.read_text())
    assert not data["providers"]["anthropic"].get("apiKey")
    assert data["providers"]["openai"]["apiKey"] == "sk-a"
    # openai still counts as configured; anthropic no longer does.
    assert onboard_commands._configured_providers() == ["openai"]


def test_provider_picker_back_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider picker surfaces a back sentinel choice."""
    import questionary

    captured: dict[str, list] = {}

    class _FQ:
        def ask(self):
            return onboard_commands._BACK

    def _select(message, choices, **kw):
        captured["values"] = [getattr(c, "value", None) for c in choices]
        return _FQ()

    monkeypatch.setattr(questionary, "select", _select)
    result = onboard_commands._select_provider()
    assert result is onboard_commands._BACK
    assert onboard_commands._BACK in captured["values"]


# --------------------------------------------------------------------------- back navigation (state machine)


def test_back_navigation_rewinds_one_screen(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A screen returning _BACK rewinds the state machine by one index."""
    calls: list[str] = []

    def _s1(**_):
        calls.append("s1")
        return None

    def _s2(**_):
        calls.append("s2")
        # First visit to s2 goes back; second proceeds.
        return onboard_commands._BACK if calls.count("s2") == 1 else None

    def _s3(**_):
        calls.append("s3")
        return None

    monkeypatch.setattr(onboard_commands, "_check_tty_or_die", lambda non_interactive: None)
    monkeypatch.setattr(onboard_commands, "_pick_language", lambda: None)
    monkeypatch.setattr(onboard_commands, "_handle_existing_config", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_bootstrap_empty_config", lambda: None)
    monkeypatch.setattr(onboard_commands, "_step1_provider", _s1)
    monkeypatch.setattr(onboard_commands, "_step2_sandbox", _s2)
    monkeypatch.setattr(onboard_commands, "_step3_channel", _s3)
    monkeypatch.setattr(onboard_commands, "_step4_memory", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_deep_research", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_import", lambda **_: None)

    onboard_commands.run_wizard(non_interactive=False)
    # s2 returns BACK once → s1 replays → s2 again → forward.
    assert calls == ["s1", "s2", "s1", "s2", "s3"]


def test_first_screen_back_does_not_skip_step1(
    tmp_env: Path, monkeypatch: pytest.MonkeyPatch, stub_verify, stub_step3
) -> None:
    """BUG-1 regression: Back on the first screen must NOT skip required Step 1.

    Drives the REAL ``_step1_provider``: the picker first returns the back
    sentinel (which used to fall through and skip provider config entirely,
    leaving config unpopulated and re-tripping the gate), then a real provider.
    The wizard must re-display Step 1 and only advance once a provider+model
    are written.
    """
    picks = iter([onboard_commands._BACK, "openai"])
    monkeypatch.setattr(onboard_commands, "_check_tty_or_die", lambda non_interactive: None)
    monkeypatch.setattr(onboard_commands, "_pick_language", lambda: None)
    monkeypatch.setattr(onboard_commands, "_select_provider", lambda: next(picks))
    monkeypatch.setattr(onboard_commands, "_prompt_api_key", lambda provider, **kw: "sk-back-test")
    monkeypatch.setattr(onboard_commands, "_pick_model", lambda provider, spec, **_: spec.default_model)
    # Optional steps are no-ops here; we only assert Step 1 wasn't skipped.
    monkeypatch.setattr(onboard_commands, "_step2_sandbox", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step3_channel", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step4_memory", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_deep_research", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_import", lambda **_: None)

    onboard_commands.run_wizard(non_interactive=False)

    # Provider + model were written despite the first Back — config is populated,
    # so the gate would NOT re-trigger (no infinite loop).
    data = json.loads(tmp_env.read_text())
    assert data["providers"]["openai"]["apiKey"] == "sk-back-test"
    assert data["agents"]["defaults"]["model"] == "openai/gpt-5.5"
    assert onboard_commands._is_config_populated() is True


def test_switch_provider_returns_to_picker_keeps_steps(
    tmp_env: Path, monkeypatch: pytest.MonkeyPatch, stub_step3
) -> None:
    """BUG-2 regression: 'Switch provider' on a verify failure re-runs the
    picker instead of exiting the whole wizard."""
    # First provider verify fails, second succeeds.
    calls = {"n": 0}

    def _verify(name, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "ok": False,
                "status": "invalid_key",
                "models_count": None,
                "model_ids": None,
                "elapsed_ms": 1,
                "error": "401",
            }
        return {"ok": True, "status": "valid", "models_count": 0, "model_ids": [], "elapsed_ms": 1}

    monkeypatch.setattr("raven.config.update_providers.test_provider", _verify)
    monkeypatch.setattr(onboard_commands, "_check_tty_or_die", lambda non_interactive: None)
    monkeypatch.setattr(onboard_commands, "_pick_language", lambda: None)
    # Picker returns anthropic first (fails), then openai (succeeds on switch).
    picks = iter(["anthropic", "openai"])
    monkeypatch.setattr(onboard_commands, "_select_provider", lambda: next(picks))
    monkeypatch.setattr(onboard_commands, "_prompt_api_key", lambda provider, **kw: f"sk-{provider}")
    monkeypatch.setattr(onboard_commands, "_pick_model", lambda provider, spec, **_: spec.default_model)
    # On the failure submenu, choose "switch".
    monkeypatch.setattr(onboard_commands, "_failure_choice", lambda options, *, non_interactive: "switch")
    monkeypatch.setattr(onboard_commands, "_step2_sandbox", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step3_channel", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step4_memory", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_deep_research", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_import", lambda **_: None)

    # Should complete (not raise typer.Exit) — steps 2/3/4 ran.
    onboard_commands.run_wizard(non_interactive=False)
    data = json.loads(tmp_env.read_text())
    # Switched to openai; its key written, default model is openai's.
    assert data["providers"]["openai"]["apiKey"] == "sk-openai"
    assert data["agents"]["defaults"]["model"] == "openai/gpt-5.5"


def test_add_provider_keeps_existing(tmp_env: Path, monkeypatch: pytest.MonkeyPatch, stub_verify, stub_step3) -> None:
    """Adding a second provider in the existing-config entry doesn't drop the first."""
    _seed_provider("openai", "sk-first", "openai/gpt-4o-mini")

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    # Entry menu: "add" once, then "done".
    entry_answers = iter(["add", "done"])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(entry_answers)))
    monkeypatch.setattr(onboard_commands, "_select_provider", lambda: "anthropic")
    monkeypatch.setattr(onboard_commands, "_prompt_api_key", lambda provider, **kw: "sk-second")
    monkeypatch.setattr(onboard_commands, "_pick_model", lambda provider, spec, **_: spec.default_model)

    onboard_commands._step1_provider(
        provider=None,
        api_key=None,
        base_url=None,
        model=None,
        non_interactive=False,
        warnings=[],
    )

    data = json.loads(tmp_env.read_text())
    assert data["providers"]["openai"]["apiKey"] == "sk-first"
    assert data["providers"]["anthropic"]["apiKey"] == "sk-second"


def test_configure_existing_model_non_interactive_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Headless callers can't pick a model interactively, so the helper bails."""
    called = {"verify": False}
    monkeypatch.setattr(onboard_commands, "_verify_provider", lambda *a, **k: called.__setitem__("verify", True))

    assert onboard_commands._configure_existing_provider_model(non_interactive=True) is False
    assert called["verify"] is False


def test_configure_existing_model_no_configured_provider_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing configured the provider list is empty, so there's nothing to pick."""
    monkeypatch.setattr(onboard_commands, "_configured_providers", lambda: [])

    assert onboard_commands._configure_existing_provider_model(non_interactive=False) is False


def _patch_single_provider_pick(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    """Make the provider ``select`` return ``provider`` and list it as configured."""
    import questionary

    class _FQ:
        def ask(self) -> str:
            return provider

    monkeypatch.setattr(onboard_commands, "_configured_providers", lambda: [provider])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ())


def test_configure_existing_model_happy_path_persists_and_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ok -> pick model -> persist -> test probe ok -> True."""
    _patch_single_provider_pick(monkeypatch, "minimax_global")
    monkeypatch.setattr(
        onboard_commands, "_verify_provider", lambda *a, **k: (True, "valid", ["minimax-global/MiniMax-M3"])
    )
    monkeypatch.setattr(onboard_commands, "_pick_model", lambda provider, spec, **_: "minimax-global/MiniMax-M3")
    persisted: list[str] = []
    monkeypatch.setattr(onboard_commands, "_persist_default_model", lambda m: persisted.append(m))
    monkeypatch.setattr(onboard_commands, "_run_test_probe", lambda *a, **k: "ok")

    assert onboard_commands._configure_existing_provider_model(non_interactive=False) is True
    assert persisted == ["minimax-global/MiniMax-M3"]


def test_configure_existing_model_verify_failure_returns_false_without_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed connectivity check aborts before any model is written."""
    _patch_single_provider_pick(monkeypatch, "openai")
    monkeypatch.setattr(onboard_commands, "_verify_provider", lambda *a, **k: (False, "invalid_key", None))
    persisted: list[str] = []
    monkeypatch.setattr(onboard_commands, "_persist_default_model", lambda m: persisted.append(m))

    assert onboard_commands._configure_existing_provider_model(non_interactive=False) is False
    assert persisted == []


def test_configure_existing_model_reauth_delegates_to_oauth_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe asking for re-auth hands off to the OAuth login and returns its result."""
    _patch_single_provider_pick(monkeypatch, "minimax_global")
    monkeypatch.setattr(onboard_commands, "_verify_provider", lambda *a, **k: (True, "valid", []))
    monkeypatch.setattr(onboard_commands, "_pick_model", lambda provider, spec, **_: "minimax-global/MiniMax-M3")
    monkeypatch.setattr(onboard_commands, "_persist_default_model", lambda m: None)
    monkeypatch.setattr(onboard_commands, "_run_test_probe", lambda *a, **k: "reauth")
    login_calls: list[str] = []
    monkeypatch.setattr(onboard_commands, "_run_oauth_login", lambda p: login_calls.append(p) or True)

    assert onboard_commands._configure_existing_provider_model(non_interactive=False) is True
    assert login_calls == ["minimax_global"]


def test_step1_model_action_invokes_existing_model_config(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The step-1 'Choose default model' action routes to the helper, then 'done' exits."""
    _seed_provider("openai", "sk-seed", "openai/gpt-4o-mini")

    import questionary

    class _FQ:
        def __init__(self, a: str) -> None:
            self._a = a

        def ask(self) -> str:
            return self._a

    entry_answers = iter(["model", "done"])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(entry_answers)))
    calls: list[bool] = []
    monkeypatch.setattr(
        onboard_commands,
        "_configure_existing_provider_model",
        lambda *, non_interactive: calls.append(non_interactive) or True,
    )

    onboard_commands._step1_provider(
        provider=None,
        api_key=None,
        base_url=None,
        model=None,
        non_interactive=False,
        warnings=[],
    )

    assert calls == [False]


def test_skip_memory_disables_backend_effective(tmp_env: Path, everos_isolated: Path, stub_verify, stub_step3) -> None:
    """BUG-3 regression: --skip-memory leaves effective memory.backend=None
    (schema default is 'everos', which would activate EverOS without models)."""
    r = runner.invoke(
        app,
        [
            "onboard",
            "--non-interactive",
            "--provider",
            "openai",
            "--api-key",
            "sk-fake",
            "--skip-channel",
            "--skip-memory",
            "--yes",
        ],
    )
    assert r.exit_code == 0, r.stdout
    from raven.config.raven import load_raven_config

    assert load_raven_config().memory.backend is None


def test_fresh_bootstrap_defaults_memory_backend_everos(
    tmp_env: Path, stub_verify, stub_step3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh config seeds memory.backend="everos" (schema default). EverOS
    degrades gracefully without models, and Step 4 / the skip-guard resolve it
    back to None when memory is opted out or left unconfigured."""
    onboard_commands._bootstrap_empty_config()
    from raven.config.raven import load_raven_config

    assert load_raven_config().memory.backend == "everos"


def test_fresh_bootstrap_seeds_extension_blocks(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap materializes the memory / plugins / skillForge safe subset so a
    fresh config exposes the knobs without writing optional service endpoints
    or bearer tokens into the user's plaintext config."""
    onboard_commands._bootstrap_empty_config()
    data = json.loads(tmp_env.read_text())

    assert data["memory"]["backend"] == "everos"  # schema default seeded
    assert data["memory"]["memoryTopK"] == 5
    assert "mode" not in data["plugins"]["config"]["everos-memory"]
    assert data["plugins"]["config"]["everos-memory"]["base_url"] == "http://localhost:18791"
    assert data["skillForge"]["everos"] == {"enabled": True}
    assert data["skillForge"]["router"]["hub"]["endpoint"] == "https://skillhub.evermind.ai"
    assert data["skillForge"]["router"]["hub"]["apiKey"] is None
    # No optional service fields written to the user's plaintext config.
    for leaked in ("embeddingApiKey", "rerankerApiKey", "massLibraryDb"):
        assert leaked not in data["skillForge"]


def test_bootstrap_backfills_preexisting_config(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A config that predates the extension blocks gets them backfilled on the
    next onboard — without clobbering values the user already set."""
    # Simulate an older config: populated, memory.backend set, but no plugins
    # / skillForge blocks and a hand-tuned memoryTopK.
    tmp_env.write_text(
        json.dumps(
            {
                "providers": {"openai": {"apiKey": "sk-keep"}},
                "agents": {"defaults": {"model": "openai/gpt-4o"}},
                "memory": {"backend": "everos", "memoryTopK": 20},
            }
        )
    )

    onboard_commands._bootstrap_empty_config()
    data = json.loads(tmp_env.read_text())

    # Pre-existing values untouched.
    assert data["providers"]["openai"]["apiKey"] == "sk-keep"
    assert data["memory"]["backend"] == "everos"
    assert data["memory"]["memoryTopK"] == 20
    # Missing blocks / keys backfilled.
    assert data["memory"]["userId"] == "default"
    assert data["plugins"]["config"]["everos-memory"]["base_url"] == "http://localhost:18791"
    assert data["skillForge"]["router"]["hub"]["endpoint"] == "https://skillhub.evermind.ai"


def test_prompt_channel_fields_gates_skip_on_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional fields get an ``(optional)`` label + skip hint; a required field
    that is not the first prompt (feishu ``app_secret``) must NOT show a skip
    hint. Regression guard for the ``idx>0`` heuristic that told users they
    could skip a required credential.
    """
    import questionary

    monkeypatch.setattr(onboard_commands, "_LANG", "en")
    captured: list[tuple[str, Any]] = []

    class _Prompt:
        def __init__(self, label: str, placeholder: Any = None, **_: Any) -> None:
            self._label = label
            self._placeholder = placeholder

        def ask(self) -> str:
            captured.append((self._label, self._placeholder))
            return "x"  # non-empty: records the field without triggering back/skip

    monkeypatch.setattr(questionary, "text", lambda label, **kw: _Prompt(label, **kw))
    monkeypatch.setattr(questionary, "password", lambda label, **kw: _Prompt(label, **kw))

    onboard_commands._prompt_channel_fields("feishu")

    # promptable order: app_id, app_secret (both required), encrypt_key, verification_token (optional)
    def _ph_text(placeholder: Any) -> Any:
        return placeholder[0][1] if placeholder else None

    app_id_lbl, app_id_ph = captured[0]
    app_secret_lbl, app_secret_ph = captured[1]
    encrypt_lbl, encrypt_ph = captured[2]

    assert "(optional)" not in app_id_lbl
    assert "(optional)" not in app_secret_lbl
    assert "(optional)" in encrypt_lbl

    assert "back" in _ph_text(app_id_ph)  # first field: rewind affordance
    assert app_secret_ph is None  # required later field: no skip hint
    assert "skip" in _ph_text(encrypt_ph)  # optional field: skip hint


# --------------------------------------------------------------------------- step 5 (deep_research)


def test_total_steps_is_six() -> None:
    # deep_research (step 5) + import (step 6) bumped the wizard from 4 to 6;
    # the progress dots + "Step n/N" header derive from this constant.
    assert onboard_commands._TOTAL_STEPS == 6


def test_step5_skip_or_non_interactive_never_configures(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both the --skip-deep-research and non-interactive paths must return without
    # entering the interactive configure flow (which would hit questionary/network).
    import raven.cli.deep_research_commands as drc

    calls: list = []
    monkeypatch.setattr(drc, "configure_deep_research", lambda **k: calls.append(k))
    assert onboard_commands._step5_deep_research(skip=True, non_interactive=False, warnings=[]) is None
    assert onboard_commands._step5_deep_research(skip=False, non_interactive=True, warnings=[]) is None
    assert calls == []


def test_step5_interactive_delegates_to_shared_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    import raven.cli.deep_research_commands as drc

    seen: dict = {}
    monkeypatch.setattr(drc, "configure_deep_research", lambda **k: seen.update(k) or True)
    onboard_commands._step5_deep_research(skip=False, non_interactive=False, warnings=["w"])
    assert seen.get("non_interactive") is False and seen.get("warnings") == ["w"]


def test_load_raw_config_raises_on_malformed(tmp_env: Path) -> None:
    # onboard's read gate must not silently treat a malformed config as empty
    # (which would let it misread state / write over a config with a typo).
    from raven.config.loader import ConfigReadError

    tmp_env.write_text("{  // comment => invalid JSON\n}", encoding="utf-8")
    with pytest.raises(ConfigReadError):
        onboard_commands._load_raw_config()


def test_removal_guard_sees_a_model_saved_under_a_former_name() -> None:
    """The guard exists to stop a removal from orphaning the default model.

    A model id written before the provider was renamed routes to the very
    provider being removed, which is exactly when the warning has to fire.
    """
    from raven.providers.registry import find_by_name

    spec = find_by_name("zai")
    assert onboard_commands._model_routes_to_provider("zhipu/glm-4.6", spec) is True
    assert onboard_commands._model_routes_to_provider("zai/glm-4.6", spec) is True


def test_the_wizard_offers_every_provider_the_registry_carries() -> None:
    """The picker must not be a hand-picked subset of the registry.

    Eight providers were configurable through the CLI and absent from the wizard,
    so a new user could not reach them and had to guess at the generic
    OpenAI-compatible flow instead.
    """
    from raven.cli.onboard_commands import _CURATED_PROVIDERS
    from raven.providers.registry import PROVIDERS

    offered = {entry["name"] for entry in _CURATED_PROVIDERS}
    registered = {spec.name for spec in PROVIDERS}
    assert registered - offered == set(), f"registry providers missing from the wizard: {sorted(registered - offered)}"
    assert offered - registered == set(), f"wizard offers providers with no spec: {sorted(offered - registered)}"


def test_the_curated_groups_cover_the_flat_list_and_carry_both_fallbacks() -> None:
    """Grouping is what keeps twenty rows readable; the fallbacks close the set."""
    from raven.cli.onboard_commands import _CURATED_GROUPS, _PICK_LITELLM_VENDOR

    kinds = [group["kind"] for group in _CURATED_GROUPS]
    assert kinds == ["api_key", "oauth", "local", "fallback"]
    fallback = {entry["name"] for entry in _CURATED_GROUPS[-1]["providers"]}
    assert fallback == {_PICK_LITELLM_VENDOR, "custom"}
    # Local deployments are offered, which they were not: reaching Ollama meant
    # the custom-endpoint path, which routes through the generic OpenAI driver
    # and so loses the behaviour litellm applies to "ollama_chat/".
    local = {entry["name"] for group in _CURATED_GROUPS if group["kind"] == "local" for entry in group["providers"]}
    assert local == {"ollama_chat", "hosted_vllm"}


def test_the_vendor_step_offers_litellm_names_the_picker_does_not_already_list() -> None:
    """The second step exists so the first one stays short.

    It must not re-offer what the picker already shows, and it must not import
    LiteLLM to build the list -- that costs two seconds on a path that only
    renders choices.
    """
    import subprocess
    import sys

    probe = (
        "import sys, json\n"
        "from raven.cli.onboard_commands import _litellm_vendor_choices\n"
        "rest = _litellm_vendor_choices()\n"
        "print(json.dumps({'litellm': 'litellm' in sys.modules, 'count': len(rest), 'has': 'mistral' in rest,"
        " 'excludes_listed': 'openai' not in rest}))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["litellm"] is False, "building the vendor list imported litellm"
    assert result["has"] is True, "a vendor litellm supports is missing from the second step"
    assert result["excludes_listed"] is True, "the second step re-offers what the picker already lists"
    assert result["count"] > 50


def test_minimax_precedes_deepseek_and_carries_the_open_source_partner_marker() -> None:
    """A deliberate placement, so a later reordering cannot drop it silently.

    "open-source partner" rather than a bare "partner": in a list of vendors the
    short form reads as paid placement. Only the API-key entry is marked -- the
    OAuth ones are the same vendor and already carry "(OAuth)".
    """
    from raven.cli.onboard_commands import _CURATED_GROUPS

    api_key_group = next(g for g in _CURATED_GROUPS if g["kind"] == "api_key")
    names = [entry["name"] for entry in api_key_group["providers"]]
    assert names.index("minimax") == names.index("deepseek") - 1

    minimax = api_key_group["providers"][names.index("minimax")]
    assert minimax["label"] == "MiniMax (open-source partner)"
    assert minimax["label_zh"] == "MiniMax(开源合作伙伴)"

    oauth_group = next(g for g in _CURATED_GROUPS if g["kind"] == "oauth")
    for entry in oauth_group["providers"]:
        assert "partner" not in entry["label"], entry["label"]
        assert "合作伙伴" not in entry["label_zh"], entry["label_zh"]


def test_no_picker_label_names_the_routing_library() -> None:
    """LiteLLM is how Raven reaches a vendor, not something a user configures.

    A label that names it leaks an implementation detail and reads as though the
    user needed an account with it.
    """
    from raven.cli.onboard_commands import _CURATED_GROUPS

    for group in _CURATED_GROUPS:
        for entry in group["providers"]:
            for text in (entry["label"], entry.get("label_zh", "")):
                assert "litellm" not in text.lower(), f"{entry['name']}: {text}"


def test_a_local_deployment_is_configured_by_address_not_by_key(tmp_path, monkeypatch) -> None:
    """Ollama and vLLM authenticate on nothing; they are reached by URL.

    Sending them through the api_key prompt stopped the user at a minimum-length
    check for a credential that does not exist, which is what made offering them
    in the picker impossible before.
    """
    from raven.cli import onboard_commands

    (tmp_path / ".raven").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    written: dict[str, Any] = {}
    monkeypatch.setattr(onboard_commands, "_write_provider_fields", lambda p, f: written.update({p: f}))

    result = onboard_commands._collect_credentials(
        "ollama_chat",
        is_oauth=False,
        is_custom=False,
        is_local=True,
        api_key=None,
        base_url="http://127.0.0.1:11434",
        model=None,
        non_interactive=True,
    )

    assert result is None
    assert written == {"ollama_chat": {"api_base": "http://127.0.0.1:11434"}}
    assert "api_key" not in written["ollama_chat"], "a local deployment was asked for a key"


def test_a_local_deployment_without_an_address_says_so(monkeypatch, tmp_path) -> None:
    """The address is the one thing it cannot be configured without."""
    from raven.cli import onboard_commands

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(typer.BadParameter, match="base-url"):
        onboard_commands._collect_credentials(
            "ollama_chat",
            is_oauth=False,
            is_custom=False,
            is_local=True,
            api_key=None,
            base_url=None,
            model=None,
            non_interactive=True,
        )


def test_a_vendor_with_no_spec_is_configured_by_the_wizard_not_rejected(monkeypatch, tmp_path) -> None:
    """The second picker step offers 117 vendors Raven carries no spec for.

    Every one of them used to reach `spec.name` on a None and tear the wizard
    down after the key was already on disk. The gate that produced that -- "the
    wizard does not cover this" -- was the older limitation: credentials go in
    under the vendor's name and the model list comes from the vendor itself, so
    a spec is metadata here, not permission.
    """
    from raven.cli import onboard_commands
    from raven.providers.registry import find_by_name

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".raven").mkdir()

    assert onboard_commands._validate_provider_name("mistral") == "mistral"
    assert find_by_name("mistral") is None, "mistral gained a spec; pick another spec-less vendor"


def test_a_typo_in_the_vendor_step_is_a_message_not_a_traceback(monkeypatch, tmp_path) -> None:
    """Both entrances share one gate.

    The flag path validated; the picker path assigned the raw string, so a
    mistyped vendor name reached the config layer as an uncaught KeyError.
    """
    from raven.cli import onboard_commands

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(typer.BadParameter, match="mistrall"):
        onboard_commands._validate_provider_name("mistrall")


def test_resolve_model_with_test_runs_for_a_provider_with_no_spec(monkeypatch, tmp_path) -> None:
    """Drives the path that crashed, rather than asserting a constant about it.

    Thirteen tests asserted what the picker lists and none walked into it, which
    is why a crash on every one of those vendors shipped green.
    """
    from raven.cli import onboard_commands

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".raven").mkdir()
    monkeypatch.setattr(
        onboard_commands,
        "_verify_provider",
        lambda provider, skip_test=False: (True, "valid", ["mistral-large-latest"]),
    )
    monkeypatch.setattr(onboard_commands, "_persist_default_model", lambda model: None)

    chosen = onboard_commands._resolve_model_with_test(
        "mistral",
        None,  # no spec, which is the whole point
        is_custom=False,
        custom_model=None,
        user_model_flag="mistral/mistral-large-latest",
        non_interactive=True,
        warnings=[],
        skip_test=True,
    )
    assert chosen == "mistral/mistral-large-latest"


def test_the_wizard_offers_known_models_when_the_provider_cannot_be_reached(monkeypatch, tmp_path) -> None:
    """A failed fetch must not leave the user typing an id from memory.

    Deleting this fallback left all 86 onboard tests green, so half of what the
    candidate chain is for had nothing asserting it.
    """
    from raven.cli import onboard_commands
    from raven.providers.registry import find_by_name

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    offered: dict[str, Any] = {}

    class _Stub:
        def __init__(self, label, **kw):
            offered["choices"] = list(kw.get("choices") or [])

        def ask(self):
            return offered["choices"][0]

    fake_questionary = SimpleNamespace(autocomplete=_Stub, text=_Stub)
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: fake_questionary)

    chosen = onboard_commands._pick_model(
        "moonshot",
        find_by_name("moonshot"),
        current_model=None,
        model_ids=None,  # the fetch came back empty
        probe_status="network_error",
        user_provided_model=None,
        non_interactive=False,
    )

    assert offered["choices"], "no candidates were offered after a failed fetch"
    assert chosen == offered["choices"][0]
    assert any(c.startswith("moonshot/") for c in offered["choices"]), offered["choices"][:3]


def test_codex_picker_preserves_auto_as_default_above_live_catalog(monkeypatch) -> None:
    from raven.cli import onboard_commands
    from raven.providers.openai_codex_catalog import AUTO_CODEX_MODEL
    from raven.providers.registry import find_by_name

    offered: dict[str, Any] = {}

    class _Prompt:
        def __init__(self, _label, **kwargs):
            offered["choices"] = list(kwargs["choices"])
            offered["default"] = kwargs["default"]

        def ask(self):
            return offered["default"]

    monkeypatch.setattr(
        onboard_commands,
        "_require_questionary",
        lambda: SimpleNamespace(autocomplete=_Prompt),
    )

    chosen = onboard_commands._pick_model(
        "openai_codex",
        find_by_name("openai_codex"),
        current_model=None,
        model_ids=["gpt-new-default", "gpt-secondary"],
        probe_status="valid",
        user_provided_model=None,
        non_interactive=False,
    )

    assert offered["default"] == AUTO_CODEX_MODEL
    assert offered["choices"] == [AUTO_CODEX_MODEL, "gpt-new-default", "gpt-secondary"]
    assert chosen == AUTO_CODEX_MODEL


def test_a_spec_less_vendors_model_id_carries_its_route_prefix() -> None:
    """A bare id is routed by keyword and fallback, not to the section configured.

    Returning the vendor's id unprefixed produced "mistral-large-latest", which
    resolves to OpenAI when OpenAI also holds a key -- so the wizard handed back
    a default model that spends someone else's credential.
    """
    from raven.cli.onboard_commands import _format_model_for_provider

    assert _format_model_for_provider("mistral", None, "mistral-large-latest") == "mistral/mistral-large-latest"
    # Already prefixed stays put rather than being prefixed twice.
    assert _format_model_for_provider("mistral", None, "mistral/mistral-large-latest") == "mistral/mistral-large-latest"


def test_that_prefix_is_what_stops_the_key_going_elsewhere() -> None:
    """The consequence, asserted where it lands rather than on the string.

    This is the assertion the crash-fix needed and did not have: the id the
    wizard writes must resolve to the provider it was configured for, with that
    provider's credential, even when a keyword-matching vendor is also set up.
    """
    from raven.cli.onboard_commands import _format_model_for_provider
    from raven.config.schema import Config

    model = _format_model_for_provider("mistral", None, "mistral-large-latest")
    config = Config.model_validate(
        {
            "agents": {"defaults": {"model": model}},
            "providers": {"mistral": {"apiKey": "sk-MISTRAL"}, "openai": {"apiKey": "sk-OPENAI"}},
        }
    )
    assert config.get_provider_name(model) == "mistral"
    assert config.get_api_key(model) == "sk-MISTRAL"


def test_rolling_back_a_failed_setup_restores_what_was_there(monkeypatch, tmp_path) -> None:
    """Restores the prior state, and does not touch an OAuth provider at all.

    Two shapes were wrong: keying the rollback off the previous api_key skipped a
    local deployment entirely, leaving a mistyped address in place of a working
    one; and writing credential fields for an OAuth provider raises, which turned
    a failed verification into a dead wizard.
    """
    from raven.cli.onboard_commands import _roll_back_provider_fields, _write_provider_fields
    from raven.config.loader import set_config_path
    from raven.config.update_providers import get_provider_config
    from raven.providers.registry import find_by_name

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"providers": {"ollama_chat": {"apiBase": "http://my-nas:11434"}}}))
    set_config_path(cfg)
    try:
        prior = get_provider_config("ollama_chat", redact_secrets=False)
        _write_provider_fields("ollama_chat", {"api_base": "http://typo:9999"})
        # The wizard's own rollback, called rather than re-implemented here.
        _roll_back_provider_fields(
            "ollama_chat",
            find_by_name("ollama_chat"),
            old_key=prior.get("api_key"),
            old_base=prior.get("api_base"),
        )
        assert get_provider_config("ollama_chat", redact_secrets=False)["api_base"] == "http://my-nas:11434"

        # And why the branch must not run for OAuth: the ops layer refuses these
        # fields, and the wizard's wrapper turns that refusal into an exit -- so
        # rolling back an OAuth provider ends the whole run.
        from raven.config.update_providers import set_provider_fields

        oauth = find_by_name("github_copilot")
        assert oauth is not None and oauth.is_oauth
        with pytest.raises(RuntimeError, match="OAuth"):
            set_provider_fields("github_copilot", {"api_key": ""}, config_path=cfg)
        with pytest.raises(typer.Exit):
            _write_provider_fields("github_copilot", {"api_key": ""})
        # So the rollback must not go near them -- calling it is a no-op.
        _roll_back_provider_fields("github_copilot", oauth, old_key=None, old_base=None)
    finally:
        set_config_path(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("slug", "status", "expected", "absent"),
    [
        # Unreachable: a local deployment's usual cause is a wrong address, and
        # this is the branch it lands in -- retry alone left it unreachable.
        ("ollama_chat", "network_error", "rebase", "rekey"),
        ("hosted_vllm", "network_error", "rebase", "rekey"),
        # A vendor reached over the network gets retry only; the address is not
        # the user's to change, and the key is not what failed.
        ("deepseek", "network_error", "retry", "rebase"),
        # Rejected credentials: the field to fix is the one the provider uses.
        ("ollama_chat", "invalid_key", "rebase", "rekey"),
        ("deepseek", "invalid_key", "rekey", "rebase"),
        ("github_copilot", "invalid_key", "reauth", "rekey"),
    ],
)
def test_the_failure_menu_offers_the_field_the_provider_actually_has(
    slug: str, status: str, expected: str, absent: str, monkeypatch
) -> None:
    """What is worth changing after a failure depends on how the provider is reached.

    A local deployment fails most often on a wrong address, and both failure
    branches offered "re-enter key" for a provider that holds none -- so the one
    field worth editing had no route back to it.
    """
    from raven.cli import onboard_commands
    from raven.providers.registry import find_by_name

    seen: list[list[str]] = []
    monkeypatch.setattr(
        onboard_commands,
        "_failure_choice",
        lambda options, non_interactive: (seen.append([v for _, v in options]), "switch")[1],
    )
    monkeypatch.setattr(onboard_commands, "_verify_provider", lambda provider, skip_test=False: (False, status, None))

    result = onboard_commands._resolve_model_with_test(
        slug,
        find_by_name(slug),
        is_custom=False,
        custom_model=None,
        user_model_flag=f"{slug}/probe",
        non_interactive=False,
        warnings=[],
    )

    assert result is None, "switch should unwind to the picker"
    assert seen, "the failure menu was never shown"
    assert expected in seen[0], f"{slug}/{status}: offered {seen[0]}"
    assert absent not in seen[0], f"{slug}/{status}: should not offer {absent}, got {seen[0]}"


def test_managing_an_oauth_provider_explains_instead_of_exiting(monkeypatch, tmp_path, capsys) -> None:
    """Update and Remove both wrote credential fields, which OAuth providers refuse.

    Generalising "not every provider has a key" only as far as local deployments
    left the OAuth ones ending the wizard from a menu meant for editing.
    """
    from raven.cli.onboard_commands import _roll_back_provider_fields
    from raven.providers.registry import find_by_name

    spec = find_by_name("github_copilot")
    assert spec is not None and spec.is_oauth
    # The shared guard both menu actions now use.
    _roll_back_provider_fields("github_copilot", spec, old_key=None, old_base=None)


def test_a_spec_less_provider_can_have_its_default_model_changed(monkeypatch, tmp_path) -> None:
    """Configuring a vendor and then editing its default model are one capability.

    The "choose default model" menu filtered out anything without a registry
    entry, so the new gate let a vendor be configured and then refused to let its
    model be changed -- the only way back was to re-enter the key through "add a
    provider". Removing that filter requires the spec dereference further down to
    be guarded, or the menu crashes on the very providers it just started
    offering; the two must move together, which is what this asserts.
    """
    from raven.cli import onboard_commands

    cfg_dir = tmp_path / ".raven"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"providers": {"mistral": {"apiKey": "sk-m"}, "openai": {"apiKey": "sk-o"}}})
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    offered: list[str] = []

    class _Choice:
        def __init__(self, _title, value=None):
            self.value = value

    class _Select:
        def __init__(self, _label, **kw):
            offered.extend(c.value for c in kw.get("choices") or [])

        def ask(self):
            return "mistral"

    monkeypatch.setattr(
        onboard_commands,
        "_require_questionary",
        lambda: SimpleNamespace(select=_Select, autocomplete=_Select, Choice=_Choice),
    )
    monkeypatch.setattr(
        onboard_commands, "_verify_provider", lambda provider: (True, "valid", ["mistral-large-latest"])
    )
    monkeypatch.setattr(onboard_commands, "_pick_model", lambda provider, spec, **_: f"{provider}/probe")
    monkeypatch.setattr(onboard_commands, "_persist_default_model", lambda model: None)
    # Reaching this without an AttributeError is the second half of the fix: the
    # probe is told whether the provider is OAuth, read off a spec that is None.
    monkeypatch.setattr(onboard_commands, "_run_test_probe", lambda provider, **kw: "ok")

    assert onboard_commands._configure_existing_provider_model(non_interactive=False) is True
    assert "mistral" in offered, f"a spec-less provider was filtered out: {offered}"


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        # What a user actually types: the vendor's own name for the model.
        ("mistral-large-latest", "mistral/mistral-large-latest"),
        ("mistral/mistral-large-latest", "mistral/mistral-large-latest"),
    ],
)
def test_every_exit_of_the_model_step_prefixes_what_it_returns(typed: str, expected: str, monkeypatch) -> None:
    """The invariant belongs on the exits, not on one branch.

    It was applied where the candidate list is built, which is the one branch a
    spec-less vendor never reaches: nothing can pre-check it, so there is no
    list, so the id is typed -- and typed ids went to config as typed. Three
    rounds of review found this same defect on three different branches, so this
    drives each exit rather than asserting the formatter in isolation.
    """
    from raven.cli import onboard_commands

    class _Prompt:
        def __init__(self, _label, **_kw):
            pass

        def ask(self):
            return typed

    monkeypatch.setattr(
        onboard_commands, "_require_questionary", lambda: SimpleNamespace(autocomplete=_Prompt, text=_Prompt)
    )

    # The flag exit.
    assert (
        onboard_commands._pick_model(
            "mistral",
            None,
            current_model=None,
            model_ids=None,
            probe_status="skipped",
            user_provided_model=typed,
            non_interactive=True,
        )
        == expected
    )
    # The typed exit, reached when there is no candidate list at all.
    assert (
        onboard_commands._pick_model(
            "mistral",
            None,
            current_model=None,
            model_ids=None,
            probe_status="skipped",
            user_provided_model=None,
            non_interactive=False,
        )
        == expected
    )


def test_what_the_model_step_returns_is_served_by_the_provider_it_was_configured_for() -> None:
    """The consequence, taken from the production value rather than a literal.

    Every earlier test on this fed an already-prefixed id in, so none of them
    could tell whether the wizard produces one. This takes what the step returns
    and asks who would be billed for it.
    """
    from raven.cli import onboard_commands
    from raven.config.schema import Config

    produced = onboard_commands._pick_model(
        "mistral",
        None,
        current_model=None,
        model_ids=None,
        probe_status="skipped",
        user_provided_model="mistral-large-latest",
        non_interactive=True,
    )
    config = Config.model_validate(
        {
            "agents": {"defaults": {"model": produced}},
            "providers": {"mistral": {"apiKey": "sk-MISTRAL"}, "openai": {"apiKey": "sk-OPENAI"}},
        }
    )
    assert config.get_provider_name(produced) == "mistral"
    assert config.get_api_key(produced) == "sk-MISTRAL"


def test_a_hyphenated_vendor_gets_the_prefix_litellm_will_accept() -> None:
    """Config names are matched loosely; a wire prefix cannot be.

    LiteLLM hyphenates three vendors. Prefixing with the normalized form produced
    "nano_gpt/...", which LiteLLM rejects outright -- so the provider configured
    successfully and then could not be called.
    """
    from raven.cli.onboard_commands import _format_model_for_provider
    from raven.providers.litellm_setup import import_litellm

    for typed_name in ("nano-gpt", "nano_gpt"):
        model = _format_model_for_provider(typed_name, None, "gpt-4o")
        assert model == "nano-gpt/gpt-4o", typed_name
        # And LiteLLM agrees it can route it.
        assert import_litellm().get_llm_provider(model)[1] == "nano-gpt"


def test_a_spec_less_vendor_is_offered_the_catalogue_rows_it_has() -> None:
    """Returning nothing for these is what forced the id to be typed.

    Every offered id carries the prefix, which the catalogue itself does not
    guarantee: Mistral's rows have it, Bedrock's do not. Offering an unprefixed
    one would put the bare id straight back into config.
    """
    from raven.providers.common_models import litellm_models_for

    for slug in ("mistral", "fireworks_ai", "bedrock"):
        models = litellm_models_for(slug)
        assert models, f"{slug}: no candidates offered"
        assert all(m.startswith(f"{slug}/") for m in models), [m for m in models if not m.startswith(f"{slug}/")][:3]
        # And no id was prefixed twice on the way through.
        assert not any(m.startswith(f"{slug}/{slug}/") for m in models)


def test_the_wizard_never_reads_a_spec_auth_flag_itself() -> None:
    """One answer to "how is this provider reached", and it does not live here.

    Every decision the wizard makes about a provider follows from that question,
    and it was derived independently at thirteen sites off the spec flags. Each of
    two review rounds found a site that disagreed with the rest: a rollback that
    wrote credential fields to an OAuth provider and ended the run, a menu that
    offered it a key prompt, a prompt that guarded a spec on one line and
    dereferenced it on the next. It is a registry function now, because the model
    picker needed the same answer and had been keeping a coarser one of its own.
    """
    import re
    from pathlib import Path as _Path

    path = _Path(__file__).resolve().parents[1] / "raven" / "cli" / "onboard_commands.py"
    offenders = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\.is_(oauth|local)\b|\.requires_api_base\b", line) and not line.lstrip().startswith("#")
    ]
    assert not offenders, "ask credential_kind() instead:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("github_copilot", "oauth"),
        ("openai_codex", "oauth"),
        ("minimax_global", "oauth"),
        ("ollama_chat", "local"),
        ("hosted_vllm", "local"),
        ("custom", "endpoint"),
        ("anthropic", "key"),
        ("mistral", "key"),  # no spec at all
    ],
)
def test_credential_kind_covers_every_shape(provider: str, expected: str) -> None:
    from raven.providers.registry import credential_kind

    assert credential_kind(provider) == expected


def test_every_registered_provider_has_exactly_one_credential_kind() -> None:
    """Sweep, so a provider added later cannot fall through the classification."""
    from raven.providers.registry import CRED_ENDPOINT, CRED_KEY, CRED_LOCAL, CRED_OAUTH, PROVIDERS, credential_kind

    known = {CRED_OAUTH, CRED_LOCAL, CRED_ENDPOINT, CRED_KEY}
    for spec in PROVIDERS:
        assert credential_kind(spec.name) in known, spec.name


def test_the_picker_result_goes_through_the_same_gate_as_the_flag(monkeypatch, tmp_path) -> None:
    """N1: the joint that let 117 vendors crash, and stayed uncovered for three rounds.

    The flag path validated its input; the picker path assigned it. Deleting the
    validation call restores the original defect -- a typed name reaching the
    config layer as an uncaught KeyError mid-setup -- so the call itself is what
    needs holding down, not the validator in isolation.
    """
    from raven.cli import onboard_commands

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".raven").mkdir()
    validated: list[str] = []
    real = onboard_commands._validate_provider_name

    def spy(name: str) -> str:
        validated.append(name)
        return real(name)

    monkeypatch.setattr(onboard_commands, "_validate_provider_name", spy)
    monkeypatch.setattr(onboard_commands, "_select_provider", lambda: "mistrall")  # a typo
    printed: list[str] = []
    monkeypatch.setattr(onboard_commands.console, "print", lambda *a, **k: printed.append(str(a[0]) if a else ""))
    # Second pass: back out so the loop ends.
    calls = {"n": 0}

    def picker():
        calls["n"] += 1
        return "mistrall" if calls["n"] == 1 else onboard_commands._BACK

    monkeypatch.setattr(onboard_commands, "_select_provider", picker)

    assert (
        onboard_commands._configure_one_provider(
            provider=None, api_key=None, base_url=None, model=None, non_interactive=False, warnings=[]
        )
        is None
    )
    assert validated == ["mistrall"], "the picker result bypassed the gate"
    assert any("mistrall" in line for line in printed), "the typo was not reported to the user"


def test_a_failed_setup_calls_the_rollback(monkeypatch, tmp_path) -> None:
    """N2: deleting this call silently undoes both rollback fixes.

    `_roll_back_provider_fields` has its own tests, but nothing asserted the
    failure path reaches it -- so removing the call left a mistyped address in
    place of a working one with CI green.
    """
    from raven.cli import onboard_commands

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".raven").mkdir()
    rolled: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        onboard_commands,
        "_roll_back_provider_fields",
        lambda provider, spec, **kw: rolled.append((provider, kw)),
    )
    monkeypatch.setattr(onboard_commands, "_collect_credentials", lambda provider, **kw: None)
    # The model step reports "switch provider", which is the failure path.
    monkeypatch.setattr(onboard_commands, "_resolve_model_with_test", lambda provider, spec, **kw: None)
    calls = {"n": 0}

    def picker():
        calls["n"] += 1
        return "deepseek" if calls["n"] == 1 else onboard_commands._BACK

    monkeypatch.setattr(onboard_commands, "_select_provider", picker)

    onboard_commands._configure_one_provider(
        provider=None, api_key=None, base_url=None, model=None, non_interactive=False, warnings=[]
    )
    assert rolled, "a failed setup did not roll back"
    assert rolled[0][0] == "deepseek"
    assert set(rolled[0][1]) == {"old_key", "old_base"}, rolled[0][1]


def test_reconfiguring_a_local_server_is_seeded_with_its_own_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M6: the seeding fix is what stops a working address being replaced.

    Seeding the registry default unconditionally meant someone whose server runs
    somewhere other than localhost was offered localhost, and pressing Enter to
    move past a field they had already filled in wrote it -- the same data loss as
    the failed-setup rollback, reached by an ordinary keypress instead of a failure.
    """
    import questionary

    from raven.providers.registry import find_by_name

    captured: dict[str, Any] = {}

    class _FQ:
        def ask(self) -> str:
            return "http://gpu-box.lan:11434"

    def _text(message: Any, *, default: Any = None, **kwargs: Any) -> Any:
        captured["default"] = default
        return _FQ()

    monkeypatch.setattr(questionary, "text", _text)
    spec = find_by_name("ollama_chat")
    assert spec is not None and spec.default_api_base

    onboard_commands._prompt_local_api_base(spec, current="http://gpu-box.lan:11434")
    assert captured["default"] == "http://gpu-box.lan:11434", (
        "the configured address was replaced by the registry default"
    )

    onboard_commands._prompt_local_api_base(spec, current="")
    assert captured["default"] == spec.default_api_base, "a first-time setup lost its default"


def test_backing_out_of_the_vendor_sublist_returns_to_the_provider_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U8: an empty submit closes the sub-list the user opened, one level.

    Passing its ``_BACK`` straight up dropped them on the language screen -- three
    steps back from where they were -- so the row that opened the sub-list has to
    be redisplayed instead.
    """
    rows = iter([onboard_commands._PICK_LITELLM_VENDOR, "deepseek"])
    monkeypatch.setattr(onboard_commands, "_select_provider_row", lambda: next(rows))
    monkeypatch.setattr(onboard_commands, "_prompt_litellm_vendor", lambda: onboard_commands._BACK)

    assert onboard_commands._select_provider() == "deepseek"

    # Backing out of the provider list itself still leaves the step.
    rows = iter([onboard_commands._PICK_LITELLM_VENDOR, onboard_commands._BACK])
    monkeypatch.setattr(onboard_commands, "_select_provider_row", lambda: next(rows))
    assert onboard_commands._select_provider() is onboard_commands._BACK


def test_switching_provider_discards_every_flag_not_just_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Flag values belong to the pass they were typed for.

    They were carried into the next one, so switching from a failed keyed
    provider to a local deployment hit the guard that rejects --api-key for one
    and ended the wizard on a usage error, losing the steps this loop exists to
    keep. The quieter halves of the same bug: the stale key was written to the
    newly picked provider with no prompt, and the stale base URL pointed it at
    the previous provider's machine.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".raven").mkdir()
    seen: list[tuple[str, Any, Any, Any]] = []

    def _collect(provider: str, **kw: Any) -> Any:
        seen.append((provider, kw.get("api_key"), kw.get("base_url"), kw.get("model")))
        return onboard_commands._BACK if provider == "ollama_chat" else None

    picks = iter(["ollama_chat", onboard_commands._BACK])
    monkeypatch.setattr(onboard_commands, "_select_provider", lambda: next(picks))
    monkeypatch.setattr(onboard_commands, "_collect_credentials", _collect)
    monkeypatch.setattr(onboard_commands, "_resolve_model_with_test", lambda *a, **k: None)

    # No exception: reaching the local deployment used to raise BadParameter here.
    assert (
        onboard_commands._configure_one_provider(
            provider="deepseek",
            api_key="sk-stale",
            base_url="http://previous-box:1234",
            model="deepseek-chat",
            non_interactive=False,
            warnings=[],
        )
        is None
    )
    assert seen[0] == ("deepseek", "sk-stale", "http://previous-box:1234", "deepseek-chat"), (
        "the flags have to apply on the pass they were given for"
    )
    assert seen[1] == ("ollama_chat", None, None, None), f"a flag survived the switch: {seen[1]}"


def test_no_rewind_clears_the_flags_by_hand() -> None:
    """One answer to "what does a rewind discard", three call sites.

    Each of the three used to answer it separately and all three answered it the
    same incomplete way -- only the provider flag -- which is the shape of bug
    this branch exists to remove.
    """
    import ast
    from pathlib import Path as _Path

    path = _Path(__file__).resolve().parents[1] / "raven" / "cli" / "onboard_commands.py"
    source = path.read_text()
    tree = ast.parse(source)
    outer = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_configure_one_provider"
    )
    rewind = next(node for node in ast.walk(outer) if isinstance(node, ast.FunctionDef) and node.name == "_rewind")
    lines = source.splitlines()
    allowed = range(rewind.lineno, (rewind.end_lineno or rewind.lineno) + 1)
    body = range(outer.lineno, (outer.end_lineno or outer.lineno) + 1)

    offenders = [
        f"line {i}: {lines[i - 1].strip()}"
        for i in body
        if "flag_provider = " in lines[i - 1] and "flag_provider = provider" not in lines[i - 1] and i not in allowed
    ]
    assert not offenders, "call _rewind() instead:\n" + "\n".join(offenders)


def test_a_provider_with_no_model_endpoint_is_not_reported_as_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """openai / anthropic / deepseek / gemini reach this with nothing wrong.

    They ship no api_base, so the pre-check is skipped rather than failed and
    there is no model list on a healthy run. The step printed "couldn't reach the
    provider" straight after the line saying the check had been skipped, so the
    first thing the user saw on the happy path was two contradictory sentences.
    """
    from raven.providers.registry import find_by_name

    offered: dict[str, Any] = {}

    class _Stub:
        def __init__(self, label: Any, **kw: Any) -> None:
            offered["choices"] = list(kw.get("choices") or [])

        def ask(self) -> Any:
            return offered["choices"][0]

    monkeypatch.setattr(
        onboard_commands, "_require_questionary", lambda: SimpleNamespace(autocomplete=_Stub, text=_Stub)
    )

    for status, unreachable_expected in (("skipped", False), ("network_error", True)):
        capsys.readouterr()
        onboard_commands._pick_model(
            "anthropic",
            find_by_name("anthropic"),
            current_model=None,
            model_ids=None,
            probe_status=status,
            user_provided_model=None,
            non_interactive=False,
        )
        out = capsys.readouterr().out.lower()
        assert ("couldn't reach" in out or "could not reach" in out) is unreachable_expected, (
            f"status={status!r} printed: {out.strip()}"
        )


def test_updating_a_self_hosted_endpoint_can_change_its_address(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A key plus the address it is sent to; the menu only re-asked for the key.

    The URL is the field that moves when the user redeploys, and it was the one
    this menu could not reach -- leaving the stored address pointing at a machine
    that is gone, with no way out but editing the config file.
    """
    from raven.config.update_providers import set_provider_fields

    set_provider_fields("custom", {"api_key": "sk-old", "api_base": "http://old-box:8000/v1"})

    import questionary

    class _FQ:
        def __init__(self, a: Any) -> None:
            self._a = a

        def ask(self) -> Any:
            return self._a

    select_answers = iter(["custom", "update", onboard_commands._BACK])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(onboard_commands, "_prompt_api_key", lambda provider, **kw: "sk-new")

    seeded: dict[str, Any] = {}

    def _base_url(default: str = "https://", **kw: Any) -> Any:
        seeded["default"] = default
        return "http://new-box:9000/v1"

    monkeypatch.setattr(onboard_commands, "_prompt_base_url", _base_url)

    onboard_commands._manage_existing_providers(non_interactive=False)

    section = json.loads(tmp_env.read_text())["providers"]["custom"]
    assert section.get("apiBase") == "http://new-box:9000/v1", section
    assert section.get("apiKey") == "sk-new", section
    assert seeded["default"] == "http://old-box:8000/v1", "the stored address was not offered back"


def test_a_provider_whose_endpoint_only_the_user_knows_is_asked_for_it() -> None:
    """Azure gives every tenant its own resource URL, so there is nothing to default to.

    It was classified by name -- only "custom" counted -- so Azure was asked for a
    key alone and stored with no endpoint, and its own client raises
    "api_base is required" on the first call. Picking it from the curated list
    could not produce a working provider.
    """
    from raven.providers.registry import CRED_ENDPOINT, PROVIDERS, credential_kind

    assert credential_kind("azure_openai") == CRED_ENDPOINT
    for spec in PROVIDERS:
        expected = CRED_ENDPOINT if spec.requires_api_base else None
        if expected is not None:
            assert credential_kind(spec.name) == expected, spec.name


def test_the_model_picker_reports_the_same_credential_shape_as_the_wizard() -> None:
    """The picker knew only two shapes and offered a local deployment a key prompt.

    It kept its own literal list of who needs an endpoint, and reported every
    non-OAuth provider as taking an API key -- so Ollama was asked for a key it
    cannot use and never asked for the address it needs. The RPC surface reports
    the shared answer now; this reads it back off the payload rather than
    re-deriving it.
    """
    from raven.providers.registry import CRED_ENDPOINT, CRED_LOCAL, PROVIDERS, credential_kind
    from raven.tui_rpc.methods.model import _build_provider_entry

    for spec in PROVIDERS:
        entry = _build_provider_entry(spec.name, current_provider=None)
        kind = credential_kind(spec.name)
        assert entry["auth_type"] == kind, spec.name
        assert entry["needs_api_base"] is (kind in (CRED_ENDPOINT, CRED_LOCAL)), spec.name


def test_configuring_azure_stores_the_endpoint_it_was_given(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The consequence, read back off disk rather than from the prompt count."""
    monkeypatch.setattr(
        onboard_commands,
        "_collect_fields",
        lambda prompts: ["sk-azure", "https://my-resource.openai.azure.com", "gpt-4o-deployment"],
    )

    returned = onboard_commands._collect_credentials(
        "azure_openai",
        is_oauth=False,
        is_custom=True,
        is_local=False,
        api_key=None,
        base_url=None,
        model=None,
        non_interactive=False,
    )

    section = json.loads(tmp_env.read_text())["providers"]["azure_openai"]
    assert section["apiBase"] == "https://my-resource.openai.azure.com", section
    assert section["apiKey"] == "sk-azure"
    # Azure takes a deployment name where every other provider takes a model id,
    # so the step locks it in rather than offering the picker.
    assert returned == "gpt-4o-deployment"


def test_a_mistyped_local_address_can_be_retyped_without_losing_the_setup(
    tmp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U4: the branch a user reaches by getting their own server's address wrong.

    A local deployment that cannot be reached is almost always a typo, so the
    failure menu offers the address back. What that choice then runs had no test:
    it re-reads the stored address, seeds the prompt with it, writes what comes
    back, and re-verifies -- and returning None from it would have been read as
    "switch provider" and rolled the setup back instead of retrying.
    """
    from raven.config.update_providers import set_provider_fields
    from raven.providers.registry import find_by_name

    set_provider_fields("ollama_chat", {"api_base": "http://typo:11434"})

    attempts: list[str] = []

    def _probe(provider: str, *a: Any, **kw: Any) -> dict[str, Any]:
        stored = json.loads(tmp_env.read_text())["providers"]["ollama_chat"]["apiBase"]
        attempts.append(stored)
        ok = stored == "http://gpu-box:11434"
        return {"ok": ok, "status": "valid" if ok else "network_error", "error": "" if ok else "unreachable"}

    monkeypatch.setattr("raven.config.update_providers.test_provider", _probe)
    monkeypatch.setattr(onboard_commands, "_failure_choice", lambda options, **kw: "rebase")
    seeded: dict[str, Any] = {}

    def _retype(spec: Any, *, current: str = "", **kw: Any) -> str:
        seeded["current"] = current
        return "http://gpu-box:11434"

    monkeypatch.setattr(onboard_commands, "_prompt_local_api_base", _retype)

    ok, status, _models = onboard_commands._verify_provider("ollama_chat")
    assert not ok and status == "network_error"

    # Drive the branch the menu selects.
    result = onboard_commands._resolve_model_with_test(
        "ollama_chat",
        find_by_name("ollama_chat"),
        is_custom=False,
        custom_model=None,
        user_model_flag="ollama_chat/codegeex4",
        non_interactive=False,
        warnings=[],
        skip_test=True,
    )

    assert seeded["current"] == "http://typo:11434", "the address being fixed was not offered back"
    assert json.loads(tmp_env.read_text())["providers"]["ollama_chat"]["apiBase"] == "http://gpu-box:11434"
    assert result is not None, "a retyped address must not read as 'switch provider'"
    assert attempts[-1] == "http://gpu-box:11434", "the retyped address was not re-verified"

    # Ctrl+C at that prompt quits, as it does in the sibling re-enter-key branch.
    # Returning None would be read as "switch provider" and roll the setup back.
    monkeypatch.setattr(onboard_commands, "_prompt_local_api_base", lambda *a, **kw: None)
    monkeypatch.setattr(onboard_commands, "_failure_choice", lambda options, **kw: "rebase")
    monkeypatch.setattr(
        "raven.config.update_providers.test_provider",
        lambda *a, **kw: {"ok": False, "status": "network_error", "error": "unreachable"},
    )
    with pytest.raises(typer.Exit):
        onboard_commands._resolve_model_with_test(
            "ollama_chat",
            find_by_name("ollama_chat"),
            is_custom=False,
            custom_model=None,
            user_model_flag="ollama_chat/codegeex4",
            non_interactive=False,
            warnings=[],
            skip_test=True,
        )


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"api_key": "sk-nope"}, "takes no --api-key"),
        ({"base_url": "gpu-box:11434"}, "must start with http"),
    ],
)
def test_the_flag_path_rejects_credentials_a_local_deployment_cannot_use(
    tmp_env: Path, flags: dict[str, Any], expected: str
) -> None:
    """U6: both guards face command-line users and neither was covered.

    Dropping the key silently would look like it had been accepted, and a
    scheme-less address passed the interactive validator's absence and only failed
    at first use.
    """
    with pytest.raises(typer.BadParameter) as excinfo:
        onboard_commands._collect_credentials(
            "ollama_chat",
            is_oauth=False,
            is_custom=False,
            is_local=True,
            api_key=flags.get("api_key"),
            base_url=flags.get("base_url"),
            model=None,
            non_interactive=True,
        )
    assert expected in str(excinfo.value)


def test_removing_a_spec_less_provider_warns_when_it_serves_the_default_model(
    tmp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U12: who serves the default is decided differently for a vendor with no spec.

    It is reached by its prefix alone, so that is the whole test -- and treating
    "no spec" as "not the source" skipped the warning entirely, leaving a default
    model pointing at a provider whose key had just been removed.
    """
    import questionary

    from raven.config.update_providers import set_provider_fields

    set_provider_fields("mistral", {"api_key": "sk-mistral"})
    onboard_commands._persist_default_model("mistral/mistral-large-latest")

    asked: list[str] = []

    class _FQ:
        def __init__(self, a: Any) -> None:
            self._a = a

        def ask(self) -> Any:
            return self._a

    def _confirm(message: Any, **kw: Any) -> Any:
        asked.append(str(message))
        return _FQ(False)  # decline, so nothing is removed

    select_answers = iter(["mistral", "remove", onboard_commands._BACK])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(questionary, "confirm", _confirm)
    monkeypatch.setattr(onboard_commands, "_configured_providers", lambda: ["mistral"])

    onboard_commands._manage_existing_providers(non_interactive=False)

    assert asked, "removing the provider behind the default model asked nothing"
    assert "default model" in asked[0].lower() or "默认模型" in asked[0]
    # Declined, so the key is still there.
    assert json.loads(tmp_env.read_text())["providers"]["mistral"].get("apiKey") == "sk-mistral"
