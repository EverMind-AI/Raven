"""claude-agent-acp: a precise tool name, and a result sent twice.

Three things this adapter does that the spec does not describe:

- It names the tool it actually ran in ``_meta.claudeCode.toolName`` (``Bash``,
  ``Read``, ...). That is finer than ``kind``, which cannot tell ``Glob`` from
  ``Grep`` -- both are ``kind: "search"``.
- It sends the same output twice: ``rawOutput`` carries the text, and
  ``content`` carries that same text wrapped in a markdown fence. The fence is
  for a client that renders markdown; a transcript row is not one, and the
  literal ```` ```console ```` was appearing in the rendered output.
- It never emits a ``tool_call`` for ``TodoWrite`` or the Task tools. Their
  state goes to a ``sessionUpdate: "plan"`` frame instead, so the plan row is
  the only place they appear, and it is named for the tool that produced it.

``Bash`` and ``Read`` are the two names measured on the wire (v0.66.0), and any
other name is reported exactly as sent rather than checked against a list: the
record keeps the transport's own vocabulary, so a tool this adapter adds or
renames upstream is recorded for what it is without an entry anywhere.
Those names now reach a client unchanged: the read boundary renames only the
ACP spec kinds, so a claude_code row is rendered under Claude Code's own
vocabulary.
"""

from __future__ import annotations

import re
from typing import Any

from raven.agent.subagent.acp_dialects.base import AcpDialect, ToolResult, _dict, content_texts

# A fence the adapter added, not one the tool's own output contained: it wraps
# the whole payload, so an inner fence (a result that really is markdown) never
# matches and is left alone.
_FENCED = re.compile(r"\A```[\w-]*\n(?P<body>.*?)\n?```\Z", re.DOTALL)


def _unfence(text: str) -> str:
    match = _FENCED.match(text.strip())
    return match.group("body") if match else text


class ClaudeCodeDialect(AcpDialect):
    key = "claude-agent-acp"
    plan_tool_name = "TodoWrite"

    def tool_name(self, update: dict[str, Any]) -> str:
        named = _dict(_dict(update.get("_meta")).get("claudeCode")).get("toolName")
        if isinstance(named, str) and named:
            return named
        return super().tool_name(update)

    def result(self, update: dict[str, Any]) -> ToolResult:
        ok = update.get("status") == "completed"
        raw = update.get("rawOutput")
        if isinstance(raw, str) and raw.strip():
            return ToolResult(text=raw, ok=ok)
        return ToolResult(text=_unfence("".join(content_texts(update.get("content")))), ok=ok)


__all__ = ["ClaudeCodeDialect"]
