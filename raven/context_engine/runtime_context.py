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
from raven.utils.image_store import expand_images
from raven.utils.wire import WIRE_AFTER_FIELD as WIRE_AFTER_FIELD
from raven.utils.wire import WIRE_CONTENT_FIELD as WIRE_CONTENT_FIELD

RUNTIME_CONTEXT_KEY = "_runtime_context"
"""Set on the envelope the assembler built, holding the lines it opened with.

Underscored so no provider payload carries it (every transport sends an
allowlist of keys). ``_save_turn`` pops it, so it never reaches the session.
"""

RUNTIME_CONTEXT_FIELD = "runtime_context"
"""The same lines as the session entry stores them, beside the user's words."""


PROVIDER_MESSAGE_KEYS = frozenset(
    {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
        "reasoning_content",
        "thinking_blocks",
    }
)
"""The keys a provider payload keeps; the wire rows are already this shape."""


def provider_row(message: dict[str, Any]) -> dict[str, Any]:
    """``message`` reduced to the keys a provider payload carries."""
    return {key: value for key, value in message.items() if key in PROVIDER_MESSAGE_KEYS}


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


def replayed_content(
    message: dict[str, Any],
    *,
    can_see_images: bool = True,
    tool_images_ok: bool = True,
) -> Any:
    """The content ``message`` was sent with, as far as its filed forms go.

    A wire form filed beside the display content wins when present; an entry
    filed before the field existed, and every other entry, comes back as its
    display content. Archived pictures are expanded only when the receiving
    model and transport can take them, which is the same verdict the live
    assembly applied to the message they came from.
    """
    wire = message.get(WIRE_CONTENT_FIELD)
    content = expand_images(
        message.get("content") if wire is None else wire,
        message,
        can_see_images=can_see_images,
        tool_images_ok=tool_images_ok,
    )
    lines = message.get(RUNTIME_CONTEXT_FIELD)
    if message.get("role") != "user" or not isinstance(lines, str) or not lines:
        return content
    return with_runtime_context(lines, content)


MCP_NOTICE_HEADER = "MCP plugin changes since the last note (an earlier note stands until one here replaces it):"
"""Opens a turn's MCP lines, so a plugin no line names reads as unchanged rather than resolved."""

TOOL_NOTICE_HEADER = (
    "Tool availability changes since the last note (an earlier note stands until one here replaces it):"
)
"""Opens a turn's tool-availability lines, so a tool no line names reads as unchanged."""

_MCP_SUBJECT = re.compile(r"MCP plugin '(?P<name>(?:[^'\\]|\\.)*)': ")
_LEGACY_MCP_SUBJECT = re.compile(r"MCP plugin '(?P<name>.+?)': ")
"""The name a line filed before the escaping held: the raw text up to the first ``': ``.

Read when the strict pattern above does not describe a line :func:`mcp_notice` wrote,
which is how a raw name holding a quote or a lone backslash is still recovered.
"""

_TOOL_SUBJECT = re.compile(r"Tool '(?P<name>(?:[^'\\]|\\.)*)': ")

_CLEARED = "the earlier note about it no longer applies; this turn's tool definitions show what it offers."
_TOOL_CLEARED = "available again -- the earlier note no longer applies."


def mcp_notice(name: str, text: str) -> str:
    """One MCP plugin's line; the name in it is what later turns compare the plugin by.

    The name's own quotes and backslashes are escaped, so :func:`notice_subject`
    reads back exactly this name whatever it contains.
    """
    quoted = name.replace("\\", "\\\\").replace("'", "\\'")
    return f"MCP plugin '{quoted}': {text}"


def tool_notice(name: str, text: str) -> str:
    """One tool's availability line, keyed by the tool name the same way.

    A tool and an MCP plugin can share a name (``web_search`` is plausible for
    both), so the kind is part of the key :func:`notice_key` reads back; the
    two lines never replace each other.
    """
    quoted = name.replace("\\", "\\\\").replace("'", "\\'")
    return f"Tool '{quoted}': {text}"


def notice_subject(line: str) -> str | None:
    """The plugin ``line`` speaks of, or ``None`` for a line that names none.

    A line :func:`mcp_notice` wrote is read back through its escapes. A line a
    session filed before that escaping existed is read the way it was written, up
    to the first ``': ``. The two formats are ambiguous for a raw name holding a
    backslash: it can look escaped, and the escaped reading wins there.
    """
    key = _mcp_key(line)
    return key[1] if key is not None else None


def notice_key(line: str) -> tuple[str, str] | None:
    """What ``line`` speaks of: ``("mcp", plugin)``, ``("tool", name)``, or ``None``.

    The kind is in the key so a plugin and a tool of the same name keep their
    own notes; a line that names neither (a header, a host notice) is unkeyed and
    is sent every turn.
    """
    mcp = _mcp_key(line)
    if mcp is not None:
        return mcp
    match = _TOOL_SUBJECT.match(line)
    if match is not None and _written_escaped(match["name"]):
        return "tool", re.sub(r"\\(.)", r"\1", match["name"])
    return None


def _mcp_key(line: str) -> tuple[str, str] | None:
    match = _MCP_SUBJECT.match(line)
    if match is not None and _written_escaped(match["name"]):
        return "mcp", re.sub(r"\\(.)", r"\1", match["name"])
    legacy = _LEGACY_MCP_SUBJECT.match(line)
    return ("mcp", legacy["name"]) if legacy else None


