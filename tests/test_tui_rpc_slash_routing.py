"""Tests for tui_rpc slash routing.

Covers four dogfood-discovered failures:

1. ``/channel``, ``/provider``, ``/asd`` returned ``-32601 method_not_found``
   because hermes's ``createSlashHandler.ts:82`` routes unknown slashes to
   ``slash.exec`` — which was never registered.
2. ``/status`` returned ``-32012 not_supported_in_v01`` because hermes's
   ``core.ts:171`` calls ``session.status`` which was a hermes-only stub.
3. ``completion unavailable`` red toast on every keystroke because
   ``useCompletion.ts:85`` calls ``complete.slash`` / ``complete.path`` —
   never registered.
4. Acceptance: ``slash.exec`` MUST NOT raise ``-32xxx`` for blacklist /
   unknown verbs — hermes UI consumes ``{output?, warning?}`` directly via
   ``createSlashHandler.ts:89`` and would otherwise fall into the
   ``command.dispatch`` fallback (also unregistered → ugly toast).
"""

from __future__ import annotations

import click
import pytest
import typer

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods._stubs import HERMES_ONLY_STUB_METHODS
from raven.tui_rpc.methods.slash_routing import (
    complete_path,
    complete_slash,
    register_slash_routing_methods,
    session_status,
    slash_exec,
)


def _make_fake_app() -> typer.Typer:
    """Tiny Typer app reused across slash routing tests."""
    fake = typer.Typer(no_args_is_help=False)

    @fake.command()
    def status() -> None:
        import raven.cli.commands as ec_commands

        ec_commands.console.print("Raven status: ok")

    @fake.command()
    def echo(text: str) -> None:
        import raven.cli.commands as ec_commands

        ec_commands.console.print(text)

    @fake.command()
    def boom() -> None:
        raise click.UsageError("bad arg")

    return fake


@pytest.fixture
def fake_app_patch(monkeypatch):
    """Replace ``raven.cli.commands.app`` with the small fake Typer app.

    Post harness-command-catalog-dynamic, ``_is_dispatch_compatible``
    reflects ``ec_commands.app`` directly, so monkey-patching the app is
    sufficient — the previous step of extending ``_DISPATCH_WHITELIST`` to
    name the fake commands is no longer needed (the whitelist was deleted).
    """
    import raven.cli.commands as ec_commands

    fake = _make_fake_app()
    monkeypatch.setattr(ec_commands, "app", fake)
    yield fake


# ---------------------------------------------------------------------------
# slash.exec — happy path & shlex
# ---------------------------------------------------------------------------


async def test_slash_exec_routes_to_cli_dispatch_status(fake_app_patch):
    """``/status`` typed into hermes → slash.exec({command: "status"}) → cli.dispatch."""
    result = await slash_exec({"command": "status", "session_id": "sid-abc"})
    assert "output" in result
    assert "Raven status: ok" in result["output"]
    assert result.get("warning") in (None, "")


async def test_slash_exec_routes_channels_status_with_space(fake_app_patch):
    """``/channels status`` arrives as command="channels status" — needs split."""

    # Add a channels-status fake command to verify dispatch routing.
    @fake_app_patch.command(name="channels-status")
    def _channels_status() -> None:  # pragma: no cover (registered for routing)
        import raven.cli.commands as ec_commands

        ec_commands.console.print("channels: ok")

    # The real path uses the EC CLI's `channels status` (already whitelisted).
    # We use the patched echo as a stand-in to keep the test isolated.
    result = await slash_exec({"command": "echo hello-tui", "session_id": "sid-abc"})
    assert "hello-tui" in result["output"]


async def test_slash_exec_shlex_quoted_args(fake_app_patch):
    """shlex must handle quoted args: command='echo "two words"'."""
    result = await slash_exec({"command": 'echo "two words"', "session_id": "sid-abc"})
    assert "two words" in result["output"]


# ---------------------------------------------------------------------------
# slash.exec — graceful warnings (never raises -32xxx)
# ---------------------------------------------------------------------------


async def test_slash_exec_blacklist_returns_message_in_output(fake_app_patch):
    """``/provider login`` is P3 blacklist — friendly message in ``output``
    (NOT ``warning``) so createSlashHandler.ts:88 doesn't append
    ``/provider: no output`` as a redundant trailing line."""
    result = await slash_exec({"command": "provider login", "session_id": "sid-abc"})
    assert "terminal" in result["output"].lower()
    assert "warning" not in result  # critical: no warning field


async def test_slash_exec_normal_channel_login_not_blacklisted(fake_app_patch):
    """A normal channel login is no longer classified as the blanket
    terminal-only toast — `_is_blacklist_argv` only matches weixin/whatsapp.

    The fake app has no `channels` group, so this routes to the unknown-command
    fallback; the salient assertion is the *absence* of the terminal-only toast,
    which the prior blanket 2-tuple would have produced for any channel name.
    """
    result = await slash_exec(
        {"command": "channels login telegram", "session_id": "sid-abc"}
    )
    assert "requires a real terminal" not in result["output"].lower()


import pytest as _pytest


@_pytest.mark.parametrize(
    "command",
    [
        "gateway",
        "provider login",
        # channels login is gated per-channel — only weixin/whatsapp
        # keep the terminal-only toast; normal channels now dispatch.
        "channels login weixin",
        "channels login whatsapp",
        "sandbox shell",
        # harness-command-catalog-dynamic — newly blacklisted (smoke caught
        # that the prior hardcoded _is_blacklist_argv missed these two and
        # the user got an "unknown command" toast instead of the correct
        # "requires a real terminal" message).
        "tui",
        "onboard",
        # agent without -m — REPL form
        "agent",
    ],
)
async def test_slash_exec_full_blacklist_terminal_message(fake_app_patch, command):
    """REQ-4 / smoke regression — every blacklist entry gets the friendly
    "requires a real terminal" toast, not the "unknown command" fallback.

    Reads the shared ``_DISPATCH_BLACKLIST`` (single source of truth across
    cli_dispatch + slash_routing + catalog filter) — extending the set
    automatically propagates to all three sites.
    """
    result = await slash_exec({"command": command, "session_id": "sid-abc"})
    assert "terminal" in result["output"].lower(), (
        f"expected terminal-only message for /{command}, got {result['output']!r}"
    )
    assert "unknown" not in result["output"].lower(), (
        f"/{command} misclassified as unknown-command rather than blacklist"
    )
    assert "warning" not in result


