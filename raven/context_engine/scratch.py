"""Turn scratch — the per-turn block the user message carries to the model.

Recalled memory and routed skills change with every query. Rendered into the
system prompt they sit ahead of the whole conversation in every provider's
cache prefix, so each turn discards everything cached behind them. Rendered at
the end of the turn's user message instead, they leave the system prompt
byte-identical across turns, so its cache entry and the tools' are read back on
the next turn.

The history behind them has to carry the same bytes, or the turn boundary
breaks the provider's cache just the same: the envelope is filed with the
scratch beside the user's words, and the replay puts it back where it was
appended. The stored ``content`` stays the user's words alone, so the session
log and everything that reads it (the transcript, the novelty window) see the
question and not the recall.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# Same bracketed-sentence shape as ContextBuilder._RUNTIME_CONTEXT_TAG: it tells
# the model what it is reading. The scratch is filed with the message and
# replayed, so the tag says what the block is: the recall of the turn it was
# asked in.
TURN_SCRATCH_TAG = "[Turn Scratch — recalled for the turn it was asked in]"

TURN_SCRATCH_KEY = "_turn_scratch"
"""Set on the envelope the scratch was appended to, holding the appended text.

Underscored so no provider payload carries it (every transport sends an
allowlist of keys). ``_save_turn`` pops it and files the text as
``TURN_SCRATCH_FIELD``.
"""

TURN_SCRATCH_FIELD = "turn_scratch"
"""The scratch as the session entry stores it, beside the user's words."""

TURN_SCRATCH_AT_FIELD = "turn_scratch_at"
"""Where the scratch stood in the message it was cut from.

A hook note lands on the message after the scratch was appended, so the
scratch is not always last, and the replay puts it back where it stood.
"""


def render_turn_scratch(parts: Sequence[str]) -> str:
    """Join segment texts into one tagged block, or ``""`` when there is none."""
    body = "\n\n---\n\n".join(part for part in parts if part)
    return f"{TURN_SCRATCH_TAG}\n\n{body}" if body else ""


def append_turn_scratch(content: Any, scratch: str) -> Any:
    """Append ``scratch`` to a user message's content, keeping its shape.

    A string stays a string. Converting it to a one-element block list would be
    irreversible downstream: ``prompt_cache.strip`` only unwraps content that is
    exactly one text block, and "content must be a string" is among the
    commonest ways an OpenAI-compatible endpoint refuses a request.
    """
    if not scratch:
        return content
    if isinstance(content, str):
        return f"{content}\n\n{scratch}"
    if isinstance(content, list):
        return [*content, {"type": "text", "text": scratch}]
    return content


def mark_turn_scratch(message: dict[str, Any], scratch: str) -> dict[str, Any]:
    """Append ``scratch`` to ``message`` and record on it exactly what was appended.

    The record (``TURN_SCRATCH_KEY``) is what every reader strips by. Cutting at
    the first sight of the tag would also cut a user who pasted a log carrying
    it, or a later message that quotes one; the recorded text is only ever on
    the envelope the assembler built.
    """
    if not scratch:
        return message
    message["content"] = append_turn_scratch(message.get("content"), scratch)
    message[TURN_SCRATCH_KEY] = scratch
    return message


def cut_turn_scratch(content: Any, scratch: str | None) -> Any | None:
    """``content`` without the ``scratch`` that was appended to it.

    Only that exact text is removed: its last occurrence after a blank line in a
    string (a hook note appended after it stays), or the text block that is
    exactly it.
    A string marked for caching on the way out arrives as one text block, so a
    block carrying the string-shaped suffix is cut the same way. ``None`` when
    nothing else is left, which is the caller's cue to skip the message rather
    than record an empty one.
    """
    if not scratch:
        return content
    suffix = f"\n\n{scratch}"
    if isinstance(content, str):
        return _cut_text(content, scratch, suffix)
    if isinstance(content, list):
        kept: list[Any] = []
        changed = False
        for block in content:
            text = block.get("text") if isinstance(block, dict) else None
            if text == scratch:
                changed = True
            elif isinstance(text, str) and suffix in text:
                changed = True
                rest = _cut_text(text, scratch, suffix)
                if rest is not None:
                    kept.append({**block, "text": rest})
            else:
                kept.append(block)
        if not changed:
            return content
        return kept or None
    return content


