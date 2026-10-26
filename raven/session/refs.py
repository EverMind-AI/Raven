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
from raven.utils.refs import SourceRef

SESSION_REF_STORE = "session"
"""The ``store`` name for refs into ``session.messages``."""

TOOL_RESULT_TRUNCATION_MARKER = "\n... (truncated)"
"""What the loop's ``_save_turn`` appends to a tool result it had to cut.

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
    """Whether a stored tool result was cut on the way to disk."""
    return isinstance(content, str) and content.endswith(TOOL_RESULT_TRUNCATION_MARKER)


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
    under a cap at or below ``limit`` goes out as stored, marker included.
    """
    body = text[: -len(TOOL_RESULT_TRUNCATION_MARKER)] if is_truncated(text) else text
    if len(body) <= limit:
        return text
    return f"{text[:limit]}\n... ({len(body) - limit:,} more characters of the stored result not shown here)"


__all__ = [
    "SESSION_REF_STORE",
    "TOOL_RESULT_TRUNCATION_MARKER",
    "TOOL_RESULT_VIEW_MAX_CHARS",
    "belongs_to",
    "is_truncated",
    "session_ref",
    "tool_result_view",
]
