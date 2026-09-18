"""End-to-end tests for a knowledge base: create, upload, index, search.

The vector store and the records are real; only the embedding endpoint is
stubbed, because it is the one piece that is a network call. The stub embeds
by counting words, which is enough for "the nearer text ranks first" without
pretending to be a model.
"""

from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from raven.knowledge._embedding import EmbeddingConfig, EmbeddingError
from raven.knowledge._manager import DuplicateBaseNameError, KnowledgeError, KnowledgeManager

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


class _Endpoints:
    """The endpoints a manager can reach, other than the configured one.

    A base built with a model the current pin no longer names has to be
    embedded through whatever still serves that model, and the manager finds
    it by building a client for the base's own model. This stands in for that
    lookup: ``serve`` makes a model answerable, ``refuse`` makes it fail the
    way an endpoint that does not host it would.
    """

    def __init__(self) -> None:
        self.clients: dict[str, _StubClient] = {}
        self.refusals: dict[str, Exception] = {}
        #: Every config the manager asked for a client with, so a test can say
        #: which endpoint it decided on and not only which model.
        self.built: list[EmbeddingConfig] = []

    def serve(self, model: str, dimensions: int = DIM) -> "_StubClient":
        client = _StubClient(model=model, dimensions=dimensions)
        self.clients[model] = client
        return client

    def refuse(self, model: str, error: Exception) -> None:
        self.refusals[model] = error

    def build(self, config: EmbeddingConfig):
        self.built.append(config)
        if config.model in self.refusals:
            raise self.refusals[config.model]
        return self.clients.get(config.model) or self.serve(config.model)


@pytest.fixture
def endpoints(monkeypatch):
    """Route the manager's per-base client building at the stubs above."""
    registry = _Endpoints()
    monkeypatch.setattr("raven.knowledge._manager.embedding_client", registry.build)
    monkeypatch.setattr(
        "raven.knowledge._manager.embedding_config_for",
        lambda provider, model, dimensions=None: (
            EmbeddingConfig(
                model=model, base_url="https://other/v1", api_key="k", provider=provider, dimensions=dimensions
            )
            if provider
            else None
        ),
    )
    return registry


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


#: The smallest PNG that decodes: one transparent pixel. What the parser is
#: routed by is the filename, and what the vision path needs is bytes Pillow
#: will open.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _StubVision:
    """A vision model that answers without a network."""

    max_figures = 64

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    async def describe(self, image: bytes, *, mime: str = "", context_above: str = "", context_below: str = "") -> str:
        self.calls.append((context_above, context_below))
        return self.text


def _docx_with_picture() -> bytes:
    """A minimal Word package holding one paragraph and one embedded picture."""
    import io
    import zipfile

    namespaces = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    )
    body = (
        "<w:p><w:r><w:t>alpha alpha discussion of the first topic.</w:t></w:r></w:p>"
        '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Picture 1"/>'
        '<a:graphic><a:graphicData><a:blip r:embed="rId7"/></a:graphicData></a:graphic>'
        "</wp:inline></w:drawing></w:r></w:p>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("word/document.xml", f"<w:document {namespaces}><w:body>{body}</w:body></w:document>")
        package.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId7" Target="media/image1.png" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/>'
            "</Relationships>",
        )
        package.writestr("word/media/image1.png", _PNG)
    return buffer.getvalue()


MARKDOWN = b"""# Handbook

## Alpha section

alpha alpha alpha discussion of the first topic.

## Beta section

beta beta beta notes on the second topic.
"""


async def _ready_base(manager, content: bytes = MARKDOWN, filename: str = "handbook.md"):
    base = await manager.create_base(name="handbook")
    # Small enough that the fixture document is more than one chunk, which is
    # what the paging, deleting and disabling tests below are about. The base
    # default would swallow it whole.
    manager.configure_base(base.id, chunk_size=12)
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
    bad = manager.add_document(base.id, filename="firmware.bin", content=b"\x00\x01\x02")
    good = manager.add_document(base.id, filename="ok.md", content=MARKDOWN)

    await manager.index_pending()

    assert manager.get_document(bad.id).status == "failed"
    assert "no parser" in manager.get_document(bad.id).error
    assert manager.get_document(good.id).status == "ready"


