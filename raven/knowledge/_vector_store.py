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

from raven.knowledge._types import DocumentSummary, VectorRecord, VectorSearchResult


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
    ) -> list[VectorSearchResult]:
        """The ``top_k`` nearest records, most similar first."""

    @abstractmethod
    async def list_documents(
        self,
        collection: str,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[DocumentSummary]:
        """One summary per distinct ``document_id`` in ``collection``."""
