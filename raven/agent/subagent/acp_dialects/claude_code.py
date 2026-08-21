"""claude-agent-acp: a precise tool name, and a result sent twice.

Two things this adapter does that the spec does not describe:

- It names the tool it actually ran in ``_meta.claudeCode.toolName`` (``Bash``,
  ``Read``, ...). That is finer than ``kind``, which cannot tell ``Glob`` from
  ``Grep`` -- both are ``kind: "search"``.
- It sends the same output twice: ``rawOutput`` carries the text, and
  ``content`` carries that same text wrapped in a markdown fence. The fence is
  for a client that renders markdown; a transcript row is not one, and the
  literal ```` ```console ```` was appearing in the rendered output.

``Bash`` and ``Read`` are the two names measured on the wire (v0.66.0). The rest
of the table is Claude Code's published tool set, and any name not in it falls
through to the base dialect's ``kind`` mapping -- so a renamed or added tool
degrades to the spec answer instead of being mislabelled.
"""

from __future__ import annotations

import re
from typing import Any

from raven.agent.subagent.acp_dialects.base import AcpDialect, ToolResult, _dict, content_texts

_TOOL_NAMES = {
    "Bash": "exec",
    "BashOutput": "exec",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "NotebookEdit": "edit_file",
    "Glob": "find",
    "Grep": "grep",
    "LS": "list_dir",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Task": "spawn",
}

# A fence the adapter added, not one the tool's own output contained: it wraps
# the whole payload, so an inner fence (a result that really is markdown) never
# matches and is left alone.
_FENCED = re.compile(r"\A```[\w-]*\n(?P<body>.*?)\n?```\Z", re.DOTALL)


def _unfence(text: str) -> str:
    match = _FENCED.match(text.strip())
    return match.group("body") if match else text


class ClaudeCodeDialect(AcpDialect):
    key = "claude-agent-acp"

    def tool_name(self, update: dict[str, Any]) -> str:
        named = _dict(_dict(update.get("_meta")).get("claudeCode")).get("toolName")
        if isinstance(named, str) and named in _TOOL_NAMES:
            return _TOOL_NAMES[named]
        return super().tool_name(update)

    def result(self, update: dict[str, Any]) -> ToolResult:
        ok = update.get("status") == "completed"
        raw = update.get("rawOutput")
        if isinstance(raw, str) and raw.strip():
            return ToolResult(text=raw, ok=ok)
        return ToolResult(text=_unfence("".join(content_texts(update.get("content")))), ok=ok)


__all__ = ["ClaudeCodeDialect"]