async def test_an_image_with_no_vision_model_fails_with_the_reason(manager, monkeypatch) -> None:
    """A picture has no text of its own, so a build that cannot look at one has
    nothing to index -- and the row has to say that rather than reporting a
    document with no chunks as ready."""
    monkeypatch.setattr("raven.knowledge._vision.load_vision_model", lambda: None)
    base = await manager.create_base(name="handbook")
    picture = manager.add_document(base.id, filename="chart.png", content=_PNG)
    good = manager.add_document(base.id, filename="ok.md", content=MARKDOWN)

    await manager.index_pending()

    assert manager.get_document(picture.id).status == "failed"
    assert "no vision model is configured" in manager.get_document(picture.id).error
    assert manager.get_document(good.id).status == "ready"


async def test_an_image_is_indexed_as_one_chunk(manager, monkeypatch) -> None:
    """One picture, one chunk, however much the model had to say about it: the
    section is marked as a figure, and the chunker never splits one."""
    described = ("A bar chart. " * 400).strip()
    monkeypatch.setattr(
        "raven.knowledge._vision.load_vision_model",
        lambda: _StubVision(described),
    )
    base = await manager.create_base(name="handbook")
    picture = manager.add_document(base.id, filename="chart.png", content=_PNG)

    indexed = await manager.index_document(picture.id)

    assert indexed.status == "ready"
    assert indexed.chunk_count == 1
    chunks, _ = await manager.document_chunks(picture.id)
    assert chunks[0].chunk.text == described


async def test_a_picture_nobody_could_read_warns_on_a_searchable_row(manager, monkeypatch) -> None:
    """The case this warning exists for: the document indexed, the row says
    ready, and half of what the file shows is not in the index. Nothing else
    would ever tell a reader -- a search about the missing part just answers
    worse."""
    monkeypatch.setattr("raven.knowledge._vision.load_vision_model", lambda: None)
    base = await manager.create_base(name="handbook")
    doc = manager.add_document(base.id, filename="report.docx", content=_docx_with_picture())

    indexed = await manager.index_document(doc.id)

    assert indexed.status == "ready", "still indexed, still searchable"
    assert indexed.chunk_count >= 1
    assert "no vision model is configured" in indexed.warning
    assert indexed.error == "", "a warning is not a failure"


async def test_the_warning_goes_when_the_reindex_reads_the_picture(manager, monkeypatch) -> None:
    """Otherwise a row keeps reporting a gap that has since been filled."""
    monkeypatch.setattr("raven.knowledge._vision.load_vision_model", lambda: None)
    base = await manager.create_base(name="handbook")
    doc = manager.add_document(base.id, filename="report.docx", content=_docx_with_picture())
    assert (await manager.index_document(doc.id)).warning

    monkeypatch.setattr("raven.knowledge._vision.load_vision_model", lambda: _StubVision("A bar chart."))
    assert (await manager.index_document(doc.id)).warning == ""


async def test_a_document_that_parsed_whole_carries_no_warning(manager) -> None:
    base, doc = await _ready_base(manager)

    assert doc.warning == ""


async def test_indexing_a_document_whose_base_is_gone_fails_it(manager) -> None:
    base = await manager.create_base(name="handbook")
    doc = manager.add_document(base.id, filename="a.md", content=MARKDOWN)
    await manager.delete_base(base.id)

    assert await manager.index_document(doc.id) is None


# ── the staleness rule ────────────────────────────────────────────


async def test_a_base_is_searched_with_the_model_it_was_built_with(manager, endpoints) -> None:
    """The vectors answer to the model the base was built with, so that is the
    model the query is embedded with -- moving the configured pin does not move
    the space an existing collection lives in."""
    base, _ = await _ready_base(manager)
    manager.stub.model = "a-different-model"
    old_model = endpoints.serve("stub-embed")

    hits = (await manager.search([base.id], "alpha", top_k=1)).hits

    assert len(hits) == 1
    assert old_model.calls == [["alpha"]], "the query went to the base's own model"
    assert manager.stub.calls[-1] != ["alpha"], "and not to the one configured now"


async def test_a_base_whose_model_cannot_be_reached_answers_by_keyword(manager, endpoints) -> None:
    """The words still work. A base whose model has gone answers worse rather
    than not at all, and says which of its answers came from words."""
    base, _ = await _ready_base(manager)
    manager.stub.model = "a-different-model"
    endpoints.refuse("stub-embed", EmbeddingError("model not served here"))

    outcome = await manager.search([base.id], "alpha")

    assert outcome.hits, "the text is still there to match against"
    assert "alpha" in outcome.hits[0].chunk.text.lower()
    assert base.id in outcome.by_keyword
    assert "Point the base at a provider that serves it" in outcome.by_keyword[base.id]