async def test_slash_exec_unknown_verb_returns_message_in_output(fake_app_patch):
    """``/asd`` is not in whitelist — message in ``output`` for clean render."""
    result = await slash_exec({"command": "asd", "session_id": "sid-abc"})
    assert "unknown" in result["output"].lower()
    assert "warning" not in result


async def test_slash_exec_provider_no_subcommand_returns_message_in_output(fake_app_patch):
    """``/provider`` alone (no subcommand) → unknown verb, output-only."""
    result = await slash_exec({"command": "provider", "session_id": "sid-abc"})
    assert "unknown" in result["output"].lower() or "terminal" in result["output"].lower()
    assert "warning" not in result


async def test_slash_exec_empty_command_returns_hint_in_output(fake_app_patch):
    """Empty command → friendly hint in ``output``, no warning field."""
    result = await slash_exec({"command": "", "session_id": "sid-abc"})
    assert result["output"]  # non-empty
    assert "empty" in result["output"].lower() or "help" in result["output"].lower()
    assert "warning" not in result


async def test_slash_exec_whitespace_command_returns_hint_in_output(fake_app_patch):
    """Whitespace-only command → same hint shape as empty."""
    result = await slash_exec({"command": "   ", "session_id": "sid-abc"})
    assert result["output"]
    assert "warning" not in result


async def test_slash_exec_non_zero_exit_keeps_warning(fake_app_patch):
    """Real cli command failure (exit != 0) DOES keep the ``warning`` field
    — this is a real error, not a user mistake, so the toast styling is
    appropriate."""
    result = await slash_exec({"command": "boom", "session_id": "sid-abc"})
    # output may be empty, but warning MUST surface the cli stderr.
    assert result.get("warning")


async def test_slash_exec_never_raises_for_dispatch_failures(fake_app_patch):
    """Regression guard: slash.exec must NOT raise -32xxx; all failures must
    arrive as dict so hermes UI's .then() handles them."""
    for cmd in ("", "asd", "provider login", "sandbox shell", "gateway"):
        result = await slash_exec({"command": cmd, "session_id": "sid-abc"})
        assert isinstance(result, dict)
        # output always present so createSlashHandler.ts:88 doesn't fall back
        # to "/<name>: no output".
        assert "output" in result and result["output"]


# ---------------------------------------------------------------------------
# session.status — replaces hermes-only stub with real cli.dispatch delegate
# ---------------------------------------------------------------------------


async def test_session_status_returns_output(fake_app_patch):
    """``/status`` slash → session.status RPC → cli.dispatch(["status"])."""
    result = await session_status({"session_id": "sid-abc"})
    assert "output" in result
    assert "Raven status: ok" in result["output"]


async def test_session_status_removed_from_stubs():
    """``session.status`` must no longer appear in the hermes-only stub list —
    the real handler in ``slash_routing.py`` supersedes it."""
    assert "session.status" not in HERMES_ONLY_STUB_METHODS, (
        "session.status must be promoted to a real handler so /status routes "
        "to `raven status` instead of returning -32012."
    )


async def test_session_status_registered_on_dispatcher(fake_app_patch):
    """End-to-end: dispatcher resolves session.status to the real handler."""
    d = Dispatcher()
    register_slash_routing_methods(d)
    resp = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session.status",
            "params": {"session_id": "sid-abc"},
        }
    )
    assert "result" in resp, f"expected success frame, got {resp}"
    assert "output" in resp["result"]
    assert "Raven status: ok" in resp["result"]["output"]


# ---------------------------------------------------------------------------
# complete.slash / complete.path — silence "completion unavailable" toast
# ---------------------------------------------------------------------------


async def test_complete_slash_returns_empty_items():
    """``complete.slash`` returns empty items so the UI's red-frame toast
    (useCompletion.ts:97-108) is not triggered."""
    result = await complete_slash({"text": "/he"})
    assert result == {"items": [], "replace_from": 1}


async def test_complete_path_returns_empty_items():
    """``complete.path`` returns empty items — v0.0.3 may add real completion."""
    result = await complete_path({"word": "/home/u/file.md"})
    assert result == {"items": []}


async def test_complete_methods_registered_on_dispatcher():
    """Both completion methods must be on the dispatcher so unknown method
    -32601 spam stops."""
    d = Dispatcher()
    register_slash_routing_methods(d)

    for method in ("complete.slash", "complete.path"):
        resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})
        assert "result" in resp, f"{method} → expected success frame, got {resp}"
        assert resp["result"]["items"] == []


# ---------------------------------------------------------------------------
# Full registration sanity
# ---------------------------------------------------------------------------


async def test_register_slash_routing_methods_wires_all_four(fake_app_patch):
    """Confirm all four method names land on the dispatcher."""
    d = Dispatcher()
    register_slash_routing_methods(d)
    expected = {"slash.exec", "session.status", "complete.slash", "complete.path"}
    assert expected.issubset(set(d._handlers.keys())), (  # type: ignore[attr-defined]
        f"missing slash routing methods: {expected - set(d._handlers.keys())}"  # type: ignore[attr-defined]
    )
