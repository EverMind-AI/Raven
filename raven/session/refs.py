"""The session log's side of the ``[[ref ...]]`` contract.

:mod:`raven.utils.refs` owns the token grammar; this module owns what the
``session`` store puts inside one. Two parties need to agree on it and neither
may import the other: the tool-result pruner mints a ref when it replaces a
result body with a one-line call record, and ``context_retrieve`` resolves that
ref back to the verbatim original. Both import this.

The scope is the log's :class:`SessionRevision` scope, ``<log_id>@<generation>``.
The grammar's third convention asks that "a store whose sequence can restart must
fold the generation into ``scope``, so a stale ref resolves to nothing rather
than to the wrong entry". ``session.messages`` is append-only within a
generation, so an ordinal is a stable name for a message; ``/new`` starts a log
with a fresh ``log_id`` and an undo bumps ``generation``, so a ref minted before
either names a scope that no longer exists -- a miss rather than a wrong answer.
"""

from __future__ import annotations

from typing import Any

from raven.contracts.context import SessionRevision
from raven.security.trust import fence_span
from raven.utils.refs import SourceRef

SESSION_REF_STORE = "session"
"""The ``store`` name for refs into ``session.messages``."""

TOOL_RESULT_TRUNCATION_MARKER = "\n... (truncated)"
"""What the loop appends to a tool result it had to cut.

The cut lands before the model reads the result and again on the way into the
session log, so both sides carry the same bytes. A fenced result carries the
marker at the cut end of its body, inside the fence: after a head preview and
before a tail preview. An unfenced one carries it at the same end.

Machine-recognisable on purpose: ``context.toolResultMaxChars`` defaults high
enough that nothing written today is cut, but history written under the old
16000-character cap is not recoverable, and retrieval has to say so instead of
returning a prefix as though it were the whole thing.
"""


def session_ref(revision: SessionRevision, lo: int, hi: int | None = None) -> SourceRef:
    """A ref into ``revision`` of the log, covering ordinals ``lo``..``hi``."""
    return SourceRef(store=SESSION_REF_STORE, scope=revision.scope, lo=lo, hi=lo if hi is None else hi)


def belongs_to(ref: SourceRef, revision: SessionRevision) -> bool:
    """Whether ``ref`` names entries of this revision of the log."""
    return ref.store == SESSION_REF_STORE and ref.scope == revision.scope


def is_truncated(content: Any) -> bool:
    """Whether a stored tool result was cut on the way to disk.

    A fenced result carries the marker at the cut end of its body, inside the
    fence; an unfenced one carries it at the same end. A head preview puts it
    after the preview, a tail preview before it.
    """
    if not isinstance(content, str):
        return False
    if content.startswith(TOOL_RESULT_TRUNCATION_MARKER) or content.endswith(TOOL_RESULT_TRUNCATION_MARKER):
        return True
    span = fence_span(content)
    return span is not None and (
        span[1].startswith(TOOL_RESULT_TRUNCATION_MARKER) or span[1].endswith(TOOL_RESULT_TRUNCATION_MARKER)
    )


def cap_tool_result(content: Any, cap: int) -> Any:
    """``content`` cut to ``cap`` characters of its body, saying it was cut.

    A tool result is fenced as untrusted data before it reaches the model. The
    cut lands inside that fence -- the body is shortened and the marker goes at
    its end -- so the genuine END marker and any trusted note after it stay
    whole. Non-string content (a multimodal result) is returned unchanged: the
    cap has always guarded ``str`` content only.

    Called when the result enters the conversation and again when it is written
    to the session, so the model and a later replay read the same bytes; the
    provider's cached prefix survives the turn boundary.
    """
    if not isinstance(content, str) or not cap or is_truncated(content):
        return content
    span = fence_span(content)
    if span is None:
        return content if len(content) <= cap else content[:cap] + TOOL_RESULT_TRUNCATION_MARKER
    opening, body, closing = span
    if len(body) <= cap:
        return content
    return f"{opening}{body[:cap]}{TOOL_RESULT_TRUNCATION_MARKER}{closing}"


TOOL_RESULT_VIEW_MAX_CHARS = 16_000
"""How much of one stored tool result leaves the session as a copy.

The session files tool results whole (``context.toolResultMaxChars``) so that
``context_retrieve`` can hand the original back. Everything else that is sent
a copy -- a transcript drawn by a client, a turn indexed by a memory backend --
gets at most this much of each, which is the cap results were filed under
before: a client drops frames over 1 MiB, and a memory backend may refuse a
single message larger than it accepts.
"""


def tool_result_view(text: str, limit: int = TOOL_RESULT_VIEW_MAX_CHARS) -> str:
    """``text`` cut to ``limit`` characters, saying how much of the stored result was left out.

    A result already cut on the way to disk counts by its body, so one filed
    under a cap at or below ``limit`` goes out as stored, marker included. A
    fenced result is counted by the body inside the fence, not by the fence's
    own bytes; a marker at either cut end is stripped for the count.
    """
    span = fence_span(text)
    body = span[1] if span is not None else text
    stored = body
    if stored.startswith(TOOL_RESULT_TRUNCATION_MARKER):
        stored = stored[len(TOOL_RESULT_TRUNCATION_MARKER) :]
    elif stored.endswith(TOOL_RESULT_TRUNCATION_MARKER):
        stored = stored[: -len(TOOL_RESULT_TRUNCATION_MARKER)]
    if len(stored) <= limit:
        return text
    more = f"... ({len(stored) - limit:,} more characters of the stored result not shown here)"
    if span is not None:
        opening, body, closing = span
        return f"{opening}{body[:limit]}\n{more}{closing}"
    return f"{text[:limit]}\n{more}"


__all__ = [
    "SESSION_REF_STORE",
    "TOOL_RESULT_TRUNCATION_MARKER",
    "TOOL_RESULT_VIEW_MAX_CHARS",
    "belongs_to",
    "cap_tool_result",
    "is_truncated",
    "session_ref",
    "tool_result_view",
]
