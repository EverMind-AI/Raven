"""How a record is laid out on disk, and how that layout is allowed to change.

The store is a single SQLite file per agent home, so the layout is a schema
with a version rather than a set of files that quietly disagree with each
other. Migrations are forward-only and numbered, and the version lives in the
database itself, so the file always knows what it holds.

The identity of a record is derived rather than generated: the same company
found by two runs gets the same id, which is what lets the journal refer to
one thing across both runs.
"""

from __future__ import annotations

import sqlite3

__all__ = [
    "SCHEMA_VERSION",
    "derive_id",
    "migrate",
]


# Bumped when the layout changes. Every version up to this one is applied in
# order by :func:`migrate`, so a file written by an older build is brought
# forward rather than replaced.
SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS records (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'new',
    fields      TEXT NOT NULL DEFAULT '{}',
    provenance  TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS records_identity
    ON records (type, dedupe_key);

CREATE INDEX IF NOT EXISTS records_by_state
    ON records (type, state);

CREATE INDEX IF NOT EXISTS records_by_updated
    ON records (updated_at);

CREATE TABLE IF NOT EXISTS journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id  TEXT NOT NULL,
    at         TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state   TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS journal_by_record
    ON journal (record_id, seq);
"""

# One entry per schema version, applied in order. Version 1 is the first
# layout; a later version adds its own statement to the end of this list.
_MIGRATIONS: list[str] = [_SCHEMA_V1]


def derive_id(record_type: str, dedupe_key: str) -> str:
    """The id a record of this kind and key has.

    Derived, not generated, so that two runs looking at the same thing agree
    on what to call it. The journal and the record then refer to one thing
    rather than to two rows that happen to say the same thing.
    """
    from hashlib import sha256

    digest = sha256(f"{record_type}\x00{dedupe_key}".encode()).hexdigest()
    return digest[:16]


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database up to :data:`SCHEMA_VERSION`, and report where it got to.

    The version is read from the database rather than assumed, so a file
    written by a newer build is left alone: this only ever moves forward
    from what it finds.
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"the record store is at schema version {current}, newer than this build's {SCHEMA_VERSION}"
        )
    for version in range(current, SCHEMA_VERSION):
        conn.executescript(_MIGRATIONS[version])
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return SCHEMA_VERSION
