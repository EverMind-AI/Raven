"""``shell.exec`` RPC handler -- the TUI's ``!command`` escape.

ui-tui runs this for two things: a line the user prefixed with ``!``, and each
``$(...)`` span it interpolates before sending a prompt. Both are the human's
own command, so the agent-facing guards on ``ExecTool`` (deny-list, approval
prompt) do not apply -- a user who types ``!rm -rf build`` is not an agent that
needs talking out of it.

What does still apply is the sandbox. When a run is configured with one, the
command goes through that same executor, so this surface cannot be used to
reach the host from a session the operator deliberately confined. Only when
there is no live loop to borrow an executor from does it fall back to running
directly.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from raven.sandbox.direct_executor import DirectExecutor

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods.session import AgentLoopFactory


# Bounds a command that never returns. The TUI blocks its composer for the
# whole call, so an unbounded wait would look like a hung UI with no way back.
_TIMEOUT_SECONDS = 120


def _exec_tool(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    """The live ``ExecTool``, or ``None`` when there is no loop to ask."""
    loop = agent_loop_factory() if agent_loop_factory is not None else None
    tools = getattr(loop, "tools", None) if loop is not None else None
    return tools.get("exec") if tools is not None else None


async def shell_exec(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``shell.exec`` -- run one command, return its streams and exit code.

    A non-zero exit is a result, not an RPC error: the client prints the output
    then ``exit <code>``, which it cannot do if the call rejects.
    """
    command = str(params.get("command") or "").strip()
    if not command:
        return {"code": 0, "stdout": "", "stderr": ""}

    tool = _exec_tool(agent_loop_factory)
    executor = getattr(tool, "_executor", None) or DirectExecutor()
    cwd = getattr(tool, "working_dir", None) or os.getcwd()

    try:
        # An executor reports its own timeout as a result (exit code -1 with a
        # "Timed out" stderr), so there is no timeout branch here -- inventing
        # a second convention would just disagree with the agent's own exec.
        result = await executor.exec(command, cwd=cwd, timeout=_TIMEOUT_SECONDS)
    except Exception as exc:
        # Surfaced as a failed command rather than a -32603 so the client can
        # render it the way it renders any other command that did not work.
        return {"code": 127, "stdout": "", "stderr": str(exc)}

    return {
        "code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def register_shell_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register ``shell.exec`` on a dispatcher instance."""

    async def _exec(params: dict) -> dict:
        return await shell_exec(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("shell.exec", _exec)


__all__ = ["shell_exec", "register_shell_methods"]