async def test_a_width_change_is_also_stale(manager) -> None:
    """The base records both the model and the width, and the width leg has to
    stand on its own: a model can be redeployed at a different width under the
    same name. Clearing the memo is what a fresh process does.

    Stale means the vectors cannot be queried, not that the base is gone: it
    answers by keyword and says why, and the refusal that matters stays on the
    indexing side, where writing into a mismatched collection would corrupt
    it."""
    base, _ = await _ready_base(manager)
    manager.stub.dimensions = DIM + 1
    manager._widths.clear()

    outcome = await manager.search([base.id], "alpha")

    assert base.id in outcome.by_keyword
    assert "rebuild the base" in outcome.by_keyword[base.id]


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
    manager.stub.dimensions = DIM + 1
    manager._widths.clear()
    doc = manager.add_document(base.id, filename="b.md", content=b"beta")

    assert (await manager.index_document(doc.id)).status == "failed"
    assert "rebuild" in manager.get_document(doc.id).error


async def test_a_new_document_is_embedded_with_the_base_own_model(manager, endpoints) -> None:
    """A document added to an older base has to land in the space that base's
    collection already holds, or nothing it says is findable."""
    base, _ = await _ready_base(manager)
    manager.stub.model = "a-different-model"
    old_model = endpoints.serve("stub-embed")
    doc = manager.add_document(base.id, filename="b.md", content=b"# beta\n\nbeta beta beta\n")

    assert (await manager.index_document(doc.id)).status == "ready"
    assert old_model.calls, "the chunks went to the base's own model"
    assert (await manager.search([base.id], "beta", top_k=1)).hits


# -- one embedding per model, not one per search -------------------


async def test_each_base_is_embedded_with_its_own_model(manager, endpoints) -> None:
    """The point of the whole arrangement: two bases built with two models are
    two different vector spaces, and one query vector cannot address both."""
    first, _ = await _ready_base(manager)
    # The pin moves, a second base is built on it, and it moves back -- so the
    # two bases hold vectors from two models and neither is the odd one out.
    manager.stub.model = "second-model"
    second = await manager.create_base(name="second")
    doc = manager.add_document(second.id, filename="s.md", content=b"# gamma\n\ngamma gamma\n")
    assert (await manager.index_document(doc.id)).status == "ready"
    manager.stub.model = "stub-embed"
    other = endpoints.serve("second-model")

    outcome = await manager.search([first.id, second.id], "alpha gamma")

    assert manager.stub.calls[-1] == ["alpha gamma"], "the base on the current model used it"
    assert other.calls == [["alpha gamma"]], "the base on the older model used that one"
    assert outcome.by_keyword == {}
    assert {hit.chunk.source for hit in outcome.hits} == {"handbook.md", "s.md"}


async def test_bases_sharing_a_model_share_one_embedding_call(manager) -> None:
    """The common case still costs one round trip: what is grouped is the
    model, not the base."""
    first, _ = await _ready_base(manager)
    second = await manager.create_base(name="second")
    doc = manager.add_document(second.id, filename="s.md", content=b"# gamma\n\ngamma\n")
    await manager.index_document(doc.id)
    before = len(manager.stub.calls)

    await manager.search([first.id, second.id], "alpha")

    assert len(manager.stub.calls) - before == 1


async def test_one_unreachable_base_does_not_take_the_others_down(manager, endpoints) -> None:
    """Asking a mixed set is ordinary. The base that fell back to words is
    named, so a caller is not left thinking every hit was scored the same
    way."""
    good, _ = await _ready_base(manager)
    manager.stub.model = "gone-model"
    stranded = await manager.create_base(name="stranded")
    doc = manager.add_document(stranded.id, filename="s.md", content=b"# gamma\n\ngamma\n")
    await manager.index_document(doc.id)
    manager.stub.model = "stub-embed"
    endpoints.refuse("gone-model", EmbeddingError("no endpoint serves it"))

    outcome = await manager.search([good.id, stranded.id], "alpha")

    assert "handbook.md" in {hit.chunk.source for hit in outcome.hits}
    assert list(outcome.by_keyword) == [stranded.id]
    assert "no endpoint serves it" in outcome.by_keyword[stranded.id]


