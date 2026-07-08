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
    assert data["agents"]["defaults"]["model"] == "openai/gpt-4o-mini"


def test_onboard_non_interactive_skips_optional_steps(
    tmp_env: Path, everos_isolated: Path, stub_verify, stub_step3
) -> None:
    """Non-interactive mode auto-skips sandbox / channel / memory steps.

    ``everos_isolated`` keeps ``_memory_enabled`` from reading the dev
    machine's real ``~/.everos/config.toml``: the seeded backend="everos" is
    only kept when both required models (llm + embedding) are configured, so an
    empty (isolated) EverOS config makes the skip-guard resolve it back to None.
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

    r = runner.invoke(app, ["onboard"])
    assert r.exit_code == 0, r.stdout

    data = json.loads(tmp_env.read_text())
    assert data["providers"]["anthropic"]["apiKey"] == "sk-int-test"
    assert data["agents"]["defaults"]["model"] == "anthropic/claude-sonnet-4-5"


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
    assert data["agents"]["defaults"]["model"] == "anthropic/claude-sonnet-4-5"


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

    cfg = tmp_path / ".everos" / "config.toml"
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
    """Enabling memory + LLM (custom source) + embedding (reuse LLM endpoint)
    writes the EverOS toml; rerank/multimodal skipped."""
    import tomllib

    import questionary

    _seed_provider("openrouter", "sk-or", "openrouter/anthropic/claude-sonnet-4-5")

    # _step4_memory select() calls, in order:
    #   1. enable memory                -> "on"
    #   2. LLM source picker            -> ("custom",)
    #   3. embedding source picker      -> ("reuse_llm",)
    #   4. rerank "Configure it?"       -> "skip"
    #   5. multimodal "Configure it?"   -> "skip"
    select_answers = iter(["on", ("custom",), ("reuse_llm",), "skip", "skip"])
    # text(): LLM base_url, LLM model, embedding model (model lists can't be
    # fetched offline, so the picker falls back to free-text entry).
    text_answers = iter(["https://llm/v1", "mem-llm", "mem-embed"])
    # password(): LLM api key.
    password_answers = iter(["k-llm"])

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ(next(text_answers)))
    monkeypatch.setattr(questionary, "password", lambda *a, **kw: _FQ(next(password_answers)))
    # No network: model list can't be fetched → free-text entry; probes succeed.
    monkeypatch.setattr(onboard_commands, "_fetch_everos_models", lambda *a, **kw: None)
    monkeypatch.setattr(onboard_commands, "_probe_everos_chat", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(onboard_commands, "_probe_everos_embedding", lambda *a, **kw: (True, "ok"))

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
    # embedding reused the LLM endpoint's key/base.
    assert everos["embedding"]["api_key"] == "k-llm"
    assert everos["embedding"]["base_url"] == "https://llm/v1"
    assert "rerank" not in everos
    assert "multimodal" not in everos


class _FakeResp:
    def __init__(self, status_code: int, payload, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **kw):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def test_probe_everos_chat_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chat probe never raises and only reports success on a real completion:
    a non-dict body, missing/empty choices, non-200, non-JSON, and network errors
    all read as failure."""
    import httpx

    def _probe(resp):
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _FakeClient(resp))
        return onboard_commands._probe_everos_chat("m", api_key="k", base_url="https://x/v1")

    assert _probe(_FakeResp(200, {"choices": [{"message": {"content": "hi"}}]})) == (True, "ok")
    assert _probe(_FakeResp(200, {"choices": []}))[0] is False
    # A non-dict choice item (e.g. an error string) is not a false green.
    assert _probe(_FakeResp(200, {"choices": ["error: model not found"]}))[0] is False
    assert _probe(_FakeResp(200, []))[0] is False  # top-level non-dict, no crash
    ok, detail = _probe(_FakeResp(400, None, text="model not found"))
    assert ok is False and "400" in detail
    assert _probe(_FakeResp(200, ValueError("no json")))[0] is False
    assert _probe(httpx.HTTPError("boom"))[0] is False


