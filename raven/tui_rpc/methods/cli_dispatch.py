"""``cli.dispatch`` RPC handler — in-process EC CLI command runner.

Design summary:
1. **S1 / D7 contract**: ``argv: list[str]`` + ``width: int`` (required, 20..500)
   + ``timeout_s: float`` (optional, default 30s). Result is
   ``{stdout, stderr, exit_code}`` (with optional ``error_code`` only for the
   in-band -32013 cli_command_failed case — the spec keeps -32013 out-of-band-free).
2. **S2 dispatch-compat**: ``_is_dispatch_compatible`` reflects
   ``raven.cli.commands.app`` (Typer 0.20+) — any registered command
   that is NOT in ``_DISPATCH_BLACKLIST`` and NOT ``agent``-without-``-m``
   is accepted. The v0.0.2 hardcoded ``_DISPATCH_WHITELIST`` was removed by
   ``harness-command-catalog-dynamic`` so CLI rename / new-command churn
   does not require TUI tuple updates. Interactive Rich widgets remain
   blocked by the blacklist (``provider login`` / ``channels login`` /
   ``sandbox shell``) + recursive ``tui`` / ``onboard`` wizard.
3. **S3 ``standalone_mode=False``**: Click library mode — suppresses
   ``sys.exit()``, surfaces errors as exceptions. We still defensively catch
   ``SystemExit`` (Typer's ``typer.Exit`` inherits from it).
4. **S4 ANSI filter**: applied to ``stdout`` / ``stderr`` post-render so the
   TUI Ink reconciler is never corrupted by cursor movement / clear-screen.
5. **S5 truecolor**: ``Console(color_system="truecolor")`` prevents 256-color
   downgrade in non-TTY ``force_terminal`` mode.

Concurrency: a module-level ``asyncio.Lock`` serializes calls — without it,
overlapping dispatches would race on the monkey-patched module-level
``console`` references. v0.2 may revisit with per-call Console pools.

Spec disambiguation (specs §4 wins over design.md §3 D7 skeleton):
- ``-32013 cli_command_failed`` → NOT raised; signaled via ``exit_code != 0``
  inside ``CliResult``.
- ``-32014 cli_command_timeout`` and ``-32015 not_dispatch_compatible`` are
  raised so the dispatcher emits JSON-RPC error frames. (The design.md
  skeleton mixed both styles; specs §4 is canonical.)
"""

from __future__ import annotations

import asyncio
import contextlib
import io
from typing import TYPE_CHECKING

import click
from loguru import logger
from pydantic import ValidationError
from rich.console import Console

import raven.cli.commands as ec_cli
from raven.tui_rpc._ansi_filter import filter_ansi
from raven.tui_rpc._confirm_injection import confirm_injection
from raven.tui_rpc._console_injection import inject_consoles
from raven.tui_rpc.confirm_broker import _CONFIRM_HARD_LIMIT_S
from raven.tui_rpc.errors import (
    CliCommandTimeoutError,
    ConfigValidationError,
    NotDispatchCompatibleError,
)
from raven.tui_rpc.methods._typer_reflect import collect_command_names as _collect_command_names
from raven.tui_rpc.models import CliDispatchParams

if TYPE_CHECKING:
    from raven.tui_rpc.confirm_broker import ConfirmBroker
    from raven.tui_rpc.dispatcher import Dispatcher


