"""codex-acp: no content, a structured rawOutput, and its own exit code.

Measured on v1.1.14, this adapter reports a finished call with no ``content`` at
all -- only ``rawOutput``, as an object. Serialising that object is what put
``{"formatted_output": "...", "exit_code": 0}`` in the transcript where the
command's output belonged.

The ``exit_code`` inside it is also the only place a failed command is reported:
the frame's own ``status`` is ``completed`` for a command that exited non-zero,
because the *call* completed. Reading the status alone marked every failed
command as successful.

Its ``read`` calls carry no ``rawInput`` whatsoever; the base dialect already
falls back to ``locations``, which is where the path is.
"""

from __future__ import annotations

from typing import Any

from raven.agent.subagent.acp_dialects.base import AcpDialect, ToolResult, _dict


class CodexDialect(AcpDialect):
    key = "codex-acp"

    def result(self, update: dict[str, Any]) -> ToolResult:
        ok = update.get("status") == "completed"
        raw = update.get("rawOutput")
        if isinstance(raw, dict):
            formatted = raw.get("formatted_output")
            exit_code = raw.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                ok = False
            if isinstance(formatted, str):
                # An empty-but-present formatted_output is the real answer for a
                # command that printed nothing, so the exit code is what says so.
                return ToolResult(text=formatted or f"(no output, exit {exit_code})", ok=ok)

        text = super().result(update).text
        return ToolResult(text=text, ok=ok)

    def argument(self, update: dict[str, Any]) -> str:
        """The command as sent, not the title's truncation of it.

        The title is clipped to about 100 characters on the wire, so a long
        pipeline loses its tail there while ``rawInput.command`` keeps it.
        """
        command = _dict(update.get("rawInput")).get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()
        return super().argument(update)


__all__ = ["CodexDialect"]
