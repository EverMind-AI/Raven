"""Records: durable beliefs about things, with the evidence kept beside them.

A research agent that only remembers what it said in this conversation cannot
answer "has this one been written to yet", and a run that cannot answer that
is how a prospect gets told twice that nobody ever contacted them. So the
things a run learns are kept, keyed by what they are rather than by when
they were seen, so that a second run looking at the same company finds the
first run's record instead of making a new one.

The evidence sits next to the claim on purpose. A record holds what it
believes and where each belief came from, separately, so a wrong field can
be corrected without discarding the parts that were right, and so a reader
can ask about one field without having to trust the whole record.

The store is one SQLite file per agent home, written by one writer at a time.
The unique index on the thing's identity is what turns a repeated write into
a merge rather than a second row, and the lock is what keeps two runs from
writing it at the same time.
"""

from raven.records.grants import (
    delete_grant,
    grants_path,
    has_grant,
    read_grants,
    write_grant,
)
from raven.records.library import records_store, set_store_for_tests
from raven.records.lifecycle import (
    RECORD_STATES,
    TERMINAL_STATES,
    allowed_transitions,
    can_transition,
    check_transition,
    is_terminal,
    transition,
)
from raven.records.provenance import (
    merge_provenance,
    provenance_for,
    source_urls,
    sourced_fields,
)
from raven.records.queries import (
    already_contacted,
    find,
    missing_provenance,
    ranked_by,
    with_state,
)
from raven.records.schema import SCHEMA_VERSION, derive_id, migrate
from raven.records.store import RecordStore, records_db_path
from raven.records.types import (
    LifecycleError,
    Provenance,
    Record,
    RecordsError,
    RecordsIdError,
    RecordType,
    now_stamp,
)

__all__ = [
    "RECORD_STATES",
    "SCHEMA_VERSION",
    "TERMINAL_STATES",
    "LifecycleError",
    "Provenance",
    "Record",
    "RecordStore",
    "RecordType",
    "RecordsError",
    "RecordsIdError",
    "allowed_transitions",
    "already_contacted",
    "can_transition",
    "check_transition",
    "delete_grant",
    "derive_id",
    "find",
    "grants_path",
    "has_grant",
    "is_terminal",
    "migrate",
    "merge_provenance",
    "missing_provenance",
    "now_stamp",
    "provenance_for",
    "ranked_by",
    "read_grants",
    "records_db_path",
    "records_store",
    "set_store_for_tests",
    "source_urls",
    "sourced_fields",
    "transition",
    "with_state",
    "write_grant",
]
