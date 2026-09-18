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
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from loguru import logger

from raven.knowledge._chunker import ChunkerBase
from raven.knowledge._embedding import (
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    asking_for,
    embedding_client,
    embedding_config_for,
    load_embedding_config,
)
from raven.knowledge._naive_chunker import NaiveChunker
from raven.knowledge._notes import collecting, joined
from raven.knowledge._records import (
    DEFAULT_TOP_K,
    DocumentOrigin,
    KnowledgeBaseRecord,
    KnowledgeDocumentRecord,
    RecordStore,
)
from raven.knowledge._structure import StructuredTextParser
from raven.knowledge._types import Chunk, StoredChunk, TextBlock, VectorRecord, VectorSearchResult
from raven.knowledge._vector_store import VectorStoreBase
from raven.knowledge._vision import VisionError
from raven.knowledge.parser import LayoutType, ParserBase, section_metadata
from raven.knowledge.parser.doc_parser import LegacyDocParser
from raven.knowledge.parser.docx_parser import DocxParser
from raven.knowledge.parser.excel_parser import ExcelParser
from raven.knowledge.parser.image_parser import ImageParser
from raven.knowledge.parser.text_parser import TextParser


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
    #: Bases that answered by keyword rather than by meaning, base id to the
    #: reason their vectors could not be reached. Not an error and not a
    #: skip -- those bases are in the results -- but a reader comparing two
    #: sets of hits deserves to know that some of them came from words. The
    #: reason is the same sentence the base would have failed with, so it
    #: still says what to fix.
    by_keyword: dict[str, str] = field(default_factory=dict)


def chunk_id_for(document_id: str, text: str, occurrence: int = 0) -> str:
    """The id a piece of text has in a document, derived from the text itself.

    Content-derived rather than random, so a piece keeps its name across a
    rebuild: reindexing a document that has not changed writes the same ids
    back, and a reader who disabled a paragraph last week is still looking at
    the same paragraph.

    ``occurrence`` is what keeps that from merging two pieces. A document
    repeats itself -- a table header, a page footer, a boilerplate clause --
    and hashing content alone would give both copies one id, so acting on one
    would act on the other and a store keyed by id would hold whichever was
    written last. The counter is the copy's index among identical texts in the
    same document, so the first copy of a text is stable no matter how many
    more appear after it.
    """
    seed = f"{document_id}\x00{occurrence}\x00{text}".encode()
    return sha256(seed).hexdigest()[:32]


def chunk_ids_for(document_id: str, texts: list[str]) -> list[str]:
    """Ids for one document's pieces, counting repeats as they are met."""
    seen: dict[str, int] = {}
    ids: list[str] = []
    for text in texts:
        occurrence = seen.get(text, 0)
        seen[text] = occurrence + 1
        ids.append(chunk_id_for(document_id, text, occurrence))
    return ids


#: How far down a list a hit still counts for in the merge below. Sixty is the
#: constant the original reciprocal-rank-fusion paper used and the one
#: `skill_forge.fusion` already uses here; a hit at rank 1 scores 1/61, at rank
#: 10 scores 1/70, so the top of every list is worth more than the tail of any
#: other without one list's scale deciding the outcome.
_RRF_K = 60


def _merged(ranked: list[list[VectorSearchResult]], top_k: int) -> list[VectorSearchResult]:
    """Fold several ranked lists into one, by rank rather than by score.

    Sorting the concatenation by score is what a single-model search could get
    away with. It stops being sound the moment two lists are scored on
    different scales -- a cosine similarity against a BM25 score, or two models
    whose similarities are not calibrated to each other -- because then the
    ordering is decided by which scale runs hotter rather than by which hit is
    better. Reciprocal rank fusion asks each list only for its order, which is
    the part every scorer agrees on the meaning of.

    One list passes through untouched, which is the ordinary case and keeps its
    own scores intact for anything reading them.
    """
    if len(ranked) == 1:
        return ranked[0][:top_k]

    fused: dict[tuple[str, str], tuple[float, VectorSearchResult]] = {}
    for hits in ranked:
        for position, hit in enumerate(hits):
            key = (hit.document_id, hit.chunk.text)
            score, held = fused.get(key, (0.0, hit))
            fused[key] = (score + 1.0 / (_RRF_K + position + 1), held)
    best = sorted(fused.values(), key=lambda pair: pair[0], reverse=True)
    return [hit for _, hit in best[:top_k]]


