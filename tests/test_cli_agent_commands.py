"""CLI tests for ``raven agent``.

The ``agent`` command is a one-shot ``-m`` single-turn runner (the
interactive REPL was removed; ``raven tui`` is the interactive front-end).
Smoke-level coverage: ``--help`` works, options are surfaced, the
``no-API-key`` path exits cleanly, bare invocation points at the TUI.
"""

from __future__ import annotations

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


def test_agent_help_works() -> None:
    """``raven agent --help`` lists the key options."""
    r = runner.invoke(app, ["agent", "--help"])
    assert r.exit_code == 0
    assert "one-shot agent turn" in r.stdout
    # core options surfaced
    assert "--message" in r.stdout
    assert "--session" in r.stdout
    assert "--workspace" in r.stdout
    assert "--config" in r.stdout
    assert "--markdown" in r.stdout


def test_agent_without_message_prints_pointer_and_exits_nonzero(tmp_config: Path) -> None:
    """Bare ``raven agent`` no longer enters an interactive loop: it points
    at ``raven tui`` / ``agent -m`` and exits non-zero."""
    r = runner.invoke(app, ["agent"])
    assert r.exit_code != 0
    assert "REPL was removed" in r.stdout
    assert "raven tui" in r.stdout
    assert "-m" in r.stdout


def test_agent_without_api_key_exits_cleanly(tmp_config: Path) -> None:
    """With no provider configured, the command must exit non-zero — and
    crucially must not raise a *crash* exception (NameError / AttributeError /
    ImportError). ``typer.testing.CliRunner`` captures the exception, so the
    only reliable way to detect a regression like a missing import is to
    inspect ``r.exception`` directly.
    """
    from raven.config.loader import save_config
    from raven.config.schema import Config

    save_config(Config())  # default config, no keys

    r = runner.invoke(app, ["agent", "-m", "hello"])
    # Reject any crash-class exception: those signal a refactor regression,
    # not user error. typer.Exit(...) is fine (intentional non-zero exit).
    if r.exception is not None:
        assert not isinstance(r.exception, (NameError, AttributeError, ImportError)), (
            f"Crash-class exception leaked through: {r.exception!r}"
        )
    assert r.exit_code != 0


# ============================================================================
# Session binding flags (task 3.3)
# ============================================================================


def test_agent_help_shows_continue_flag() -> None:
    """--continue flag appears in agent --help."""
    r = runner.invoke(app, ["agent", "--help"])
    assert r.exit_code == 0
    assert "--continue" in r.stdout


def test_agent_help_shows_resume_flag() -> None:
    """--resume flag appears in agent --help."""
    r = runner.invoke(app, ["agent", "--help"])
    assert r.exit_code == 0
    assert "--resume" in r.stdout