def strip_turn_scratch(message: dict[str, Any] | Any) -> Any | None:
    """The content of ``message`` without the scratch recorded on it.

    A message the assembler did not mark is returned as it is.
    """
    if not isinstance(message, dict):
        return None
    return cut_turn_scratch(message.get("content"), message.get(TURN_SCRATCH_KEY))


def split_turn_scratch(content: Any, scratch: str | None) -> tuple[Any, int | None]:
    """``content`` without the ``scratch``, and where the scratch stood.

    The scratch is not always last: a hook note lands on the message after
    the scratch was appended. The offset says where it stood, so the replay
    puts it back in the same place rather than at the end.

    Only that exact text is removed: its last occurrence after a blank line in
    a string (a hook note appended after it stays), or the text block that is
    exactly it. A string marked for caching on the way out arrives as one text
    block, so a block carrying the string-shaped suffix is cut the same way.
    ``None`` for the content is the caller's cue to skip the message rather
    than record an empty one.
    """
    if not scratch:
        return content, None
    suffix = f"\n\n{scratch}"
    if isinstance(content, str):
        head, sep, tail = content.rpartition(suffix)
        if not sep:
            return content, None
        rest = head + tail
        return (rest if rest.strip() else None), len(head)
    if isinstance(content, list):
        kept: list[Any] = []
        at: int | None = None
        for block in content:
            text = block.get("text") if isinstance(block, dict) else None
            if text == scratch:
                at = len(kept)
            elif isinstance(text, str) and suffix in text:
                at = len(kept)
                rest = _cut_text(text, scratch, suffix)
                if rest is not None:
                    kept.append({**block, "text": rest})
            else:
                kept.append(block)
        if at is None:
            return content, None
        return (kept or None), at
    return content, None


def replayed_scratch(message: dict[str, Any], content: Any) -> Any:
    """``content`` with the scratch filed on ``message`` put back where it was.

    The scratch is found by the text that identifies it, never by a position:
    a hook note lands on the message after the scratch was appended, so the
    scratch is not always last.
    """
    scratch = message.get(TURN_SCRATCH_FIELD)
    if not isinstance(scratch, str) or not scratch:
        return content
    at = message.get(TURN_SCRATCH_AT_FIELD)
    if isinstance(content, str) and isinstance(at, int):
        return f"{content[:at]}\n\n{scratch}{content[at:]}"
    if isinstance(content, list) and isinstance(at, int):
        return [*content[:at], {"type": "text", "text": scratch}, *content[at:]]
    return append_turn_scratch(content, scratch)


def strip_turn_scratch_by_tag(content: Any) -> Any | None:
    """Fallback for content that can no longer carry the mark: a recorded request.

    Cuts a string from the last tag that follows a blank line, or drops the text
    blocks that begin with the tag. Wider than :func:`cut_turn_scratch` --
    a tag the user pasted goes too -- so it is for comparisons where both sides
    pass through it, never for what is written down.
    """
    if isinstance(content, str):
        head, sep, _ = content.rpartition(f"\n\n{TURN_SCRATCH_TAG}")
        if not sep:
            return None if content.startswith(TURN_SCRATCH_TAG) else content
        return head or None
    if isinstance(content, list):
        kept = [block for block in content if not _is_scratch_block(block)]
        if len(kept) == len(content):
            return content
        return kept or None
    return content


def _cut_text(text: str, scratch: str, suffix: str) -> str | None:
    if text == scratch:
        return None
    head, sep, tail = text.rpartition(suffix)
    if not sep:
        return text
    rest = head + tail
    return rest if rest.strip() else None


def _is_scratch_block(block: Any) -> bool:
    return isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].startswith(TURN_SCRATCH_TAG)


__all__ = [
    "TURN_SCRATCH_KEY",
    "TURN_SCRATCH_TAG",
    "append_turn_scratch",
    "cut_turn_scratch",
    "mark_turn_scratch",
    "render_turn_scratch",
    "strip_turn_scratch",
    "strip_turn_scratch_by_tag",
]
