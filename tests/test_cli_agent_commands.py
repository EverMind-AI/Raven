"""CLI tests for ``raven agent``.

The ``agent`` command is an interactive REPL with optional ``-m`` single-turn
mode. Smoke-level coverage: ``--help`` works, options are surfaced, the
``no-API-key`` path exits cleanly.
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
    assert "Interact with the agent" in r.stdout
    # core options surfaced
    assert "--message" in r.stdout
    assert "--session" in r.stdout
    assert "--workspace" in r.stdout
    assert "--config" in r.stdout
    assert "--markdown" in r.stdout


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


def test_agent_default_mints_fresh_session(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert cap1["session_id"] != cap2["session_id"], (
        "each bare invocation must mint a NEW session"
    )


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


def test_agent_resume_binds_resolved_session(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--resume <prefix>`` resolves and binds that cli session."""
    from raven.session.manager import SessionManager

    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SessionManager(ws)
    seeded = "20990101_000000_bbbbbb"
    s = mgr.get_or_create(f"cli:{seeded}")
    s.add_message("user", "earlier turn")
    mgr.save(s)

    r, captured = _invoke_agent_capturing_session(
        monkeypatch, ws, ["--resume", seeded[:20]]
    )
    assert r.exit_code == 0, r.stdout
    assert captured["session_id"] == f"cli:{seeded}"


