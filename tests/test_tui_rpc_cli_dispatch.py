"""Tests for tui_rpc cli.dispatch handler + Console injection + ANSI filter.

Acceptance is gated by:
- `test_happy_path_fake_echo` — end-to-end dispatch + Console injection + width sync
- `test_standalone_mode_false_catches_usage_error` — no process death on bad arg
- `test_width_sync_propagates_to_console` — TUI-supplied width flows to Rich

Implementation contract: monkey-patch the EC CLI modules' module-level
``console``.

Error mapping:
- -32013 cli_command_failed   → NOT raised; signaled via exit_code != 0 in result
- -32014 cli_command_timeout  → raised so the dispatcher emits a JSON-RPC error frame
- -32015 not_dispatch_compatible → raised so the dispatcher emits a JSON-RPC error frame

Why: the CliResult shape embeds error_code in the result, but timeout / not-compat
are *errors*. We resolve in favor of treating them as errors: the handler raises
CliCommandTimeoutError / NotDispatchCompatibleError; the dispatcher converts them
to JSON-RPC error frames. Only exit_code != 0 (-32013) stays in-band.
"""

from __future__ import annotations

import asyncio
from io import StringIO

import click
import pytest
import typer

from raven.tui_rpc._ansi_filter import filter_ansi
from raven.tui_rpc._console_injection import _CONSOLE_HOSTS, inject_consoles
from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.errors import (
    CliCommandTimeoutError,
    ConfigValidationError,
    NotDispatchCompatibleError,
)
from raven.tui_rpc.methods.cli_dispatch import (
    _is_dispatch_compatible,
    cli_dispatch,
    register_cli_methods,
)

# ---------------------------------------------------------------------------
# Test fixtures — build a fake Typer app and patch ec_cli.app for isolation.
# ---------------------------------------------------------------------------


def _make_fake_app() -> typer.Typer:
    """Build a small Typer app with deterministic commands for testing.

    Commands:
        echo <text>        — print <text> to stdout via module-level console
        boom               — raise click.UsageError("bad arg")
        slow               — time.sleep(10) (used by the timeout test)
    """
    fake = typer.Typer(no_args_is_help=False)

    @fake.command()
    def echo(text: str) -> None:
        # Use the patched module-level console so injection is testable.
        import raven.cli.commands as ec_commands

        ec_commands.console.print(text)

    @fake.command()
    def boom() -> None:
        raise click.UsageError("bad arg")

    @fake.command()
    def typer_exit_one() -> None:
        """B1 regression test: typer.Exit(1) must propagate to exit_code=1."""
        raise typer.Exit(1)

    @fake.command()
    def typer_exit_three() -> None:
        """B1 regression test: typer.Exit(3) must propagate to exit_code=3."""
        raise typer.Exit(3)

    @fake.command()
    def slow() -> None:
        import time

        time.sleep(10)

    @fake.command()
    def aborts() -> None:
        """C1 test: raise click.Abort (the EOF-stdin confirm failure mode)."""
        raise click.exceptions.Abort()

    @fake.command()
    def render_table(rows: int = 3) -> None:
        """Render a Rich Table; used by the width-sync test."""
        from rich.table import Table

        import raven.cli.commands as ec_commands

        table = Table(title="Sample")
        table.add_column("col1")
        table.add_column("col2")
        for i in range(rows):
            table.add_row(f"r{i}-a-very-long-cell", f"r{i}-b-also-long-cell")
        ec_commands.console.print(table)

    return fake


@pytest.fixture
def fake_app_patch(monkeypatch):
    """Patch `raven.cli.commands.app` to a fake Typer; whitelist its commands.

    The cli_dispatch handler calls `ec_cli.app(argv, standalone_mode=False)` where
    `ec_cli` is `raven.cli.commands`. We swap `app` for our fake and extend the
    whitelist for the duration of the test.
    """
    import raven.cli.commands as ec_commands

    fake = _make_fake_app()
    monkeypatch.setattr(ec_commands, "app", fake)

    # Reflection picks up the fake Typer commands directly off
    # ``ec_commands.app`` — no whitelist injection needed.
    yield fake


# ---------------------------------------------------------------------------
# Test 1 — happy path end-to-end (fake echo via patched app)
# ---------------------------------------------------------------------------