def _default_parsers() -> list[ParserBase]:
    """Structured first, plain text for everything it does not claim.

    Order matters: the structured parser takes the two formats it can find
    headings in, and TextParser has to stay behind it for CSV, JSON, YAML,
    RST and plain text, which would otherwise have no parser at all. The two
    Word parsers claim media types no other parser here answers to, so their
    position is free -- but the spreadsheet parser has to come before the plain
    one, because both claim `text/csv` and a row of separated values read as
    prose indexes the separators.

    The image parser is registered whether or not a vision model is configured:
    what it claims does not change with the config, and a file picker that
    stopped offering pictures the moment a pin was cleared would look like the
    build had lost the ability to read them.
    """
    return [StructuredTextParser(), DocxParser(), LegacyDocParser(), ExcelParser(), ImageParser(), TextParser()]


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
        # None means "whatever each base is configured for"; a chunker passed
        # in overrides every base, which is what a test or an embedder wants.
        self._chunker = chunker
        self._embedding = embedding
        # model -> measured vector width. Probing costs one embedding call, so
        # it is done once per model rather than per base or per search.
        self._widths: dict[str, int] = {}
        # (provider, model) -> client, for bases not on today's endpoint.
        # Built once rather than per search: a client is a config and a
        # connection pool, and a mixed-model search rebuilds the same few.
        self._clients: dict[tuple[str, str], EmbeddingClient] = {}
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
        return embedding_client(config)

    def _client_named(self, provider: str, model: str) -> EmbeddingClient:
        """The client for a model a reader picked, on the provider serving it.

        The pair is what a picker offers and what a base records: a model id
        names no credential, so the provider is the half that says where the
        call goes out. An empty provider means the configured endpoint asked
        for that model -- what a base written before providers were recorded
        falls back to, and what a caller holding an injected endpoint (a test,
        an embedder) is pointing at.
        """
        configured = self._embedding or load_embedding_config()
        if configured is not None and configured.model == model and (not provider or configured.provider == provider):
            return embedding_client(configured)
        if not provider:
            return embedding_client(asking_for(self._endpoint(), model))
        config = embedding_config_for(provider, model)
        if config is None:
            raise KnowledgeError(
                f"provider {provider!r} has no usable credential, so {model!r} cannot be embedded through it"
            )
        return embedding_client(config)

    def _client_for(self, base: KnowledgeBaseRecord) -> EmbeddingClient:
        """The client that speaks the model this base was built with.

        A base is searched with its own model or not at all. The collection
        holds vectors from whatever was configured the day it was built, and a
        query embedded with anything else lands somewhere unrelated in the same
        space -- so the model follows the base, not the session.

        Three cases, in the order they are tried:

        - the base wants what is configured now (the common one, and the only
          one before this existed): today's client, unchanged;
        - the base recorded its provider: that provider's endpoint, asked for
          the base's model, because a model id does not name a credential;
        - the base recorded a model but no provider (every base written before
          the field existed): today's endpoint, asked for the base's model,
          which is the last thing left to try. Whether it still serves that
          model is not knowable from here -- if it does not, the embed fails
          and the base answers by keyword instead.
        """
        configured = self._client()
        if base.embedding_model == configured.model:
            return configured

        cached = self._clients.get((base.embedding_provider, base.embedding_model))
        if cached is not None:
            return cached

        config = embedding_config_for(base.embedding_provider, base.embedding_model, base.dimensions)
        if config is None:
            if base.embedding_provider:
                raise StaleBaseError(
                    f"knowledge base {base.name!r} was indexed with {base.embedding_model!r} "
                    f"through {base.embedding_provider!r}, which has no usable credentials now; "
                    "restore that provider or rebuild the base"
                )
            # No provider was ever recorded, so the only endpoint there is to
            # try is the configured one, asked for this base's model. Reading
            # the memory backend's own file for an older endpoint is not an
            # option any more: raven stopped inheriting it, because a knowledge
            # base that needs the memory plugin installed to answer is a
            # knowledge base that stops working when it is not.
            config = asking_for(self._endpoint(), base.embedding_model, base.dimensions or None)
        client = embedding_client(config)
        self._clients[(base.embedding_provider, base.embedding_model)] = client
        return client

    def _unsearchable(self, base: KnowledgeBaseRecord, exc: Exception) -> str:
        """Why a base dropped out, said so a reader can act on it.

        The raw failure is the endpoint's own words -- "model not found", a
        timeout, a 401 -- and none of them mention the base or say what to do
        about it. What went wrong is the same in every case: the model this
        base holds vectors from could not be reached, and until it can the
        base cannot be searched without answering from the wrong space.

        Deliberately not diagnosed further. Whether the endpoint is down or
        simply does not host this model is not reliably distinguishable from
        here, and guessing the first would tell someone to wait when they need
        to act, while guessing the second would tell them to rebuild a base
        that is fine.
        """
        if isinstance(exc, StaleBaseError):
            return str(exc)
        where = f"through {base.embedding_provider!r}" if base.embedding_provider else "on the configured endpoint"
        return (
            f"knowledge base {base.name!r} holds vectors from {base.embedding_model!r}, and asking for that model "
            f"{where} failed ({exc}). Point the base at a provider that serves it, or rebuild it on the model "
            "configured now."
        )

    def embedding_reach(self, base: KnowledgeBaseRecord) -> str:
        """Why this base's model cannot be reached, or ``""`` when it can.

        Asked without calling anything: a list of bases is drawn on every visit
        to the page, and probing each endpoint to draw it would spend a request
        per base per render. So this answers from what is recorded -- which is
        enough to catch the case that actually happens, a base whose model is
        not the configured one and which names no provider of its own. An
        endpoint that is merely down still looks reachable here and fails at
        the request, which is the right place to find that out.

        A reason rather than a flag, because the panel has to say what to do
        and the two cases differ: a provider whose credential has gone is a
        different repair from a base that never recorded one.
        """
        if not self.embeds(base):
            return ""
        configured = self._embedding or load_embedding_config()
        if configured is not None and base.embedding_model == configured.model:
            return ""
        if base.embedding_provider:
            if embedding_config_for(base.embedding_provider, base.embedding_model) is not None:
                return ""
            return "no_credential"
        return "no_provider"

    def _endpoint(self) -> EmbeddingConfig:
        config = self._embedding or load_embedding_config()
        if config is None:
            raise KnowledgeError(
                "no embedding endpoint is configured; set [embedding] in the EverOS config before using a knowledge base"
            )
        return config

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
        """Refuse a base whose vectors no longer answer to the model it holds.

        Keyed on the model and the width, deliberately not on the base URL: an
        operator moving the same model behind a new gateway or rotating a key
        changes neither the vectors nor what a query embeds to, and forcing a
        rebuild for that would throw away a working index for nothing.

        The model half is now satisfied by construction -- ``_client_for``
        hands back a client for the base's own model -- so what is left to
        catch is a width that moved under a name that did not: a provider
        re-pointing a model id at a bigger one, or an endpoint answering a
        model it does not serve with some default. Either way the collection
        was sized to the old number and the new vectors do not belong in it.
        """
        width = await self._width_of(client)
        if base.embedding_model == client.model and base.dimensions == width:
            return
        raise StaleBaseError(
            f"knowledge base {base.name!r} was indexed with {base.embedding_model!r} "
            f"({base.dimensions}d) but {client.model!r} now answers with {width}d; "
            "rebuild the base to search it"
        )

    # ── bases ─────────────────────────────────────────────────────

    async def create_base(
        self,
        *,
        name: str,
        description: str = "",
        embedding: bool = True,
        embedding_model: str = "",
        embedding_provider: str = "",
    ) -> KnowledgeBaseRecord:
        """A new base, with or without vectors.

        ``embedding_model`` names the model to build it on, with
        ``embedding_provider`` saying who serves it. Left out, the base is
        built on the configured pin -- which is what every base was built on
        before the choice existed, and what the page offers as the default.

        ``embedding=False`` is a base that keeps its documents and is never
        searched by vector: no model, no width, and no collection to size. The
        case is real -- a place to put files that the agent reads whole, or
        that a person opens from the page.

        The absence is recorded as an empty model and a zero width, which is
        what every reader here already tests for.
        """
        self._refuse_taken_name(name)
        if not embedding:
            return self._records.create_base(name=name, embedding_model="", dimensions=0, description=description)
        client = self._client_named(embedding_provider, embedding_model) if embedding_model else self._client()
        width = await self._width_of(client)
        record = self._records.create_base(
            name=name,
            embedding_model=client.model,
            dimensions=width,
            description=description,
            # What the endpoint says it is, falling back to what was asked for:
            # a client built from a resolved provider carries it, and one built
            # from an injected config may not.
            embedding_provider=getattr(client, "provider", "") or embedding_provider,
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
        moving it is a rebuild of every vector in the base rather than a
        setting. :meth:`switch_embedding` is that rebuild.
        """
        return self._records.configure_base(base_id, **settings)

    async def switch_embedding(self, base_id: str, *, model: str, provider: str = "") -> KnowledgeBaseRecord | None:
        """Move a base onto another embedding model, rebuilding what it holds.

        Not a setting, and not reversible by undoing it: the vectors in the
        collection were made by the old model, and no query embedded by the new
        one lands anywhere near them. So the collection is dropped and made
        again at the new model's width, and every document goes back to the
        queue to be cut and embedded a second time from the blob it was stored
        with. Nothing a reader typed is lost; what is lost is the indexing, and
        that is what re-running it restores.

        An empty ``model`` turns embedding off: the base keeps its documents
        and holds no vectors. The reverse -- turning it on -- is the same call
        with a model, which is why a base created without one is no longer
        stuck that way.

        The width is measured before anything is dropped. A model that cannot
        be reached therefore leaves the base exactly as it was, rather than
        emptying it and failing.
        """
        base = self._records.get_base(base_id)
        if base is None:
            return None
        if base.embedding_model == model and base.embedding_provider == provider:
            return base

        client = None
        if model:
            # Resolved before anything is compared, because a base records the
            # id its endpoint is called with and a picker offers the id the
            # provider is configured under -- which are the same model spelled
            # two ways wherever a storage prefix is involved.
            client = self._client_named(provider, model)
            model, provider = client.model, getattr(client, "provider", "") or provider
            if base.embedding_model == model:
                # The same model through a different account is not a rebuild:
                # the vectors are the ones this model makes wherever it is
                # served from, and only the address has moved.
                if base.embedding_provider == provider:
                    return base
                return self._records.configure_base(base_id, embedding_provider=provider)

        width = await self._width_of(client) if client is not None else 0
        await self._store.delete_collection(base_id)
        if model:
            await self._store.create_collection(base_id, width)
        return self._records.rebuild_base(
            base_id,
            embedding_model=model,
            dimensions=width,
            embedding_provider=provider if model else "",
        )

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
            # The base's own model, not the configured one: a document added
            # to an older base has to be embedded into the space its
            # collection already holds, or it is unfindable by any query.
            client = self._client_for(base)
            await self._assert_current(base, client)
            chunks, warning = await self._chunks_for(record, base)
            if not chunks:
                return self._records.set_status(document_id, "ready", chunk_count=0, warning=warning)
            texts = [chunk.text for chunk in chunks]
            vectors = await client.embed(texts)
            ids = chunk_ids_for(document_id, texts)
            # Replaces rather than appends: a reindex of the same document
            # would otherwise leave the previous run's chunks in the
            # collection, and every hit would come back twice. Everything the
            # document held goes, including pieces a person wrote by hand and
            # pieces they had turned off -- a reindex is the document being
            # cut again, and there is nothing for those to be attached to.
            await self._store.delete(base.id, document_id)
            await self._store.insert(
                base.id,
                [
                    VectorRecord(vector=v, document_id=document_id, chunk=c, chunk_id=i)
                    for v, c, i in zip(vectors, chunks, ids, strict=True)
                ],
            )
        except (KnowledgeError, EmbeddingError, VisionError, ValueError, OSError) as exc:
            logger.warning("knowledge: indexing {} failed: {}", record.source, exc)
            return self._records.set_status(document_id, "failed", error=str(exc))
        # Ready, and carrying whatever the parse could not do. The two are not
        # in tension: the document is indexed and searchable, and the warning
        # says which part of it is not in the index -- which nothing else would
        # ever tell a reader, because a file with half its figures missing
        # looks exactly like one with none.
        return self._records.set_status(document_id, "ready", chunk_count=len(chunks), warning=warning)

    def _indexed_document(self, document_id: str) -> tuple[KnowledgeDocumentRecord, KnowledgeBaseRecord] | None:
        """The document and its base, when the pair can hold chunks at all.

        One lookup for every chunk operation, because they all need the same
        two records and the same three reasons to answer with nothing: no such
        document, no base behind it, or a base with no vectors to hold pieces.
        """
        record = self._records.get_document(document_id)
        if record is None:
            return None
        base = self._records.get_base(record.base_id)
        if base is None or not self.embeds(base):
            return None
        return record, base

    async def document_chunks(
        self,
        document_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        enabled: bool | None = None,
    ) -> tuple[list[StoredChunk], int]:
        """One page of a document's chunks in reading order, and the total.

        Read back from the store rather than re-cut from the file: what a
        reader wants to see is what the search actually matches against, and
        re-running the parser would show them what a *rebuild* would produce,
        which is a different thing the moment a setting has moved.

        Empty for a document with nothing indexed -- a base with no model, a
        document that failed or is still queued -- which is the same answer as
        a document whose base was deleted underneath it.
        """
        found = self._indexed_document(document_id)
        if found is None:
            return [], 0
        _, base = found
        return await self._store.list_chunks(base.id, document_id, offset=offset, limit=limit, enabled=enabled)

    async def search_document(self, document_id: str, query: str, *, limit: int = 20) -> list[StoredChunk]:
        """The pieces of one document that answer a query, best first.

        The same retrieval the whole base gets, narrowed to one file: by vector
        where the base's model can be reached, by keyword where it cannot. A
        disabled piece is not returned, because this is retrieval and that is
        what disabled means -- the way to see those is to list rather than
        search.

        Answered as stored pieces rather than as hits so the panel gets one
        shape either way, with each piece's id and state on it.
        """
        found = self._indexed_document(document_id)
        if found is None or not query.strip():
            return []
        _, base = found
        try:
            client = self._client_for(base)
            await self._assert_current(base, client)
            vector = (await client.embed([query]))[0]
            hits = await self._store.search(base.id, vector, top_k=limit, document_id=document_id)
        except (KnowledgeError, EmbeddingError, OSError) as exc:
            logger.warning("knowledge: searching {} by keyword: {}", document_id, exc)
            hits = await self._store.keyword_search(base.id, query, top_k=limit, document_id=document_id)

        held, _ = await self._store.list_chunks(base.id, document_id)
        by_id = {piece.chunk_id: piece for piece in held}
        ordered: list[StoredChunk] = []
        for hit in hits:
            piece = by_id.get(hit.chunk_id)
            if piece is not None:
                ordered.append(piece)
            elif hit.chunk_id == "":
                # A row from before ids existed: readable, not addressable.
                ordered.append(StoredChunk(chunk_id="", chunk=hit.chunk))
        return ordered

    async def set_chunks_enabled(self, document_id: str, chunk_ids: list[str], enabled: bool) -> int:
        """Turn pieces of one document on or off. Returns how many changed.

        Off means out of retrieval entirely -- not a ranking penalty -- so the
        filter lives in the store's own search rather than in any one caller.
        """
        found = self._indexed_document(document_id)
        if found is None or not chunk_ids:
            return 0
        _, base = found
        return await self._store.set_chunks_enabled(base.id, chunk_ids, enabled)

    async def delete_chunks(self, document_id: str, chunk_ids: list[str]) -> int:
        """Remove pieces of one document. Returns how many are left after it.

        The document's recorded chunk count is rewritten from what remains, so
        the row on the page keeps agreeing with the index -- it is the number
        a reader uses to tell an indexed document from an empty one.
        """
        found = self._indexed_document(document_id)
        if found is None or not chunk_ids:
            return 0
        _, base = found
        await self._store.delete_chunks(base.id, chunk_ids)
        _, total = await self._store.list_chunks(base.id, document_id)
        self._records.set_status(document_id, "ready", chunk_count=total)
        return total

    async def update_chunk(self, document_id: str, chunk_id: str, text: str) -> StoredChunk:
        """Rewrite one piece's text, and the vector that answers for it.

        Written as a replacement rather than an edit in place, because the
        vector is the point: a piece whose text changed and whose vector did
        not would be found by the old words and read as the new ones, which is
        the one failure a reader of this panel would never catch.

        Its id changes with its text -- ids are derived from content, and an
        edited piece is not the piece that was there. Its place in the reading
        order is kept, so a correction stays where the passage it corrects sat,
        and so is its on/off state. It is marked as written by hand: it no
        longer says what the file says, and the panel should not pretend
        otherwise.
        """
        found = self._indexed_document(document_id)
        if found is None:
            raise KnowledgeError("this document has no index to edit")
        _, base = found
        body = text.strip()
        if not body:
            raise KnowledgeError("a chunk needs some text")

        held, _ = await self._store.list_chunks(base.id, document_id)
        existing = next((piece for piece in held if piece.chunk_id == chunk_id), None)
        if existing is None:
            raise KnowledgeError("that chunk is not in this document")
        if existing.chunk.text == body:
            return existing

        client = self._client_for(base)
        await self._assert_current(base, client)
        vector = (await client.embed([body]))[0]

        taken = {piece.chunk_id for piece in held if piece.chunk_id != chunk_id}
        occurrence = 0
        while chunk_id_for(document_id, body, occurrence) in taken:
            occurrence += 1
        chunk = existing.chunk.model_copy(
            update={
                "content": TextBlock(text=body),
                "metadata": {**existing.chunk.metadata, "manual": True},
            }
        )
        stored = StoredChunk(
            chunk_id=chunk_id_for(document_id, body, occurrence),
            chunk=chunk,
            enabled=existing.enabled,
            manual=True,
        )
        # Inserted before the old row is dropped would leave two pieces holding
        # one place in the reading order if the insert failed halfway; dropped
        # first leaves the document one piece short, which the panel shows and
        # a retry fixes.
        await self._store.delete_chunks(base.id, [chunk_id])
        await self._store.insert(
            base.id,
            [
                VectorRecord(
                    vector=vector,
                    document_id=document_id,
                    chunk=chunk,
                    chunk_id=stored.chunk_id,
                    enabled=existing.enabled,
                    manual=True,
                )
            ],
        )
        return stored

    async def add_chunk(self, document_id: str, text: str) -> StoredChunk:
        """Append a piece a person wrote to the end of a document.

        Appended, not inserted: it was not cut from anywhere in the document,
        and putting it between two pieces that *were* would claim a place in
        the text it does not have. It is embedded with the base's own model,
        like every other piece, so it is found by the same queries.

        It lives exactly as long as the parse it sits behind. Reindexing the
        document deletes every piece of it, this one included.
        """
        found = self._indexed_document(document_id)
        if found is None:
            raise KnowledgeError("this document has no index to add a piece to")
        record, base = found
        body = text.strip()
        if not body:
            raise KnowledgeError("a chunk needs some text")

        client = self._client_for(base)
        await self._assert_current(base, client)
        vector = (await client.embed([body]))[0]

        held, total = await self._store.list_chunks(base.id, document_id)
        taken = {piece.chunk_id for piece in held}
        occurrence = 0
        while chunk_id_for(document_id, body, occurrence) in taken:
            occurrence += 1
        chunk = Chunk(
            content=TextBlock(text=body),
            source=record.source,
            chunk_index=(held[-1].chunk.chunk_index + 1) if held else 0,
            total_chunks=total + 1,
            metadata=section_metadata(reading_order=total, layout_type=LayoutType.TEXT, manual=True),
        )
        stored = StoredChunk(
            chunk_id=chunk_id_for(document_id, body, occurrence),
            chunk=chunk,
            enabled=True,
            manual=True,
        )
        await self._store.insert(
            base.id,
            [
                VectorRecord(
                    vector=vector,
                    document_id=document_id,
                    chunk=chunk,
                    chunk_id=stored.chunk_id,
                    manual=True,
                )
            ],
        )
        self._records.set_status(document_id, "ready", chunk_count=total + 1)
        return stored

    def _chunker_for(self, base: KnowledgeBaseRecord) -> ChunkerBase:
        """The chunker this base is configured for.

        One chunker was built at construction and used for every base until
        now, which left the four settings a reader can change -- the strategy,
        the size, the separator and the overlap -- saved, displayed, and read
        by nothing. They decide here.

        ``smart_chunking`` is not consulted yet. It is meant to pick between
        two different ideas rather than two settings of one -- the document's
        own structure as the boundary, or the delimiters -- and the structural
        half is being reworked, so every base is cut the naive way until it is
        worth choosing again. The setting is kept rather than removed: it is
        the shape the answer will take, and the panel says it is not in force
        rather than offering a switch that decides nothing.

        An explicitly supplied chunker still wins: a caller that passed one is
        testing or embedding this engine somewhere with its own idea of a
        chunk.
        """
        if self._chunker is not None:
            return self._chunker
        return NaiveChunker(
            chunk_size=base.chunk_size,
            separator=base.separator,
            overlap_size=base.chunk_overlap,
            table_context_size=base.table_context_size,
            image_context_size=base.image_context_size,
        )

    async def _chunks_for(self, record: KnowledgeDocumentRecord, base: KnowledgeBaseRecord) -> tuple[list[Chunk], str]:
        """This document's chunks, and what the parse could not do.

        The second half is the parse's own notes -- a picture no model could
        read, a figure budget the file ran past. None of them stops the
        document being indexed, and all of them are invisible without a channel
        of their own: the row would say ready, the file would be searchable,
        and the part that never made it in would show up only as an answer
        that is quietly worse.
        """
        content = self.read_document(record.id)
        if content is None:
            raise KnowledgeError("the uploaded file is missing from the store")
        parser = self._parser_for(record.media_type)
        if parser is None:
            raise KnowledgeError(f"no parser for {record.media_type}")
        with collecting() as notes:
            sections = await parser.parse(content, record.source)
        return await self._chunker_for(base).chunk(sections), joined(notes)

    async def index_pending(self) -> int:
        """Index everything queued, oldest first. Returns how many were tried."""
        pending = self._records.pending_documents()
        for document in pending:
            await self.index_document(document.id)
        return len(pending)

    # ── search ────────────────────────────────────────────────────

    async def search(self, base_ids: list[str], query: str, top_k: int | None = None) -> SearchOutcome:
        """Search across bases, merged and ranked together.

        One embedding call per distinct model, not one per search: a base is
        searched with the vector its own model produces, because that is the
        space its collection lives in. Bases built with the same model share a
        call, so the common case -- every base on the configured endpoint --
        still costs exactly one round trip.

        A base whose model cannot be reached is searched by keyword instead,
        and named in ``skipped`` so a reader knows which answers came from
        words rather than from meaning. Asking a mixed set is ordinary, and a
        base that cannot be embedded against still holds the text it was given.

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
        started = perf_counter()
        vectors: dict[tuple[str, str], list[float]] = {}
        searchable: list[tuple[KnowledgeBaseRecord, list[float]]] = []
        by_word: list[KnowledgeBaseRecord] = []
        fell_back: dict[str, str] = {}
        for base in bases:
            key = (base.embedding_provider, base.embedding_model)
            try:
                if key not in vectors:
                    client = self._client_for(base)
                    await self._assert_current(base, client)
                    vectors[key] = (await client.embed([query]))[0]
            except (KnowledgeError, EmbeddingError, OSError) as exc:
                # The words still work. A base whose model has gone answers by
                # keyword rather than not at all -- worse retrieval than the
                # vectors it holds, and the alternative is a base that has gone
                # dark for a reason its reader cannot act on today.
                reason = self._unsearchable(base, exc)
                logger.warning("knowledge: base {} falls back to keywords: {}", base.name, reason)
                fell_back[base.id] = reason
                by_word.append(base)
                continue
            searchable.append((base, vectors[key]))
        embedded = perf_counter()

        ranked: list[list[VectorSearchResult]] = []
        for base, vector in searchable:
            ranked.append(await self._store.search(base.id, vector, top_k=top_k))
        for base in by_word:
            ranked.append(await self._store.keyword_search(base.id, query, top_k=top_k))
        searched = perf_counter()

        return SearchOutcome(
            hits=_merged(ranked, top_k),
            embed_ms=(embedded - started) * 1000.0,
            search_ms=(searched - embedded) * 1000.0,
            by_keyword=fell_back,
        )
