"""Slash command routing handlers.

Wires four RPC methods that the fork-imported hermes UI invokes but were not
previously registered, causing dogfood failures:

* ``slash.exec`` — hermes routes any *unknown* slash here
  (``ui-tui/src/app/createSlashHandler.ts:82``) expecting
  ``{output?, warning?}``. We shlex-split the command, delegate to
  ``cli.dispatch``, and map the result. **Never raises -32xxx** — failures
  arrive as ``{output: "", warning: "..."}`` so the UI's ``.then()`` branch
  consumes them (the ``.catch()`` branch would fall through to
  ``command.dispatch`` which is also unregistered).
* ``session.status`` — replaces the hermes-only stub (-32012) with a real
  delegate to ``cli.dispatch(["status"])`` so the ``/status`` slash renders
  Raven status output instead of a "not supported" toast.
* ``complete.slash`` / ``complete.path`` — return empty completion lists so
  ``useCompletion.ts`` stops surfacing the "completion unavailable" red-frame
  toast on every keystroke.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.tui_rpc.errors import (
    CliCommandTimeoutError,
    ConfigValidationError,
    NotDispatchCompatibleError,
)
from raven.tui_rpc.methods.cli_dispatch import (
    _DISPATCH_BLACKLIST,
    _is_agent_repl,
    cli_dispatch,
)

if TYPE_CHECKING:
    from raven.tui_rpc.confirm_broker import ConfirmBroker
    from raven.tui_rpc.dispatcher import Dispatcher


_DEFAULT_WIDTH = 100
"""Default Rich console width when slash.exec is invoked from the hermes UI.