async def test_happy_path_fake_echo(fake_app_patch):
    """Acceptance proxy: argv → stdout → exit_code=0."""
    result = await cli_dispatch({"argv": ["echo", "hello-tui"], "width": 80})
    assert result["exit_code"] == 0
    assert "hello-tui" in result["stdout"]
    assert "error_code" not in result or result.get("error_code") is None


# ---------------------------------------------------------------------------
# Test 2 — Console injection swaps all CLI modules
# ---------------------------------------------------------------------------


def test_console_injection_patches_all_modules():
    """inject_consoles must replace `mod.console` on all hosts and restore on exit.

    The patch list was extended 4 → 12 after a merge introduced 8 new CLI modules.
    """
    assert len(_CONSOLE_HOSTS) == 12, (
        "patch list locks 12 console hosts"
    )
    originals = {mod: mod.console for mod in _CONSOLE_HOSTS}

    from rich.console import Console as RichConsole

    sentinel = RichConsole(file=StringIO(), force_terminal=True, color_system="truecolor", width=80)
    with inject_consoles(sentinel):
        for mod in _CONSOLE_HOSTS:
            assert mod.console is sentinel, f"{mod.__name__}.console was not patched"

    # After exit: all restored.
    for mod, orig in originals.items():
        assert mod.console is orig, f"{mod.__name__}.console not restored"


# ---------------------------------------------------------------------------
# Test 3 — whitelist (_is_dispatch_compatible)
# ---------------------------------------------------------------------------


def test_dispatch_compatible_whitelist():
    # Acceptance proxy: channels status MUST be allowed (v0.0.1 demo)
    assert _is_dispatch_compatible(["channels", "status"]) is True
    assert _is_dispatch_compatible(["status"]) is True
    assert _is_dispatch_compatible(["skill", "list"]) is True
    # Out-of-scope
    assert _is_dispatch_compatible(["run"]) is False
    assert _is_dispatch_compatible(["nonexistent"]) is False
    # Empty argv → reject (nothing to dispatch)
    assert _is_dispatch_compatible([]) is False


def test_dispatch_compatible_b2_whitelist_extension():
    """B2 surface: P1 commands added via Task A coverage report (post-refactor CLI).

    NOTE 2026-05-22 post merge from refactor/Raven: trunk removed 6 skill
    subcommands (refresh / stats / rebuild-index / import-files / cases /
    rollback) — only ``skill list`` + ``skill get`` remain on trunk. The
    reflection-based dispatch model handles this gracefully (the dropped
    commands no longer exist in ``ec.cli.commands.app`` → reflection returns
    False, indistinguishable from a typo). Dropping ``skill stats`` from this
    assertion accordingly.
    """
    assert _is_dispatch_compatible(["cron", "list"]) is True
    assert _is_dispatch_compatible(["cron", "get"]) is True
    assert _is_dispatch_compatible(["cron", "get", "42"]) is True  # positional arg prefix
    assert _is_dispatch_compatible(["sentinel", "status"]) is True
    assert _is_dispatch_compatible(["sentinel", "routines"]) is True
    assert _is_dispatch_compatible(["sentinel", "nudges"]) is True
    assert _is_dispatch_compatible(["sentinel", "decisions"]) is True
    assert _is_dispatch_compatible(["sandbox", "list"]) is True


def test_dispatch_blacklist_p3():
    """Interactive/long-running blacklist commands are hard-rejected."""
    assert _is_dispatch_compatible(["gateway"]) is False  # long-running daemon
    assert _is_dispatch_compatible(["gateway", "start"]) is False  # prefix match
    assert _is_dispatch_compatible(["provider", "login"]) is False  # OAuth flow
    # channels login is gated per-channel now (weixin/whatsapp only).
    assert _is_dispatch_compatible(["channels", "login", "weixin"]) is False  # QR long-poll
    assert _is_dispatch_compatible(["channels", "login", "whatsapp"]) is False  # npm subprocess
    assert _is_dispatch_compatible(["sandbox", "shell"]) is False  # stdin hijack


