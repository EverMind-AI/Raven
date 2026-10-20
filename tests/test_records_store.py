"""What the record store promises, asserted against the store itself.

The store has one job that the rest of the tree depends on: a second run
that meets the same thing must find the first run's record rather than make
a second one, and the evidence for a claim must travel with the claim. Both
are easy to state and easy to lose to an implementation detail, so they are
pinned here rather than left to a reader to infer.
"""

from __future__ import annotations

import pytest

from raven.records import (
    Provenance,
    Record,
    RecordsError,
    RecordStore,
    RecordType,
    missing_provenance,
    ranked_by,
    records_store,
    set_store_for_tests,
    with_state,
)


@pytest.fixture
def store(tmp_path):
    """A store on its own file, and no shared one left behind."""
    instance = RecordStore(tmp_path / "records.db")
    set_store_for_tests(None)
    yield instance
    instance.close()
    set_store_for_tests(None)


def _company(key: str, **fields: object) -> Record:
    return Record(
        type=RecordType.COMPANY,
        dedupe_key=key,
        fields=dict(fields),
        provenance=[
            Provenance(field="name", source_url="https://example.test/acme", source="sec_edgar", method="fetch")
        ],
    )


def test_a_second_write_of_the_same_thing_keeps_one_record(store: RecordStore) -> None:
    first = store.put(_company("acme", name="Acme", employees=10))
    second = store.put(_company("acme", name="Acme Inc", employees=12))

    assert store.count(RecordType.COMPANY) == 1
    assert first.id == second.id
    assert second.fields["name"] == "Acme Inc"
    assert second.fields["employees"] == 12


def test_a_second_write_keeps_the_evidence_of_both(store: RecordStore) -> None:
    store.put(_company("acme"))
    store.put(
        Record(
            type=RecordType.COMPANY,
            dedupe_key="acme",
            fields={"employees": 12},
            provenance=[
                Provenance(field="employees", source_url="https://example.test/registry", source="opencorporates")
            ],
        )
    )

    stored = store.get(RecordType.COMPANY, "acme")
    assert stored is not None
    assert {entry.field for entry in stored.provenance} == {"name", "employees"}
    assert len(stored.sources_for("name")) == 1


def test_two_kinds_with_one_key_are_two_records(store: RecordStore) -> None:
    store.put(_company("acme"))
    store.put(
        Record(
            type=RecordType.CONTACT,
            dedupe_key="acme",
            fields={"name": "Acme"},
            provenance=[Provenance(field="name", source_url="https://example.test/x", source="sec_edgar")],
        )
    )

    assert store.count() == 2
    assert store.get(RecordType.COMPANY, "acme") is not None
    assert store.get(RecordType.CONTACT, "acme") is not None


def test_a_field_with_no_source_is_visible_as_one(store: RecordStore) -> None:
    stored = store.put(_company("acme", name="Acme", employees=12))

    assert stored.unsourced_fields() == ["employees"]
    assert [record.dedupe_key for record in missing_provenance(store)] == ["acme"]


def test_records_can_be_filtered_by_state_and_ranked(store: RecordStore) -> None:
    store.put(_company("acme", score=3))
    store.put(_company("beta", score=9))
    store.put(_company("gamma", score=6))

    ranked = ranked_by(store, "score", record_type=RecordType.COMPANY)
    assert [record.dedupe_key for record in ranked] == ["beta", "gamma", "acme"]

    unranked = store.put(_company("delta"))
    assert [record.dedupe_key for record in ranked_by(store, "score")] == ["beta", "gamma", "acme", "delta"]
    assert unranked.dedupe_key == "delta"


def test_a_record_that_does_not_carry_the_field_sorts_last(store: RecordStore) -> None:
    store.put(_company("acme", score=3))
    store.put(_company("beta"))

    assert [record.dedupe_key for record in ranked_by(store, "score")] == ["acme", "beta"]


def test_states_filter_by_kind(store: RecordStore) -> None:
    store.put(_company("acme"))
    store.put(Record(type=RecordType.ARTIFACT, dedupe_key="deck", fields={"kind": "deck"}))

    assert [record.dedupe_key for record in with_state(store, "new")] == ["acme", "deck"]
    assert [record.dedupe_key for record in with_state(store, "new", record_type=RecordType.COMPANY)] == ["acme"]


def test_a_move_is_written_down_in_the_journal(store: RecordStore) -> None:
    stored = store.put(_company("acme"))
    moved = store.transition(stored.id, "discovered", reason="it is worth the research")

    entries = store.journal(stored.id)
    assert len(entries) == 1
    assert entries[0]["from_state"] == "new"
    assert entries[0]["to_state"] == "discovered"
    assert entries[0]["reason"] == "it is worth the research"
    assert moved.state == "discovered"
    assert store.get(RecordType.COMPANY, "acme").state == "discovered"


def test_a_second_run_cannot_demote_a_record_it_finds_again(store: RecordStore) -> None:
    stored = store.put(_company("acme"))
    store.transition(stored.id, "discovered")
    store.transition(stored.id, "qualified")

    merged = store.put(_company("acme", name="Acme Inc"))

    assert merged.state == "qualified"


def test_a_deleted_record_takes_its_journal_with_it(store: RecordStore) -> None:
    stored = store.put(_company("acme"))
    store.transition(stored.id, "discovered")

    assert store.delete(stored.id) is True
    assert store.get(RecordType.COMPANY, "acme") is None
    assert store.journal(stored.id) == []
    assert store.delete(stored.id) is False


def test_two_stores_over_one_file_keep_one_record(tmp_path) -> None:
    path = tmp_path / "records.db"
    with RecordStore(path) as first, RecordStore(path) as second:
        first.put(_company("acme", name="Acme"))
        second.put(_company("acme", name="Acme Inc"))

        assert first.count(RecordType.COMPANY) == 1
        assert second.get(RecordType.COMPANY, "acme").fields["name"] == "Acme Inc"


def test_the_shared_store_is_one_per_process(tmp_path) -> None:
    set_store_for_tests(None)
    first = records_store(tmp_path / "records.db")
    assert records_store() is first
    set_store_for_tests(None)


def test_a_record_can_be_read_by_its_id(store: RecordStore) -> None:
    stored = store.put(_company("acme"))

    assert store.get_by_id(stored.id) is not None
    assert store.get_by_id("nosuchrecord") is None


def test_a_move_of_a_record_that_is_not_there_is_reported(store: RecordStore) -> None:
    with pytest.raises(RecordsError):
        store.transition("nosuchrecord", "discovered")


def test_the_lock_can_be_taken_twice_by_the_same_store(store: RecordStore) -> None:
    with store.locked():
        with store.locked():
            store.put(_company("acme"))

    assert store.count(RecordType.COMPANY) == 1


def test_closing_twice_is_not_an_error(store: RecordStore) -> None:
    store.close()
    store.close()


def test_the_database_lives_beside_the_home(store: RecordStore) -> None:
    from raven.records import records_db_path

    assert records_db_path().name == "records.db"


def test_a_row_that_cannot_be_read_is_reported(store: RecordStore) -> None:
    stored = store.put(_company("acme"))
    with store.locked():
        store._conn.execute("UPDATE records SET type = 'spaceship' WHERE id = ?", (stored.id,))
        store._conn.execute("UPDATE records SET fields = 'not json' WHERE id = ?", (stored.id,))

    with pytest.raises(RecordsError):
        store.get_by_id(stored.id)

    with store.locked():
        store._conn.execute("UPDATE records SET fields = '[]' WHERE id = ?", (stored.id,))

    with pytest.raises(RecordsError):
        store.get_by_id(stored.id)