The hermes ``createSlashHandler`` does not propagate viewport width to
``slash.exec`` (it only sends ``{command, session_id}``). 100 cols is a sane
middle ground — wide tables can be revisited in v0.0.3 if a width hint is
added to the hermes slash handler.
"""

_SLASH_TIMEOUT_S = 20.0
"""Tighter than cli.dispatch's 30s default — slash commands are interactive
and a 20s ceiling keeps the UI responsive."""


def _shape_unknown_command_warning(command: str) -> dict[str, str]:
    """Build a friendly message for an unknown / out-of-scope verb.

    We put the message in ``output`` (not ``warning``) on purpose: hermes's
    ``createSlashHandler.ts:88`` falls back to ``/<name>: no output`` when
    ``output`` is empty, so a ``warning``-only response renders as

        warning: unknown command: /asd …
        /asd: no output

    which has an ugly redundant trailing line. Putting the message in
    ``output`` collapses to a clean single line.
    """
    return {
        "output": f"unknown command: /{command} — try `/help` or run `raven --help` in a terminal",
    }


def _shape_blacklist_warning(command: str) -> dict[str, str]:
    """Friendly message for P3 terminal-only commands (output, not warning).

    Same rationale as :func:`_shape_unknown_command_warning`.
    """
    return {
        "output": f"/{command} requires a real terminal — run it directly with `raven {command}`",
    }


async def slash_exec(params: dict[str, Any], *, confirm_broker: "ConfirmBroker | None" = None) -> dict[str, Any]:
    """Route an unknown hermes slash to ``cli.dispatch``.

    Hermes UI invokes us with ``{command: "channels status", session_id: ...}``
    when the user types a slash that is not in hermes's local registry. We:

    1. shlex-split ``command`` into argv (preserves quoted args).
    2. Delegate to ``cli.dispatch`` with default width / tighter timeout.
    3. Map the result to ``SlashExecResponse`` ``{output, warning?}``:
       - whitelist + exit 0 → ``{output: stdout}``
       - whitelist + exit != 0 → ``{output: stdout, warning: stderr|exit_code}``
       - blacklist → toast-friendly "use a real terminal" warning
       - unknown verb → toast-friendly "unknown command" warning
       - timeout → "command exceeded N s timeout" warning
       - empty / whitespace command → "empty slash command" warning

    Never raises -32xxx. Hermes's ``createSlashHandler.ts:83-92`` consumes the
    response via ``r?.output`` / ``r?.warning``.
    """
    raw_command = str(params.get("command", "")).strip()
    if not raw_command:
        # Empty / whitespace slash → friendly hint in output (no warning field
        # to avoid the createSlashHandler.ts:88 "/: no output" tail).
        return {"output": "(empty slash command — type /help for a list)"}

    try:
        argv = shlex.split(raw_command)
    except ValueError as exc:
        return {"output": f"could not parse command: {exc}"}

    if not argv:
        return {"output": "(empty slash command — type /help for a list)"}

    try:
        result = await cli_dispatch(
            {
                "argv": argv,
                "width": _DEFAULT_WIDTH,
                "timeout_s": _SLASH_TIMEOUT_S,
            },
            confirm_broker=confirm_broker,
        )
    except NotDispatchCompatibleError:
        # Either P3 blacklist (provider login / gateway / sandbox shell /
        # channels login / agent-REPL) or a verb not in the whitelist. We
        # distinguish by checking the well-known blacklist prefixes.
        if _is_blacklist_argv(argv):
            return _shape_blacklist_warning(raw_command)
        return _shape_unknown_command_warning(raw_command)
    except CliCommandTimeoutError:
        return {"output": f"command exceeded {_SLASH_TIMEOUT_S:.0f}s timeout"}
    except ConfigValidationError as exc:
        logger.debug("slash.exec: cli.dispatch param validation failed: {}", exc)
        return {"output": f"invalid slash payload: {exc}"}

    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    exit_code = int(result.get("exit_code", 0) or 0)

    if exit_code == 0:
        return {"output": stdout}

    # Non-zero exit — surface stderr (or a generic hint) as the warning so the
    # user can see what went wrong without losing partial stdout.
    warning = stderr.strip() or f"command exited with status {exit_code}"
    return {"output": stdout, "warning": warning}


def _is_blacklist_argv(argv: list[str]) -> bool:
    """Identify P3 blacklist hits so we can choose the right toast message.

    Reads ``_DISPATCH_BLACKLIST`` (cli_dispatch.py) as the single source of
    truth — keeps this module aligned with any future blacklist extension
    automatically. harness-command-catalog-dynamic added ``tui`` + ``onboard``
    to the set and the prior hardcoded variant here would have silently
    misclassified both as "unknown command" rather than the correct
    "requires a real terminal" toast.

    ``agent`` no-``-m`` is special-cased separately (the blacklist set itself
    cannot encode "argv[0]==agent AND no -m" as a prefix tuple).
    """
    if not argv:
        return False
    for prefix in _DISPATCH_BLACKLIST:
        plen = len(prefix)
        if len(argv) >= plen and tuple(argv[:plen]) == prefix:
            return True
    # agent REPL — handled by the cli_dispatch ``_is_agent_repl`` helper.
    return _is_agent_repl(argv)


async def session_status(params: dict[str, Any]) -> dict[str, Any]:
    """Real ``session.status`` handler — delegates to ``raven status``.

    Replaces the hermes-only -32012 stub so the ``/status`` slash renders
    Raven's Rich status output via ``SessionStatusResponse {output}``.
    """
    result = await cli_dispatch(
        {
            "argv": ["status"],
            "width": _DEFAULT_WIDTH,
            "timeout_s": _SLASH_TIMEOUT_S,
        }
    )
    return {"output": result.get("stdout", "")}


async def complete_slash(params: dict[str, Any]) -> dict[str, Any]:
    """No-op completion provider for slash names.

    Silences ``useCompletion.ts:97-108``'s "completion unavailable" red-frame
    toast. Real completion (slash registry walk) is v0.0.3 polish.
    """
    return {"items": [], "replace_from": 1}


async def complete_path(params: dict[str, Any]) -> dict[str, Any]:
    """No-op completion provider for filesystem paths.

    Same rationale as :func:`complete_slash`. v0.0.3 may add glob-based
    suggestions; v0.0.2 just stops the toast spam.
    """
    return {"items": []}


def register_slash_routing_methods(dispatcher: "Dispatcher", *, confirm_broker: "ConfirmBroker | None" = None) -> None:
    """Register all four slash routing handlers.

    ``confirm_broker`` is pre-bound to ``slash.exec`` so destructive commands
    typed as slashes in the TUI get the confirm round-trip (the production
    path users actually take). ``session.status`` / ``complete.*`` never
    confirm, so they stay broker-less.
    """

    async def _slash_exec(params: dict[str, Any]) -> dict[str, Any]:
        return await slash_exec(params, confirm_broker=confirm_broker)

    dispatcher.register("slash.exec", _slash_exec)
    dispatcher.register("session.status", session_status)
    dispatcher.register("complete.slash", complete_slash)
    dispatcher.register("complete.path", complete_path)


__all__ = [
    "complete_path",
    "complete_slash",
    "register_slash_routing_methods",
    "session_status",
    "slash_exec",
]