def test_channels_login_normal_channels_dispatch():
    """Normal (no-op) channel logins are dispatch-compatible; only
    weixin/whatsapp stay blacklisted.

    The other ~10 channels inherit BaseChannel.login (a no-op returning True),
    so `channels login <normal>` performs no terminal I/O and can run in-process.
    A bare `channels login` (no channel name) also dispatches now — it resolves
    to a valid group+subcommand and surfaces Typer's missing-argument error
    rather than the blanket terminal-only toast.
    """
    assert _is_dispatch_compatible(["channels", "login", "telegram"]) is True
    assert _is_dispatch_compatible(["channels", "login", "slack"]) is True
    assert _is_dispatch_compatible(["channels", "login"]) is True  # group + valid subcommand
    # weixin/whatsapp remain gated (real interactive login flows).
    assert _is_dispatch_compatible(["channels", "login", "weixin"]) is False
    assert _is_dispatch_compatible(["channels", "login", "whatsapp"]) is False


def test_blacklist_full_coverage():
    """7-entry blacklist (5 original + tui + onboard).

    `tui` blocks recursive Ink+Node spawn; `onboard` blocks prompt_toolkit
    wizard stdin hijack. Both necessary because reflection will otherwise
    let them through.
    """
    from raven.tui_rpc.methods.cli_dispatch import _DISPATCH_BLACKLIST

    expected_entries = {
        ("gateway",),
        ("provider", "login"),
        ("channels", "login", "weixin"),
        ("channels", "login", "whatsapp"),
        ("sandbox", "shell"),
        ("tui",),
        ("onboard",),
    }
    assert _DISPATCH_BLACKLIST == expected_entries, (
        f"blacklist drift; expected 7 prefix tuples (+ agent REPL special-case), got "
        f"{_DISPATCH_BLACKLIST}"
    )
    # Hard-reject probe (prefix match)
    assert _is_dispatch_compatible(["tui"]) is False
    assert _is_dispatch_compatible(["tui", "--help"]) is False  # prefix
    assert _is_dispatch_compatible(["onboard"]) is False
    assert _is_dispatch_compatible(["onboard", "--reset"]) is False  # prefix


# ---------------------------------------------------------------------------
# Reflection-based dispatch compat (_DISPATCH_WHITELIST deleted)
# ---------------------------------------------------------------------------


def test_dispatch_compatible_via_reflection_newly_active():
    """Commands previously absent from _DISPATCH_WHITELIST now dispatch-OK
    purely because reflection sees them registered on ec.cli.commands.app.

    Covers 1 representative entry per CLI group that was previously unreachable.
    """
    assert _is_dispatch_compatible(["channels", "enable"]) is True
    assert _is_dispatch_compatible(["channels", "enable", "telegram"]) is True  # prefix
    assert _is_dispatch_compatible(["provider", "list"]) is True
    assert _is_dispatch_compatible(["provider", "test"]) is True
    assert _is_dispatch_compatible(["cron", "delete"]) is True
    assert _is_dispatch_compatible(["cron", "add"]) is True
    assert _is_dispatch_compatible(["sentinel", "discover-now"]) is True  # hyphen subcmd
    assert _is_dispatch_compatible(["sandbox", "exec"]) is True
    assert _is_dispatch_compatible(["sandbox", "ls"]) is True  # alias of list
    # NOTE: trunk dropped 6 skill subcommands
    # (refresh / stats / rebuild-index / import-files / cases / rollback);
    # only ``skill list`` + ``skill get`` remain. The reflection model
    # auto-tracks this — no code change needed, just the surface shifts.
    # New on trunk: ``doctor`` top-level command — reflection picks it up
    # for free, demonstrating the architectural payoff of the dynamic catalog.
    assert _is_dispatch_compatible(["doctor"]) is True


def test_dispatch_rejects_unknown_commands():
    """The 5 'broken whitelist' entries (mcp/model/config groups
    that the real CLI doesn't ship) now auto-clean.

    Reflection does not find these groups on ec.cli.commands.app, so dispatch
    returns False up front — TUI surfaces -32015 not_dispatch_compatible
    (clean error toast) instead of the 'exit_code != 0 / no such command'
    dirty path.
    """
    # Unknown top-level group → False
    assert _is_dispatch_compatible(["mcp", "list"]) is False
    assert _is_dispatch_compatible(["mcp", "show"]) is False
    assert _is_dispatch_compatible(["model", "list"]) is False
    assert _is_dispatch_compatible(["model", "show"]) is False
    assert _is_dispatch_compatible(["config", "show"]) is False
    # Unknown subcommand under a real group
    assert _is_dispatch_compatible(["channels", "nonexistent-sub"]) is False
    assert _is_dispatch_compatible(["skill", "fly-to-the-moon"]) is False
    # Incomplete (group head only, when group has subcommands) → False
    assert _is_dispatch_compatible(["channels"]) is False
    assert _is_dispatch_compatible(["skill"]) is False


