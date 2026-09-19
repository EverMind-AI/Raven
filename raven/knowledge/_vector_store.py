"""The vector-store seam.

One collection per knowledge base, one record per chunk. The interface is kept
to what a knowledge base actually needs -- create, drop, insert, delete a
document's records, search, and enumerate documents -- so a backend is a few
hundred lines rather than a subsystem.

``metadata_filter`` is a flat ``{key: value}`` map, AND-ed, matched exactly
against ``chunk.metadata``. It is part of the interface rather than left to the
caller because a backend has to apply it *before* ranking: filtering the top-k
afterwards silently returns fewer rows than asked for, and reads as a thin
index rather than a narrow filter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from raven.knowledge._types import DocumentSummary, StoredChunk, VectorRecord, VectorSearchResult


class VectorStoreBase(ABC):
    """Abstract vector store backing one or more knowledge bases."""

    @abstractmethod
    async def create_collection(self, name: str, dimensions: int) -> None:
        """Create ``name`` sized for ``dimensions``-wide vectors.

        Idempotent: an existing collection of the same name is left alone. The
        width is fixed at creation because it comes from the embedding model,
        and a model swap is a rebuild rather than a migration.
        """

    @abstractmethod
    async def delete_collection(self, name: str) -> None:
        """Drop ``name`` and everything in it. Missing is not an error."""

    @abstractmethod
    async def has_collection(self, name: str) -> bool:
        """Whether ``name`` exists."""

    @abstractmethod
    async def insert(self, collection: str, records: list[VectorRecord]) -> None:
        """Append ``records``. An empty list is a no-op, not an error."""

    @abstractmethod
    async def delete(self, collection: str, document_id: str) -> None:
        """Remove every record belonging to ``document_id``."""

    @abstractmethod
    async def search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int = 5,
        metadata_filter: dict[str, Any] | None = None,
        document_id: str | None = None,
    ) -> list[VectorSearchResult]:
        """The ``top_k`` nearest records, most similar first."""

    @abstractmethod
    async def list_documents(
        self,
        collection: str,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[DocumentSummary]:
        """One summary per distinct ``document_id`` in ``collection``."""

    @abstractmethod
    async def list_chunks(
        self,
        collection: str,
        document_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        enabled: bool | None = None,
    ) -> tuple[list[StoredChunk], int]:
        """One page of a document's chunks, and how many there are in all.

        Reading order, which is ``chunk_index``: the chunker numbers a
        document's pieces as it walks the sections a parser produced, so the
        sequence is the document's own. The store returns rows in whatever
        order the scan finds them, so the ordering is this method's to
        guarantee rather than the caller's to hope for -- and the page is taken
        after the ordering, so it is a window on the document.

        ``enabled`` filters by state when it is set; the total counts what the
        filter admitted, so a pager over disabled pieces is paging those.
        """

    @abstractmethod
    async def keyword_search(
        self,
        collection: str,
        query: str,
        top_k: int = 5,
        document_id: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        """The nearest chunks by their words rather than by their vectors.

        What answers when a base cannot be embedded against -- its model gone,
        or never configured -- and what a reader searching inside one document
        gets when the same is true. Scores are the index's own (BM25): higher
        is still nearer, but the number means nothing beside a cosine
        similarity, so results from the two are merged by rank and never by
        value.
        """

    @abstractmethod
    async def renumber(self, collection: str, document_id: str) -> int:
        """Renumber a document's pieces 0..N-1, and answer with N.

        Called after a hand edit adds or removes one: the chunker's contract is
        that indexes run without gaps and every piece agrees on the total, and
        a store that keeps the old numbers reports positions its own document
        disagrees with."""

    @abstractmethod
    async def set_chunks_enabled(
        self, collection: str, chunk_ids: list[str], enabled: bool, *, document_id: str = ""
    ) -> int:
        """Turn pieces on or off, and answer with how many rows changed."""

    @abstractmethod
    async def delete_chunks(self, collection: str, chunk_ids: list[str], *, document_id: str = "") -> None:
        """Remove pieces outright. Unlike disabling, nothing is kept."""