async def test_a_base_with_no_provider_falls_back_to_the_configured_endpoint(manager, endpoints) -> None:
    """The last thing left to try for a base written before providers were
    recorded. Raven no longer reads the memory backend's own config for an
    older endpoint: a knowledge base that needs the memory plugin installed to
    answer is one that stops working when it is not.
    """
    base, _ = await _ready_base(manager)
    manager.stub.model = "a-different-model"
    served = endpoints.serve("stub-embed")

    hits = (await manager.search([base.id], "alpha", top_k=1)).hits

    assert hits and served.calls == [["alpha"]], "asked for the base's own model"
    assert endpoints.built[-1].model == "stub-embed"


async def test_a_base_can_be_pointed_at_the_provider_that_still_serves_it(manager, endpoints) -> None:
    """The way back for a base stranded by a change of pin, short of a rebuild:
    the model and the width are what the collection was built to and cannot
    move, but where that model is reached can."""
    base, _ = await _ready_base(manager)
    manager.stub.model = "a-different-model"
    endpoints.refuse("stub-embed", EmbeddingError("this endpoint does not serve it"))
    served_elsewhere = _StubClient(model="stub-embed")
    endpoints.clients["stub-embed"] = served_elsewhere
    endpoints.refusals.clear()

    assert manager.configure_base(base.id, embedding_provider="siliconflow").embedding_provider == "siliconflow"
    hits = (await manager.search([base.id], "alpha", top_k=1)).hits

    assert hits and served_elsewhere.calls == [["alpha"]]


async def test_a_base_can_be_built_on_a_picked_model(manager, endpoints) -> None:
    """The page offers every embedding model the install can reach, so the pair
    it picked is what the base is built on -- not the configured pin, which is
    only the default the picker starts on."""
    picked = endpoints.serve("picked-model")

    base = await manager.create_base(name="handbook", embedding_model="picked-model", embedding_provider="openai")

    assert (base.embedding_model, base.embedding_provider) == ("picked-model", "openai")
    assert picked.probes == 1, "the width was measured against the model that was picked"
    assert manager.stub.probes == 0, "and the configured one was never asked"


async def test_switching_the_model_rebuilds_what_the_base_holds(manager, endpoints) -> None:
    """The vectors in the collection were made by the old model, and no query
    embedded by the new one lands anywhere near them. So the documents go back
    to the queue rather than staying ready against an index that is gone."""
    base, doc = await _ready_base(manager)
    endpoints.serve("second-model")

    switched = await manager.switch_embedding(base.id, model="second-model", provider="openai")

    assert (switched.embedding_model, switched.embedding_provider) == ("second-model", "openai")
    assert [d.status for d in manager.list_documents(base.id)] == ["pending"]
    assert manager.get_document(doc.id).chunk_count == 0


async def test_the_requeued_documents_index_on_the_new_model(manager, endpoints) -> None:
    """Which is the point of requeueing them: the blob is still on disk, so a
    reindex is all it takes to have the base searchable again."""
    base, _ = await _ready_base(manager)
    second = endpoints.serve("second-model")
    await manager.switch_embedding(base.id, model="second-model", provider="openai")

    assert await manager.index_pending() == 1

    hits = (await manager.search([base.id], "alpha", top_k=1)).hits
    assert hits and "first topic" in hits[0].chunk.text
    assert ["alpha"] in second.calls, "the query went to the model the base now holds"


async def test_the_same_model_through_another_account_is_not_a_rebuild(manager, endpoints) -> None:
    """Nothing about the vectors changed -- only the address the next call goes
    out on -- so requeueing every document would be work for nothing."""
    base, doc = await _ready_base(manager)
    endpoints.serve("stub-embed")

    switched = await manager.switch_embedding(base.id, model="stub-embed", provider="siliconflow")

    assert switched.embedding_provider == "siliconflow"
    assert manager.get_document(doc.id).status == "ready"


async def test_a_model_that_cannot_be_reached_leaves_the_base_alone(manager, endpoints) -> None:
    """The width is measured before anything is dropped, so a picked model that
    refuses costs the reader a message rather than their index."""
    base, doc = await _ready_base(manager)
    endpoints.refuse("second-model", EmbeddingError("this endpoint does not serve it"))

    with pytest.raises(EmbeddingError):
        await manager.switch_embedding(base.id, model="second-model", provider="openai")

    assert manager.get_base(base.id).embedding_model == "stub-embed"
    assert manager.get_document(doc.id).status == "ready"
    assert (await manager.search([base.id], "alpha", top_k=1)).hits


