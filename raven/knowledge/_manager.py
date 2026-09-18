"""The knowledge base as one object: records, blobs, index and search.

Holds the four pieces together and owns the rules that only make sense across
them -- which collection a base writes to, what happens to the index when a
base is deleted, and when a base has drifted from the configured embedding
model far enough that searching it would be wrong.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from loguru import logger

from raven.knowledge._chunker import ChunkerBase
from raven.knowledge._embedding import (
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    embedding_client,
    load_embedding_config,
)
from raven.knowledge._parser import ParserBase, TextParser
from raven.knowledge._records import (
    DEFAULT_TOP_K,
    DocumentOrigin,
    KnowledgeBaseRecord,
    KnowledgeDocumentRecord,
    RecordStore,
)
from raven.knowledge._structure import HeadingAwareChunker, StructuredTextParser
from raven.knowledge._types import Chunk, VectorRecord, VectorSearchResult
from raven.knowledge._vector_store import VectorStoreBase


class KnowledgeError(RuntimeError):
    """A knowledge base could not do what was asked of it."""


class DuplicateBaseNameError(KnowledgeError):
    """A base already goes by that name.

    Names are how a person tells their bases apart -- the rail shows nothing
    else -- so two bases called the same thing leaves them picking between two
    identical rows and finding out which was which by opening both.
    """


class StaleBaseError(KnowledgeError):
    """The base was indexed with an embedding model that is no longer configured.

    Not recoverable by retrying: the vectors in the collection answer to the
    old model, and a query embedded with the new one lands somewhere unrelated
    in the same space. The base has to be rebuilt, and saying so is the only
    honest answer -- searching anyway returns confident nonsense.
    """


@dataclass(frozen=True)
class SearchOutcome:
    """The hits, and what each half of the search cost.

    Returned instead of a bare list because a recall surface has to report the
    cost, and only this call can tell the embedding round trip apart from the
    index query.
    """

    hits: list[VectorSearchResult] = field(default_factory=list)
    embed_ms: float = 0.0
    search_ms: float = 0.0


def _default_parsers() -> list[ParserBase]:
    """Structured first, plain text for everything it does not claim.

    Order matters: the structured parser takes the two formats it can find
    headings in, and TextParser has to stay behind it for CSV, JSON, YAML,
    RST and plain text, which would otherwise have no parser at all.
    """
    return [StructuredTextParser(), TextParser()]


def supported_extensions() -> list[str]:
    """Every filename extension the default parsers offer uploads for.

    What a file picker's ``accept`` and a folder walk's filter are built from.
    A module function and not only a method, because the caller that needs it
    is answering "what can be uploaded", which must not be the call that
    builds a manager and its directories.
    """
    seen: set[str] = set()
    for parser in _default_parsers():
        seen.update(parser.supported_extensions())
    return sorted(seen)


class KnowledgeManager:
    """Every knowledge base in one raven home."""

    def __init__(
        self,
        root: "str | Path",
        *,
        store: VectorStoreBase | None = None,
        records: RecordStore | None = None,
        parsers: list[ParserBase] | None = None,
        chunker: ChunkerBase | None = None,
        embedding: EmbeddingConfig | None = None,
    ) -> None:
        self._root = Path(root)
        self._blobs = self._root / "blobs"
        self._records = records or RecordStore(self._root / "records.json")
        self._parsers = parsers if parsers is not None else _default_parsers()
        self._chunker = chunker or HeadingAwareChunker()
        self._embedding = embedding
        # model -> measured vector width. Probing costs one embedding call, so
        # it is done once per model rather than per base or per search.
        self._widths: dict[str, int] = {}
        if store is None:
            from raven.knowledge._lancedb import LanceDBVectorStore

            store = LanceDBVectorStore(self._root / "vectors")
        self._store = store

    # ── embedding ─────────────────────────────────────────────────

    def _client(self) -> EmbeddingClient:
        config = self._embedding or load_embedding_config()
        if config is None:
            raise KnowledgeError(
                "no embedding endpoint is configured; pick an embedding model and the provider that serves it "
                "before using a knowledge base"
            )
        return embedding_client(config)

    def embedding_available(self) -> bool:
        """Whether a base could be created right now."""
        return (self._embedding or load_embedding_config()) is not None

    async def _width_of(self, client: EmbeddingClient) -> int:
        """The configured model's vector width, measured.

        A pinned ``dimensions`` is checked against the model rather than
        trusted. The deployment this was built against pins 1024 for a model
        that returns 4096, and trusting that sizes the collection to a width no
        vector fits: the failure then surfaces as an Arrow cast error at the
        first insert, with nothing in it pointing back at the config line that
        caused it. Measured once per model -- one embedding call, against a
        misconfiguration that is otherwise found by a user.
        """
        width = self._widths.get(client.model)
        if width is None:
            width = await client.probe_dimensions()
            self._widths[client.model] = width
        declared = client.declared_dimensions
        if declared and declared != width:
            raise KnowledgeError(
                f"the configured embedding width ({declared}) is not what {client.model!r} "
                f"returns ({width}); correct or remove `dimensions` in the EverOS embedding config"
            )
        return width

    async def _assert_current(self, base: KnowledgeBaseRecord, client: EmbeddingClient) -> None:
        """Refuse a base whose vectors answer to a different model.

        Keyed on the model and the width, deliberately not on the base URL: an
        operator moving the same model behind a new gateway or rotating a key
        changes neither the vectors nor what a query embeds to, and forcing a
        rebuild for that would throw away a working index for nothing.
        """
        width = await self._width_of(client)
        if base.embedding_model == client.model and base.dimensions == width:
            return
        raise StaleBaseError(
            f"knowledge base {base.name!r} was indexed with {base.embedding_model!r} "
            f"({base.dimensions}d) but {client.model!r} ({width}d) is configured now; "
            "rebuild the base to search it"
        )

    # ── bases ─────────────────────────────────────────────────────

    async def create_base(self, *, name: str, description: str = "", embedding: bool = True) -> KnowledgeBaseRecord:
        """A new base, with or without vectors.

        ``embedding=False`` is a base that keeps its documents and is never
        searched by vector: no model, no width, and no collection to size. The
        case is real -- a place to put files that the agent reads whole, or
        that a person opens from the page -- and it is the one shape that must
        not quietly acquire an index, because a base built with a model cannot
        be un-built without a rebuild.

        The absence is recorded as an empty model and a zero width, which is
        what every reader here already tests for.
        """
        self._refuse_taken_name(name)
        if not embedding:
            return self._records.create_base(name=name, embedding_model="", dimensions=0, description=description)
        client = self._client()
        width = await self._width_of(client)
        record = self._records.create_base(
            name=name,
            embedding_model=client.model,
            dimensions=width,
            description=description,
        )
        # The collection is named for the id, not the display name: a rename
        # is an edit, and a collection that followed it would strand its rows.
        await self._store.create_collection(record.id, width)
        return record

    def _refuse_taken_name(self, name: str, *, allow: str | None = None) -> None:
        """Stop a second base taking a name another one already has.

        Compared casefolded and stripped, because two bases called "t3" and
        "T3 " are the same problem as two called "t3": the rail shows the name
        and nothing else, so the reader cannot tell them apart either way.
        What is stored is still what was typed.

        ``allow`` is the base being renamed, which may of course keep its own
        name -- without it, saving a rename that changed only the description
        would fail against itself.
        """
        wanted = name.strip().casefold()
        for base in self._records.list_bases():
            if base.id != allow and base.name.strip().casefold() == wanted:
                raise DuplicateBaseNameError(f"a knowledge base called {base.name!r} already exists")

    @staticmethod
    def embeds(base: KnowledgeBaseRecord) -> bool:
        """Whether this base has vectors at all.

        One question, asked in one place: indexing, searching and the staleness
        check each have to skip a base with no model, and three spellings of
        "is the model empty" is how one of them ends up not skipping.
        """
        return bool(base.embedding_model)

    def list_bases(self) -> list[KnowledgeBaseRecord]:
        return self._records.list_bases()

    def get_base(self, base_id: str) -> KnowledgeBaseRecord | None:
        return self._records.get_base(base_id)

    def rename_base(
        self, base_id: str, *, name: str | None = None, description: str | None = None
    ) -> KnowledgeBaseRecord | None:
        # The same rule as creation, or renaming is the way around it.
        if name is not None:
            self._refuse_taken_name(name, allow=base_id)
        return self._records.rename_base(base_id, name=name, description=description)

    def configure_base(self, base_id: str, **settings: object) -> KnowledgeBaseRecord | None:
        """Write the settings a reader can change after the base exists.

        Not the embedding model: the collection is sized to its width, so
        changing it is a rebuild of every vector in the base rather than a
        setting, and the stale-base check exists because that is detectable.
        """
        return self._records.configure_base(base_id, **settings)

    async def delete_base(self, base_id: str) -> bool:
        """Drop the base, its documents, their blobs and the collection.

        The collection goes first: a records-only delete would leave vectors
        under an id nothing lists any more, and every later base would share
        the store with an index no one can name or reclaim.
        """
        if self._records.get_base(base_id) is None:
            return False
        for document in self._records.list_documents(base_id):
            self._blob_path(document.id).unlink(missing_ok=True)
        await self._store.delete_collection(base_id)
        return self._records.delete_base(base_id)

    # ── documents ─────────────────────────────────────────────────

    def _blob_path(self, document_id: str) -> Path:
        return self._blobs / document_id

    def _parser_for(self, media_type: str) -> ParserBase | None:
        for parser in self._parsers:
            if media_type in parser.supported_media_types:
                return parser
        return None

    def supported_media_types(self) -> list[str]:
        """Every media type some registered parser claims."""
        seen: list[str] = []
        for parser in self._parsers:
            for media_type in parser.supported_media_types:
                if media_type not in seen:
                    seen.append(media_type)
        return sorted(seen)

    def supported_extensions(self) -> list[str]:
        """Every filename extension this manager's parsers offer uploads for."""
        seen: set[str] = set()
        for parser in self._parsers:
            seen.update(parser.supported_extensions())
        return sorted(seen)

    def add_document(
        self,
        base_id: str,
        *,
        filename: str,
        content: bytes,
        origin: DocumentOrigin = "file",
        origin_ref: str = "",
    ) -> KnowledgeDocumentRecord:
        """Take an upload and queue it. Indexing happens separately.

        The bytes are kept: a reindex after a model change, and the page's own
        "show me this document", both need the original, and asking the user
        to upload it again is not a recovery path.

        A note and a fetched page arrive here too, as the markdown they were
        captured as. Nothing downstream needs to know which: one blob store,
        one parser table, one indexer -- ``origin`` is what a row is labelled
        with, not a second way of keeping a document.
        """
        if self._records.get_base(base_id) is None:
            raise KnowledgeError(f"no knowledge base {base_id!r}")
        media_type = mimetypes.guess_type(filename)[0] or "text/plain"
        record = self._records.add_document(
            base_id=base_id,
            source=filename,
            media_type=media_type,
            size=len(content),
            origin=origin,
            origin_ref=origin_ref,
        )
        self._write_blob(record.id, content)
        return record

    def _write_blob(self, document_id: str, content: bytes) -> None:
        self._blobs.mkdir(parents=True, exist_ok=True)
        path = self._blob_path(document_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)

    async def replace_document(
        self,
        document_id: str,
        *,
        filename: str,
        content: bytes,
    ) -> KnowledgeDocumentRecord | None:
        """Rewrite one document in place and queue it for indexing again.

        The chunks of the old text go first. They are what a search answers
        with, so leaving them until the reindex writes over them would answer
        from a note the reader has already rewritten -- and if the reindex
        fails, leave them for good.
        """
        record = self._records.get_document(document_id)
        if record is None:
            return None
        await self._store.delete(record.base_id, document_id)
        media_type = mimetypes.guess_type(filename)[0] or "text/plain"
        self._write_blob(document_id, content)
        return self._records.update_document(
            document_id,
            source=filename,
            media_type=media_type,
            size=len(content),
        )

    def list_documents(self, base_id: str) -> list[KnowledgeDocumentRecord]:
        return self._records.list_documents(base_id)

    def get_document(self, document_id: str) -> KnowledgeDocumentRecord | None:
        return self._records.get_document(document_id)

    def read_document(self, document_id: str) -> bytes | None:
        path = self._blob_path(document_id)
        return path.read_bytes() if path.is_file() else None

    def document_path(self, document_id: str) -> Path | None:
        """Where the stored copy is, for a caller that must not read it all.

        Beside ``read_document`` rather than instead of it: the indexer wants
        the bytes, and a viewer wants a handle it can stream and convert from.
        Reading a 25 MB upload into memory to hand it back out again is the
        thing this exists to avoid.

        The path is inside raven's state directory, which the viewer's own path
        policy refuses on purpose. That is not a contradiction: a caller reaches
        this by document id, so nothing the page sent names a location, and the
        handle is served rather than the request's own path.
        """
        path = self._blob_path(document_id)
        return path if path.is_file() else None

    async def delete_document(self, document_id: str) -> bool:
        record = self._records.get_document(document_id)
        if record is None:
            return False
        await self._store.delete(record.base_id, document_id)
        self._blob_path(document_id).unlink(missing_ok=True)
        return self._records.delete_document(document_id)

    # ── indexing ──────────────────────────────────────────────────

    async def index_document(self, document_id: str) -> KnowledgeDocumentRecord | None:
        """Parse, chunk, embed and store one queued document.

        Failure is recorded on the document rather than raised: one unreadable
        upload must not stop the queue behind it, and the reason belongs next
        to the row on the page, where the person who uploaded it is looking.
        """
        record = self._records.get_document(document_id)
        if record is None:
            return None
        base = self._records.get_base(record.base_id)
        if base is None:
            return self._records.set_status(document_id, "failed", error="its knowledge base is gone")

        if not self.embeds(base):
            # Stored, not indexed, and ready is the truth of it: the file is in
            # the base and can be opened. Calling it failed would send a reader
            # looking for a fault, and leaving it pending would promise an
            # indexer that is never coming.
            return self._records.set_status(document_id, "ready", chunk_count=0)

        self._records.set_status(document_id, "indexing")
        try:
            client = self._client()
            await self._assert_current(base, client)
            chunks = await self._chunks_for(record)
            if not chunks:
                return self._records.set_status(document_id, "ready", chunk_count=0)
            vectors = await client.embed([chunk.text for chunk in chunks])
            # Replaces rather than appends: a reindex of the same document
            # would otherwise leave the previous run's chunks in the
            # collection, and every hit would come back twice.
            await self._store.delete(base.id, document_id)
            await self._store.insert(
                base.id,
                [
                    VectorRecord(vector=v, document_id=document_id, chunk=c)
                    for v, c in zip(vectors, chunks, strict=True)
                ],
            )
        except (KnowledgeError, EmbeddingError, ValueError, OSError) as exc:
            logger.warning("knowledge: indexing {} failed: {}", record.source, exc)
            return self._records.set_status(document_id, "failed", error=str(exc))
        return self._records.set_status(document_id, "ready", chunk_count=len(chunks))

    async def _chunks_for(self, record: KnowledgeDocumentRecord) -> list[Chunk]:
        content = self.read_document(record.id)
        if content is None:
            raise KnowledgeError("the uploaded file is missing from the store")
        parser = self._parser_for(record.media_type)
        if parser is None:
            raise KnowledgeError(f"no parser for {record.media_type}")
        sections = await parser.parse(content, record.source)
        return await self._chunker.chunk(sections)

    async def index_pending(self) -> int:
        """Index everything queued, oldest first. Returns how many were tried."""
        pending = self._records.pending_documents()
        for document in pending:
            await self.index_document(document.id)
        return len(pending)

    # ── search ────────────────────────────────────────────────────

    async def search(self, base_ids: list[str], query: str, top_k: int | None = None) -> SearchOutcome:
        """Search across bases, merged and ranked together.

        One embedding call for the query, not one per base: they are searched
        with the same vector, and a base whose model no longer matches is
        refused rather than searched with it.

        The two costs are timed apart because they answer different questions.
        Embedding is a round trip to whatever endpoint is configured and runs
        to hundreds of milliseconds; the store query is the index doing its
        job. A surface that reports one number as "how long the search took"
        should be reporting the second, or it is describing the provider.
        """
        bases = [b for b in (self._records.get_base(i) for i in base_ids) if b is not None]
        # Skipped rather than refused: asking a mixed set of bases is ordinary,
        # and one with no vectors is not an error in the others.
        bases = [b for b in bases if self.embeds(b)]
        if not bases or not query.strip():
            return SearchOutcome(hits=[], embed_ms=0.0, search_ms=0.0)
        # The bases' own settings when the caller names no number, and the
        # largest of them when several are asked at once: a base configured to
        # answer with ten chunks should still be able to, and the merge below
        # cuts the total back to that same figure.
        if top_k is None:
            top_k = max(int(getattr(b, "top_k", DEFAULT_TOP_K) or DEFAULT_TOP_K) for b in bases)
        client = self._client()
        for base in bases:
            await self._assert_current(base, client)

        started = perf_counter()
        vector = (await client.embed([query]))[0]
        embedded = perf_counter()
        hits: list[VectorSearchResult] = []
        for base in bases:
            hits.extend(await self._store.search(base.id, vector, top_k=top_k))
        searched = perf_counter()
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return SearchOutcome(
            hits=hits[:top_k],
            embed_ms=(embedded - started) * 1000.0,
            search_ms=(searched - embedded) * 1000.0,
        )
