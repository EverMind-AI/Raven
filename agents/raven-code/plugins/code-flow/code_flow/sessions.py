"""The session ledger: which sessions of this process work where, right now.

Parallel raven-code DAG nodes are sessions of one process on one working
directory, so the process can count them itself. A record is opened at a
session's first turn (pinning the base commit its workspace reports measure
against), marked in flight at every turn start and clear at every send.

A turn that fails or is cancelled fires no send, so an in-flight mark can
be left behind; the ledger treats a mark older than ``ttl_s`` as a ghost
and stops counting it. A turn that keeps iterating refreshes its mark at
every iteration, so a long turn never reads as a ghost. Records themselves
stay until the session is deleted (the observer) or unseen for ten ttls.

The ledger also remembers which sessions have shared a directory: whenever a
session begins a turn where another session's record lives, both records name
each other as peers, for the rest of their lives. The workspace report reads
that to know whether the tree's history past its base can still be
attributed to one session -- nothing locks the directory any more, so git
alone cannot say whose commit is whose.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from code_flow.gitfacts import head_of

DEFAULT_TTL_S = 3600.0


@dataclass
class SessionRecord:
    cwd: str
    base_commit: str | None
    in_flight: bool
    seen_at: float
    peers: set[str] = field(default_factory=set)

    @property
    def shared(self) -> bool:
        """Whether another session of this process has had a record on this
        directory while this one lived. Once true it stays true: from then on
        the tree's history past this session's base may hold another session's
        work, whichever of them is asked."""
        return bool(self.peers)


class SessionLedger:
    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        head: Callable[[Path | None], str | None] = head_of,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._head = head
        self._records: dict[str, SessionRecord] = {}

    def begin_turn(self, session_key: str, cwd: Path | None) -> SessionRecord:
        now = self._clock()
        self._sweep(now)
        cwd_text = str(cwd) if cwd is not None else ""
        record = self._records.get(session_key)
        if record is None or record.cwd != cwd_text:
            # A session repointed to another directory starts a fresh base.
            record = SessionRecord(cwd=cwd_text, base_commit=self._head(cwd), in_flight=True, seen_at=now)
            self._records[session_key] = record
        else:
            record.in_flight = True
            record.seen_at = now
        for key, other in self._records.items():
            if key != session_key and other.cwd == cwd_text:
                other.peers.add(session_key)
                record.peers.add(key)
        return record

    def touch(self, session_key: str) -> None:
        """Refresh an in-flight session's mark from inside its turn.

        The ttl exists for a turn that stopped reporting (failed, cancelled);
        a turn that is still iterating is alive however long it has run, and
        without this a session working past the ttl would vanish from the
        count its peers are told.
        """
        record = self._records.get(session_key)
        if record is not None and record.in_flight:
            record.seen_at = self._clock()

    def end_turn(self, session_key: str) -> None:
        record = self._records.get(session_key)
        if record is not None:
            record.in_flight = False
            record.seen_at = self._clock()

    def forget(self, session_key: str) -> None:
        self._records.pop(session_key, None)

    def record(self, session_key: str) -> SessionRecord | None:
        return self._records.get(session_key)

    def peers_in_flight(self, session_key: str) -> int:
        """How many OTHER sessions are mid-turn in this session's directory."""
        mine = self._records.get(session_key)
        if mine is None:
            return 0
        now = self._clock()
        return sum(
            1
            for key, other in self._records.items()
            if key != session_key and other.cwd == mine.cwd and other.in_flight and now - other.seen_at <= self._ttl_s
        )

    def _sweep(self, now: float) -> None:
        for key, record in list(self._records.items()):
            age = now - record.seen_at
            if record.in_flight and age > self._ttl_s:
                record.in_flight = False
            if age > 10 * self._ttl_s:
                del self._records[key]


LEDGER = SessionLedger()

__all__ = ["DEFAULT_TTL_S", "LEDGER", "SessionLedger", "SessionRecord"]