async def test_embedding_can_be_turned_off_and_on_again(manager, endpoints) -> None:
    """The one choice that used to be fixed at creation: a base made without a
    model was rebuilt or nothing, and now it is the same call as any other
    switch."""
    base, doc = await _ready_base(manager)

    off = await manager.switch_embedding(base.id, model="")
    assert (off.embedding_model, off.dimensions, off.embedding_provider) == ("", 0, "")
    assert not manager.embeds(off)

    endpoints.serve("stub-embed")
    on = await manager.switch_embedding(base.id, model="stub-embed", provider="openai")
    assert manager.embeds(on)
    assert await manager.index_pending() >= 1
    assert manager.get_document(doc.id).status == "ready"


async def test_switching_to_what_the_base_already_has_changes_nothing(manager) -> None:
    base, doc = await _ready_base(manager)
    before = manager.get_base(base.id)

    same = await manager.switch_embedding(base.id, model=before.embedding_model, provider=before.embedding_provider)

    assert same == before
    assert manager.get_document(doc.id).status == "ready"


async def test_a_new_base_records_who_served_its_model(manager) -> None:
    """Recorded at creation because a model id does not name a credential: the
    query has to go back out on that provider's address later."""
    manager.stub.provider = "siliconflow"

    base = await manager.create_base(name="recorded")

    assert base.embedding_provider == "siliconflow"


async def test_editing_a_chunk_re_embeds_it(manager) -> None:
    """A piece whose text changed and whose vector did not would be found by
    the old words and read as the new ones."""
    base, doc = await _ready_base(manager)
    held, _ = await manager.document_chunks(doc.id)
    target = held[0]

    written = await manager.update_chunk(doc.id, target.chunk_id, "epsilon epsilon epsilon")

    assert written.chunk.text == "epsilon epsilon epsilon"
    assert written.chunk_id != target.chunk_id, "the id follows the content"
    assert written.chunk.chunk_index == target.chunk.chunk_index, "and its place is kept"
    assert written.manual is True
    hits = (await manager.search([base.id], "epsilon")).hits
    assert any("epsilon" in hit.chunk.text for hit in hits)
    after, total = await manager.document_chunks(doc.id)
    assert total == len(held), "an edit replaces, it does not add"


async def test_an_edited_chunk_keeps_the_state_it_was_in(manager) -> None:
    _, doc = await _ready_base(manager)
    held, _ = await manager.document_chunks(doc.id)
    target = held[0]
    await manager.set_chunks_enabled(doc.id, [target.chunk_id], False)

    written = await manager.update_chunk(doc.id, target.chunk_id, "rewritten while off")

    assert written.enabled is False
    after, _ = await manager.document_chunks(doc.id)
    assert next(p for p in after if p.chunk_id == written.chunk_id).enabled is False


async def test_editing_a_chunk_to_the_same_text_is_not_a_write(manager) -> None:
    """No new vector, no new id: there is nothing for either to follow."""
    _, doc = await _ready_base(manager)
    held, _ = await manager.document_chunks(doc.id)
    target = held[0]

    written = await manager.update_chunk(doc.id, target.chunk_id, target.chunk.text)

    assert written.chunk_id == target.chunk_id


async def test_editing_a_chunk_that_is_not_there_is_refused(manager) -> None:
    _, doc = await _ready_base(manager)

    with pytest.raises(KnowledgeError):
        await manager.update_chunk(doc.id, "no-such-chunk", "text")


async def test_a_legacy_word_document_has_a_parser(manager) -> None:
    """It had none, so every reindex of a base holding one failed on it -- a
    file the panel can preview and the engine refused to read."""
    from raven.knowledge._manager import _default_parsers

    claimed = {media for parser in _default_parsers() for media in parser.supported_media_types}

    assert "application/msword" in claimed
    assert ".doc" in manager.supported_extensions()


async def test_a_table_carries_its_surroundings_without_being_asked(manager) -> None:
    """On by default, and it reaches the bases that predate the setting: none
    of them stored a value, so they take what the record defaults to. A table
    on its own embeds as a grid of values with nothing saying what they are
    about."""
    base = await manager.create_base(name="tables")

    assert base.table_context_size == 64
    assert base.image_context_size == 64
    chunker = manager._chunker_for(base)
    assert chunker.table_context_size == 64


