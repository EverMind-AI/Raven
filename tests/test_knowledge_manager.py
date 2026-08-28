"""End-to-end tests for a knowledge base: create, upload, index, search.

The vector store and the records are real; only the embedding endpoint is
stubbed, because it is the one piece that is a network call. The stub embeds
by counting words, which is enough for "the nearer text ranks first" without
pretending to be a model.
"""

from __future__ import annotations

import pytest

from raven.knowledge._embedding import EmbeddingConfig
from raven.knowledge._manager import KnowledgeError, KnowledgeManager, StaleBaseError

DIM = 8
_VOCAB = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]


class _StubClient:
    """Embeds a text as a bag-of-words count over a tiny vocabulary."""

    def __init__(self, model: str = "stub-embed", dimensions: int = DIM) -> None:
        self.model = model
        self.dimensions = dimensions
        self.calls: list[list[str]] = []
        self.probes = 0
        self.declared: int | None = None

    @property
    def declared_dimensions(self) -> int | None:
        """Unpinned by default, like a real config that names no width -- so
        the manager has to measure, which is the path worth exercising."""
        return self.declared

    async def probe_dimensions(self) -> int:
        self.probes += 1
        return self.dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            lowered = text.lower()
            counts = [float(lowered.count(word)) for word in _VOCAB]
            # A zero vector has no direction, so cosine distance against it is
            # undefined; a floor keeps every text somewhere on the sphere.
            vectors.append(counts if any(counts) else [0.01] * DIM)
        return vectors


@pytest.fixture
def manager(tmp_path, monkeypatch):
    client = _StubClient()
    mgr = KnowledgeManager(
        tmp_path / "knowledge",
        embedding=EmbeddingConfig(model=client.model, base_url="https://x/v1", api_key="k", dimensions=DIM),
    )
    monkeypatch.setattr(mgr, "_client", lambda: client)
    mgr.stub = client  # type: ignore[attr-defined]
    return mgr


MARKDOWN = b"""# Handbook

## Alpha section

alpha alpha alpha discussion of the first topic.

## Beta section

beta beta beta notes on the second topic.
"""


async def _ready_base(manager, content: bytes = MARKDOWN, filename: str = "handbook.md"):
    base = await manager.create_base(name="handbook")
    doc = manager.add_document(base.id, filename=filename, content=content)
    indexed = await manager.index_document(doc.id)
    return base, indexed


# ── the whole path ────────────────────────────────────────────────


async def test_create_upload_index_search(manager) -> None:
    base, doc = await _ready_base(manager)

    assert doc.status == "ready"
    assert doc.chunk_count == 2

    hits = await manager.search([base.id], "alpha", top_k=1)
    assert len(hits) == 1
    assert "first topic" in hits[0].chunk.text


async def test_search_ranks_the_nearer_section_first(manager) -> None:
    base, _ = await _ready_base(manager)
    hits = await manager.search([base.id], "beta", top_k=2)
    assert "second topic" in hits[0].chunk.text


async def test_headings_survive_into_the_indexed_chunks(manager) -> None:
    """The structured parser is what makes a hit citable. If the plain parser
    had claimed markdown, every chunk would carry an empty heading path."""
    base, _ = await _ready_base(manager)
    hits = await manager.search([base.id], "alpha", top_k=1)
    assert hits[0].chunk.metadata.get("heading_path") == ["Handbook", "Alpha section"]


async def test_a_plain_text_upload_still_indexes(manager) -> None:
    """TextParser has to stay behind the structured one, which claims only
    markdown and HTML."""
    base, doc = await _ready_base(manager, content=b"alpha notes", filename="notes.txt")
    assert doc.status == "ready" and doc.chunk_count == 1


# ── indexing rules ────────────────────────────────────────────────


async def test_a_document_starts_queued_and_index_pending_drains_it(manager) -> None:
    base = await manager.create_base(name="handbook")
    manager.add_document(base.id, filename="a.md", content=MARKDOWN)
    manager.add_document(base.id, filename="b.md", content=b"beta")

    assert await manager.index_pending() == 2
    assert {d.status for d in manager.list_documents(base.id)} == {"ready"}
    assert await manager.index_pending() == 0


async def test_reindexing_replaces_instead_of_duplicating(manager) -> None:
    """Appending would leave the previous run's chunks in the collection and
    every hit would come back twice."""
    base, doc = await _ready_base(manager)
    await manager.index_document(doc.id)

    hits = await manager.search([base.id], "alpha", top_k=10)
    assert len({h.chunk.text for h in hits}) == len(hits)


async def test_an_unparseable_upload_fails_that_document_only(manager) -> None:
    """One bad upload must not stop the queue behind it, and the reason
    belongs on the row the person who uploaded it is looking at."""
    base = await manager.create_base(name="handbook")
    bad = manager.add_document(base.id, filename="picture.png", content=b"\x89PNG")
    good = manager.add_document(base.id, filename="ok.md", content=MARKDOWN)

    await manager.index_pending()

    assert manager.get_document(bad.id).status == "failed"
    assert "no parser" in manager.get_document(bad.id).error
    assert manager.get_document(good.id).status == "ready"


async def test_indexing_a_document_whose_base_is_gone_fails_it(manager) -> None:
    base = await manager.create_base(name="handbook")
    doc = manager.add_document(base.id, filename="a.md", content=MARKDOWN)
    await manager.delete_base(base.id)

    assert await manager.index_document(doc.id) is None


# ── the staleness rule ────────────────────────────────────────────