def test_probe_everos_malformed_url_never_raises() -> None:
    """A malformed base_url raises ``httpx.InvalidURL``, which is not an
    ``HTTPError`` subclass — both probes must catch it and report failure rather
    than crash the wizard (honoring the "never raises" contract)."""
    assert onboard_commands._probe_everos_chat("m", api_key="k", base_url="http://[::1")[0] is False
    assert onboard_commands._probe_everos_embedding("m", api_key="k", base_url="http://[::1")[0] is False
    # The model-list fetch (used by the llm/rerank/multimodal pickers) shares the
    # contract: a malformed base_url must read as "no list", not crash.
    assert onboard_commands._fetch_everos_models("http://[::1", "k") is None


def test_probe_everos_embedding_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embedding probe never raises and only reports success on a real vector:
    an error string containing "embedding", a non-dict item, a non-dict/non-JSON
    body all read as failure, not crash."""
    import httpx

    def _probe(resp):
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _FakeClient(resp))
        return onboard_commands._probe_everos_embedding("m", api_key="k", base_url="https://x/v1")

    assert _probe(_FakeResp(200, {"data": [{"embedding": [0.1, 0.2]}]})) == (True, "ok")
    # A chat endpoint whose error text contains "embedding" is not a false green.
    assert _probe(_FakeResp(200, {"data": ["embedding not supported for chat models"]}))[0] is False
    assert _probe(_FakeResp(200, {"data": [42]}))[0] is False  # non-dict item, no crash
    assert _probe(_FakeResp(200, []))[0] is False  # top-level non-dict, no crash
    ok, detail = _probe(_FakeResp(400, None, text="bad model"))
    assert ok is False and "400" in detail
    assert _probe(_FakeResp(200, ValueError("no json")))[0] is False
    assert _probe(httpx.HTTPError("boom"))[0] is False


def test_memory_enabled_requires_both_llm_and_embedding(tmp_env: Path, everos_isolated: Path) -> None:
    """A half-configured EverOS (llm only, embedding missing) is not "enabled" —
    both required roles must be on disk."""
    from raven.config.update_everos import set_everos_section

    onboard_commands._set_memory_backend("everos")
    set_everos_section("llm", {"model": "m-llm", "api_key": "k", "base_url": "https://x/v1"})
    assert onboard_commands._memory_enabled() is False
    set_everos_section("embedding", {"model": "m-emb", "api_key": "k", "base_url": "https://x/v1"})
    assert onboard_commands._memory_enabled() is True


def test_prompt_api_key_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pasted key with surrounding whitespace/newline is stripped, so it can't
    produce an illegal ``Authorization: Bearer \\nsk-...`` header downstream."""
    import questionary

    class _FQ:
        def ask(self):
            return "  sk-abc12345\n"

    monkeypatch.setattr(questionary, "password", lambda *a, **kw: _FQ())
    assert onboard_commands._prompt_api_key("openai") == "sk-abc12345"


