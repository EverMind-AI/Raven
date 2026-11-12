"""The runtime-context lines a user message was sent with, kept so a later turn replays them.

The assembler opens every turn's user message with the runtime-context block
(``segments.render.build_runtime_context``). The session write keeps the user's
own words in ``content`` and files the lines beside them; the history projection
joins them back the way the assembler did, so a later turn replays the block
this turn sent, and the provider's cached prefix still matches through it.
Both sides join through :func:`with_runtime_context`, which is what keeps them
from drifting apart.

Because the blocks stay in the history, a tool notice is sent only when it
changes: :func:`told_notices` reads back what the replayed blocks already say,
and :func:`notice_changes` keeps what differs from it.
"""

from __future__ import annotations

import re
from typing import Any

from raven.context_engine.segments.render import RUNTIME_CONTEXT_TAG

RUNTIME_CONTEXT_KEY = "_runtime_context"
"""Set on the envelope the assembler built, holding the lines it opened with.

Underscored so no provider payload carries it (every transport sends an
allowlist of keys). ``_save_turn`` pops it, so it never reaches the session.
"""

RUNTIME_CONTEXT_FIELD = "runtime_context"
"""The same lines as the session entry stores them, beside the user's words."""


def with_runtime_context(lines: str, content: Any) -> Any:
    """``content`` opened with ``lines``, in the shape the content already has."""
    if isinstance(content, list):
        return [{"type": "text", "text": lines}, *content]
    return f"{lines}\n\n{content}"


def carries_runtime_context(content: Any, lines: str) -> bool:
    """Whether ``content`` still holds ``lines``, in a string or in one of its text blocks."""
    if isinstance(content, str):
        return lines in content
    if isinstance(content, list):
        return any(isinstance(b, dict) and isinstance(b.get("text"), str) and lines in b["text"] for b in content)
    return False


def replayed_content(message: dict[str, Any]) -> Any:
    """The content ``message`` was sent with, as far as its filed lines go.

    A user entry filed before the field existed, and every other entry, comes
    back as it is.
    """
    content = message.get("content")
    lines = message.get(RUNTIME_CONTEXT_FIELD)
    if message.get("role") != "user" or not isinstance(lines, str) or not lines:
        return content
    return with_runtime_context(lines, content)


MCP_NOTICE_HEADER = "MCP plugin changes since the last note (an earlier note stands until one here replaces it):"
"""Opens a turn's MCP lines, so a plugin no line names reads as unchanged rather than resolved."""

_MCP_SUBJECT = re.compile(r"MCP plugin '(?P<name>.+?)': ")
_CLEARED = "the earlier note about it no longer applies; this turn's tool definitions show what it offers."


def mcp_notice(name: str, text: str) -> str:
    """One MCP plugin's line; the name in it is what later turns compare the plugin by."""
    return f"MCP plugin '{name}': {text}"


def notice_subject(line: str) -> str | None:
    """The plugin ``line`` speaks of, or ``None`` for a line that names none."""
    match = _MCP_SUBJECT.match(line)
    return match["name"] if match else None


def told_notices(messages: list[dict[str, Any]]) -> dict[str, str]:
    """The MCP lines a model reading ``messages`` holds as current, by plugin.

    The runtime-context blocks are read in order: a line sets its plugin's
    note and a cleared line drops it. What a compaction took out of the
    messages is not read, so it is told again.
    """
    told: dict[str, str] = {}
    for message in messages:
        for line in _block_lines(message):
            name = notice_subject(line)
            if name is None:
                continue
            if line == mcp_notice(name, _CLEARED):
                told.pop(name, None)
            else:
                told[name] = line
    return told


def notice_changes(current: list[str], told: dict[str, str]) -> list[str]:
    """The tool-notice lines to send: each plugin line unlike ``told``, and every line that names no plugin.

    A plugin ``told`` holds and ``current`` no longer names gets a cleared line,
    since a missing line now means "unchanged".
    """
    unkeyed, keyed = _split(current)
    lines = [line for name, line in keyed.items() if told.get(name) != line]
    lines += [mcp_notice(name, _CLEARED) for name in told if name not in keyed]
    return [*unkeyed, MCP_NOTICE_HEADER, *lines] if lines else unkeyed


def notice_ceiling(current: list[str], session_messages: list[dict[str, Any]]) -> list[str]:
    """Every line :func:`notice_changes` can return over a history drawn from ``session_messages``.

    The history a turn replays exists only after the history strategy has
    budgeted the turn, so the budget is taken over this: all of ``current``,
    plus a cleared line for every plugin the session ever noted.
    """
    unkeyed, keyed = _split(current)
    noticed = {name for message in session_messages for line in _block_lines(message) if (name := notice_subject(line))}
    lines = [*keyed.values(), *(mcp_notice(name, _CLEARED) for name in sorted(noticed - keyed.keys()))]
    return [*unkeyed, MCP_NOTICE_HEADER, *lines] if lines else unkeyed


def _split(lines: list[str]) -> tuple[list[str], dict[str, str]]:
    unkeyed: list[str] = []
    keyed: dict[str, str] = {}
    for line in lines:
        name = notice_subject(line)
        if name is None:
            unkeyed.append(line)
        else:
            keyed[name] = line
    return unkeyed, keyed


def _block_lines(message: dict[str, Any]) -> list[str]:
    """The lines under the runtime-context tag ``message`` was sent with; none for any other message."""
    if message.get("role") != "user":
        return []
    filed = message.get(RUNTIME_CONTEXT_FIELD)
    text = filed if isinstance(filed, str) and filed else message.get("content")
    if isinstance(text, list):
        first = text[0] if text else None
        text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str) or not text.startswith(RUNTIME_CONTEXT_TAG):
        return []
    return text.split("\n\n", 1)[0].splitlines()[1:]
