"""Turn scratch — the per-turn block that reaches the model but not the log.

Recalled memory and routed skills change with every query. Rendered into the
system prompt they sit ahead of the whole conversation in every provider's
cache prefix, so each turn discards everything cached behind them. Rendered at
the end of the turn's user message instead, they leave the system prompt
byte-identical across turns, so its cache entry and the tools' are read back on
the next turn. The history behind them does not get the same: the envelope is
filed without the scratch, so the next turn's prefix through it was never sent
in that form, and an earlier mark is read back only where the provider looks
back over previous breakpoints.

The block must not be written back to ``session.messages``. Persisting it would
turn the log from append-only into rewritten -- next turn's prefix would differ
from this turn's, losing the cache anyway, and the recalled text would
accumulate in history forever. The assembler records on the envelope exactly
what it appended (``TURN_SCRATCH_KEY``), and the loop's ``_save_turn`` removes
that text and the mark on the way in.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# Same bracketed-sentence shape as ContextBuilder._RUNTIME_CONTEXT_TAG: it tells
# the model what it is reading. What is cut on the way to disk is the recorded
# text (TURN_SCRATCH_KEY), not this literal.
TURN_SCRATCH_TAG = "[Turn Scratch — recalled for this turn only, not conversation history]"

TURN_SCRATCH_KEY = "_turn_scratch"
"""Set on the envelope the scratch was appended to, holding the appended text.

Underscored so no provider payload carries it (every transport sends an
allowlist of keys). ``_save_turn`` pops it, so it never reaches the session.
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