# -- whether a base can embed at all -------------------------------


async def test_a_base_on_the_configured_model_can_embed(manager) -> None:
    base = await manager.create_base(name="current")

    assert manager.embedding_reach(base) == ""


async def test_a_base_with_no_provider_and_another_model_says_so(manager) -> None:
    """The case that actually happens: a base built before providers were
    recorded, on a model the configured endpoint does not serve. Nothing it
    holds can be indexed until that is fixed, and the reason belongs where the
    reader is looking rather than in a line of the gateway log."""
    base = await manager.create_base(name="stranded")
    # The pin moves under it, which is what leaves a base naming a model the
    # configured endpoint does not serve.
    manager._embedding = replace(manager._embedding, model="a-different-model")

    assert manager.embedding_reach(manager.get_base(base.id)) == "no_provider"


async def test_a_base_whose_provider_has_no_credential_says_which(manager, endpoints, monkeypatch) -> None:
    """A different repair from the one above, so a different answer."""
    base = await manager.create_base(name="keyless")
    manager._embedding = replace(manager._embedding, model="a-different-model")
    manager.configure_base(base.id, embedding_provider="siliconflow")
    monkeypatch.setattr("raven.knowledge._manager.embedding_config_for", lambda *a, **k: None)

    assert manager.embedding_reach(manager.get_base(base.id)) == "no_credential"


async def test_a_base_with_no_model_is_not_unreachable(manager) -> None:
    """A base created without embedding is not broken; it is what it asked to
    be, and warning about it would be warning about a choice."""
    base = await manager.create_base(name="files only", embedding=False)

    assert manager.embedding_reach(base) == ""


# -- the settings a base is chunked by -----------------------------


async def test_a_base_is_chunked_the_way_it_is_configured(manager) -> None:
    """The four chunking settings were saved, shown, and read by nothing: one
    chunker was built at startup and used for every base."""
    base = await manager.create_base(name="naive")
    manager.configure_base(base.id, smart_chunking=False, separator="!", chunk_size=1)
    doc = manager.add_document(base.id, filename="a.md", content=b"alpha!beta!gamma")
    await manager.index_document(doc.id)

    held, total = await manager.document_chunks(doc.id)

    assert total == 3, "cut on the separator this base asked for"
    assert [p.chunk.text for p in held] == ["alpha", "beta", "gamma"]


async def test_every_base_is_cut_the_naive_way_for_now(manager) -> None:
    """The structural chunker is being reworked, so the strategy setting is
    kept and not consulted: a base asking for smart chunking is cut on its
    delimiters like every other one."""
    base = await manager.create_base(name="smart")
    manager.configure_base(base.id, smart_chunking=True, separator="!", chunk_size=1)
    doc = manager.add_document(base.id, filename="a.md", content=b"alpha!beta")
    await manager.index_document(doc.id)

    held, total = await manager.document_chunks(doc.id)

    assert total == 2 and [p.chunk.text for p in held] == ["alpha", "beta"]


async def test_an_overlap_nobody_chose_is_not_applied(manager) -> None:
    """Every base carried 215 while nothing read the setting, so no document
    was ever chunked with it and nobody picked it."""
    base = await manager.create_base(name="legacy")
    manager._records._bases[base.id] = replace(base, chunk_overlap=215)
    manager._records._save()
    manager._records._bases.clear()
    manager._records._load()

    assert manager.get_base(base.id).chunk_overlap == 0


# -- keywords, when the vectors cannot be reached ------------------


async def test_keyword_search_finds_chinese_text(manager, endpoints) -> None:
    """The reason the keyword index is tokenized by n-grams rather than by
    words: a language that writes without spaces matches nothing under the
    default tokenizer, and this deployment's documents are in one."""
    base = await manager.create_base(name="zh")
    body = "# \u6807\u9898\n\n\u5ef6\u8fdf\u5728\u7b2c\u4e8c\u5b63\u5ea6\u4e0a\u5347\uff0c\u541e\u5410\u91cf\u4e0b\u964d\u3002\n"
    doc = manager.add_document(base.id, filename="zh.md", content=body.encode())
    assert (await manager.index_document(doc.id)).status == "ready"
    manager.stub.model = "gone"
    endpoints.refuse("stub-embed", EmbeddingError("no endpoint"))

    outcome = await manager.search([base.id], "\u5ef6\u8fdf")

    assert outcome.hits, "a chinese query matched chinese text"
    assert base.id in outcome.by_keyword


