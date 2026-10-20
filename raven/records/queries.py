"""The questions the record store is asked most, in one place.

Every query here is a question a run asks before it acts: has this thing been
seen, what do we know about the things in this state, which of them are worth
acting on, and have we already contacted this one. The last one is the reason
the store exists -- a second run that cannot tell whether it already wrote to
somebody is how a prospect gets told twice that they were never contacted.
"""

from __future__ import annotations

from typing import Any

from raven.records.store import RecordStore
from raven.records.types import Record, RecordType

__all__ = [
    "already_contacted",
    "find",
    "missing_provenance",
    "ranked_by",
    "with_state",
]


def find(store: RecordStore, record_type: RecordType, dedupe_key: str) -> Record | None:
    """The record for this thing, or None."""
    return store.get(record_type, dedupe_key)


def with_state(store: RecordStore, state: str, record_type: RecordType | None = None) -> list[Record]:
    """The records in a state."""
    return store.list_records(record_type=record_type, state=state)


def ranked_by(
    store: RecordStore,
    field: str,
    record_type: RecordType | None = None,
    limit: int | None = None,
    descending: bool = True,
) -> list[Record]:
    """The records ordered by one of their fields.

    A record that does not carry the field sorts last rather than raising:
    the question is "which of these are strongest", and a record that has not
    been scored is not an error, it is an unranked one. Values that are not
    comparable with each other are not this function's problem -- a field
    that mixes a number with a string is sorted by its text, and the caller
    that picked it is the one that knows.
    """
    records = store.list_records(record_type=record_type)
    scored = [record for record in records if record.fields.get(field) is not None]
    unscored = [record for record in records if record.fields.get(field) is None]

    def key(record: Record) -> Any:
        value = record.fields.get(field)
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        return str(value)

    # The records with the field are sorted, and the records without it are
    # put last whichever way the sort runs: a record that was not scored is
    # not a record that came bottom, and reversing the order must not
    # promote it.
    scored.sort(key=key, reverse=descending)
    ordered = scored + unscored
    return ordered[:limit] if limit is not None else ordered


def missing_provenance(store: RecordStore, record_type: RecordType | None = None) -> list[Record]:
    """The records holding a field no source backs.

    A claim the store cannot defend is worth seeing before somebody acts on
    it, so this is a question the store can answer rather than something a
    reader has to reconstruct.
    """
    return [record for record in store.list_records(record_type=record_type) if record.unsourced_fields()]


def already_contacted(store: RecordStore, dedupe_key: str, record_type: RecordType = RecordType.OPPORTUNITY) -> bool:
    """Whether this thing has been written to already.

    The guard a run needs before it sends. It answers from the interaction
    records rather than from the prospect's state, because a prospect can be
    in several states at once -- engaged and lost, bounced and replied -- and
    only the record of the message itself says whether a person was written
    to. The subject is matched on the fields the interaction carries, not on
    the shape of its key, so a rule about what a key looks like can never
    decide whether a person was written to twice.
    """
    for interaction in store.list_records(record_type=RecordType.INTERACTION):
        fields = interaction.fields
        if fields.get("subject") != dedupe_key:
            continue
        subject_type = fields.get("subject_type", record_type.value)
        if subject_type == record_type.value:
            return True
    return False