# REQ-3 regression guard — table-driven; any single False here is a release block.
#
# History:
# - v0.0.2 had 14 hardcoded whitelist entries (status / channels status+list /
#   skill list+get+refresh+stats / cron list+show / sentinel × 4 / sandbox list).
# - 2026-05-22 post merge from refactor/Raven: trunk dropped 6 ``skill``
#   subcommands (refresh + stats + 4 others), leaving only ``skill list`` and
#   ``skill get``. The guard list shrinks from 14 → 12 accordingly — those
#   commands no longer exist in the CLI surface; reflection cannot find them
#   and must not be expected to.
_WORKING_ENTRIES = [
    ("status",),
    ("channels", "status"),
    ("channels", "list"),
    ("skill", "list"),
    ("skill", "get"),
    ("cron", "list"),
    ("cron", "get"),
    ("sentinel", "status"),
    ("sentinel", "routines"),
    ("sentinel", "nudges"),
    ("sentinel", "decisions"),
    ("sandbox", "list"),
]
# Sentinel — protects the "currently 12" claim. If someone removes an entry
# while touching this list, the count test fails before the parametrised cases
# silently weaken the regression guard. The number itself is *not* a forever
# invariant — it's a snapshot. Update intentionally when trunk-side CLI
# surface evolves; never silently shrink.
_EXPECTED_WORKING_COUNT = 12
assert len(_WORKING_ENTRIES) == _EXPECTED_WORKING_COUNT, (
    f"working entries snapshot drifted from {_EXPECTED_WORKING_COUNT} to "
    f"{len(_WORKING_ENTRIES)}; bump _EXPECTED_WORKING_COUNT only when CLI "
    f"surface intentionally changes (record reason in commit message)"
)


@pytest.mark.parametrize("argv", _WORKING_ENTRIES)
def test_working_entries_remain_compatible(argv):
    """Every still-present working entry must dispatch-OK after
    _DISPATCH_WHITELIST removal. Currently 12 entries (trunk dropped 6 skill
    subs; we keep the surviving 12 in scope). Any failure here is a hard
    regression of a working slash that the user could previously type.
    """
    assert _is_dispatch_compatible(list(argv)) is True, (
        f"working entry {argv!r} regressed — reflection or filter now rejects "
        f"what was previously dispatch-compatible"
    )


def test_dispatch_agent_repl_blacklist_with_m_exception():
    """Task B: `agent` (no -m) is REPL → blacklist; `agent -m` is one-shot.

    Note: `agent -m "msg"` isn't in whitelist either (one-shot agent is v0.0.3+
    scope), so it's still rejected by whitelist — but NOT by the agent-REPL
    blacklist check (which is the salient distinction for the test).
    """
    from raven.tui_rpc.methods.cli_dispatch import _is_agent_repl

    assert _is_agent_repl(["agent"]) is True  # REPL — blocked
    assert _is_agent_repl(["agent", "-m", "hi"]) is False  # one-shot — agent-REPL doesn't fire
    assert _is_agent_repl(["agent", "--message", "hi"]) is False  # long form
    assert _is_agent_repl(["agent", "--help"]) is True  # no -m → still REPL
    assert _is_agent_repl(["status"]) is False  # not agent at all
    assert _is_agent_repl([]) is False  # empty


# ---------------------------------------------------------------------------
# Test 4 — non-compatible argv raises NotDispatchCompatibleError (-32015)
# ---------------------------------------------------------------------------


async def test_blacklist_raises_32015():
    """-32015 is an error frame → handler raises."""
    with pytest.raises(NotDispatchCompatibleError) as exc_info:
        await cli_dispatch({"argv": ["run"], "width": 80})
    assert exc_info.value.code == -32015


# ---------------------------------------------------------------------------
# Test 5 — width validation (Pydantic Field constraints)
# ---------------------------------------------------------------------------


async def test_width_too_small_rejected():
    with pytest.raises(ConfigValidationError):
        await cli_dispatch({"argv": ["status"], "width": 10})


