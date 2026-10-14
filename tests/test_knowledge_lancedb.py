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


# ---------------------------------------------------------------------------
# a collection an earlier build wrote
# ---------------------------------------------------------------------------


async def _pre_upgrade(store: LanceDBVectorStore, name: str = "kb") -> None:
    """A collection in the shape a build before the chunk columns wrote it.

    Made through LanceDB rather than through the store, because the store no
    longer knows how to write this shape -- which is the point: what has to
    keep working is the collection already on an installed machine.
    """
    import pyarrow as pa

    db = await store._connect()
    schema = pa.schema(
        [
            pa.field("vector", pa.list_(pa.float32(), DIM)),
            pa.field("document_id", pa.string()),
            pa.field("source", pa.string()),
            pa.field("chunk_index", pa.int32()),
            pa.field("chunk_json", pa.string()),
            pa.field("metadata_kv", pa.list_(pa.string())),
        ]
    )
    table = await db.create_table(name, schema=schema)
    await table.add(
        [
            {
                "vector": [1.0, 0.0, 0.0, 0.0],
                "document_id": "d1",
                "source": "d1.md",
                "chunk_index": 0,
                "chunk_json": Chunk(
                    content=TextBlock(text="the older rows"),
                    source="d1.md",
                    chunk_index=0,
                    total_chunks=1,
                ).model_dump_json(),
                "metadata_kv": [],
            }
        ]
    )


async def test_a_collection_from_an_earlier_build_can_still_be_searched(store) -> None:
    """The filter names `enabled`, and a collection written before that column
    existed has no such field -- the scan does not read it as null, it refuses
    the query outright. A base indexed before this build was unsearchable until
    something happened to write to it."""
    await _pre_upgrade(store)

    hits = await store.search("kb", [1.0, 0.0, 0.0, 0.0], top_k=5)

    assert [hit.chunk.text for hit in hits] == ["the older rows"]


async def test_its_rows_come_back_enabled_and_unnamed(store) -> None:
    """Enabled by construction: there was no way to turn one off when they were
    written. Unnamed because nothing addressed a single piece back then, and a
    reader who wants to act on one reindexes the document."""
    await _pre_upgrade(store)

    hit = (await store.search("kb", [1.0, 0.0, 0.0, 0.0], top_k=5))[0]

    assert hit.chunk_id == ""


async def test_searching_one_widens_it_once_and_for_all(store) -> None:
    """A metadata edit rather than a rewrite, so it is cheap to do on the read
    that needs it -- and having been done, every later path finds the columns
    already there."""
    await _pre_upgrade(store)
    await store.search("kb", [1.0, 0.0, 0.0, 0.0], top_k=5)

    table = await store._table("kb")

    assert {"chunk_id", "enabled", "manual", "text"} <= set((await table.schema()).names)


async def test_a_keyword_search_of_one_answers_nothing_rather_than_failing(store) -> None:
    """The other half of retrieval, and the one the widening cannot rescue.

    The text column is what BM25 tokenizes, and a row written before it existed
    has nothing in it -- widening gives the column, not the words. So an older
    base answers by meaning and not by keyword until its documents are
    reindexed, which writes the text. Answering nothing is the honest outcome;
    what matters is that it does not refuse the query."""
    await _pre_upgrade(store)

    assert await store.keyword_search("kb", "older", top_k=5) == []

    await store.insert("kb", [_record("d2", 0, "the newer rows", [0.0, 1.0, 0.0, 0.0])])
    hits = await store.keyword_search("kb", "newer", top_k=5)

    assert [hit.chunk.text for hit in hits] == ["the newer rows"]


# -- renumbering after a hand edit ---------------------------------


def _numbered(doc: str, index: int, total: int, **kw) -> VectorRecord:
    """One piece that can be addressed by id, as indexing writes them."""
    return VectorRecord(
        vector=[1.0 if n == index % DIM else 0.0 for n in range(DIM)],
        document_id=doc,
        chunk_id=f"c{index:04d}",
        chunk=Chunk(
            content=TextBlock(text=f"piece {index}"),
            source=f"{doc}.md",
            chunk_index=index,
            total_chunks=total,
            metadata={"lang": "en"},
        ),
        **kw,
    )


async def _version(store, name: str = "kb") -> int:
    return await (await store._writable(name)).version()


async def test_renumbering_a_document_is_one_write(store) -> None:
    """Deleting the first piece changes the index of every piece after it and
    the total on all of them. A write per row is a commit per row: measured on
    a 400-chunk document, one deletion took the table from version 2 to 402
    and 9.9 MB on disk, with the panel's controls inert for the whole time.
    """
    await store.create_collection("kb", DIM)
    await store.insert("kb", [_numbered("d1", n, 40) for n in range(40)])
    await store.delete_chunks("kb", ["c0000"], document_id="d1")
    before = await _version(store)

    total = await store.renumber("kb", "d1")

    assert total == 39
    assert await _version(store) == before + 1, "one commit, however many rows moved"


