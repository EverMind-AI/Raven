"""Tests for the LanceDB vector store behind a knowledge base.

Against a real LanceDB on a tmp_path, not a fake: the two things most likely to
break here are the score direction and the metadata predicate, and both are
properties of the engine rather than of this code.
"""

from __future__ import annotations

import pytest

from raven.knowledge import Chunk, LanceDBVectorStore, TextBlock, VectorRecord

DIM = 4


@pytest.fixture
def store(tmp_path):
    return LanceDBVectorStore(tmp_path / "vectors")


def _record(doc: str, index: int, text: str, vector: list[float], **metadata) -> VectorRecord:
    return VectorRecord(
        vector=vector,
        document_id=doc,
        chunk=Chunk(
            content=TextBlock(text=text),
            source=f"{doc}.md",
            chunk_index=index,
            total_chunks=1,
            metadata=metadata,
        ),
    )


async def _seed(store: LanceDBVectorStore, name: str = "kb") -> None:
    await store.create_collection(name, DIM)
    await store.insert(
        name,
        [
            _record("d1", 0, "hello", [1.0, 0.0, 0.0, 0.0], lang="zh"),
            _record("d2", 0, "goodbye", [0.0, 1.0, 0.0, 0.0], lang="en"),
            _record("d2", 1, "farewell", [0.0, 0.9, 0.1, 0.0], lang="en"),
        ],
    )


async def test_create_is_idempotent_and_has_collection_agrees(store) -> None:
    assert await store.has_collection("kb") is False
    await store.create_collection("kb", DIM)
    await store.create_collection("kb", DIM)
    assert await store.has_collection("kb") is True


async def test_delete_collection_is_forgiving_of_a_missing_one(store) -> None:
    await store.delete_collection("never-made")
    await store.create_collection("kb", DIM)
    await store.delete_collection("kb")
    assert await store.has_collection("kb") is False


async def test_inserting_nothing_is_not_an_error(store) -> None:
    await store.create_collection("kb", DIM)
    await store.insert("kb", [])
    assert await store.list_documents("kb") == []


async def test_search_returns_similarity_highest_first(store) -> None:
    """The interface promises similarity, LanceDB reports cosine distance. An
    unconverted score would sort the least relevant chunk to the top and put a
    relevance floor on the wrong side of every comparison."""
    await _seed(store)
    hits = await store.search("kb", [1.0, 0.0, 0.0, 0.0], top_k=3)

    assert [h.document_id for h in hits][0] == "d1"
    assert hits == sorted(hits, key=lambda h: h.score, reverse=True)
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[0].score > hits[-1].score


async def test_search_round_trips_the_whole_chunk(store) -> None:
    await _seed(store)
    hit = (await store.search("kb", [1.0, 0.0, 0.0, 0.0], top_k=1))[0]
    assert hit.chunk.text == "hello"
    assert hit.chunk.source == "d1.md"
    assert hit.chunk.metadata == {"lang": "zh"}


async def test_metadata_filter_narrows_before_ranking(store) -> None:
    """Filtering after ranking would return one row here, not two: the nearest
    chunk is the excluded one, so a top-2 taken first and filtered second comes
    back short."""
    await _seed(store)
    hits = await store.search("kb", [1.0, 0.0, 0.0, 0.0], top_k=2, metadata_filter={"lang": "en"})

    assert len(hits) == 2
    assert {h.document_id for h in hits} == {"d2"}


async def test_metadata_filter_ands_its_keys(store) -> None:
    await store.create_collection("kb", DIM)
    await store.insert(
        "kb",
        [
            _record("a", 0, "both", [1.0, 0.0, 0.0, 0.0], lang="en", kind="note"),
            _record("b", 0, "one", [1.0, 0.0, 0.0, 0.0], lang="en", kind="page"),
        ],
    )
    hits = await store.search("kb", [1.0, 0.0, 0.0, 0.0], top_k=5, metadata_filter={"lang": "en", "kind": "note"})
    assert [h.document_id for h in hits] == ["a"]


async def test_a_value_of_a_different_type_does_not_match(store) -> None:
    """JSON-encoding the value keeps 1 and "1" apart. Flattening with str()
    would let a filter for the number match the string."""
    await store.create_collection("kb", DIM)
    await store.insert("kb", [_record("a", 0, "x", [1.0, 0.0, 0.0, 0.0], page=1)])

    assert await store.search("kb", [1.0, 0.0, 0.0, 0.0], metadata_filter={"page": "1"}) == []
    assert len(await store.search("kb", [1.0, 0.0, 0.0, 0.0], metadata_filter={"page": 1})) == 1


async def test_a_quote_in_a_metadata_value_does_not_break_the_predicate(store) -> None:
    """The predicate is SQL, and the values are user data."""
    await store.create_collection("kb", DIM)
    await store.insert("kb", [_record("a", 0, "x", [1.0, 0.0, 0.0, 0.0], note="it's fine")])

    hits = await store.search("kb", [1.0, 0.0, 0.0, 0.0], metadata_filter={"note": "it's fine"})
    assert [h.document_id for h in hits] == ["a"]


async def test_delete_removes_one_document_and_leaves_the_rest(store) -> None:
    await _seed(store)
    await store.delete("kb", "d2")

    assert {d.document_id for d in await store.list_documents("kb")} == {"d1"}
    assert {h.document_id for h in await store.search("kb", [0.0, 1.0, 0.0, 0.0], top_k=5)} == {"d1"}


async def test_list_documents_counts_chunks_per_document(store) -> None:
    await _seed(store)
    summaries = {d.document_id: d for d in await store.list_documents("kb")}

    assert summaries["d1"].chunk_count == 1
    assert summaries["d2"].chunk_count == 2
    assert summaries["d2"].source == "d2.md"
    assert summaries["d1"].metadata == {"lang": "zh"}


async def test_list_documents_honours_the_metadata_filter(store) -> None:
    await _seed(store)
    summaries = await store.list_documents("kb", metadata_filter={"lang": "zh"})
    assert [d.document_id for d in summaries] == ["d1"]


async def test_two_collections_do_not_see_each_other(store) -> None:
    await _seed(store, "kb")
    await store.create_collection("other", DIM)
    await store.insert("other", [_record("z", 0, "elsewhere", [1.0, 0.0, 0.0, 0.0])])

    assert {d.document_id for d in await store.list_documents("other")} == {"z"}
    assert "z" not in {d.document_id for d in await store.list_documents("kb")}


async def test_the_directory_is_not_created_until_it_is_used(tmp_path) -> None:
    """Building the store is a wiring step; a deployment that never opens a
    knowledge base should not find one on disk."""
    path = tmp_path / "vectors"
    LanceDBVectorStore(path)
    assert not path.exists()

    await LanceDBVectorStore(path).create_collection("kb", DIM)
    assert path.exists()


async def test_collection_names_pages_to_the_end(tmp_path) -> None:
    """``list_tables`` is paginated. Reading only the first response makes an
    existing collection look missing once a deployment outgrows one page --
    which reports an indexed base as absent and silently skips dropping it."""

    class _Response:
        def __init__(self, tables, page_token):
            self.tables = tables
            self.page_token = page_token

    class _FakeDB:
        def __init__(self):
            self.tokens_seen = []

        async def list_tables(self, page_token=None):
            self.tokens_seen.append(page_token)
            if page_token is None:
                return _Response(["kb-a", "kb-b"], "next")
            return _Response(["kb-c"], None)

    store = LanceDBVectorStore(tmp_path / "vectors")
    store._db = _FakeDB()

    assert await store.has_collection("kb-c") is True
    assert store._db.tokens_seen == [None, "next"]