async def test_width_too_large_rejected():
    with pytest.raises(ConfigValidationError):
        await cli_dispatch({"argv": ["status"], "width": 600})


async def test_width_in_range_passes_validation(fake_app_patch):
    # width=80 with a known-compatible fake command → no validation error
    result = await cli_dispatch({"argv": ["echo", "ok"], "width": 80})
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# Test 6 — standalone_mode=False catches UsageError, process survives
# ---------------------------------------------------------------------------


async def test_standalone_mode_false_catches_usage_error(fake_app_patch):
    """Acceptance proxy: bad arg → exit_code != 0, process alive."""
    result = await cli_dispatch({"argv": ["boom"], "width": 80})
    assert result["exit_code"] != 0, "UsageError should produce non-zero exit_code"
    # The process did NOT exit; we can call again.
    result2 = await cli_dispatch({"argv": ["echo", "still-alive"], "width": 80})
    assert result2["exit_code"] == 0
    assert "still-alive" in result2["stdout"]


# ---------------------------------------------------------------------------
# Test 6.5 — B1 regression: typer.Exit(N) must propagate to exit_code=N
# ---------------------------------------------------------------------------
#
# Root cause:
# Click's ``standalone_mode=False`` catches ``typer.Exit`` (a ``RuntimeError``
# subclass that does NOT inherit ``SystemExit``) and **returns** the exit_code
# from ``app()`` instead of raising. ``_invoke_ec_cli`` previously discarded
# that return value, so all ``typer.Exit(N)`` paths silently reported
# ``exit_code=0`` to the TUI. Fix: capture the return value of
# ``ec_cli.app(argv, standalone_mode=False)`` and pass it back as exit_code.


async def test_b1_typer_exit_one_propagates(fake_app_patch):
    """typer.Exit(1) (raised by command body, caught by click+standalone=False) → exit_code=1."""
    result = await cli_dispatch({"argv": ["typer-exit-one"], "width": 80})
    assert result["exit_code"] == 1, "typer.Exit(1) must propagate as exit_code=1"


async def test_b1_typer_exit_three_propagates(fake_app_patch):
    """typer.Exit(3) must propagate as exit_code=3 (any non-zero code path)."""
    result = await cli_dispatch({"argv": ["typer-exit-three"], "width": 80})
    assert result["exit_code"] == 3, "typer.Exit(3) must propagate as exit_code=3"


# ---------------------------------------------------------------------------
# Test 7 — timeout raises CliCommandTimeoutError (-32014)
# ---------------------------------------------------------------------------


async def test_timeout_raises_32014(fake_app_patch):
    with pytest.raises(CliCommandTimeoutError) as exc_info:
        await cli_dispatch({"argv": ["slow"], "width": 80, "timeout_s": 0.3})
    assert exc_info.value.code == -32014


# ---------------------------------------------------------------------------
# Test 8 / 9 — ANSI filter
# ---------------------------------------------------------------------------


def test_ansi_filter_strips_cursor_movement():
    raw = "\x1b[31mred\x1b[2A\x1b[H\x1b[2J\x1b[0m"
    out = filter_ansi(raw)
    assert "\x1b[31m" in out
    assert "\x1b[0m" in out
    assert "red" in out
    # Cursor + clear-screen sequences gone
    assert "\x1b[2A" not in out
    assert "\x1b[H" not in out
    assert "\x1b[2J" not in out


def test_ansi_filter_preserves_truecolor():
    raw = "\x1b[38;2;255;128;0morange\x1b[0m"
    assert filter_ansi(raw) == raw


def test_ansi_filter_strips_osc8_hyperlink():
    """OSC 8 hyperlink: \\x1b]8;;url\\x07TEXT\\x1b]8;;\\x07 — strip the OSC envelopes."""
    raw = "\x1b]8;;https://example.com\x07click\x1b]8;;\x07"
    out = filter_ansi(raw)
    # The visible text survives; the OSC 8 envelopes are stripped.
    assert "click" in out
    assert "\x1b]8" not in out


def test_ansi_filter_strips_decset():
    raw = "\x1b[?1049hbody\x1b[?1049l"
    out = filter_ansi(raw)
    assert "body" in out
    assert "?1049" not in out


