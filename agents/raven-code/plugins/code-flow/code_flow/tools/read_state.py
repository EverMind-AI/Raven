"""Read-before-edit records owned by one Raven-Code runtime and session.

Tool and hook factories receive separate service locators. The hook therefore
supplies its own store through the turn's context, which also reaches parallel
tool tasks. The weak registry only broadcasts session deletion; it never grants
one runtime access to another runtime's records.
"""

from __future__ import annotations

import threading
import weakref
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.plugins.context import PluginContext

Owner = tuple[Path, str, str]


def owner_for(ctx: PluginContext) -> Owner:
    return (Path(ctx.services.workspace).resolve(), ctx.services.user_id, ctx.services.agent_id)


class ReadLedger:
    """Resolved files observed by one session, with their last known versions."""

    UNREAD = "unread"
    STALE = "stale"
    OK = "ok"

    def __init__(self) -> None:
        self._seen: dict[str, tuple[int, ...]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _stamp(fp: Path) -> tuple[int, ...]:
        stat = fp.stat()
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino, stat.st_dev

    def note(self, fp: Path) -> None:
        try:
            stamp = self._stamp(fp)
        except OSError:
            self.forget(fp)
            return
        with self._lock:
            self._seen[str(fp)] = stamp

    def status(self, fp: Path) -> str:
        with self._lock:
            recorded = self._seen.get(str(fp))
        if recorded is None:
            return self.UNREAD
        try:
            current = self._stamp(fp)
        except OSError:
            return self.STALE
        return self.OK if current == recorded else self.STALE

    def has_read(self, fp: Path) -> bool:
        return self.status(fp) != self.UNREAD

    def forget(self, fp: Path) -> None:
        with self._lock:
            self._seen.pop(str(fp), None)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


_ACTIVE: ContextVar[tuple[ReadSessions, str] | None] = ContextVar("code_flow.read_session", default=None)
_STORES: weakref.WeakSet[ReadSessions] = weakref.WeakSet()
_STORES_LOCK = threading.Lock()


class ReadSessions:
    """One hook's records, selected by the acting session and cleared on deletion."""

    def __init__(self, owner: Owner | None = None) -> None:
        self.owner = owner
        self._sessions: dict[str, ReadLedger] = {}
        self._lock = threading.Lock()
        with _STORES_LOCK:
            _STORES.add(self)

    def bind(self, session_key: str) -> None:
        _ACTIVE.set((self, session_key))

    def unbind(self) -> None:
        active = _ACTIVE.get()
        if active is not None and active[0] is self:
            _ACTIVE.set(None)

    def ledger(self, session_key: str) -> ReadLedger:
        with self._lock:
            ledger = self._sessions.get(session_key)
            if ledger is None:
                ledger = self._sessions[session_key] = ReadLedger()
            return ledger

    def forget(self, session_key: str) -> None:
        with self._lock:
            ledger = self._sessions.pop(session_key, None)
        if ledger is not None:
            ledger.clear()


def current_ledger(owner: Owner | None = None) -> ReadLedger | None:
    active = _ACTIVE.get()
    if active is None:
        return None
    store, session_key = active
    if owner is not None and store.owner != owner:
        return None
    return store.ledger(session_key)


def forget_session(owner: Owner, session_key: str) -> None:
    with _STORES_LOCK:
        stores = list(_STORES)
    for store in stores:
        if store.owner == owner:
            store.forget(session_key)