async def test_a_disabled_chunk_is_not_matched_by_keyword_either(manager, endpoints) -> None:
    """Off means out of retrieval, whichever way the retrieval is done."""
    base, doc = await _ready_base(manager)
    held, _ = await manager.document_chunks(doc.id)
    target = next(p for p in held if "first topic" in p.chunk.text)
    await manager.set_chunks_enabled(doc.id, [target.chunk_id], False)
    manager.stub.model = "gone"
    endpoints.refuse("stub-embed", EmbeddingError("no endpoint"))

    outcome = await manager.search([base.id], "alpha")

    assert all("first topic" not in hit.chunk.text for hit in outcome.hits)


async def test_mixed_scoring_is_merged_by_rank_not_by_value(manager, endpoints) -> None:
    """A cosine similarity and a BM25 score are not comparable numbers. Sorting
    the concatenation would let whichever scale runs hotter decide the order,
    so the merge asks each list only for the part every scorer agrees on."""
    first, _ = await _ready_base(manager)
    # The pin moves, so the second base is built on a model that is still
    # reachable while the first one's is not: one list of cosine scores, one of
    # BM25 scores, in the same answer.
    manager.stub.model = "second-model"
    second = await manager.create_base(name="second")
    doc = manager.add_document(second.id, filename="s.md", content=b"# alpha\n\nalpha alpha alpha\n")
    await manager.index_document(doc.id)
    endpoints.refuse("stub-embed", EmbeddingError("no endpoint"))

    outcome = await manager.search([first.id, second.id], "alpha", top_k=5)

    assert outcome.hits, "both kinds of answer are in the result"
    assert first.id in outcome.by_keyword and second.id not in outcome.by_keyword


# -- reading a document's chunks back, and acting on them ----------


async def test_a_document_chunks_come_back_in_reading_order(manager) -> None:
    """Reading order is the chunker's numbering, and a scan of the store has no
    order of its own to promise -- so the store sorts rather than the caller
    hoping."""
    _, doc = await _ready_base(manager)

    held, total = await manager.document_chunks(doc.id)

    assert [piece.chunk.chunk_index for piece in held] == list(range(total))
    assert total == doc.chunk_count
    assert "first topic" in held[0].chunk.text
    assert all(piece.chunk_id for piece in held), "every piece is addressable"
    assert all(piece.enabled and not piece.manual for piece in held)


async def test_chunks_of_a_document_that_indexed_nothing_are_empty(manager) -> None:
    """A base with no model, a document that failed, one still queued: all the
    same answer, and none of them an error."""
    base = await manager.create_base(name="files only", embedding=False)
    doc = manager.add_document(base.id, filename="a.md", content=b"# alpha\n")
    await manager.index_document(doc.id)

    assert await manager.document_chunks(doc.id) == ([], 0)
    assert await manager.document_chunks("no-such-document") == ([], 0)


async def test_chunks_of_one_document_do_not_include_another(manager) -> None:
    base, _ = await _ready_base(manager)
    second = manager.add_document(base.id, filename="other.md", content=b"# gamma\n\ngamma\n")
    await manager.index_document(second.id)

    held, _ = await manager.document_chunks(second.id)

    assert held and all("gamma" in piece.chunk.text for piece in held)


async def test_a_page_is_a_window_on_the_document(manager) -> None:
    """Paged after the ordering, so page two follows page one through the
    document rather than through whatever the scan returned."""
    _, doc = await _ready_base(manager)

    first, total = await manager.document_chunks(doc.id, offset=0, limit=1)
    second, again = await manager.document_chunks(doc.id, offset=1, limit=1)

    assert total == again == doc.chunk_count
    assert [p.chunk.chunk_index for p in first] == [0]
    assert [p.chunk.chunk_index for p in second] == [1]


async def test_an_id_is_the_same_piece_after_a_rebuild(manager) -> None:
    """Content-derived on purpose: a reader who turned a paragraph off last
    week is still looking at the same paragraph after a reindex."""
    _, doc = await _ready_base(manager)
    before, _ = await manager.document_chunks(doc.id)

    await manager.index_document(doc.id)
    after, _ = await manager.document_chunks(doc.id)

    assert [p.chunk_id for p in before] == [p.chunk_id for p in after]