# ---------------------------------------------------------------------------
# Test 10 — width sync propagates to Console
# ---------------------------------------------------------------------------


async def test_width_sync_propagates_to_console(fake_app_patch):
    """Acceptance proxy: width=60 → output lines ≤ 60 cells."""
    result = await cli_dispatch({"argv": ["render-table", "--rows", "2"], "width": 60})
    assert result["exit_code"] == 0
    # ANSI-strip + check line length budget. Rich wraps tables to console width.
    import re

    plain = re.sub(r"\x1b\[[\d;]*m", "", result["stdout"])
    for line in plain.splitlines():
        # Allow trailing whitespace; the table border should be ≤ width.
        assert len(line.rstrip()) <= 60, f"line exceeds width=60: {line!r}"


# ---------------------------------------------------------------------------
# Bonus — register_cli_methods wires up dispatcher
# ---------------------------------------------------------------------------


async def test_register_cli_methods_registers_dispatch():
    d = Dispatcher()
    register_cli_methods(d)
    assert "cli.dispatch" in d.methods()


async def test_dispatcher_maps_not_compat_to_error_frame(fake_app_patch):
    """End-to-end: dispatcher receives non-whitelisted argv → -32015 error frame."""
    d = Dispatcher()
    register_cli_methods(d)
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "cli.dispatch",
        "params": {"argv": ["run"], "width": 80},
    }
    response = await d.dispatch(frame)
    assert "error" in response
    assert response["error"]["code"] == -32015


async def test_dispatcher_happy_path_returns_result_frame(fake_app_patch):
    d = Dispatcher()
    register_cli_methods(d)
    frame = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "cli.dispatch",
        "params": {"argv": ["echo", "via-dispatcher"], "width": 80},
    }
    response = await d.dispatch(frame)
    assert "result" in response, f"expected result, got {response}"
    assert response["result"]["exit_code"] == 0
    assert "via-dispatcher" in response["result"]["stdout"]


async def test_concurrent_dispatches_serialized(fake_app_patch):
    """Q7 risk 4 — `asyncio.Lock` must serialize so console patches don't race."""
    results = await asyncio.gather(
        cli_dispatch({"argv": ["echo", "a"], "width": 80}),
        cli_dispatch({"argv": ["echo", "b"], "width": 80}),
        cli_dispatch({"argv": ["echo", "c"], "width": 80}),
    )
    for r in results:
        assert r["exit_code"] == 0
    outputs = {r["stdout"].strip() for r in results}
    assert outputs == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Confirm round-trip timeout grace
# ---------------------------------------------------------------------------


async def test_dispatch_timeout_includes_confirm_grace(fake_app_patch, monkeypatch):
    """With a ConfirmBroker present, the dispatch wait_for budget = timeout_s +
    _CONFIRM_HARD_LIMIT_S (fixed enlargement)."""
    from raven.tui_rpc import confirm_broker as cb
    from raven.tui_rpc.confirm_broker import ConfirmBroker

    seen: list[float] = []
    real_wait_for = asyncio.wait_for

    async def spy(aw, timeout):
        seen.append(timeout)
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy)

    async def _send(_frame):
        return None

    broker = ConfirmBroker(_send)
    result = await cli_dispatch(
        {"argv": ["echo", "hi"], "width": 80, "timeout_s": 20.0},
        confirm_broker=broker,
    )
    assert result["exit_code"] == 0
    assert (20.0 + cb._CONFIRM_HARD_LIMIT_S) in seen


async def test_dispatch_timeout_unchanged_without_broker(fake_app_patch, monkeypatch):
    """No broker → no grace; wait_for budget stays the plain timeout_s."""
    seen: list[float] = []
    real_wait_for = asyncio.wait_for

    async def spy(aw, timeout):
        seen.append(timeout)
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy)

    await cli_dispatch({"argv": ["echo", "hi"], "width": 80, "timeout_s": 20.0})
    assert 20.0 in seen


async def test_abort_returns_confirmation_hint(fake_app_patch):
    """C1: click.Abort downgrades to a --yes hint, not `Internal error: Abort`."""
    result = await cli_dispatch({"argv": ["aborts"], "width": 80})
    assert result["exit_code"] != 0
    assert "--yes" in result["stderr"]
    assert "Internal error" not in result["stderr"]
    assert "Abort" not in result["stderr"]