# ---------------------------------------------------------------------------
# Blacklist — hard-reject prefixes (single source of truth for both
# cli.dispatch and the dynamic commands.catalog filter).
# ---------------------------------------------------------------------------
# These commands either run indefinitely, hijack stdin, or need a real TTY
# (browser/OAuth/QR). cli.dispatch entry MUST reject them up front with
# -32015 ``not_dispatch_compatible`` so the TUI surfaces a friendly toast
# guiding the user to run the command in their own terminal. The
# harness-command-catalog-dynamic L2 also imports this set from
# ``methods/commands.py`` so the catalog handler filters the same prefixes
# (preventing user-facing slash entries that would only get rejected by
# dispatch later).
#
# Source: CLI-team locked P3 5 commands @
# ``docs/sendbox/toTuiIpcBridge/from-orche-eve15-cli-team-36-commands-locked.md``
# + harness-command-catalog-dynamic extension (``tui`` + ``onboard``) per
# ``docs/openspec/changes/harness-command-catalog-dynamic/design.md §D4``.
#
# NOTE on ``agent``: ``raven agent`` (no ``-m``) is REPL mode and MUST
# be rejected. ``raven agent -m "msg"`` is one-shot mode and IS allowed.
# Detection: argv[0] == "agent" AND no ``-m`` / ``--message`` flag in argv.
_DISPATCH_BLACKLIST: set[tuple[str, ...]] = {
    ("gateway",),  # long-running daemon service
    ("provider", "login"),  # OAuth flow requires browser
    # Only weixin/whatsapp have an interactive login (QR long-poll / npm
    # subprocess); the other channels inherit BaseChannel.login (a no-op) and
    # run fine in-process, so gate per-channel rather than the whole subcommand.
    ("channels", "login", "weixin"),  # QR long-poll loop — terminal-only
    ("channels", "login", "whatsapp"),  # npm subprocess owns the TTY — terminal-only
    ("sandbox", "shell"),  # interactive shell — hijacks stdin
    # ---- harness-command-catalog-dynamic extensions (5 → 7) ----
    # Both names were previously unreachable: not in the old _DISPATCH_WHITELIST,
    # so dispatch never saw them. With Typer-reflection dispatch, both
    # become reachable; the blacklist now hard-rejects to preserve safety.
    ("tui",),  # recursive Ink+Node spawn would deadlock + steal stdin
    ("onboard",),  # prompt_toolkit three-step wizard hijacks stdin
    # ``agent`` (no -m) — handled specially in _is_dispatch_compatible
}


def _is_agent_repl(argv: list[str]) -> bool:
    """Return True for ``agent`` invocation without ``-m`` / ``--message`` flag.

    ``raven agent`` (no -m)        → REPL, blacklist hit
    ``raven agent -m "hello"``     → one-shot, OK
    ``raven agent --message "x"``  → one-shot, OK
    """
    if not argv or argv[0] != "agent":
        return False
    return not any(a in ("-m", "--message") for a in argv[1:])


# Default timeout if caller did not override.
_DEFAULT_TIMEOUT_S = 30.0

# Serializes concurrent dispatches so the monkey-patched module-level
# ``console`` references can't race.
_dispatch_lock = asyncio.Lock()


def _is_dispatch_compatible(argv: list[str]) -> bool:
    """Return True iff argv is dispatch-compatible.

    Algorithm (harness-command-catalog-dynamic design.md §D7.1):

    1. Empty argv → False.
    2. Blacklist hard reject (prefix match against ``_DISPATCH_BLACKLIST``).
    3. ``agent`` no-``-m`` REPL → False.
    4. Reflect ``ec_cli.app`` to determine whether argv resolves to a
       registered Typer command:
       - ``argv[0]`` in top-level command names → True.
       - ``argv[0]`` in subgroup names: require ``argv[1]`` in that group's
         subcommand names (incomplete invocations like ``["channels"]``
         return False). Groups whose body is a single
         ``@callback(invoke_without_command=True)`` (no subcommands) accept
         the bare group head — but ``tui`` is the only such group in EC
         and is blacklisted, so this branch is effectively reserved for
         test fakes.

    Reflection runs every call (no module-level cache). The Typer
    registered_commands / registered_groups walks are dict-iteration on
    small lists (<50 entries), microsecond cost; caching would create
    invalidation surface that fights the slash_routing tests that
    monkeypatch ``ec_cli.app``.

    Examples:
        >>> _is_dispatch_compatible(["channels", "status"])
        True
        >>> _is_dispatch_compatible(["channels", "status", "--verbose"])  # prefix args
        True
        >>> _is_dispatch_compatible(["channels"])  # incomplete (group with subs)
        False
        >>> _is_dispatch_compatible(["nonexistent"])
        False
        >>> _is_dispatch_compatible([])
        False
        >>> _is_dispatch_compatible(["gateway"])  # blacklist
        False
        >>> _is_dispatch_compatible(["agent"])  # REPL — blacklist special
        False
        >>> _is_dispatch_compatible(["sandbox", "shell"])  # blacklist
        False
    """
    if not argv:
        return False
    # Blacklist check first (hard reject)
    for prefix in _DISPATCH_BLACKLIST:
        plen = len(prefix)
        if len(argv) >= plen and tuple(argv[:plen]) == prefix:
            return False
    if _is_agent_repl(argv):
        return False
    # Reflection-based positive check (replaces the v0.0.2 hardcoded
    # _DISPATCH_WHITELIST 19-tuple set). The helper module also serves
    # ``methods/commands.py`` so the two reflection sites can't drift.
    head = argv[0]
    app = ec_cli.app
    if head in _collect_command_names(app):
        return True
    sub_app = _find_group(app, head)
    if sub_app is not None:
        sub_names = _collect_command_names(sub_app)
        if not sub_names:
            # Bare-group dispatch (e.g. ``tui`` uses @callback(invoke_without_command=True)).
            # In real EC ``tui`` is blacklisted so this branch fires only for
            # test fakes that register a Typer subgroup with no subcommands.
            return bool(sub_app.info.invoke_without_command)
        if len(argv) < 2:
            return False  # incomplete: group name alone with no subcommand
        return argv[1] in sub_names
    return False