async def test_a_repeated_passage_still_gets_two_ids(manager) -> None:
    """Hashing content alone would give both copies one name, and acting on one
    would act on the other."""
    base = await manager.create_base(name="repeats")
    twice = b"# alpha\n\nsame words here\n\n# beta\n\nsame words here\n"
    doc = manager.add_document(base.id, filename="twice.md", content=twice)
    await manager.index_document(doc.id)

    held, _ = await manager.document_chunks(doc.id)
    texts = [p.chunk.text for p in held]

    assert len(texts) != len(set(texts)) or len(held) == len({p.chunk_id for p in held})
    assert len({p.chunk_id for p in held}) == len(held), "no two pieces share a name"


async def test_a_disabled_chunk_is_not_retrieved(manager) -> None:
    """Off is not a ranking penalty. The filter is in the store's own search,
    where every caller passes through it."""
    base, doc = await _ready_base(manager)
    held, _ = await manager.document_chunks(doc.id)
    target = next(p for p in held if "first topic" in p.chunk.text)

    changed = await manager.set_chunks_enabled(doc.id, [target.chunk_id], False)

    assert changed == 1
    hits = (await manager.search([base.id], "alpha")).hits
    assert all("first topic" not in hit.chunk.text for hit in hits)
    back, _ = await manager.document_chunks(doc.id)
    assert next(p for p in back if p.chunk_id == target.chunk_id).enabled is False


async def test_turning_a_chunk_back_on_restores_it(manager) -> None:
    base, doc = await _ready_base(manager)
    held, _ = await manager.document_chunks(doc.id)
    target = held[0]
    await manager.set_chunks_enabled(doc.id, [target.chunk_id], False)

    await manager.set_chunks_enabled(doc.id, [target.chunk_id], True)

    hits = (await manager.search([base.id], "alpha")).hits
    assert any(hit.chunk.text == target.chunk.text for hit in hits)


async def test_listing_can_ask_for_one_state(manager) -> None:
    _, doc = await _ready_base(manager)
    held, _ = await manager.document_chunks(doc.id)
    await manager.set_chunks_enabled(doc.id, [held[0].chunk_id], False)

    off, off_total = await manager.document_chunks(doc.id, enabled=False)
    on, on_total = await manager.document_chunks(doc.id, enabled=True)

    assert off_total == 1 and [p.chunk_id for p in off] == [held[0].chunk_id]
    assert on_total == len(held) - 1


async def test_deleting_a_chunk_leaves_the_count_agreeing_with_the_index(manager) -> None:
    """The row on the page reads that number to tell an indexed document from
    an empty one."""
    _, doc = await _ready_base(manager)
    held, total = await manager.document_chunks(doc.id)

    left = await manager.delete_chunks(doc.id, [held[0].chunk_id])

    assert left == total - 1
    assert manager.get_document(doc.id).chunk_count == total - 1
    remaining, _ = await manager.document_chunks(doc.id)
    assert held[0].chunk_id not in {p.chunk_id for p in remaining}


async def test_a_written_chunk_is_appended_and_findable(manager) -> None:
    """Appended rather than inserted: it was not cut from anywhere in the
    document, and putting it between two pieces that were would claim a place
    in the text it does not have."""
    base, doc = await _ready_base(manager)
    _, before = await manager.document_chunks(doc.id)

    written = await manager.add_chunk(doc.id, "  epsilon epsilon epsilon  ")

    assert written.manual is True and written.enabled is True
    assert written.chunk.text == "epsilon epsilon epsilon"
    held, total = await manager.document_chunks(doc.id)
    assert total == before + 1
    assert held[-1].chunk_id == written.chunk_id, "at the end of the reading order"
    assert manager.get_document(doc.id).chunk_count == total
    hits = (await manager.search([base.id], "epsilon")).hits
    assert any("epsilon" in hit.chunk.text for hit in hits)


async def test_a_written_chunk_does_not_survive_a_reindex(manager) -> None:
    """A reindex is the document being cut again, and a piece nobody cut has
    nothing to be attached to."""
    _, doc = await _ready_base(manager)
    written = await manager.add_chunk(doc.id, "written by hand")

    await manager.index_document(doc.id)

    held, _ = await manager.document_chunks(doc.id)
    assert written.chunk_id not in {p.chunk_id for p in held}


async def test_an_empty_written_chunk_is_refused(manager) -> None:
    _, doc = await _ready_base(manager)

    with pytest.raises(KnowledgeError):
        await manager.add_chunk(doc.id, "   \n  ")


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
