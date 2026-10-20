"""The record of a deployment accepting to keep what a source would not let it keep.

Some sources let a deployment read their data and do not let it store it.
A record that keeps such a field is a deployment that has decided something
the source did not agree to, so the decision is written down and the store
checks it. These tests hold both halves: that the check refuses, and that the
grant opens it.
"""

from __future__ import annotations

import pytest

from raven.records import (
    Provenance,
    Record,
    RecordsError,
    RecordStore,
    RecordType,
    delete_grant,
    grants_path,
    has_grant,
    read_grants,
    write_grant,
)


@pytest.fixture
def store(tmp_path):
    instance = RecordStore(tmp_path / "records.db")
    yield instance
    instance.close()


def _places_record() -> Record:
    return Record(
        type=RecordType.COMPANY,
        dedupe_key="acme",
        fields={"name": "Acme"},
        provenance=[
            Provenance(
                field="name",
                source_url="https://example.test/places",
                source="google_places",
                method="fetch",
            )
        ],
    )


def test_a_field_the_source_will_not_let_us_keep_is_refused(store: RecordStore) -> None:
    with pytest.raises(RecordsError) as raised:
        store.put(_places_record())

    assert "do not let this deployment keep the data" in str(raised.value)
    assert "google_places" in str(raised.value)
    assert store.count(RecordType.COMPANY) == 0


def test_a_field_the_source_will_let_us_keep_is_accepted(store: RecordStore) -> None:
    stored = store.put(
        Record(
            type=RecordType.COMPANY,
            dedupe_key="acme",
            fields={"name": "Acme"},
            provenance=[
                Provenance(field="name", source_url="https://example.test/edgar", source="sec_edgar", method="fetch")
            ],
        )
    )

    assert stored.fields["name"] == "Acme"


def test_a_field_with_no_source_named_is_not_judged_by_the_registry(store: RecordStore) -> None:
    stored = store.put(
        Record(
            type=RecordType.COMPANY,
            dedupe_key="acme",
            fields={"name": "Acme"},
            provenance=[Provenance(field="name", source_url="https://example.test/x", method="fetch")],
        )
    )

    assert stored.fields["name"] == "Acme"


def test_a_grant_opens_the_field_it_names(store: RecordStore) -> None:
    with pytest.raises(RecordsError):
        store.put(_places_record())

    record_id = store.put(
        Record(
            type=RecordType.COMPANY,
            dedupe_key="acme",
            fields={"name": "Acme"},
            provenance=[
                Provenance(field="name", source_url="https://example.test/edgar", source="sec_edgar", method="fetch")
            ],
        )
    ).id
    write_grant(record_id, "google_places", basis="we hold a licence that says so")

    try:
        assert has_grant(record_id, "google_places") is True
        stored = store.put(_places_record())
        assert stored.fields["name"] == "Acme"
    finally:
        delete_grant(record_id, "google_places")


def test_a_grant_is_readable_and_can_be_withdrawn(store: RecordStore) -> None:
    record_id = store.put(
        Record(
            type=RecordType.COMPANY,
            dedupe_key="acme",
            fields={"name": "Acme"},
            provenance=[
                Provenance(field="name", source_url="https://example.test/edgar", source="sec_edgar", method="fetch")
            ],
        )
    ).id

    write_grant(record_id, "google_places", basis="we hold a licence that says so")
    assert len(read_grants(record_id)) == 1
    assert read_grants(record_id)[0]["basis"] == "we hold a licence that says so"

    assert delete_grant(record_id, "google_places") is True
    assert read_grants(record_id) == []


def test_a_grant_without_a_reason_is_not_accepted() -> None:
    with pytest.raises(RecordsError):
        write_grant("some-record", "google_places", basis="")


def test_a_grant_must_name_a_source() -> None:
    with pytest.raises(RecordsError):
        write_grant("some-record", "", basis="we hold a licence that says so")


def test_a_grant_record_that_cannot_be_read_is_reported() -> None:
    path = grants_path("somerecord")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json", encoding="utf-8")

    with pytest.raises(RecordsError):
        write_grant("somerecord", "google_places", basis="we hold a licence that says so")
    with pytest.raises(RecordsError):
        delete_grant("somerecord", "google_places")


def test_a_grant_record_that_is_not_a_mapping_is_reported() -> None:
    path = grants_path("somerecord")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")

    assert delete_grant("somerecord", "google_places") is False


def test_a_withdrawal_of_a_grant_that_was_never_given_removes_nothing() -> None:
    write_grant("somerecord", "google_places", basis="we hold a licence that says so")

    assert delete_grant("somerecord", "some_other_source") is False
    assert len(read_grants("somerecord")) == 1


def test_a_withdrawal_of_a_record_that_has_no_grants_removes_nothing() -> None:
    assert delete_grant("somerecord", "google_places") is False
