"""The runtime-context lines a user message was sent with, kept so a later turn replays them.

The assembler opens every turn's user message with the runtime-context block
(``segments.render.build_runtime_context``). The session write keeps the user's
own words in ``content`` and files the lines beside them; the history projection
joins them back the way the assembler did, so the message a later turn replays
is the message this turn sent, and the provider's cached prefix still matches.
Both sides join through :func:`with_runtime_context`, which is what keeps them
from drifting apart.
"""

from __future__ import annotations

from typing import Any

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
