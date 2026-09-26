"""Tests for the knowledge registry: what survives a restart, and what a
delete takes with it."""

from __future__ import annotations

import json

import pytest

from raven.knowledge._records import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_SEPARATOR,
    DEFAULT_TOP_K,
    RecordStore,
)


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


def test_a_registry_written_before_origins_existed_still_loads(tmp_path) -> None:
    """The field has a default for exactly this: every document in such a
    registry is a file, which is what the default says."""
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps(
            {
                "bases": {
                    "b1": {
                        "name": "handbook",
                        "description": "",
                        "embedding_model": "bge-m3",
                        "dimensions": 8,
                        "created_at": "2026-08-24T00:00:00",
                        "updated_at": "2026-08-24T00:00:00",
                    }
                },
                "documents": {
                    "d1": {
                        "base_id": "b1",
                        "source": "a.md",
                        "media_type": "text/markdown",
                        "size": 1,
                        "status": "ready",
                        "created_at": "2026-08-24T00:00:00",
                        "updated_at": "2026-08-24T00:00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = RecordStore(path)

    assert store.get_document("d1").origin == "file"
    assert store.get_document("d1").origin_ref == ""


def test_rewriting_a_document_sends_it_back_to_the_queue(store) -> None:
    base = _base(store)
    doc = store.add_document(base_id=base.id, source="a.md", media_type="text/markdown", size=1)
    store.set_status(doc.id, "failed", chunk_count=3, error="no parser")

    updated = store.update_document(doc.id, source="b.md", media_type="text/markdown", size=9)

    assert (updated.source, updated.size) == ("b.md", 9)
    # Back to pending with nothing counted: the text those chunks were
    # embedded from is gone, and reporting them would describe a document
    # that no longer exists. The old failure goes with it.
    assert (updated.status, updated.chunk_count, updated.error) == ("pending", 0, "")
    assert store.pending_documents() == [updated]


def test_rewriting_a_missing_document_returns_none(store) -> None:
    assert store.update_document("nope", source="b.md", media_type="text/markdown", size=1) is None


def test_settings_are_written_one_field_at_a_time(store) -> None:
    base = _base(store)

    store.configure_base(base.id, top_k=12)
    store.configure_base(base.id, chunk_size=1024, chunk_overlap=200)

    kept = store.get_base(base.id)
    assert (kept.top_k, kept.chunk_size, kept.chunk_overlap) == (12, 1024, 200)
    # The fields nobody sent stay where they were.
    assert (kept.smart_chunking, kept.separator) == (True, "\n\n")


def test_a_setting_nobody_defined_is_a_caller_mistake(store) -> None:
    """Dropping it quietly would leave a surface reporting a value it never
    stored."""
    base = _base(store)

    with pytest.raises(ValueError, match="not a knowledge base setting"):
        store.configure_base(base.id, embedding_model="something-else")


def test_a_rebuild_moves_the_model_and_requeues_everything_under_it(store) -> None:
    """The two halves are one write: a document left saying ready would be
    claiming chunks that the rebuild is about to throw away."""
    base = _base(store)
    doc = store.add_document(base_id=base.id, source="a.md", media_type="text/markdown", size=10)
    store.set_status(doc.id, "ready", chunk_count=4)

    rebuilt = store.rebuild_base(base.id, embedding_model="bge-m3", dimensions=1024, embedding_provider="siliconflow")

    assert (rebuilt.embedding_model, rebuilt.dimensions) == ("bge-m3", 1024)
    assert rebuilt.embedding_provider == "siliconflow"
    kept = store.get_document(doc.id)
    assert (kept.status, kept.chunk_count) == ("pending", 0)


def test_a_rebuild_survives_a_reload(store, tmp_path) -> None:
    base = _base(store)
    store.rebuild_base(base.id, embedding_model="", dimensions=0)

    reloaded = RecordStore(tmp_path / "knowledge" / "records.json").get_base(base.id)

    assert (reloaded.embedding_model, reloaded.dimensions) == ("", 0)


def test_rebuilding_a_base_that_is_gone_returns_none(store) -> None:
    assert store.rebuild_base("nope", embedding_model="bge-m3", dimensions=8) is None


def test_configuring_a_base_that_is_gone_returns_none(store) -> None:
    assert store.configure_base("nope", top_k=3) is None


def test_a_registry_written_before_settings_existed_still_loads(tmp_path) -> None:
    """Every field defaulted, so such a base loads with the behaviour it
    already had rather than failing to load at all."""
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps(
            {
                "bases": {
                    "b1": {
                        "name": "handbook",
                        "description": "",
                        "embedding_model": "bge-m3",
                        "dimensions": 8,
                        "created_at": "2026-08-24T00:00:00",
                        "updated_at": "2026-08-24T00:00:00",
                    }
                },
                "documents": {},
            }
        ),
        encoding="utf-8",
    )

    base = RecordStore(path).get_base("b1")

    assert (base.top_k, base.smart_chunking, base.separator) == (
        DEFAULT_TOP_K,
        True,
        DEFAULT_SEPARATOR,
    )
    assert (base.chunk_size, base.chunk_overlap, base.file_processing) == (
        DEFAULT_CHUNK_SIZE,
        DEFAULT_CHUNK_OVERLAP,
        "",
    )


def test_a_new_base_starts_on_the_defaults_the_panel_shows(store) -> None:
    """One source for these numbers: the record's fields, the handlers' fallback
    for a base that predates them, and the panel's Restore Defaults all have to
    agree, and three copies are three chances to drift."""
    base = _base(store)

    assert (base.top_k, base.chunk_size, base.chunk_overlap) == (
        DEFAULT_TOP_K,
        DEFAULT_CHUNK_SIZE,
        DEFAULT_CHUNK_OVERLAP,
    )
    # The overlap has to leave a chunk with something of its own in it.
    assert DEFAULT_CHUNK_OVERLAP < DEFAULT_CHUNK_SIZE


def test_a_registry_this_version_writes_still_loads_on_the_previous_one(tmp_path) -> None:
    """The documented rollback. A previous loader builds each record by handing
    every stored key to a dataclass constructor and drops the row when one is a
    keyword it does not take -- so a field written beside the record makes
    every base and document vanish from a build that is rolled back, with the
    records still on disk and nothing listing them.

    Stands in for that loader with the field set it accepts, which is what the
    shipped build is: this asserts the stored shape, not our own reader."""
    legacy_base = {"name", "embedding_model", "dimensions", "created_at", "updated_at", "description"}
    legacy_doc = {
        "base_id",
        "source",
        "media_type",
        "size",
        "status",
        "created_at",
        "updated_at",
        "chunk_count",
        "error",
    }

    store = RecordStore(tmp_path / "records.json")
    base = store.create_base(name="handbook", embedding_model="bge-m3", dimensions=8)
    store.configure_base(base.id, top_k=12, chunk_size=1024, smart_chunking=False)
    store.add_document(base_id=base.id, source="a.md", media_type="text/markdown", size=1)
    store.add_document(base_id=base.id, source="plan.md", media_type="text/markdown", size=2, origin="note")

    raw = json.loads((tmp_path / "records.json").read_text(encoding="utf-8"))

    assert set(raw["bases"]) and set(raw["documents"])
    for fields in raw["bases"].values():
        assert set(fields) <= legacy_base, f"a rolled-back loader refuses {sorted(set(fields) - legacy_base)}"
    for fields in raw["documents"].values():
        assert set(fields) <= legacy_doc, f"a rolled-back loader refuses {sorted(set(fields) - legacy_doc)}"


def test_the_settings_survive_the_round_trip_they_are_kept_apart_for(tmp_path) -> None:
    store = RecordStore(tmp_path / "records.json")
    base = store.create_base(name="handbook", embedding_model="bge-m3", dimensions=8)
    store.configure_base(base.id, top_k=12, chunk_size=1024, separator="\n---\n")
    doc = store.add_document(
        base_id=base.id,
        source="docs.md",
        media_type="text/markdown",
        size=2,
        origin="url",
        origin_ref="https://example.com/docs",
    )

    reopened = RecordStore(tmp_path / "records.json")

    kept = reopened.get_base(base.id)
    assert (kept.top_k, kept.chunk_size, kept.separator) == (12, 1024, "\n---\n")
    back = reopened.get_document(doc.id)
    assert (back.origin, back.origin_ref) == ("url", "https://example.com/docs")


def test_a_registry_using_none_of_them_is_written_as_it_always_was(tmp_path) -> None:
    """The sibling keys appear only when there is something to put in them, so
    an installation that has touched no setting writes what it always did."""
    store = RecordStore(tmp_path / "records.json")
    store.create_base(name="handbook", embedding_model="bge-m3", dimensions=8)

    raw = json.loads((tmp_path / "records.json").read_text(encoding="utf-8"))

    assert sorted(raw) == ["bases", "documents"]


def test_a_field_from_a_later_version_is_ignored_rather_than_losing_the_row(tmp_path) -> None:
    """The mirror of the case above: losing a base because a build after this
    one added a field would be the same bug in the other direction."""
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps(
            {
                "bases": {
                    "b1": {
                        "name": "handbook",
                        "description": "",
                        "embedding_model": "bge-m3",
                        "dimensions": 8,
                        "created_at": "2026-08-24T00:00:00",
                        "updated_at": "2026-08-24T00:00:00",
                    }
                },
                "documents": {},
                "base_settings": {"b1": {"top_k": 9, "a_field_from_the_future": True}},
            }
        ),
        encoding="utf-8",
    )

    base = RecordStore(path).get_base("b1")

    assert base is not None
    assert base.top_k == 9
