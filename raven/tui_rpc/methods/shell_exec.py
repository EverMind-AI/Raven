"""``shell.exec`` RPC method handler.

Wires the RPC method the TUI's `!`-prefixed bang-shell escape calls
(``ui-tui/src/app/useSubmission.ts``'s ``shellExec`` / ``interpolate``) but
which the backend never registered, so every bang command failed with
``[rpc -32601] method_not_found`` (EverMind-AI/Raven#171).

The issue's own root-cause evidence was gathered against the released
v0.1.6 bundle, which called a method named ``command.dispatch``; that name
is gone from the current frontend source, which calls ``shell.exec``
instead (``useSubmission.ts:180,212``) — ``command.dispatch`` only survives
as dead code in ``createSlashHandler.ts``'s unreachable ``.catch()``
fallback (``slash.exec`` never rejects, so that branch cannot fire). On
current HEAD ``shell.exec`` is the method that is actually missing.

``shell.exec`` carries no ``session_id`` (both call sites pass only
``{command}``), so there is no session-scoped sandbox executor to route
through; it always runs on the host via :class:`DirectExecutor`, same as a
plain shell escape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.sandbox import DirectExecutor

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher

_TIMEOUT_S = 60


async def shell_exec(params: dict[str, Any]) -> dict[str, Any]:
    """Run a bang-shell command on the host; return ``ShellExecResponse``.

    Never raises -32xxx: ``useSubmission.ts`` reads ``{code, stdout, stderr}``
    off a resolved promise, so an empty command or a executor failure
    degrades to a non-zero ``code`` instead of an RPC error.
    """
    command = str(params.get("command", "")).strip()
    if not command:
        return {"code": 1, "stdout": "", "stderr": "empty command"}

    try:
        result = await DirectExecutor().exec(command, timeout=_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — surface as stderr, not an RPC error
        return {"code": 1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}

    return {"code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}


def register_shell_methods(dispatcher: "Dispatcher") -> None:
    """Register ``shell.exec`` on a dispatcher instance."""
    dispatcher.register("shell.exec", shell_exec)


__all__ = ["register_shell_methods", "shell_exec"]