def test_agent_session_key_passthrough(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--session <key>`` passes a full key through unchanged (any channel)."""
    ws = tmp_path / "ws"
    ws.mkdir()

    r, captured = _invoke_agent_capturing_session(
        monkeypatch, ws, ["--session", "feishu:ou_xyz"]
    )
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
def test_agent_session_binding_flags_mutually_exclusive(
    tmp_config: Path, args: list[str]
) -> None:
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
# --fake-now clock threading
# ============================================================================


def test_agent_fake_now_threads_now_fn_into_cron(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``agent --fake-now`` must thread now_fn into CronService so the
    past-schedule guard reads the simulated clock, not wall-time. Without it,
    an ``at`` reminder set under a back-dated --fake-now is rejected as "in the
    past" — the agent then leaks the real date and longrun trajectories rot.
    """
    import raven.proactive_engine.schedulers.cron.service as cron_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    captured: dict[str, object] = {}
    orig_init = cron_mod.CronService.__init__

    def _spy_init(self, *args, now_fn=None, **kwargs) -> None:
        captured["now_fn"] = now_fn
        orig_init(self, *args, now_fn=now_fn, **kwargs)

    monkeypatch.setattr(cron_mod.CronService, "__init__", _spy_init)
    _invoke_agent_capturing_session(monkeypatch, ws, ["--fake-now", "2026-05-01T08:00:00"])

    now_fn = captured.get("now_fn")
    assert now_fn is not None, "agent --fake-now did not thread now_fn into CronService"
    assert now_fn().strftime("%Y-%m-%d") == "2026-05-01"


def test_agent_without_fake_now_leaves_cron_on_wall_clock(
    tmp_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --fake-now → now_fn resolves to None so CronService keeps its
    real-clock default (production must be unaffected by the fake-clock wiring)."""
    import raven.proactive_engine.schedulers.cron.service as cron_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    captured: dict[str, object] = {"now_fn": "unset"}
    orig_init = cron_mod.CronService.__init__

    def _spy_init(self, *args, now_fn=None, **kwargs) -> None:
        captured["now_fn"] = now_fn
        orig_init(self, *args, now_fn=now_fn, **kwargs)

    monkeypatch.setattr(cron_mod.CronService, "__init__", _spy_init)
    _invoke_agent_capturing_session(monkeypatch, ws, [])

    assert captured["now_fn"] is None


# ============================================================================
# Pure helper functions (no REPL state needed)
# ============================================================================


@pytest.mark.parametrize(
    "command,expected",
    [
        ("exit", True),
        ("quit", True),
        ("/exit", True),
        ("/quit", True),
        (":q", True),
        ("EXIT", True),       # case-insensitive
        ("Quit", True),
        (" exit", False),     # leading whitespace not stripped
        ("hello", False),
        ("", False),
        ("exit later", False),
    ],
)
def test_is_exit_command(command: str, expected: bool) -> None:
    """``_is_exit_command`` detects the canonical exit triggers (case-insensitive)."""
    from raven.cli.agent_commands import _is_exit_command

    assert _is_exit_command(command) is expected


def test_exit_commands_set_contents() -> None:
    """The canonical exit triggers stay in sync with documented behavior."""
    from raven.cli.agent_commands import EXIT_COMMANDS

    assert EXIT_COMMANDS == {"exit", "quit", "/exit", "/quit", ":q"}


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


def test_agent_message_mode_mocked_provider(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


# ---------------------------------------------------------------------------
# REPL local slash commands (raven.cli._repl_slash)
#
# These run in-process and must NOT reach the LLM. The handler returns True
# when it consumed the input and False when the caller should forward it on.
# ---------------------------------------------------------------------------


class _RecordingConsole:
    """Captures only what ``_repl_slash`` itself prints. Delegated CLI
    commands print to their own module-level console (real stdout), which
    is irrelevant to the routing assertions here."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args: object, **_kwargs: object) -> None:
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def isolated_runtime(tmp_path: Path, monkeypatch) -> Path:
    """Point cron/sentinel runtime dirs at a tmp config so slash commands
    operate on throwaway state.

    ``set_config_path`` covers the read paths (cron/sentinel dirs derive from
    it). Config *writers* resolve the path via the update module's own
    ``get_config_path`` binding, so pin that too — otherwise a leaked
    monkeypatch from another test file could redirect our writes elsewhere.
    """
    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    monkeypatch.setattr("raven.config.update.get_config_path", lambda: cfg)
    yield tmp_path
    set_config_path(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["hello there", "/stop", "/restart", "/unknowntop"])
def test_slash_non_commands_fall_through(text: str) -> None:
    """Plain chat and bus-level commands (/stop, /restart) must fall through
    to the LLM/bus path — handler returns False."""
    from raven.cli._repl_slash import handle_repl_slash

    assert handle_repl_slash(text, console=_RecordingConsole()) is False


def test_slash_help_lists_namespaces() -> None:
    from raven.cli._repl_slash import handle_repl_slash

    con = _RecordingConsole()
    assert handle_repl_slash("/help", console=con) is True
    assert "/cron" in con.text and "/sentinel" in con.text


def test_cron_and_sentinel_help_handled() -> None:
    from raven.cli._repl_slash import handle_repl_slash

    con = _RecordingConsole()
    assert handle_repl_slash("/cron", console=con) is True
    assert handle_repl_slash("/sentinel help", console=con) is True
    assert "/cron list" in con.text
    assert "/sentinel status" in con.text


def test_cron_run_is_shell_only(isolated_runtime: Path) -> None:
    from raven.cli._repl_slash import handle_repl_slash

    con = _RecordingConsole()
    assert handle_repl_slash("/cron run abc123", console=con) is True
    assert "shell-only" in con.text


def test_cron_config_write_is_shell_only(isolated_runtime: Path) -> None:
    from raven.cli._repl_slash import handle_repl_slash

    con = _RecordingConsole()
    assert handle_repl_slash("/cron config set --forward-channels '*'", console=con) is True
    assert "shell-only" in con.text


def test_cron_list_runs_against_empty_store(isolated_runtime: Path) -> None:
    from raven.cli._repl_slash import handle_repl_slash

    # Delegates to cron_list; succeeds (no exception) on an empty store.
    assert handle_repl_slash("/cron list", console=_RecordingConsole()) is True


def _make_cron_job():
    from raven.config.paths import get_cron_dir
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.proactive_engine.schedulers.cron.types import CronSchedule

    svc = CronService(get_cron_dir() / "jobs.json", allowed_channels=None)
    job = svc.add_job(
        name="testjob",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hi",
        channel="cli",
        to="direct",
    )
    return svc, job


def test_cron_delete_requires_inline_yes(isolated_runtime: Path) -> None:
    """Without -y, destructive ops only preview and keep the job."""
    from raven.cli._repl_slash import handle_repl_slash

    svc, job = _make_cron_job()
    con = _RecordingConsole()
    assert handle_repl_slash(f"/cron delete {job.id}", console=con) is True
    assert "-y" in con.text  # asks for confirmation flag
    assert len(svc.list_jobs(include_disabled=True)) == 1  # not deleted


def test_cron_delete_with_yes_removes_job(isolated_runtime: Path) -> None:
    from raven.cli._repl_slash import handle_repl_slash
    from raven.config.paths import get_cron_dir
    from raven.proactive_engine.schedulers.cron.service import CronService

    svc, job = _make_cron_job()
    assert handle_repl_slash(f"/cron delete {job.id} -y", console=_RecordingConsole()) is True
    # Fresh instance: svc's cache only reloads on st_mtime change, which
    # has 1s granularity — the delete's rewrite can be invisible to it.
    fresh = CronService(get_cron_dir() / "jobs.json", allowed_channels=None)
    assert fresh.list_jobs(include_disabled=True) == []


@pytest.mark.parametrize(
    "cmd",
    [
        "/sentinel status",
        "/sentinel nudges -n 5",
        "/sentinel decisions",
        "/sentinel routines",
        "/sentinel attention",
        "/sentinel behaviors",
    ],
)
def test_sentinel_readonly_commands_handled(isolated_runtime: Path, cmd: str) -> None:
    """Read-only inspectors are routed and run without leaking exceptions
    (missing state files just print a notice)."""
    from raven.cli._repl_slash import handle_repl_slash

    assert handle_repl_slash(cmd, console=_RecordingConsole()) is True


@pytest.mark.parametrize(
    "cmd",
    [
        # global-config writes — same shell-only rule as `cron config`
        "/sentinel enable",
        "/sentinel disable",
        "/sentinel config set --max-nudges-per-hour 1",
        # trigger ops — cost LLM / rebuild a separate stack
        "/sentinel tick",
        "/sentinel discover-now feishu",
        "/sentinel behaviors-rebuild",
    ],
)
def test_sentinel_writes_and_triggers_are_shell_only(isolated_runtime: Path, cmd: str) -> None:
    """Config writes and trigger ops are consumed (return True) but rejected
    with a shell-only notice — they must NOT execute or touch config.json."""
    from raven.cli._repl_slash import handle_repl_slash
    from raven.config.loader import get_config_path

    con = _RecordingConsole()
    assert handle_repl_slash(cmd, console=con) is True
    assert "shell-only" in con.text
    # No config file was written by these REPL-rejected commands.
    assert not get_config_path().exists()
