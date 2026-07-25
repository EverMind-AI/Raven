"""Tests for the ``shell.exec`` RPC method.

Covers EverMind-AI/Raven#171: every `!`-prefixed bang command in the TUI
failed with ``[rpc -32601] method_not_found``. ``useSubmission.ts``'s
``shellExec`` / ``interpolate`` call ``gw.request('shell.exec', {command})``,
but the backend never registered a ``shell.exec`` handler.
"""

from __future__ import annotations

import pytest

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods.shell_exec import register_shell_methods, shell_exec


@pytest.fixture
def dispatcher() -> Dispatcher:
    d = Dispatcher()
    register_shell_methods(d)
    return d


async def test_shell_exec_registered_on_umbrella_dispatcher() -> None:
    """The production umbrella (register_aligned_methods_except_system) must
    expose ``shell.exec`` — the same registration-drift bug the
    slash_routing methods were added to prevent."""
    from raven.tui_rpc.methods import register_aligned_methods_except_system

    d = Dispatcher()
    register_aligned_methods_except_system(d)
    assert "shell.exec" in d.methods()


async def test_shell_exec_runs_command_and_returns_stdout(dispatcher: Dispatcher) -> None:
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "shell.exec",
        "params": {"command": "echo hello"},
    }
    response = await dispatcher.dispatch(frame)

    assert "error" not in response
    result = response["result"]
    assert result["code"] == 0
    assert result["stdout"].strip() == "hello"


async def test_shell_exec_reports_nonzero_exit_code(dispatcher: Dispatcher) -> None:
    frame = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "shell.exec",
        "params": {"command": "exit 3"},
    }
    response = await dispatcher.dispatch(frame)

    assert response["result"]["code"] == 3


async def test_shell_exec_empty_command_does_not_raise() -> None:
    result = await shell_exec({"command": "  "})

    assert result["code"] != 0
    assert result["stdout"] == ""