async def test_a_base_indexed_with_another_model_refuses_to_be_searched(manager) -> None:
    """The vectors answer to the old model; a query embedded with the new one
    lands somewhere unrelated in the same space. Searching anyway returns
    confident nonsense, so it has to say rebuild."""
    base, _ = await _ready_base(manager)
    manager.stub.model = "a-different-model"

    with pytest.raises(StaleBaseError, match="rebuild"):
        await manager.search([base.id], "alpha")


async def test_a_width_change_is_also_stale(manager) -> None:
    """The base records both the model and the width, and the width leg has to
    stand on its own: a model can be redeployed at a different width under the
    same name. Clearing the memo is what a fresh process does."""
    base, _ = await _ready_base(manager)
    manager.stub.dimensions = DIM + 1
    manager._widths.clear()

    with pytest.raises(StaleBaseError):
        await manager.search([base.id], "alpha")


async def test_a_pin_that_contradicts_the_model_is_refused(manager) -> None:
    """Found by running against a real endpoint: the deployment's config pins
    1024 for a model that returns 4096. Trusting the pin sized the collection
    to 1024 and the first insert died on an Arrow cast error naming neither the
    config nor the model."""
    manager.stub.declared = DIM + 1

    with pytest.raises(KnowledgeError, match="not what"):
        await manager.create_base(name="handbook")


async def test_a_pin_that_agrees_is_accepted(manager) -> None:
    manager.stub.declared = DIM
    base = await manager.create_base(name="handbook")
    assert base.dimensions == DIM


async def test_the_width_is_probed_once_per_model(manager) -> None:
    base, _ = await _ready_base(manager)
    await manager.search([base.id], "alpha")
    await manager.search([base.id], "beta")
    assert manager.stub.probes == 1


async def test_moving_the_endpoint_does_not_make_a_base_stale(manager) -> None:
    """Rotating a key or putting the same model behind a new gateway changes
    neither the vectors nor what a query embeds to. Rebuilding for that throws
    away a working index for nothing."""
    base, _ = await _ready_base(manager)
    manager._embedding = EmbeddingConfig(
        model=manager.stub.model, base_url="https://moved/v1", api_key="rotated", dimensions=DIM
    )

    assert await manager.search([base.id], "alpha", top_k=1)


async def test_indexing_into_a_stale_base_fails_the_document_not_the_queue(manager) -> None:
    base, _ = await _ready_base(manager)
    manager.stub.model = "a-different-model"
    doc = manager.add_document(base.id, filename="b.md", content=b"beta")

    assert (await manager.index_document(doc.id)).status == "failed"
    assert "rebuild" in manager.get_document(doc.id).error


# ── deletion ──────────────────────────────────────────────────────


async def test_deleting_a_document_removes_its_vectors_and_its_bytes(manager) -> None:
    base, doc = await _ready_base(manager)

    assert await manager.delete_document(doc.id) is True
    assert manager.read_document(doc.id) is None
    assert await manager.search([base.id], "alpha") == []


async def test_deleting_a_base_takes_the_collection_with_it(manager) -> None:
    """A records-only delete leaves vectors under an id nothing lists any
    more -- an index no one can name or reclaim."""
    base, doc = await _ready_base(manager)

    assert await manager.delete_base(base.id) is True
    assert await manager._store.has_collection(base.id) is False
    assert manager.read_document(doc.id) is None
    assert manager.get_base(base.id) is None


async def test_deleting_a_missing_base_says_so(manager) -> None:
    assert await manager.delete_base("nope") is False


# ── configuration ─────────────────────────────────────────────────


async def test_a_base_cannot_be_created_without_an_embedding_endpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("raven.knowledge._manager.load_embedding_config", lambda: None)
    mgr = KnowledgeManager(tmp_path / "knowledge")

    assert mgr.embedding_available() is False
    with pytest.raises(KnowledgeError, match="no embedding endpoint"):
        await mgr.create_base(name="handbook")


async def test_uploading_to_a_missing_base_is_refused(manager) -> None:
    with pytest.raises(KnowledgeError, match="no knowledge base"):
        manager.add_document("nope", filename="a.md", content=MARKDOWN)


async def test_the_collection_is_named_for_the_id_not_the_name(manager) -> None:
    """A rename is an edit; a collection that followed the display name would
    strand every row indexed under the old one."""
    base, _ = await _ready_base(manager)
    manager.rename_base(base.id, name="renamed")

    assert await manager._store.has_collection(base.id) is True
    assert len(await manager.search([base.id], "alpha", top_k=1)) == 1


async def test_search_merges_and_reranks_across_bases(manager) -> None:
    first = await manager.create_base(name="first")
    second = await manager.create_base(name="second")
    manager.add_document(first.id, filename="a.txt", content=b"alpha alpha alpha")
    manager.add_document(second.id, filename="b.txt", content=b"alpha")
    await manager.index_pending()

    hits = await manager.search([first.id, second.id], "alpha alpha alpha", top_k=2)
    assert len(hits) == 2
    assert hits[0].score >= hits[1].score


async def test_the_query_is_embedded_once_for_all_bases(manager) -> None:
    first = await manager.create_base(name="first")
    second = await manager.create_base(name="second")
    manager.add_document(first.id, filename="a.txt", content=b"alpha")
    manager.add_document(second.id, filename="b.txt", content=b"alpha")
    await manager.index_pending()

    before = len(manager.stub.calls)
    await manager.search([first.id, second.id], "alpha")
    assert len(manager.stub.calls) == before + 1


async def test_an_empty_query_searches_nothing(manager) -> None:
    base, _ = await _ready_base(manager)
    assert await manager.search([base.id], "   ") == []