def _find_group(app, name: str):
    """Return the sub-``typer.Typer`` instance for ``name``, or ``None``."""
    for ti in app.registered_groups:
        if ti.name == name:
            return ti.typer_instance
    return None


def _invoke_ec_cli(argv: list[str]) -> int:
    """Synchronous wrapper around the EC Typer app, used by ``asyncio.to_thread``.

    Returns the command exit code (0 on success). Click's ``standalone_mode=False``
    catches ``click.exceptions.Exit`` (Typer's ``typer.Exit``, a ``RuntimeError``
    subclass) and **returns** its ``exit_code`` from ``app()`` instead of raising,
    so we must capture the return value here. ``SystemExit`` (rare, e.g.
    ``sys.exit()`` from helper code) is also handled here for completeness.

    We resolve ``ec_cli.app`` at call time (not import time) so monkey-patches
    in tests take effect.
    """
    try:
        result = ec_cli.app(argv, standalone_mode=False)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    # ``result`` is the exit_code from typer.Exit/click.Exit when raised under
    # standalone_mode=False. Non-int returns (None for normal completion) → 0.
    return int(result) if isinstance(result, int) else 0


async def cli_dispatch(
    params: dict, *, confirm_broker: "ConfirmBroker | None" = None
) -> dict:
    """Run an EC CLI command in-process; return ``CliResult``-shaped dict.

    When ``confirm_broker`` is supplied (TUI production path), ``typer.confirm``
    / ``click.confirm`` are bridged to the broker so destructive confirms are
    answered over RPC instead of reading the EOF dispatch stdin. The
    dispatch timeout then includes a ``_CONFIRM_HARD_LIMIT_S`` grace window so a
    paused-on-confirm command is not killed mid-prompt (path B). Without a
    broker, ``typer.confirm`` keeps its native behavior and the timeout is
    unchanged.

    Raises:
        ConfigValidationError (-32011): params shape / range invalid.
        NotDispatchCompatibleError (-32015): argv not in whitelist.
        CliCommandTimeoutError (-32014): command exceeded ``timeout_s``.
    """
    # ----- Param validation (Pydantic enforces 20 ≤ width ≤ 500) -----------
    try:
        validated = CliDispatchParams.model_validate(params)
    except ValidationError as exc:
        raise ConfigValidationError(
            "cli.dispatch params invalid",
            data={"errors": exc.errors()},
        ) from exc

    argv = list(validated.argv)
    width = validated.width
    timeout_s = validated.timeout_s if validated.timeout_s is not None else _DEFAULT_TIMEOUT_S

    # ----- Whitelist (-32015) ----------------------------------------------
    if not _is_dispatch_compatible(argv):
        # DEBUG level: under slash.exec this fires on every unknown / blacklist
        # slash typed by the user (e.g. /asd, /provider login). It is normal
        # operation, not info-worthy chatter — and at INFO it corrupts the
        # Ink reconciler when stderr inheritance is on.
        logger.debug("tui_rpc.cli.dispatch: rejected non-compatible argv: {!r}", argv)
        raise NotDispatchCompatibleError(
            f"argv {argv!r} not in cli.dispatch whitelist",
            data={"argv": argv, "hint": "use native UI for this command"},
        )

    # ----- Render buffers + Rich Consoles ----------------------------------
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    out_console = Console(
        file=stdout_buf,
        force_terminal=True,
        color_system="truecolor",
        width=width,
    )
    err_console = Console(
        file=stderr_buf,
        force_terminal=True,
        color_system="truecolor",
        width=width,
    )

    exit_code = 0

    # When a confirm broker is present, bridge typer.confirm into it and grant
    # the timeout a confirm grace window (path B). loop is captured here (we are
    # on the event loop) so the worker thread can run_coroutine_threadsafe back.
    if confirm_broker is not None:
        loop = asyncio.get_running_loop()
        confirm_ctx: contextlib.AbstractContextManager = confirm_injection(confirm_broker, loop)
        effective_timeout = timeout_s + _CONFIRM_HARD_LIMIT_S
    else:
        confirm_ctx = contextlib.nullcontext()
        effective_timeout = timeout_s

    # ----- Lock + redirect + inject + invoke -------------------------------
    async with _dispatch_lock:
        try:
            with (
                contextlib.redirect_stdout(stdout_buf),
                contextlib.redirect_stderr(stderr_buf),
                inject_consoles(out_console),
                confirm_ctx,
            ):
                try:
                    # _invoke_ec_cli returns the command exit code. Click
                    # under standalone_mode=False catches typer.Exit /
                    # click.exceptions.Exit and returns the exit_code from
                    # ``app()`` (not as raised exception). Critical for B1:
                    # without capturing this return value, all typer.Exit(N)
                    # paths silently report exit_code=0 to the TUI.
                    exit_code = await asyncio.wait_for(
                        asyncio.to_thread(_invoke_ec_cli, argv),
                        timeout=effective_timeout,
                    )
                except click.exceptions.UsageError as exc:
                    err_console.print(f"[red]Usage:[/] {exc.format_message()}")
                    exit_code = 2
                except click.exceptions.Abort:
                    # C1: a confirm hit the EOF dispatch stdin (no round-trip
                    # available). Abort is a RuntimeError subclass, NOT a
                    # ClickException, so without this it falls to the broad
                    # catch below as a useless "Internal error: Abort".
                    err_console.print(
                        "[yellow]This command needs confirmation; "
                        "re-run with --yes.[/]"
                    )
                    exit_code = 1
                except click.exceptions.ClickException as exc:
                    err_console.print(f"[red]Error:[/] {exc.format_message()}")
                    exit_code = 1
        except asyncio.TimeoutError as exc:
            logger.warning("tui_rpc.cli.dispatch: timeout after {}s argv={!r}", timeout_s, argv)
            raise CliCommandTimeoutError(
                f"command exceeded {timeout_s}s timeout",
                data={"argv": argv, "timeout_s": timeout_s},
            ) from exc
        except (CliCommandTimeoutError, NotDispatchCompatibleError, ConfigValidationError):
            # Re-raise RPC errors unchanged (timeout above is the main path
            # here; the others can't actually be raised inside this block,
            # but defensively preserve them).
            raise
        except Exception as exc:  # noqa: BLE001 — last-resort catch
            logger.exception("tui_rpc.cli.dispatch: unexpected error in argv={!r}", argv)
            err_console.print(f"[red]Internal error:[/] {type(exc).__name__}: {exc}")
            exit_code = 1

    # ----- Apply ANSI filter and return ------------------------------------
    return {
        "stdout": filter_ansi(stdout_buf.getvalue()),
        "stderr": filter_ansi(stderr_buf.getvalue()),
        "exit_code": exit_code,
    }


def register_cli_methods(
    dispatcher: "Dispatcher", *, confirm_broker: "ConfirmBroker | None" = None
) -> None:
    """Register ``cli.dispatch`` on a dispatcher instance.

    ``confirm_broker`` is pre-bound via a closure (mirrors the turn/emitter
    pattern) so the in-process confirm round-trip activates on the production
    path; when ``None`` (demo runner / tests) dispatch keeps native confirm.
    """

    async def _dispatch(params: dict) -> dict:
        return await cli_dispatch(params, confirm_broker=confirm_broker)

    dispatcher.register("cli.dispatch", _dispatch)


__all__ = [
    "cli_dispatch",
    "register_cli_methods",
    "_is_dispatch_compatible",
    "_DISPATCH_BLACKLIST",
]