def _written_escaped(name: str) -> bool:
    """Whether ``name`` is the escaped form :func:`mcp_notice` writes.

    Every backslash it wrote begins an escape of ``\\`` or ``'``; a raw name filed
    before the escaping can hold a lone backslash, which means the line is old.
    """
    at = 0
    while at < len(name):
        if name[at] == "\\":
            if at + 1 == len(name) or name[at + 1] not in "\\'":
                return False
            at += 2
        else:
            at += 1
    return True


def _clears(key: tuple[str, str], line: str) -> bool:
    """Whether ``line`` clears ``key``: in the escaped form, or the raw one filed before it."""
    kind, name = key
    if kind == "tool":
        return line == tool_notice(name, _TOOL_CLEARED)
    return line == mcp_notice(name, _CLEARED) or line == f"MCP plugin '{name}': {_CLEARED}"


def _cleared_line(key: tuple[str, str]) -> str:
    kind, name = key
    return tool_notice(name, _TOOL_CLEARED) if kind == "tool" else mcp_notice(name, _CLEARED)


def told_notices(messages: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """The notes a model reading ``messages`` holds as current, by kind and name.

    The runtime-context blocks are read in order: a line sets its subject's
    note and a cleared line drops it. What a compaction took out of the
    messages is not read, so it is told again.
    """
    told: dict[tuple[str, str], str] = {}
    for message in messages:
        for line in _block_lines(message):
            key = notice_key(line)
            if key is None:
                continue
            if _clears(key, line):
                told.pop(key, None)
            else:
                told[key] = line
    return told


def notice_changes(current: list[str], told: dict[tuple[str, str], str]) -> list[str]:
    """The tool-notice lines to send: each subject line unlike ``told``, and every line that names no subject.

    A subject ``told`` holds and ``current`` no longer names gets a cleared line,
    since a missing line now means "unchanged".
    """
    unkeyed, keyed = _split(current)
    pairs = [(key, line) for key, line in keyed.items() if told.get(key) != line]
    pairs += [(key, _cleared_line(key)) for key in told if key not in keyed]
    mcp = [line for key, line in pairs if key[0] == "mcp"]
    tools = [line for key, line in pairs if key[0] == "tool"]
    lines = list(unkeyed)
    if mcp:
        lines += [MCP_NOTICE_HEADER, *mcp]
    if tools:
        lines += [TOOL_NOTICE_HEADER, *tools]
    return lines


def notice_ceiling(current: list[str], session_messages: list[dict[str, Any]]) -> list[str]:
    """Every line :func:`notice_changes` can return over a history drawn from ``session_messages``.

    The history a turn replays exists only after the history strategy has
    budgeted the turn, so the budget is taken over this: all of ``current``,
    plus a cleared line for every subject the session ever noted.
    """
    unkeyed, keyed = _split(current)
    noticed = {
        key for message in session_messages for line in _block_lines(message) if (key := notice_key(line)) is not None
    }
    pairs = list(keyed.items())
    pairs += [(key, _cleared_line(key)) for key in sorted(noticed - keyed.keys())]
    mcp = [line for key, line in pairs if key[0] == "mcp"]
    tools = [line for key, line in pairs if key[0] == "tool"]
    lines = list(unkeyed)
    if mcp:
        lines += [MCP_NOTICE_HEADER, *mcp]
    if tools:
        lines += [TOOL_NOTICE_HEADER, *tools]
    return lines


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
        present = set(lines.splitlines())
        extra = [line for line in repair if line not in (MCP_NOTICE_HEADER, TOOL_NOTICE_HEADER) or line not in present]
        told = "\n".join([lines, *extra])
        content = _replaced(message.get("content"), lines, told)
        if content is None:
            continue
        retold = list(after)
        retold[index] = {**message, "content": content, RUNTIME_CONTEXT_KEY: told}
        return retold
    return after


def without_runtime_context(message: dict[str, Any]) -> Any:
    """``message``'s content with the lines its own record holds taken out.

    A reclaim may retell a plugin note into the question's block without
    touching the words it opened with, so a comparison reads those words and
    not the block.
    """
    content = message.get("content")
    lines = message.get(RUNTIME_CONTEXT_KEY)
    if not isinstance(lines, str) or not lines:
        return content
    stripped = _replaced(content, lines, "")
    return content if stripped is None else stripped


def _replaced(content: Any, lines: str, told: str) -> Any:
    """``content`` with the ``lines`` it holds told as ``told``; ``None`` when it does not hold them."""
    if isinstance(content, str):
        return content.replace(lines, told, 1) if lines in content else None
    at = _block_holding(content, lines) if isinstance(content, list) else None
    if at is None:
        return None
    return [*content[:at], {**content[at], "text": content[at]["text"].replace(lines, told, 1)}, *content[at + 1 :]]


def _split(lines: list[str]) -> tuple[list[str], dict[tuple[str, str], str]]:
    unkeyed: list[str] = []
    keyed: dict[tuple[str, str], str] = {}
    for line in lines:
        key = notice_key(line)
        if key is None:
            unkeyed.append(line)
        else:
            keyed[key] = line
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
