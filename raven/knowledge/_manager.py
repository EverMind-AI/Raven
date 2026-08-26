"""The knowledge base as one object: records, blobs, index and search.

Holds the four pieces together and owns the rules that only make sense across
them -- which collection a base writes to, what happens to the index when a
base is deleted, and when a base has drifted from the configured embedding
model far enough that searching it would be wrong.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from loguru import logger

from raven.knowledge._chunker import ChunkerBase
from raven.knowledge._embedding import EmbeddingClient, EmbeddingConfig, EmbeddingError, load_embedding_config
from raven.knowledge._parser import ParserBase, TextParser
from raven.knowledge._records import KnowledgeBaseRecord, KnowledgeDocumentRecord, RecordStore
from raven.knowledge._structure import HeadingAwareChunker, StructuredTextParser
from raven.knowledge._types import Chunk, VectorRecord, VectorSearchResult
from raven.knowledge._vector_store import VectorStoreBase


class KnowledgeError(RuntimeError):
    """A knowledge base could not do what was asked of it."""


class StaleBaseError(KnowledgeError):
    """The base was indexed with an embedding model that is no longer configured.

    Not recoverable by retrying: the vectors in the collection answer to the
    old model, and a query embedded with the new one lands somewhere unrelated
    in the same space. The base has to be rebuilt, and saying so is the only
    honest answer -- searching anyway returns confident nonsense.
    """


def _default_parsers() -> list[ParserBase]:
    """Structured first, plain text for everything it does not claim.

    Order matters: the structured parser takes the two formats it can find
    headings in, and TextParser has to stay behind it for CSV, JSON, YAML,
    RST and plain text, which would otherwise have no parser at all.
    """
    return [StructuredTextParser(), TextParser()]


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
                "no embedding endpoint is configured; set [embedding] in the EverOS config before using a knowledge base"
            )
        return EmbeddingClient(config)

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

    async def create_base(self, *, name: str, description: str = "") -> KnowledgeBaseRecord:
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

    def list_bases(self) -> list[KnowledgeBaseRecord]:
        return self._records.list_bases()

    def get_base(self, base_id: str) -> KnowledgeBaseRecord | None:
        return self._records.get_base(base_id)

    def rename_base(
        self, base_id: str, *, name: str | None = None, description: str | None = None
    ) -> KnowledgeBaseRecord | None:
        return self._records.rename_base(base_id, name=name, description=description)

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

    def add_document(self, base_id: str, *, filename: str, content: bytes) -> KnowledgeDocumentRecord:
        """Take an upload and queue it. Indexing happens separately.

        The bytes are kept: a reindex after a model change, and the page's own
        "show me this document", both need the original, and asking the user
        to upload it again is not a recovery path.
        """
        if self._records.get_base(base_id) is None:
            raise KnowledgeError(f"no knowledge base {base_id!r}")
        media_type = mimetypes.guess_type(filename)[0] or "text/plain"
        record = self._records.add_document(
            base_id=base_id,
            source=filename,
            media_type=media_type,
            size=len(content),
        )
        self._blobs.mkdir(parents=True, exist_ok=True)
        path = self._blob_path(record.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)
        return record

    def list_documents(self, base_id: str) -> list[KnowledgeDocumentRecord]:
        return self._records.list_documents(base_id)

    def get_document(self, document_id: str) -> KnowledgeDocumentRecord | None:
        return self._records.get_document(document_id)

    def read_document(self, document_id: str) -> bytes | None:
        path = self._blob_path(document_id)
        return path.read_bytes() if path.is_file() else None

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

    async def search(self, base_ids: list[str], query: str, top_k: int = 5) -> list[VectorSearchResult]:
        """Search across bases, merged and ranked together.

        One embedding call for the query, not one per base: they are searched
        with the same vector, and a base whose model no longer matches is
        refused rather than searched with it.
        """
        bases = [b for b in (self._records.get_base(i) for i in base_ids) if b is not None]
        if not bases or not query.strip():
            return []
        client = self._client()
        for base in bases:
            await self._assert_current(base, client)

        vector = (await client.embed([query]))[0]
        hits: list[VectorSearchResult] = []
        for base in bases:
            hits.extend(await self._store.search(base.id, vector, top_k=top_k))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]
