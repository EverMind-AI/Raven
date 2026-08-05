# -*- coding: utf-8 -*-
"""The vector store classes in AgentScope."""

from ._qdrant import QdrantStore
from ._vector_store import (
    DocumentSummary,
    VectorRecord,
    VectorSearchResult,
    VectorStoreBase,
)

__all__ = [
    "DocumentSummary",
    "VectorStoreBase",
    "VectorRecord",
    "VectorSearchResult",
    "QdrantStore",
]
