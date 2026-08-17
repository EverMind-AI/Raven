"""CLI tests for ``raven onboard`` — the three-step wizard.

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
        "--non-interactive",
        "--yes",
        "--reset",
    ):
        assert flag in out, f"missing flag in help: {flag}"


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
        lambda spec, **_: spec.default_model,
    )
    # Optional steps 2-4 are covered separately; no-op them here so the
    # interactive Step 1 path can be asserted without driving every screen.
    monkeypatch.setattr(onboard_commands, "_step2_sandbox", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step3_channel", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step4_memory", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_deep_research", lambda **_: None)

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

    r = runner.invoke(app, ["onboard"])
    assert r.exit_code == 0, r.stdout

    # Catalog feeds the picker. The schema's pre-existing default model
    # (``anthropic/claude-opus-4-5``) routes to anthropic by prefix, so it
    # gets prepended as the "keep current" candidate.
    assert captured_choices["choices"] == [
        "anthropic/claude-opus-4-5",
        "claude-haiku-4-5",
        "claude-sonnet-4-5",
        "claude-opus-4-5",
    ]
    assert captured_choices["default"] == "anthropic/claude-opus-4-5"
    # User's pick made it into config
    data = json.loads(tmp_env.read_text())
    assert data["agents"]["defaults"]["model"] == "claude-haiku-4-5"


def test_format_model_for_provider_prefix_rules() -> None:
    """Provider's ``litellm_prefix`` is applied unless model_id already has one."""
    from raven.providers.registry import find_by_name

    openrouter = find_by_name("openrouter")
    deepseek = find_by_name("deepseek")
    openai = find_by_name("openai")

    # Gateway with prefix: bare id gets prefixed
    assert (
        onboard_commands._format_model_for_provider(openrouter, "anthropic/claude-sonnet-4-5")
        == "openrouter/anthropic/claude-sonnet-4-5"
    )
    # Already prefixed by us → idempotent
    assert (
        onboard_commands._format_model_for_provider(openrouter, "openrouter/anthropic/claude-sonnet-4-5")
        == "openrouter/anthropic/claude-sonnet-4-5"
    )
    # Direct provider with empty prefix → pass-through
    assert onboard_commands._format_model_for_provider(openai, "gpt-4o-mini") == "gpt-4o-mini"
    # skip_prefixes match → no double-prefix
    assert onboard_commands._format_model_for_provider(deepseek, "deepseek/deepseek-chat") == "deepseek/deepseek-chat"
    assert onboard_commands._format_model_for_provider(deepseek, "deepseek-chat") == "deepseek/deepseek-chat"


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
    ):
        spec = find_by_name(name)
        assert spec is not None, f"missing provider in registry: {name}"
        assert spec.default_model, f"{name} has empty default_model"


# --------------------------------------------------------------------------- fixtures (4-step)


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
    monkeypatch.setattr(onboard_commands, "_pick_model", lambda spec, **_: spec.default_model)
    # Optional steps are no-ops here; we only assert Step 1 wasn't skipped.
    monkeypatch.setattr(onboard_commands, "_step2_sandbox", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step3_channel", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step4_memory", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_deep_research", lambda **_: None)

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
    monkeypatch.setattr(onboard_commands, "_pick_model", lambda spec, **_: spec.default_model)
    # On the failure submenu, choose "switch".
    monkeypatch.setattr(onboard_commands, "_failure_choice", lambda options, *, non_interactive: "switch")
    monkeypatch.setattr(onboard_commands, "_step2_sandbox", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step3_channel", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step4_memory", lambda **_: None)
    monkeypatch.setattr(onboard_commands, "_step5_deep_research", lambda **_: None)

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
    monkeypatch.setattr(onboard_commands, "_pick_model", lambda spec, **_: spec.default_model)

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
    assert data["plugins"]["config"]["everos-memory"]["mode"] == "embedded"
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
    assert data["plugins"]["config"]["everos-memory"]["mode"] == "embedded"
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


def test_total_steps_is_five() -> None:
    # Adding the deep_research step bumped the wizard from 4 to 5; the progress
    # dots + "Step n/N" header derive from this constant.
    assert onboard_commands._TOTAL_STEPS == 5


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