async def test_renumbering_leaves_the_numbers_contiguous(store) -> None:
    await store.create_collection("kb", DIM)
    await store.insert("kb", [_numbered("d1", n, 5) for n in range(5)])
    await store.delete_chunks("kb", ["c0002"], document_id="d1")

    total = await store.renumber("kb", "d1")
    held, _ = await store.list_chunks("kb", "d1")

    assert [piece.chunk.chunk_index for piece in held] == list(range(total))
    assert {piece.chunk.total_chunks for piece in held} == {total}


async def test_renumbering_keeps_every_column_it_did_not_rewrite(store) -> None:
    """The rows go back through a merge that replaces what it matches, so a
    column left out of the read is a column emptied on disk. The vector is the
    one that would not be noticed: the rows still list, and the base simply
    stops answering."""
    await store.create_collection("kb", DIM)
    await store.insert(
        "kb",
        [
            _numbered("d1", 0, 4),
            _numbered("d1", 1, 4, enabled=False),
            _numbered("d1", 2, 4, manual=True),
            _numbered("d1", 3, 4),
        ],
    )
    await store.delete_chunks("kb", ["c0000"], document_id="d1")

    await store.renumber("kb", "d1")
    held, _ = await store.list_chunks("kb", "d1")

    assert [piece.enabled for piece in held] == [False, True, True]
    assert [piece.manual for piece in held] == [False, True, False]
    assert all(piece.chunk.metadata == {"lang": "en"} for piece in held)
    hits = await store.search("kb", [0.0, 0.0, 1.0, 0.0], top_k=1)
    assert hits and hits[0].chunk.text == "piece 2", "the vectors still answer"


async def test_renumbering_a_document_already_in_order_writes_nothing(store) -> None:
    await store.create_collection("kb", DIM)
    await store.insert("kb", [_numbered("d1", n, 3) for n in range(3)])
    before = await _version(store)

    assert await store.renumber("kb", "d1") == 3
    assert await _version(store) == before, "nothing moved, so nothing was written"


async def test_rows_from_before_ids_existed_are_left_alone(store) -> None:
    """They carry an empty id, which names no row. The per-row path spent a
    commit on each of them that changed nothing."""
    await store.create_collection("kb", DIM)
    await store.insert(
        "kb",
        [
            VectorRecord(
                vector=[1.0, 0.0, 0.0, 0.0],
                document_id="d1",
                chunk=Chunk(content=TextBlock(text="old"), source="d1.md", chunk_index=7, total_chunks=9),
            )
        ],
    )
    before = await _version(store)

    assert await store.renumber("kb", "d1") == 1
    assert await _version(store) == before, "nothing addressable, so nothing written"


async def test_renumbering_an_absent_collection_answers_nothing(store) -> None:
    assert await store.renumber("nosuchbase", "d1") == 0


async def test_renumbering_one_document_does_not_touch_an_identical_row_in_another(store) -> None:
    """A chunk id is derived from the text, so two documents in one base that
    hold the same sentence hold the same id.

    Keyed on the id alone, the merge matches the other document's row and
    overwrites it with this one's -- which is that document silently losing a
    piece. Caught here with the id shared on purpose; the per-row path it
    replaces scoped by the document for the same reason.
    """
    shared = "the same sentence in both"
    await store.create_collection("kb", DIM)
    await store.insert(
        "kb",
        [
            VectorRecord(
                vector=[1.0, 0.0, 0.0, 0.0],
                document_id="d1",
                chunk_id="shared",
                chunk=Chunk(content=TextBlock(text=shared), source="d1.md", chunk_index=0, total_chunks=3),
            ),
            _numbered("d1", 1, 3),
            _numbered("d1", 2, 3),
        ],
    )
    await store.insert(
        "kb",
        [
            VectorRecord(
                vector=[1.0, 0.0, 0.0, 0.0],
                document_id="d2",
                chunk_id="shared",
                chunk=Chunk(content=TextBlock(text=shared), source="d2.md", chunk_index=0, total_chunks=2),
            ),
            _numbered("d2", 1, 2),
        ],
    )
    await store.delete_chunks("kb", ["c0001"], document_id="d1")

    await store.renumber("kb", "d1")

    other, count = await store.list_chunks("kb", "d2")
    assert count == 2, "the other document kept both its pieces"
    assert [piece.chunk.source for piece in other] == ["d2.md", "d2.md"], "and they are still its own"
    assert [piece.chunk.total_chunks for piece in other] == [2, 2]
