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
        return _block_holding(content, lines) is not None
    return False


def _block_holding(content: list[Any], lines: str) -> int | None:
    return next(
        (
            i
            for i, b in enumerate(content)
            if isinstance(b, dict) and isinstance(b.get("text"), str) and lines in b["text"]
        ),
        None,
    )


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

_MCP_SUBJECT = re.compile(r"MCP plugin '(?P<name>(?:[^'\\]|\\.)*)': ")
_CLEARED = "the earlier note about it no longer applies; this turn's tool definitions show what it offers."


def mcp_notice(name: str, text: str) -> str:
    """One MCP plugin's line; the name in it is what later turns compare the plugin by.

    The name's own quotes and backslashes are escaped, so :func:`notice_subject`
    reads back exactly this name whatever it contains.
    """
    quoted = name.replace("\\", "\\\\").replace("'", "\\'")
    return f"MCP plugin '{quoted}': {text}"


def notice_subject(line: str) -> str | None:
    """The plugin ``line`` speaks of, or ``None`` for a line that names none."""
    match = _MCP_SUBJECT.match(line)
    return re.sub(r"\\(.)", r"\1", match["name"]) if match else None


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


def retell_notices(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``after``, a trimmed copy of ``before``, telling the plugin notes the trimming took away.

    A reclaim trims the history after the question was built, and the question's
    block may have left a note out because a reclaimed message had told it. The
    lines that bring ``after`` back to the state ``before`` reads are added to the
    newest envelope that recorded its block (``RUNTIME_CONTEXT_KEY``) and still
    holds it, wherever in the content the block now sits; ``after`` itself comes
    back when nothing was lost.
    """
    repair = notice_changes(list(told_notices(before).values()), told_notices(after))
    if not repair:
        return after
    for index in range(len(after) - 1, -1, -1):
        message = after[index]
        lines = message.get(RUNTIME_CONTEXT_KEY)
        if message.get("role") != "user" or not isinstance(lines, str) or not lines:
            continue
        told = "\n".join([lines, *(repair[1:] if MCP_NOTICE_HEADER in lines.splitlines() else repair)])
        content = _replaced(message.get("content"), lines, told)
        if content is None:
            continue
        retold = list(after)
        retold[index] = {**message, "content": content, RUNTIME_CONTEXT_KEY: told}
        return retold
    return after


def _replaced(content: Any, lines: str, told: str) -> Any:
    """``content`` with the ``lines`` it holds told as ``told``; ``None`` when it does not hold them."""
    if isinstance(content, str):
        return content.replace(lines, told, 1) if lines in content else None
    at = _block_holding(content, lines) if isinstance(content, list) else None
    if at is None:
        return None
    return [*content[:at], {**content[at], "text": content[at]["text"].replace(lines, told, 1)}, *content[at + 1 :]]


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
    """The lines under the runtime-context tag ``message`` was sent with; none for any other message.

    A recorded block is read from its record, since text put in front of the
    question after assembly (a recovery notice) moves it off the content's start.
    """
    if message.get("role") != "user":
        return []
    content = message.get("content")
    filed = message.get(RUNTIME_CONTEXT_FIELD)
    sent = message.get(RUNTIME_CONTEXT_KEY)
    if isinstance(filed, str) and filed:
        text = filed
    elif isinstance(sent, str) and sent and carries_runtime_context(content, sent):
        text = sent
    else:
        text = content
    if isinstance(text, list):
        first = text[0] if text else None
        text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str) or not text.startswith(RUNTIME_CONTEXT_TAG):
        return []
    return text.split("\n\n", 1)[0].splitlines()[1:]
