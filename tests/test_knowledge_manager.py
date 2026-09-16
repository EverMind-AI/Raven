"""End-to-end tests for a knowledge base: create, upload, index, search.

The vector store and the records are real; only the embedding endpoint is
stubbed, because it is the one piece that is a network call. The stub embeds
by counting words, which is enough for "the nearer text ranks first" without
pretending to be a model.
"""

from __future__ import annotations

import pytest

from raven.knowledge._embedding import EmbeddingConfig
from raven.knowledge._manager import DuplicateBaseNameError, KnowledgeError, KnowledgeManager, StaleBaseError

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


class _NoStore:
    """Enough of a vector store to build a manager. Nothing here searches."""

    async def delete_collection(self, collection: str) -> None:
        return None


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

    hits = (await manager.search([base.id], "alpha", top_k=1)).hits
    assert len(hits) == 1
    assert "first topic" in hits[0].chunk.text


async def test_search_ranks_the_nearer_section_first(manager) -> None:
    base, _ = await _ready_base(manager)
    hits = (await manager.search([base.id], "beta", top_k=2)).hits
    assert "second topic" in hits[0].chunk.text


async def test_headings_survive_into_the_indexed_chunks(manager) -> None:
    """The structured parser is what makes a hit citable. If the plain parser
    had claimed markdown, every chunk would carry an empty heading path."""
    base, _ = await _ready_base(manager)
    hits = (await manager.search([base.id], "alpha", top_k=1)).hits
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

    hits = (await manager.search([base.id], "alpha", top_k=10)).hits
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

    assert (await manager.search([base.id], "alpha", top_k=1)).hits


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
    assert (await manager.search([base.id], "alpha")).hits == []


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
    assert len((await manager.search([base.id], "alpha", top_k=1)).hits) == 1


async def test_search_merges_and_reranks_across_bases(manager) -> None:
    first = await manager.create_base(name="first")
    second = await manager.create_base(name="second")
    manager.add_document(first.id, filename="a.txt", content=b"alpha alpha alpha")
    manager.add_document(second.id, filename="b.txt", content=b"alpha")
    await manager.index_pending()

    hits = (await manager.search([first.id, second.id], "alpha alpha alpha", top_k=2)).hits
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
    assert (await manager.search([base.id], "   ")).hits == []


class TestABaseWithNoEmbeddingModel:
    """A base that keeps its documents and is never searched by vector.

    The case is real -- somewhere to put files the agent reads whole, or that a
    person opens from the page -- and it has to reach no endpoint at all. It is
    also not revisable: a collection's width is fixed when it is made, so a base
    created without one is rebuilt rather than switched.
    """

    async def test_it_is_created_without_reaching_the_endpoint(self, manager) -> None:
        base = await manager.create_base(name="files", embedding=False)

        assert base.embedding_model == ""
        assert base.dimensions == 0
        # Nothing was embedded, so nothing was asked of the endpoint -- the
        # width probe is the call this avoids.
        assert manager.stub.calls == []

    async def test_a_document_is_stored_and_not_indexed(self, manager) -> None:
        """Ready, because the file is in the base and can be opened. Failed
        would send a reader looking for a fault; pending would promise an
        indexer that is never coming."""
        base = await manager.create_base(name="files", embedding=False)
        doc = manager.add_document(base.id, filename="notes.md", content=b"# hi\n")

        indexed = await manager.index_document(doc.id)

        assert indexed.status == "ready"
        assert indexed.chunk_count == 0
        assert manager.stub.calls == []
        # And the bytes are still there to open.
        assert manager.read_document(doc.id) == b"# hi\n"

    async def test_it_is_skipped_rather_than_refused_by_a_search(self, manager) -> None:
        """Asking a mixed set is ordinary, and a base with no vectors is not an
        error in the others."""
        plain = await manager.create_base(name="files", embedding=False)
        vectored = await manager.create_base(name="handbook")
        doc = manager.add_document(vectored.id, filename="handbook.md", content=b"onboarding is here\n")
        await manager.index_document(doc.id)

        hits = (await manager.search([plain.id, vectored.id], "onboarding")).hits

        assert [h.chunk.source for h in hits] == ["handbook.md"]

    async def test_searching_only_such_a_base_answers_nothing(self, manager) -> None:
        plain = await manager.create_base(name="files", embedding=False)

        assert (await manager.search([plain.id], "anything")).hits == []

    async def test_the_question_has_one_answer(self, manager) -> None:
        """Three readers skip these bases; three spellings of "is the model
        empty" is how one of them ends up not skipping."""
        plain = await manager.create_base(name="files", embedding=False)
        vectored = await manager.create_base(name="handbook")

        assert manager.embeds(plain) is False
        assert manager.embeds(vectored) is True