def _invoke_agent_capturing_session(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, extra_args: list[str]
) -> tuple[object, dict[str, str]]:
    """Run ``agent -m`` with the provider and AgentLoop stubbed out, capturing
    the session_id that reaches the spine turn (req.conversation is the session
    key, mirroring the old session_key arg)."""
    import os as _os

    from raven.config.loader import save_config
    from raven.config.schema import Config
    from raven.spine import Text, TurnOutcome, Usage

    cfg = Config()
    cfg.providers.openrouter.api_key = "stub-test-key"
    save_config(cfg)

    captured: dict[str, str] = {}

    class _StubSubagents:
        def set_submit(self, _submit) -> None:
            pass

    class _StubAgentLoop:
        def __init__(self, **kwargs):
            self.channels_config = kwargs.get("channels_config")
            self.subagents = _StubSubagents()

        def configure_personalization(self, *_args) -> None:
            pass

        async def run_turn(self, req, emit, drain, *, stream, **_kw) -> TurnOutcome:
            captured["session_id"] = req.conversation
            await emit(Text(content="stub-response", source=req.source))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

        async def await_pending_extractions(self, **_kw) -> None:
            pass

        async def close_mcp(self) -> None:
            pass

    # The -m path hard-exits via os._exit(0) (torch segfault guard); make it a
    # catchable SystemExit so the CliRunner sees a clean exit instead of the
    # whole pytest process dying.
    monkeypatch.setattr(_os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    monkeypatch.setattr("raven.cli.agent_commands.make_provider", lambda _: object())
    monkeypatch.setattr("raven.agent.loop.AgentLoop", _StubAgentLoop)
    # This test exercises session keying, not memory: don't boot the real
    # (bundled) everos backend / plugin tools inside the CliRunner (the
    # embedded everos runtime is heavy and not under test here).
    monkeypatch.setattr(
        "raven.cli.agent_commands.maybe_build_memory_backend",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "raven.cli.agent_commands.build_plugin_tools",
        lambda *a, **k: [],
    )
    r = runner.invoke(app, ["agent", "-m", "hi", "-w", str(workspace), *extra_args])
    return r, captured


def test_agent_default_mints_fresh_session(tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare ``agent -m`` mints a fresh ``cli:{chat_id}`` per invocation."""
    import re

    ws = tmp_path / "ws"
    ws.mkdir()

    r1, cap1 = _invoke_agent_capturing_session(monkeypatch, ws, [])
    assert r1.exit_code == 0, r1.stdout
    assert re.fullmatch(r"cli:\d{8}_\d{6}_[0-9a-f]{6}", cap1["session_id"]), (
        f"expected freshly minted cli session key, got {cap1['session_id']!r}"
    )

    r2, cap2 = _invoke_agent_capturing_session(monkeypatch, ws, [])
    assert r2.exit_code == 0
    assert cap1["session_id"] != cap2["session_id"], "each bare invocation must mint a NEW session"


def test_agent_continue_binds_most_recent_cli_session(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-c`` binds the agent to the most-recent persisted cli session."""
    from raven.session.manager import SessionManager

    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SessionManager(ws)
    seeded = "20990101_000000_aaaaaa"
    s = mgr.get_or_create(f"cli:{seeded}")
    s.add_message("user", "earlier turn")
    mgr.save(s)

    r, captured = _invoke_agent_capturing_session(monkeypatch, ws, ["-c"])
    assert r.exit_code == 0, r.stdout
    assert captured["session_id"] == f"cli:{seeded}"


def test_agent_resume_binds_resolved_session(tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--resume <prefix>`` resolves and binds that cli session."""
    from raven.session.manager import SessionManager

    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SessionManager(ws)
    seeded = "20990101_000000_bbbbbb"
    s = mgr.get_or_create(f"cli:{seeded}")
    s.add_message("user", "earlier turn")
    mgr.save(s)

    r, captured = _invoke_agent_capturing_session(monkeypatch, ws, ["--resume", seeded[:20]])
    assert r.exit_code == 0, r.stdout
    assert captured["session_id"] == f"cli:{seeded}"


def test_agent_session_key_passthrough(tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--session <key>`` passes a full key through unchanged (any channel)."""
    ws = tmp_path / "ws"
    ws.mkdir()

    r, captured = _invoke_agent_capturing_session(monkeypatch, ws, ["--session", "feishu:ou_xyz"])
    assert r.exit_code == 0, r.stdout
    assert captured["session_id"] == "feishu:ou_xyz"


def test_agent_bare_session_resolves_cross_channel(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--session <bare id>`` resolves to an existing session on a non-cli
    channel — it must NOT be mis-routed to a colon-less/malformed key."""
    from raven.session.manager import SessionManager

    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SessionManager(ws)
    cid = "20990101_000000_cccccc"
    s = mgr.get_or_create(f"tui:{cid}")
    s.add_message("user", "earlier turn")
    mgr.save(s)

    r, captured = _invoke_agent_capturing_session(monkeypatch, ws, ["--session", cid])
    assert r.exit_code == 0, r.stdout
    assert captured["session_id"] == f"tui:{cid}"


def test_agent_unknown_bare_session_falls_back_to_cli(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--session <bare id>`` with no matching session falls back to a proper
    ``cli:<id>`` key — never a colon-less/malformed path."""
    ws = tmp_path / "ws"
    ws.mkdir()
    cid = "20990101_000000_dddddd"

    r, captured = _invoke_agent_capturing_session(monkeypatch, ws, ["--session", cid])
    assert r.exit_code == 0, r.stdout
    assert captured["session_id"] == f"cli:{cid}"


@pytest.mark.parametrize(
    "args",
    [
        ["-c", "--resume", "x"],
        ["--session", "cli:abc", "-c"],
        ["--session", "cli:abc", "--resume", "x"],
        ["--session", "cli:abc", "-c", "--resume", "x"],
    ],
)
def test_agent_session_binding_flags_mutually_exclusive(tmp_config: Path, args: list[str]) -> None:
    """More than one of --session/--continue/--resume exits with usage error."""
    r = runner.invoke(app, ["agent", "-m", "hi", *args])
    assert r.exit_code == 2, f"expected usage error, got {r.exit_code}: {r.stdout}"
    assert "mutually exclusive" in r.stdout


def test_agent_continue_without_prior_session_starts_fresh(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-c`` with no stored cli session prints a notice and mints fresh."""
    import re

    ws = tmp_path / "ws"
    ws.mkdir()

    r, captured = _invoke_agent_capturing_session(monkeypatch, ws, ["-c"])
    assert r.exit_code == 0, r.stdout
    assert re.fullmatch(r"cli:\d{8}_\d{6}_[0-9a-f]{6}", captured["session_id"])
    assert "no previous cli session" in r.stdout


# ============================================================================
# No cron on the one-shot path
# ============================================================================


def test_agent_message_mode_constructs_no_cron_service(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``agent -m`` must not build a CronService (and hence never registers
    CronTool): with the REPL gone this process is never a cron runner, so a
    cli-bound job it created would have nothing to fire it."""
    import raven.proactive_engine.schedulers.cron.service as cron_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    constructed: list[object] = []
    orig_init = cron_mod.CronService.__init__

    def _spy_init(self, *args, **kwargs) -> None:
        constructed.append(self)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(cron_mod.CronService, "__init__", _spy_init)
    r, _ = _invoke_agent_capturing_session(monkeypatch, ws, [])

    assert r.exit_code == 0, r.stdout
    assert constructed == [], "agent -m must not construct a CronService"


# ============================================================================
# Pure helper functions
# ============================================================================


def test_print_agent_response_with_markdown(capsys: pytest.CaptureFixture) -> None:
    """``_print_agent_response`` renders the body — markdown mode."""
    from raven.cli.agent_commands import _print_agent_response

    _print_agent_response("# hi", render_markdown=True)
    out = capsys.readouterr().out
    # rich's Markdown renderer typically prints the heading text
    assert "hi" in out


def test_print_agent_response_plain(capsys: pytest.CaptureFixture) -> None:
    """``_print_agent_response`` renders plain text — markdown disabled."""
    from raven.cli.agent_commands import _print_agent_response

    _print_agent_response("hello world", render_markdown=False)
    out = capsys.readouterr().out
    assert "hello world" in out


# ============================================================================
# agent -m single-shot mode (mocked)
# ============================================================================


def test_agent_message_mode_mocked_provider(tmp_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``agent -m 'hi'`` with a mocked provider must reach a clean exit
    (no traceback). We mock ``make_provider`` so the agent loop builds
    without contacting any LLM."""
    from raven.config.loader import save_config
    from raven.config.schema import Config

    # Save a config with the openrouter key so the gateway-validation passes.
    cfg = Config()
    cfg.providers.openrouter.api_key = "stub-test-key"
    save_config(cfg)

    monkeypatch.setattr(
        "raven.cli.agent_commands.make_provider",
        lambda _: (_ for _ in ()).throw(RuntimeError("mock-no-provider")),
    )

    # We can't drive the full agent loop without a real provider; we only
    # assert that the CLI exits cleanly (no uncaught traceback) when the
    # provider build raises a controlled error.
    r = runner.invoke(app, ["agent", "-m", "hello"])
    if r.exception is not None:
        assert not isinstance(r.exception, (NameError, AttributeError, ImportError)), (
            f"Crash-class exception leaked through: {r.exception!r}"
        )


# ============================================================================
# Provider error rendering
# ============================================================================


def test_agent_auth_error_exit_nonzero_with_guidance(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 from the provider must exit non-zero, show a fix hint, and must
    not dump the duplicated exception name or the raw JSON error body.

    The provider is a real ``LiteLLMProvider`` whose ``acompletion`` is stubbed
    to raise the same exception shape litellm raises on an OpenRouter 401 —
    no network involved.
    """
    import os as _os

    from raven.config.loader import save_config
    from raven.config.schema import Config
    from raven.spine import Text, TurnOutcome, Usage

    cfg = Config()
    cfg.providers.openrouter.api_key = "sk-or-v1-stub-invalid"
    save_config(cfg)

    class AuthenticationError(Exception):
        status_code = 401

    async def _raise_401(**_kwargs):
        raise AuthenticationError(
            "litellm.AuthenticationError: AuthenticationError: OpenrouterException - "
            '{"error":{"message":"User not found.","code":401}}'
        )

    # Pre-register the env var with monkeypatch so the provider's _setup_env
    # write is rolled back with the test instead of leaking a fake key.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-stub-invalid")
    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", _raise_401)

    class _StubSubagents:
        def set_submit(self, _submit) -> None:
            pass

    class _AuthFailAgentLoop:
        def __init__(self, **kwargs):
            self.channels_config = kwargs.get("channels_config")
            self.subagents = _StubSubagents()

        def configure_personalization(self, *_args) -> None:
            pass

        async def run_turn(self, req, emit, drain, *, stream, **_kw) -> TurnOutcome:
            from raven.providers.litellm_provider import LiteLLMProvider

            provider = LiteLLMProvider(
                api_key="sk-or-v1-stub-invalid",
                default_model="anthropic/claude-opus-4-5",
                provider_name="openrouter",
            )
            resp = await provider.chat_with_retry(messages=[{"role": "user", "content": req.text}])
            await emit(Text(content=resp.content or "", source=req.source))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

        async def await_pending_extractions(self, **_kw) -> None:
            pass

        async def close_mcp(self) -> None:
            pass

    monkeypatch.setattr(_os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    monkeypatch.setattr("raven.cli.agent_commands.make_provider", lambda _: object())
    monkeypatch.setattr("raven.agent.loop.AgentLoop", _AuthFailAgentLoop)
    monkeypatch.setattr("raven.cli.agent_commands.maybe_build_memory_backend", lambda *a, **k: None)
    monkeypatch.setattr("raven.cli.agent_commands.build_plugin_tools", lambda *a, **k: [])

    ws = tmp_path / "ws"
    ws.mkdir()
    result = runner.invoke(app, ["agent", "-m", "hi", "-w", str(ws)])

    assert result.exit_code != 0
    assert result.output.count("AuthenticationError") <= 1
    assert "raven provider" in result.output
    assert '{"error"' not in result.output


def test_print_llm_error_renders_diagnosis_and_marks_exit(capsys: pytest.CaptureFixture) -> None:
    """``_print_llm_error`` turns the canonical error content into a
    diagnosis + fix hint and marks the one-shot exit code; ordinary reply
    content is left for the normal renderer."""
    from raven.cli import agent_commands
    from raven.providers.base import LLMProvider, format_llm_error

    class _AuthError(Exception):
        status_code = 401

    exc = _AuthError(
        "litellm.AuthenticationError: AuthenticationError: OpenrouterException - "
        '{"error":{"message":"User not found.","code":401}}'
    )
    content = format_llm_error(exc, LLMProvider.classify_error(exc), provider="openrouter")

    agent_commands._ONE_SHOT_EXIT["code"] = 0
    try:
        assert agent_commands._print_llm_error(content) is True
        out = capsys.readouterr().out
        assert "API key invalid" in out
        assert "openrouter 401" in out
        assert "raven provider test openrouter" in out
        assert '{"error"' not in out
        assert agent_commands._ONE_SHOT_EXIT["code"] == 1

        agent_commands._ONE_SHOT_EXIT["code"] = 0
        assert agent_commands._print_llm_error("just a normal reply") is False
        assert agent_commands._ONE_SHOT_EXIT["code"] == 0
    finally:
        agent_commands._ONE_SHOT_EXIT["code"] = 0