def test_prompt_base_url_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Base URL input is stripped of surrounding whitespace/newline."""
    import questionary

    class _FQ:
        def ask(self):
            return "  https://host/v1 \n"

    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ())
    assert onboard_commands._prompt_base_url() == "https://host/v1"


def test_memory_llm_probe_failure_reprompts_then_persists(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model the endpoint doesn't serve fails the real chat probe; Re-enter
    loops back and only a probe-passing model is persisted (no false green)."""
    import tomllib

    from raven.config.update_providers import set_provider_fields

    set_provider_fields("openai", {"api_key": "sk-main", "api_base": "https://api.openai.com/v1"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    # picker -> reuse main model; failure menu -> Re-enter; picker -> reuse again.
    select_answers = iter([("reuse_main",), "rekey", ("reuse_main",)])
    probe_results = iter([(False, "model not served by this endpoint"), (True, "ok")])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(onboard_commands, "_probe_everos_chat", lambda *a, **kw: next(probe_results))

    warnings: list[str] = []
    onboard_commands._config_everos_role(
        section="llm", main_model="openai/gpt-4o-mini", non_interactive=False, warnings=warnings
    )
    with everos_isolated.open("rb") as f:
        everos = tomllib.load(f)
    assert everos["llm"]["model"] == "gpt-4o-mini"
    assert warnings == []


def test_memory_embedding_probe_failure_rejects_and_reprompts(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat-only endpoint fails the real embedding probe; Re-enter loops back
    and only a probe-passing model is persisted (no false green)."""
    import tomllib

    from raven.config.update_everos import set_everos_section

    set_everos_section("llm", {"model": "m", "api_key": "k-llm", "base_url": "https://llm/v1"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    select_answers = iter([("reuse_llm",), "rekey", ("reuse_llm",)])
    text_answers = iter(["deepseek-chat", "text-embedding-3-small"])
    probe_results = iter([(False, "model does not support embeddings"), (True, "ok")])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ(next(text_answers)))
    monkeypatch.setattr(onboard_commands, "_fetch_everos_models", lambda *a, **kw: None)
    monkeypatch.setattr(onboard_commands, "_probe_everos_embedding", lambda *a, **kw: next(probe_results))

    warnings: list[str] = []
    onboard_commands._config_everos_role(
        section="embedding", main_model="deepseek/deepseek-chat", non_interactive=False, warnings=warnings
    )
    with everos_isolated.open("rb") as f:
        everos = tomllib.load(f)
    assert everos["embedding"]["model"] == "text-embedding-3-small"
    assert warnings == []


def test_memory_embedding_probe_failure_continue_records_warning(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Continue-anyway on a failed embedding probe persists but flags the endpoint
    in ``warnings`` (the old connectivity check silently reported a false green)."""
    import tomllib

    from raven.config.update_everos import set_everos_section

    set_everos_section("llm", {"model": "m", "api_key": "k-llm", "base_url": "https://llm/v1"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    select_answers = iter([("reuse_llm",), "continue"])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ("deepseek-chat"))
    monkeypatch.setattr(onboard_commands, "_fetch_everos_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        onboard_commands, "_probe_everos_embedding", lambda *a, **kw: (False, "model does not support embeddings")
    )

    warnings: list[str] = []
    onboard_commands._config_everos_role(
        section="embedding", main_model="deepseek/deepseek-chat", non_interactive=False, warnings=warnings
    )
    assert warnings == ["Memory embedding"]
    with everos_isolated.open("rb") as f:
        everos = tomllib.load(f)
    assert everos["embedding"]["model"] == "deepseek-chat"


def test_memory_embedding_step_shows_capability_hint(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The embedding step prints the capability-guidance line so a chat-only-LLM
    user is told upfront to pick an embedding-capable provider."""
    from raven.config.update_everos import set_everos_section

    set_everos_section("llm", {"model": "m", "api_key": "k-llm", "base_url": "https://llm/v1"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(("reuse_llm",)))
    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ("text-embedding-3-small"))
    monkeypatch.setattr(onboard_commands, "_fetch_everos_models", lambda *a, **kw: None)
    monkeypatch.setattr(onboard_commands, "_probe_everos_embedding", lambda *a, **kw: (True, "ok"))

    printed: list[str] = []
    monkeypatch.setattr(onboard_commands.console, "print", lambda *a, **kw: printed.append(" ".join(str(x) for x in a)))
    onboard_commands._config_everos_role(
        section="embedding", main_model="deepseek/deepseek-chat", non_interactive=False, warnings=[]
    )
    blob = "\n".join(printed)
    assert "embedding-capable" in blob
    assert "DashScope" in blob


def test_custom_provider_sends_test_probe(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom provider now sends the real test message (was previously trusted
    without one) — a wrong base_url/model surfaces here, not at first chat."""
    from raven.config.update_providers import set_provider_fields
    from raven.providers.registry import find_by_name

    spec = find_by_name("custom")
    set_provider_fields("custom", {"api_key": "k", "api_base": "https://x/v1"})
    monkeypatch.setattr(onboard_commands, "_verify_provider", lambda p: (True, "ok", None))
    calls: list[bool] = []

    def _probe():
        calls.append(True)
        return ("hi", 1, 0.1)

    monkeypatch.setattr(onboard_commands, "send_probe", _probe)
    out = onboard_commands._resolve_model_with_test(
        spec, is_custom=True, custom_model="my/model", user_model_flag=None, non_interactive=False, warnings=[]
    )
    assert calls == [True]  # the probe ran for the custom provider
    assert out == "my/model"


def test_custom_provider_probe_failure_switch_rewinds(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a custom-provider probe failure, choosing Switch returns None so the
    caller rewinds to the provider picker."""
    from raven.config.update_providers import set_provider_fields
    from raven.providers.registry import find_by_name

    spec = find_by_name("custom")
    set_provider_fields("custom", {"api_key": "k", "api_base": "https://x/v1"})
    monkeypatch.setattr(onboard_commands, "_verify_provider", lambda p: (True, "ok", None))

    def _boom():
        raise RuntimeError("model not found")

    monkeypatch.setattr(onboard_commands, "send_probe", _boom)
    monkeypatch.setattr(onboard_commands, "_failure_choice", lambda opts, **kw: "switch")
    out = onboard_commands._resolve_model_with_test(
        spec, is_custom=True, custom_model="m", user_model_flag=None, non_interactive=False, warnings=[]
    )
    assert out is None


def test_test_probe_failure_menu_offers_rekey_and_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test-message failure now offers re-enter-key and switch-provider (not
    just retry/repick/continue), matching the connectivity-failure menu."""
    captured: dict[str, list[str]] = {}

    def _cap(opts, **kw):
        captured["vals"] = [v for _, v in opts]
        return "continue"

    monkeypatch.setattr(onboard_commands, "_failure_choice", _cap)

    def _boom():
        raise RuntimeError("x")

    monkeypatch.setattr(onboard_commands, "send_probe", _boom)
    onboard_commands._run_test_probe("openai", non_interactive=False, warnings=[])
    assert {"retry", "repick", "rekey", "switch", "continue"} <= set(captured["vals"])


def test_pick_model_empty_submit_uses_default_not_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing the prefilled default and submitting empty falls back to the
    default model rather than killing the whole wizard."""
    import questionary

    from raven.providers.registry import find_by_name

    spec = find_by_name("deepseek")

    class _FQ:
        def ask(self):
            return ""  # user cleared the prefilled default, then hit Enter

    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ())
    result = onboard_commands._pick_model(
        spec, current_model=None, model_ids=None, user_provided_model=None, non_interactive=False
    )
    assert result == "deepseek/deepseek-chat"


def test_pick_model_ctrl_c_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C (questionary returns None) still exits — only empty submit is
    softened to the default."""
    import questionary
    import typer

    from raven.providers.registry import find_by_name

    spec = find_by_name("deepseek")

    class _FQ:
        def ask(self):
            return None

    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ())
    with pytest.raises(typer.Exit):
        onboard_commands._pick_model(
            spec, current_model=None, model_ids=None, user_provided_model=None, non_interactive=False
        )


def test_config_everos_role_required_giveup_returns_abort(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A required role (embedding) backing out of the picker is not an
    inescapable loop: it offers a bounded 'give up EverOS' exit that signals
    ``_ABORT_EVEROS`` up to the caller."""
    from raven.config.update_everos import set_everos_section

    set_everos_section("llm", {"model": "m", "api_key": "k-llm", "base_url": "https://llm/v1"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    # source picker -> Back (_BACK); required-role bounded-exit prompt -> abort.
    select_answers = iter([onboard_commands._BACK, "abort"])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    out = onboard_commands._config_everos_role(
        section="embedding", main_model="deepseek/deepseek-chat", non_interactive=False, warnings=[]
    )
    assert out is onboard_commands._ABORT_EVEROS


def test_step4_giveup_embedding_keeps_markdown(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Giving up a required role mid-step disables EverOS and keeps Markdown
    memory rather than flipping the backend on half-configured."""
    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ("on"))  # enable EverOS

    def _fake_role(*, section, **kw):
        return onboard_commands._ABORT_EVEROS if section == "embedding" else None

    monkeypatch.setattr(onboard_commands, "_config_everos_role", _fake_role)
    onboard_commands._step4_memory(skip=False, non_interactive=False, main_model="deepseek/deepseek-chat", warnings=[])
    from raven.config.raven import load_raven_config

    assert load_raven_config().memory.backend != "everos"


def test_memory_embedding_backout_prints_switch_guidance(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-submitting the embedding model picker prints actionable guidance
    every round (not a silent bounce), so re-picking the same chat-only reuse
    endpoint isn't a dead end."""
    from raven.config.update_everos import set_everos_section

    set_everos_section("llm", {"model": "m", "api_key": "k-llm", "base_url": "https://llm/v1"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    # source picker -> reuse llm; (model empty -> back, guidance printed) -> source
    # picker -> Back; required-role bounded exit -> abort.
    select_answers = iter([("reuse_llm",), onboard_commands._BACK, "abort"])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ(""))  # empty model submit -> _BACK
    monkeypatch.setattr(onboard_commands, "_fetch_everos_models", lambda *a, **kw: None)

    printed: list[str] = []
    monkeypatch.setattr(onboard_commands.console, "print", lambda *a, **kw: printed.append(" ".join(str(x) for x in a)))
    out = onboard_commands._config_everos_role(
        section="embedding", main_model="deepseek/deepseek-chat", non_interactive=False, warnings=[]
    )
    blob = "\n".join(printed)
    assert "embedding-capable" in blob
    assert out is onboard_commands._ABORT_EVEROS


def test_memory_embedding_verify_fail_reprints_switch_hint(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed /embeddings verify reprints the switch-provider nudge before
    looping back, so a retry isn't a blind loop."""
    from raven.config.update_everos import set_everos_section

    set_everos_section("llm", {"model": "m", "api_key": "k-llm", "base_url": "https://llm/v1"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    # source -> reuse llm; verify fails -> failure menu "rekey"; source -> Back;
    # required-role bounded exit -> abort.
    select_answers = iter([("reuse_llm",), "rekey", onboard_commands._BACK, "abort"])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ("text-embedding-3-small"))
    monkeypatch.setattr(onboard_commands, "_probe_everos_embedding", lambda *a, **kw: (False, "HTTP 404: "))

    printed: list[str] = []
    monkeypatch.setattr(onboard_commands.console, "print", lambda *a, **kw: printed.append(" ".join(str(x) for x in a)))
    out = onboard_commands._config_everos_role(
        section="embedding", main_model="deepseek/deepseek-chat", non_interactive=False, warnings=[]
    )
    blob = "\n".join(printed)
    assert "embedding-capable" in blob
    assert out is onboard_commands._ABORT_EVEROS


def test_memory_embedding_skips_model_list_fetch(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Embedding never fetches/shows the ``/models`` list: on a reused chat
    endpoint those are chat models, misleading as embedding candidates. The id is
    entered directly and the ``/embeddings`` verify is the arbiter."""
    import tomllib

    from raven.config.update_everos import set_everos_section

    set_everos_section("llm", {"model": "m", "api_key": "k-llm", "base_url": "https://llm/v1"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    fetch_calls: list[bool] = []

    def _spy_fetch(*a, **kw):
        fetch_calls.append(True)
        return ["deepseek-chat", "deepseek-reasoner"]  # even if it would return, must not be called

    monkeypatch.setattr(onboard_commands, "_fetch_everos_models", _spy_fetch)
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(("reuse_llm",)))
    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ("text-embedding-3-small"))
    monkeypatch.setattr(onboard_commands, "_probe_everos_embedding", lambda *a, **kw: (True, "ok"))

    onboard_commands._config_everos_role(
        section="embedding", main_model="deepseek/deepseek-chat", non_interactive=False, warnings=[]
    )
    assert fetch_calls == []  # embedding must not fetch the /models list
    with everos_isolated.open("rb") as f:
        everos = tomllib.load(f)
    assert everos["embedding"]["model"] == "text-embedding-3-small"


def test_memory_llm_reuse_pulls_provider_creds(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse-main-model writes the provider's stored key/base into the LLM section."""
    import tomllib

    from raven.config.update_providers import set_provider_fields

    set_provider_fields("openai", {"api_key": "sk-main", "api_base": "https://api.openai.com/v1"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    # openai IS OpenAI-compatible → the source picker offers "reuse main chat
    # model", which brings the model id + creds along (no further prompts).
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(("reuse_main",)))
    monkeypatch.setattr(onboard_commands, "_probe_everos_chat", lambda *a, **kw: (True, "ok"))

    onboard_commands._config_everos_role(
        section="llm", main_model="openai/gpt-4o-mini", non_interactive=False, warnings=[]
    )
    with everos_isolated.open("rb") as f:
        everos = tomllib.load(f)
    # Reuse strips the litellm route prefix to the bare model id EverOS sends.
    assert everos["llm"]["model"] == "gpt-4o-mini"
    assert everos["llm"]["api_key"] == "sk-main"
    assert everos["llm"]["base_url"] == "https://api.openai.com/v1"


def test_memory_rerank_reuse_llm_endpoint(
    tmp_env: Path, everos_isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rerank can reuse the memory LLM's endpoint via the source picker."""
    import tomllib

    from raven.config.update_everos import set_everos_section

    set_everos_section("llm", {"model": "m", "api_key": "k-llm", "base_url": "https://llm/v1"})

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    # rerank "Configure it?" -> redo; source -> reuse the LLM endpoint;
    # rerank service type -> deepinfra.
    select_answers = iter(["redo", ("reuse_llm",), "deepinfra"])
    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(next(select_answers)))
    monkeypatch.setattr(questionary, "text", lambda *a, **kw: _FQ("rerank-model"))
    # Offline → model list can't be fetched, falls back to the free-text id.
    monkeypatch.setattr(onboard_commands, "_fetch_everos_models", lambda *a, **kw: None)

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
    assert everos["rerank"]["api_key"] == "k-llm"  # reused, not re-prompted
    assert everos["rerank"]["base_url"] == "https://llm/v1"


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
    """A custom endpoint's bare model is reusable; reuse pulls its api_base/key
    and keeps the bare id (regression: custom users were forced to re-enter)."""
    from raven.config.update_providers import set_provider_fields

    set_provider_fields("custom", {"api_key": "sk-cust", "api_base": "https://my-llm/v1"})
    # Bare model id (no prefix) — this is how a custom default model is stored.
    assert onboard_commands._model_is_openai_compatible("qwen-max")

    creds = onboard_commands._resolve_reuse_llm_creds("qwen-max")
    assert creds["model"] == "qwen-max"  # bare id used as-is, not stripped
    assert creds["api_key"] == "sk-cust"
    assert creds["base_url"] == "https://my-llm/v1"

    # And the LLM reuse path writes those into the EverOS toml.
    import tomllib

    import questionary

    class _FQ:
        def __init__(self, a):
            self._a = a

        def ask(self):
            return self._a

    monkeypatch.setattr(questionary, "select", lambda *a, **kw: _FQ(("reuse_main",)))
    monkeypatch.setattr(onboard_commands, "_probe_everos_chat", lambda *a, **kw: (True, "ok"))
    onboard_commands._config_everos_role(section="llm", main_model="qwen-max", non_interactive=False, warnings=[])
    with everos_isolated.open("rb") as fh:
        everos = tomllib.load(fh)
    assert everos["llm"] == {
        "model": "qwen-max",
        "api_key": "sk-cust",
        "base_url": "https://my-llm/v1",
    }


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
    monkeypatch.setattr(onboard_commands, "_scancode_login", lambda c: routed.append(c))
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

    onboard_commands.run_wizard(non_interactive=False)

    # Provider + model were written despite the first Back — config is populated,
    # so the gate would NOT re-trigger (no infinite loop).
    data = json.loads(tmp_env.read_text())
    assert data["providers"]["openai"]["apiKey"] == "sk-back-test"
    assert data["agents"]["defaults"]["model"] == "openai/gpt-4o-mini"
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

    # Should complete (not raise typer.Exit) — steps 2/3/4 ran.
    onboard_commands.run_wizard(non_interactive=False)
    data = json.loads(tmp_env.read_text())
    # Switched to openai; its key written, default model is openai's.
    assert data["providers"]["openai"]["apiKey"] == "sk-openai"
    assert data["agents"]["defaults"]["model"] == "openai/gpt-4o-mini"


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
