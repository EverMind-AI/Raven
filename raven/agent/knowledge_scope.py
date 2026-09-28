"""Which knowledge bases a session's turns may search.

A reader picks bases in the composer the way they pin a working directory, and
for the same reason: a conversation is about something, and what it is about
decides which material answers it. The selection rides on the session's
metadata beside ``workdir`` and is bound around the turn body, so a tool asks
"what may I search" rather than being handed a list it could have chosen.

It is bound and not passed because one loop serves every session on the
process. A tool built with a base id would answer the session it was built for
and every session after it -- which is the whole of why this is a ContextVar
and not a constructor argument, the same shape ``raven.agent.workdir`` uses.

Nothing here decides *whether* the material is searched. The bases a turn may
reach are the reader's choice; when to reach them is the agent's.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The bases the running turn may search. Empty is the ordinary case: a
#: conversation nobody pointed at a base searches none, and the tool that
#: searches them is withheld rather than offered and refused.
_CURRENT: ContextVar[tuple[str, ...]] = ContextVar("knowledge_bases", default=())

#: Which conversation the running turn belongs to. Carried beside the bases so
#: a tool can file what it found under the turn that asked for it: a structured
#: payload is written where the call runs and popped where the turn streams,
#: and those are not always the same task -- the registry runs a tool under
#: `asyncio.wait_for`, which copies the context, so the write cannot be a
#: ContextVar the caller reads back. Keyed by this instead, the way
#: `deliver_files` keys its manifest.
_SESSION: ContextVar[str] = ContextVar("knowledge_session", default="")

#: Where the selection is kept on a session. The same metadata dict the
#: working-directory override lives in, so one session record answers both.
METADATA_KEY = "knowledge_bases"


@contextmanager
def bind(base_ids: "tuple[str, ...]", session: str = "") -> "Iterator[None]":
    """Bind ``base_ids`` as this turn's searchable bases, and whose turn it is."""
    tokens = (_CURRENT.set(tuple(base_ids)), _SESSION.set(session))
    try:
        yield
    finally:
        _SESSION.reset(tokens[1])
        _CURRENT.reset(tokens[0])


def selected() -> tuple[str, ...]:
    """The bases the running turn may search, in the order they were picked."""
    return _CURRENT.get()


def session() -> str:
    """Which conversation the running turn belongs to, or '' outside one."""
    return _SESSION.get()


def read(sessions: Any, session_key: str) -> tuple[str, ...]:
    """What one session selected, read off its record.

    Ids only, and nothing is looked up: whether a base still exists is the
    engine's question at search time, and a session that names one since
    deleted should still open. The search answers nothing for it, which is
    what a deleted base holds.
    """
    if sessions is None or not session_key:
        return ()
    try:
        stored = sessions.get_or_create(session_key).metadata.get(METADATA_KEY)
    except Exception:  # noqa: BLE001 - a session that cannot be read has no selection
        return ()
    if not isinstance(stored, (list, tuple)):
        return ()
    return tuple(str(one) for one in stored if str(one))


def write(sessions: Any, session_key: str, base_ids: "list[str] | tuple[str, ...]") -> tuple[str, ...]:
    """Record what a session may search, and answer what was recorded.

    Deduplicated with the order kept: the reader picked a list, not a set, and
    the order is the only thing saying which base they reached for first.
    """
    seen: list[str] = []
    for one in base_ids:
        named = str(one).strip()
        if named and named not in seen:
            seen.append(named)
    sessions.get_or_create(session_key).metadata[METADATA_KEY] = list(seen)
    return tuple(seen)


__all__ = ["METADATA_KEY", "bind", "read", "selected", "session", "write"]
