"""Tests for the knowledge registry: what survives a restart, and what a
delete takes with it."""

from __future__ import annotations

import json

import pytest

from raven.knowledge._records import RecordStore


@pytest.fixture
def store(tmp_path):
    return RecordStore(tmp_path / "knowledge" / "records.json")


def _base(store: RecordStore, name: str = "notes"):
    return store.create_base(name=name, embedding_model="text-embedding-3-small", dimensions=1536)


def test_a_base_round_trips_through_disk(tmp_path) -> None:
    path = tmp_path / "knowledge" / "records.json"
    created = _base(RecordStore(path))

    reopened = RecordStore(path).get_base(created.id)
    assert reopened == created
    assert reopened.dimensions == 1536


def test_the_registry_directory_is_made_on_first_write(tmp_path) -> None:
    path = tmp_path / "knowledge" / "records.json"
    assert not path.parent.exists()
    _base(RecordStore(path))
    assert path.exists()


def test_an_unreadable_registry_starts_empty_instead_of_raising(tmp_path) -> None:
    """A half-written file is what a full disk leaves behind. Refusing to open
    would take the whole gateway down over a list of names."""
    path = tmp_path / "records.json"
    path.write_text("{not json", encoding="utf-8")

    store = RecordStore(path)
    assert store.list_bases() == []


def test_a_malformed_entry_is_dropped_and_the_rest_load(tmp_path) -> None:
    path = tmp_path / "records.json"
    good = _base(RecordStore(path))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["bases"]["broken"] = {"name": "no other fields"}
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert [b.id for b in RecordStore(path).list_bases()] == [good.id]


def test_the_write_is_atomic(tmp_path, monkeypatch) -> None:
    """The temp file carries the new content and the registry is swapped, so a
    reader never sees a partial write and a failed write never truncates."""
    path = tmp_path / "records.json"
    store = RecordStore(path)
    seen: list[tuple[str, str]] = []

    real_replace = __import__("os").replace

    def _spy(src, dst):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("raven.knowledge._records.os.replace", _spy)
    _base(store)

    assert len(seen) == 1
    src, dst = seen[0]
    assert src.endswith(".tmp") and dst == str(path)
    assert not (tmp_path / "records.json.tmp").exists()


def test_rename_touches_only_the_two_editable_fields(store) -> None:
    base = _base(store)
    updated = store.rename_base(base.id, name="renamed", description="why it exists")

    assert (updated.name, updated.description) == ("renamed", "why it exists")
    assert (updated.embedding_model, updated.dimensions) == (base.embedding_model, base.dimensions)
    assert updated.created_at == base.created_at


def test_rename_leaves_the_other_field_alone_when_it_is_not_given(store) -> None:
    base = store.create_base(name="notes", embedding_model="m", dimensions=8, description="kept")
    assert store.rename_base(base.id, name="renamed").description == "kept"


def test_renaming_a_missing_base_returns_none(store) -> None:
    assert store.rename_base("nope", name="x") is None


def test_deleting_a_base_takes_its_documents_with_it(store) -> None:
    """Leaving them behind strands rows that list by base id: nothing can reach
    them and nothing can delete them."""
    base = _base(store)
    other = _base(store, "other")
    doc = store.add_document(base_id=base.id, source="a.md", media_type="text/markdown", size=3)
    kept = store.add_document(base_id=other.id, source="b.md", media_type="text/markdown", size=3)

    assert store.delete_base(base.id) is True
    assert store.get_document(doc.id) is None
    assert store.get_document(kept.id) is not None


def test_deleting_a_missing_base_says_so(store) -> None:
    assert store.delete_base("nope") is False


def test_documents_list_per_base_in_creation_order(store) -> None:
    base = _base(store)
    first = store.add_document(base_id=base.id, source="a.md", media_type="text/markdown", size=1)
    second = store.add_document(base_id=base.id, source="b.md", media_type="text/markdown", size=1)

    assert [d.id for d in store.list_documents(base.id)] == [first.id, second.id]


def test_a_new_document_starts_pending_and_shows_up_in_the_queue(store) -> None:
    base = _base(store)
    doc = store.add_document(base_id=base.id, source="a.md", media_type="text/markdown", size=1)

    assert doc.status == "pending"
    assert [d.id for d in store.pending_documents()] == [doc.id]


def test_a_successful_retry_clears_the_earlier_error(store) -> None:
    """Otherwise the page shows a failure next to a document it calls ready."""
    base = _base(store)
    doc = store.add_document(base_id=base.id, source="a.md", media_type="text/markdown", size=1)

    store.set_status(doc.id, "failed", error="embedding endpoint refused the request")
    done = store.set_status(doc.id, "ready", chunk_count=7)

    assert (done.status, done.chunk_count, done.error) == ("ready", 7, "")


def test_setting_status_on_a_missing_document_returns_none(store) -> None:
    assert store.set_status("nope", "ready") is None


def test_a_document_left_indexing_is_requeued_on_load(tmp_path) -> None:
    """Indexing runs in the gateway process, so a document still marked
    indexing at load lost its indexer with the last process. Left alone it
    stays in that state for good -- reported as in progress, picked up by
    nobody."""
    path = tmp_path / "records.json"
    store = RecordStore(path)
    base = _base(store)
    doc = store.add_document(base_id=base.id, source="a.md", media_type="text/markdown", size=1)
    store.set_status(doc.id, "indexing")

    reopened = RecordStore(path)
    assert reopened.get_document(doc.id).status == "pending"
    assert [d.id for d in reopened.pending_documents()] == [doc.id]


def test_a_ready_document_is_not_requeued(tmp_path) -> None:
    path = tmp_path / "records.json"
    store = RecordStore(path)
    base = _base(store)
    doc = store.add_document(base_id=base.id, source="a.md", media_type="text/markdown", size=1)
    store.set_status(doc.id, "ready", chunk_count=2)

    reopened = RecordStore(path)
    assert reopened.get_document(doc.id).status == "ready"
    assert reopened.pending_documents() == []


def test_the_requeue_is_persisted_not_just_in_memory(tmp_path) -> None:
    """A second reader must see the same queue as the first."""
    path = tmp_path / "records.json"
    store = RecordStore(path)
    base = _base(store)
    doc = store.add_document(base_id=base.id, source="a.md", media_type="text/markdown", size=1)
    store.set_status(doc.id, "indexing")

    RecordStore(path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["documents"][doc.id]["status"] == "pending"