class TestTwoBasesCannotShareAName:
    """The rail shows a base's name and nothing else.

    Two rows reading "t3" leave a reader picking between them and finding out
    which was which by opening both -- and a delete then asks them to be sure
    about which of two identical rows they meant.
    """

    async def test_a_second_base_cannot_take_the_name(self, manager) -> None:
        await manager.create_base(name="t3")

        with pytest.raises(DuplicateBaseNameError):
            await manager.create_base(name="t3")

        assert [b.name for b in manager.list_bases()] == ["t3"]

    @pytest.mark.parametrize("second", ["T3", "t3 ", " T3"])
    async def test_case_and_spacing_do_not_make_it_a_different_name(self, manager, second: str) -> None:
        """Two bases called "t3" and "T3 " are the same problem as two called
        "t3": the reader cannot tell those apart either."""
        await manager.create_base(name="t3")

        with pytest.raises(DuplicateBaseNameError):
            await manager.create_base(name=second)

    async def test_what_was_typed_is_what_is_stored(self, manager) -> None:
        """Compared casefolded, kept as written: the rule is about telling
        bases apart, not about how a name may be spelled."""
        base = await manager.create_base(name="Handbook")

        assert base.name == "Handbook"

    async def test_a_rename_cannot_take_a_name_either(self, manager) -> None:
        """Or renaming is simply the way around the rule."""
        await manager.create_base(name="t1")
        second = await manager.create_base(name="t2")

        with pytest.raises(DuplicateBaseNameError):
            manager.rename_base(second.id, name="t1")

        assert manager.get_base(second.id).name == "t2"

    async def test_a_base_may_keep_its_own_name(self, manager) -> None:
        """Without this, saving a rename that touched only the description
        would fail against the base itself."""
        base = await manager.create_base(name="t1")

        renamed = manager.rename_base(base.id, name="t1", description="notes")

        assert renamed.name == "t1"
        assert renamed.description == "notes"

    async def test_a_freed_name_can_be_taken_again(self, manager) -> None:
        first = await manager.create_base(name="t3")
        await manager.delete_base(first.id)

        again = await manager.create_base(name="t3")

        assert again.name == "t3"


# ── rewriting a document in place ─────────────────────────────────


async def test_rewriting_a_document_replaces_its_text_and_requeues_it(manager) -> None:
    base, doc = await _ready_base(manager)

    rewritten = await manager.replace_document(
        doc.id,
        filename="handbook v2.md",
        content=b"# Handbook\n\n## Gamma section\n\ngamma gamma gamma on the third topic.\n",
    )

    assert (rewritten.source, rewritten.status, rewritten.chunk_count) == ("handbook v2.md", "pending", 0)
    assert manager.read_document(doc.id) == (
        b"# Handbook\n\n## Gamma section\n\ngamma gamma gamma on the third topic.\n"
    )
    indexed = await manager.index_document(doc.id)
    assert indexed.status == "ready"
    hits = (await manager.search([base.id], "gamma", top_k=5)).hits
    assert "third topic" in hits[0].chunk.text


async def test_the_old_text_stops_being_searchable_the_moment_it_is_rewritten(manager) -> None:
    """Before the reindex, not after: chunks are what a search answers with,
    and answering from a note the reader has already rewritten is worse than
    answering with nothing. If the reindex then fails, they stay gone."""
    base, doc = await _ready_base(manager)

    await manager.replace_document(doc.id, filename="handbook.md", content=b"# Handbook\n\nquite different now.\n")

    assert (await manager.search([base.id], "alpha")).hits == []


async def test_rewriting_a_document_that_is_gone_answers_nothing(manager) -> None:
    assert await manager.replace_document("nope", filename="x.md", content=b"x") is None


async def test_a_document_remembers_which_kind_of_source_it_came_from(manager) -> None:
    base = await manager.create_base(name="handbook")

    note = manager.add_document(base.id, filename="plan.md", content=b"# Plan", origin="note")
    page = manager.add_document(
        base.id, filename="docs.md", content=b"# Docs", origin="url", origin_ref="https://example.com/docs"
    )
    uploaded = manager.add_document(base.id, filename="handbook.md", content=MARKDOWN)

    assert (note.origin, note.origin_ref) == ("note", "")
    assert (page.origin, page.origin_ref) == ("url", "https://example.com/docs")
    # The default, so a registry written before the field existed still loads,
    # and every document in one is what the default says it is.
    assert (uploaded.origin, uploaded.origin_ref) == ("file", "")


async def test_the_manager_builds_the_client_the_endpoint_calls_for(tmp_path) -> None:
    """Which vendor is being spoken to is a fact about the base URL, so the
    manager must not hardcode the plain client past it."""
    from raven.knowledge._embedding import EmbeddingClient, SiliconFlowEmbeddingClient

    def built(base_url: str, model: str):
        mgr = KnowledgeManager(
            tmp_path / base_url.replace("/", "_"),
            store=_NoStore(),
            embedding=EmbeddingConfig(model=model, base_url=base_url, api_key="k"),
        )
        return mgr._client()

    assert isinstance(built("https://api.siliconflow.cn/v1", "BAAI/bge-large-zh-v1.5"), SiliconFlowEmbeddingClient)
    assert type(built("https://api.openai.com/v1", "text-embedding-3-small")) is EmbeddingClient
