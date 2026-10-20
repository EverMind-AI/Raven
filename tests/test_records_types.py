"""The shapes, the evidence helpers, and the questions -- read and written back.

Everything a record is made of has to survive a round trip through storage,
because the record on disk is what a later run reads and what a reader
inspects. So the round trip is pinned here, along with the helpers that answer
the two questions a run asks most: what do we know about this thing, and has
this one been written to already.
"""

from __future__ import annotations

import pytest

from raven.records import (
    Provenance,
    Record,
    RecordsError,
    RecordStore,
    RecordType,
    already_contacted,
    find,
    merge_provenance,
    provenance_for,
    source_urls,
    sourced_fields,
)
from raven.records.grants import grants_path, read_grants


def _provenance(**overrides: object) -> Provenance:
    values: dict[str, object] = {
        "field": "name",
        "source_url": "https://example.test/acme",
        "source": "sec_edgar",
        "method": "fetch",
    }
    values.update(overrides)
    return Provenance(**values)  # type: ignore[arg-type]


def test_provenance_must_name_the_field_and_the_source() -> None:
    with pytest.raises(RecordsError):
        Provenance(field="", source_url="https://example.test/x")
    with pytest.raises(RecordsError):
        Provenance(field="name", source_url="")


def test_provenance_leaves_the_unset_parts_out() -> None:
    data = _provenance().to_dict()

    assert data == {
        "field": "name",
        "source_url": "https://example.test/acme",
        "method": "fetch",
        "source": "sec_edgar",
    }


def test_provenance_carries_the_parts_it_has() -> None:
    data = _provenance(
        retrieved_at="2026-10-20T00:00:00+00:00",
        tier="public",
        confidence=0.8,
        reason_kind="summary",
        metric_basis="2025",
        timeframe="FY2025",
    ).to_dict()

    assert data["retrieved_at"] == "2026-10-20T00:00:00+00:00"
    assert data["tier"] == "public"
    assert data["confidence"] == 0.8
    assert data["reason_kind"] == "summary"
    assert data["metric_basis"] == "2025"
    assert data["timeframe"] == "FY2025"


def test_provenance_survives_a_round_trip() -> None:
    original = _provenance(confidence=0.5, tier="licensed")

    read_back = Provenance.from_dict(original.to_dict())

    assert read_back == original


def test_provenance_reads_a_row_that_never_had_the_optional_parts() -> None:
    read_back = Provenance.from_dict({"field": "name", "source_url": "https://example.test/x"})

    assert read_back.source == ""
    assert read_back.confidence is None
    assert read_back.method == "fetch"


def test_a_record_must_have_a_dedupe_key() -> None:
    with pytest.raises(RecordsError):
        Record(type=RecordType.COMPANY, dedupe_key="")


def test_a_record_starts_at_new_and_is_stamped() -> None:
    record = Record(type=RecordType.COMPANY, dedupe_key="acme")

    assert record.state == "new"
    assert record.created_at
    assert record.updated_at == record.created_at


def test_a_record_survives_a_round_trip() -> None:
    original = Record(
        type=RecordType.COMPANY,
        dedupe_key="acme",
        fields={"name": "Acme"},
        provenance=[_provenance()],
        state="discovered",
        id="abc123",
        created_at="2026-10-20T00:00:00+00:00",
        updated_at="2026-10-20T01:00:00+00:00",
    )

    read_back = Record.from_dict(original.to_dict())

    assert read_back.type is RecordType.COMPANY
    assert read_back.dedupe_key == "acme"
    assert read_back.fields == {"name": "Acme"}
    assert read_back.provenance == original.provenance
    assert read_back.state == "discovered"
    assert read_back.id == "abc123"


def test_a_record_reads_a_row_that_never_had_a_state() -> None:
    read_back = Record.from_dict({"type": "company", "dedupe_key": "acme"})

    assert read_back.state == "new"
    assert read_back.provenance == []


def test_a_record_with_an_unknown_type_is_reported() -> None:
    with pytest.raises(RecordsError):
        Record.from_dict({"type": "spaceship", "dedupe_key": "acme"})


def test_merging_keeps_both_readings_of_the_same_field() -> None:
    merged = merge_provenance(
        [_provenance(method="fetch")],
        [_provenance(source_url="https://example.test/registry", method="api")],
    )

    assert len(merged) == 2
    assert {entry.source_url for entry in merged} == {
        "https://example.test/acme",
        "https://example.test/registry",
    }


def test_merging_the_same_source_again_keeps_the_later_reading() -> None:
    earlier = _provenance(retrieved_at="2026-10-19T00:00:00+00:00")
    later = _provenance(retrieved_at="2026-10-20T00:00:00+00:00")

    merged = merge_provenance([earlier], [later])

    assert len(merged) == 1
    assert merged[0].retrieved_at == "2026-10-20T00:00:00+00:00"


def test_the_evidence_helpers_answer_for_one_field() -> None:
    provenance = [_provenance(), _provenance(field="employees", source_url="https://example.test/registry")]

    assert len(provenance_for(provenance, "name")) == 1
    assert sourced_fields(provenance) == {"name", "employees"}
    assert source_urls(provenance) == ["https://example.test/acme", "https://example.test/registry"]


def test_the_find_query_answers_for_the_thing(tmp_path) -> None:
    with RecordStore(tmp_path / "records.db") as store:
        store.put(
            Record(
                type=RecordType.COMPANY,
                dedupe_key="acme",
                fields={"name": "Acme"},
                provenance=[_provenance()],
            )
        )

        assert find(store, RecordType.COMPANY, "acme") is not None
        assert find(store, RecordType.COMPANY, "beta") is None


def test_the_guard_answers_from_the_interaction_records(tmp_path) -> None:
    with RecordStore(tmp_path / "records.db") as store:
        store.put(
            Record(
                type=RecordType.OPPORTUNITY,
                dedupe_key="acme",
                fields={"name": "Acme"},
                provenance=[_provenance()],
            )
        )
        store.put(
            Record(
                type=RecordType.INTERACTION,
                dedupe_key="acme:email",
                fields={"subject": "acme", "subject_type": "opportunity", "channel": "email"},
                provenance=[_provenance()],
            )
        )

        assert already_contacted(store, "acme") is True
        assert already_contacted(store, "beta") is False


def test_a_record_of_another_kind_does_not_count_as_contacted(tmp_path) -> None:
    with RecordStore(tmp_path / "records.db") as store:
        store.put(
            Record(
                type=RecordType.INTERACTION,
                dedupe_key="acme:email",
                fields={"subject": "acme", "subject_type": "company", "channel": "email"},
                provenance=[_provenance()],
            )
        )

        assert already_contacted(store, "acme", RecordType.OPPORTUNITY) is False
        assert already_contacted(store, "acme", RecordType.COMPANY) is True


def test_a_grant_record_that_cannot_be_read_is_reported(tmp_path) -> None:
    path = grants_path("somerecord")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json", encoding="utf-8")

    with pytest.raises(RecordsError):
        read_grants("somerecord")


def test_a_grant_record_that_is_not_a_mapping_is_reported(tmp_path) -> None:
    path = grants_path("somerecord")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(RecordsError):
        read_grants("somerecord")


def test_a_record_id_cannot_be_used_as_a_path(tmp_path) -> None:
    with pytest.raises(RecordsError):
        grants_path("a/b")
    with pytest.raises(RecordsError):
        grants_path("")
