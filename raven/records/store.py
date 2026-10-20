"""The record store: one SQLite file per agent home, one writer at a time.

A record store has to answer two questions that pull in opposite directions.
"How do you know this?" wants the evidence kept next to the claim. "Has this
already been done?" wants one row per thing, so that a second run cannot tell
a prospect they were never contacted. The first is a field on the row; the
second is a unique index, and it is the index that makes the store worth
having.

The index does the second half for us: writing the same company twice is not
an error, it is a merge. The two runs' sources are combined, the fields the
newer run read replace the ones it read, and the record keeps one identity
across both. So the store never refuses a write to protect itself, and it
never grows a second row for a thing that is already there.

One writer at a time, enforced twice: an exclusive lock file for other
processes, and a lock inside this one for other threads. The lock file is the
repository's own portable advisory lock rather than ``fcntl`` directly,
because ``fcntl`` degrades to unlocked on Windows and a concurrent write there
would be lost rather than serialized.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from raven.records.lifecycle import can_transition, check_transition
from raven.records.provenance import merge_provenance
from raven.records.schema import derive_id, migrate
from raven.records.types import Provenance, Record, RecordsError, RecordType, now_stamp
from raven.utils.portable_lock import file_lock

__all__ = ["RecordStore", "records_db_path"]


def records_db_path() -> Path:
    """The record database for the active agent home."""
    from raven.config.paths import get_runtime_subdir

    return get_runtime_subdir("records") / "records.db"


class RecordStore:
    """Durable records, one row per thing, with the sources kept beside them.

    The store is safe to use from several threads and several processes. Use
    it as a context manager to close the handle, or call :meth:`close`.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else records_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self._db_path.with_name(self._db_path.name + ".lock")
        self._thread_lock = threading.Lock()
        self._depth = threading.local()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        migrate(self._conn)

    # -- locking ---------------------------------------------------------

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the writer lock for the body.

        Re-entrant for one store instance: a method that already holds the
        lock and calls another that takes it again will not deadlock on
        itself. The file lock is taken first and the thread lock second, and
        always in that order, so two holders cannot be waiting on each other.
        """
        depth = getattr(self._depth, "value", 0)
        if depth:
            self._depth.value = depth + 1
            try:
                yield
            finally:
                self._depth.value = depth
            return
        with file_lock(self._lock_path):
            with self._thread_lock:
                self._depth.value = 1
                try:
                    yield
                finally:
                    self._depth.value = 0

    # -- reading ---------------------------------------------------------

    def get(self, record_type: RecordType, dedupe_key: str) -> Record | None:
        """The record for this thing, or None when there is not one."""
        with self.locked():
            return self._get(record_type, dedupe_key)

    def get_by_id(self, record_id: str) -> Record | None:
        """The record with this id, or None."""
        with self.locked():
            row = self._conn.execute(
                "SELECT id, type, dedupe_key, state, fields, provenance, created_at, updated_at "
                "FROM records WHERE id = ?",
                (record_id,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def list_records(
        self,
        record_type: RecordType | None = None,
        state: str | None = None,
    ) -> list[Record]:
        """The records that match, oldest first.

        A missing filter means no filter: every kind of record, or every
        record in a state. The two are separate arguments because the
        questions "which prospects are still new" and "what did we send" are
        both asked, and neither is the other.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if record_type is not None:
            clauses.append("type = ?")
            params.append(record_type.value)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.locked():
            rows = self._conn.execute(
                "SELECT id, type, dedupe_key, state, fields, provenance, created_at, updated_at "
                f"FROM records{where} ORDER BY created_at, rowid",
                params,
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def journal(self, record_id: str) -> list[dict[str, Any]]:
        """The moves this record has made, oldest first."""
        with self.locked():
            rows = self._conn.execute(
                "SELECT seq, at, from_state, to_state, reason FROM journal "
                "WHERE record_id = ? ORDER BY seq",
                (record_id,),
            ).fetchall()
        return [
            {"seq": seq, "at": at, "from_state": from_state, "to_state": to_state, "reason": reason}
            for seq, at, from_state, to_state, reason in rows
        ]

    def count(self, record_type: RecordType | None = None) -> int:
        """How many records the store holds."""
        with self.locked():
            if record_type is None:
                row = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM records WHERE type = ?", (record_type.value,)
                ).fetchone()
        return int(row[0]) if row else 0

    # -- writing ---------------------------------------------------------

    def put(self, record: Record) -> Record:
        """Write the record, merging it into the one already there.

        A record that names a thing the store already holds is not a second
        row: its sources are added to the ones already held, and the fields
        it read replace the fields it read. The record that comes back is
        what is now stored.
        """
        with self.locked():
            return self._put(record)

    def transition(self, record_id: str, nxt: str, reason: str = "") -> Record:
        """Move the record to a new state, and write the move down.

        A move the record's kind does not allow is refused, and nothing is
        written: the record stays where it was, because a refused move is
        information rather than a failure to work around.
        """
        with self.locked():
            current = self._get_by_id(record_id)
            if current is None:
                raise RecordsError(f"no record with id {record_id!r}")
            check_transition(current.type, current.state, nxt)
            moved = Record(
                type=current.type,
                dedupe_key=current.dedupe_key,
                fields=dict(current.fields),
                provenance=list(current.provenance),
                state=nxt,
                id=current.id,
                created_at=current.created_at,
                updated_at=now_stamp(),
            )
            self._write(moved)
            self._conn.execute(
                "INSERT INTO journal (record_id, at, from_state, to_state, reason) VALUES (?, ?, ?, ?, ?)",
                (moved.id, moved.updated_at, current.state, nxt, reason),
            )
            return moved

    def delete(self, record_id: str) -> bool:
        """Remove the record and its moves. Whether anything was there."""
        with self.locked():
            cursor = self._conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
            self._conn.execute("DELETE FROM journal WHERE record_id = ?", (record_id,))
            return cursor.rowcount > 0

    def close(self) -> None:
        """Close the database handle."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "RecordStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals -------------------------------------------------------

    def _get(self, record_type: RecordType, dedupe_key: str) -> Record | None:
        row = self._conn.execute(
            "SELECT id, type, dedupe_key, state, fields, provenance, created_at, updated_at "
            "FROM records WHERE type = ? AND dedupe_key = ?",
            (record_type.value, dedupe_key),
        ).fetchone()
        return _row_to_record(row) if row else None

    def _get_by_id(self, record_id: str) -> Record | None:
        row = self._conn.execute(
            "SELECT id, type, dedupe_key, state, fields, provenance, created_at, updated_at "
            "FROM records WHERE id = ?",
            (record_id,),
        ).fetchone()
        return _row_to_record(row) if row else None

    def _put(self, record: Record) -> Record:
        existing = self._get(record.type, record.dedupe_key)
        if existing is None:
            stored = Record(
                type=record.type,
                dedupe_key=record.dedupe_key,
                fields=dict(record.fields),
                provenance=list(record.provenance),
                state=record.state,
                id=derive_id(record.type.value, record.dedupe_key),
                created_at=record.created_at or now_stamp(),
                updated_at=now_stamp(),
            )
            self._check_storable(stored)
            self._write(stored)
            return stored

        # The state only ever moves forward. A second run meeting the same
        # thing brings the state it was in, and a run that found a prospect
        # already pitched does not un-pitch them.
        state = existing.state
        if record.state and can_transition(existing.type, existing.state, record.state):
            state = record.state

        merged = Record(
            type=existing.type,
            dedupe_key=existing.dedupe_key,
            fields={**existing.fields, **record.fields},
            provenance=merge_provenance(existing.provenance, record.provenance),
            state=state,
            id=existing.id,
            created_at=existing.created_at,
            updated_at=now_stamp(),
        )
        self._check_storable(merged)
        self._write(merged)
        return merged

    def _check_storable(self, record: Record) -> None:
        """Refuse a record holding a field the source does not let us keep.

        Some sources let a deployment read their data and do not let it
        store it. A record that keeps such a field is a deployment that has
        decided something the source did not agree to, so the decision has to
        be on record before the write lands.
        """
        from raven.records.grants import has_grant
        from raven.sources.policy import policy_for

        for entry in record.provenance:
            if not entry.source:
                continue
            policy = policy_for(entry.source)
            if policy is None or policy.store_allowed:
                continue
            if has_grant(record.id, entry.source):
                continue
            raise RecordsError(
                f"the terms of {policy.label} do not let this deployment keep the data, "
                f"so the field {entry.field!r} may not be stored on this record. "
                f"Either drop the field, or record that this deployment may keep it "
                f"(raven records grant {record.id!r} {entry.source!r} --basis \"...\")."
            )

    def _write(self, record: Record) -> None:
        self._conn.execute(
            "INSERT INTO records (id, type, dedupe_key, state, fields, provenance, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (type, dedupe_key) DO UPDATE SET "
            "state = excluded.state, fields = excluded.fields, provenance = excluded.provenance, "
            "updated_at = excluded.updated_at",
            (
                record.id,
                record.type.value,
                record.dedupe_key,
                record.state,
                json.dumps(record.fields, ensure_ascii=False, sort_keys=True),
                json.dumps([p.to_dict() for p in record.provenance], ensure_ascii=False),
                record.created_at,
                record.updated_at,
            ),
        )


def _row_to_record(row: tuple[Any, ...]) -> Record:
    """Read one row back.

    A row that cannot be read is reported rather than skipped: a record
    store that quietly drops the rows it cannot parse becomes a store that
    agrees with whoever wrote the last broken file.
    """
    record_id, raw_type, dedupe_key, state, raw_fields, raw_provenance, created_at, updated_at = row
    try:
        record_type = RecordType(raw_type)
    except ValueError as exc:
        raise RecordsError(f"record {record_id!r} has an unknown type {raw_type!r}") from exc
    try:
        fields = json.loads(raw_fields or "{}")
        provenance = json.loads(raw_provenance or "[]")
    except json.JSONDecodeError as exc:
        raise RecordsError(f"record {record_id!r} holds unreadable json ({exc})") from exc
    if not isinstance(fields, dict) or not isinstance(provenance, list):
        raise RecordsError(f"record {record_id!r} holds json of the wrong shape")
    return Record(
        type=record_type,
        dedupe_key=dedupe_key,
        fields=fields,
        provenance=[Provenance.from_dict(p) for p in provenance],
        state=state,
        id=record_id,
        created_at=created_at,
        updated_at=updated_at,
    )
