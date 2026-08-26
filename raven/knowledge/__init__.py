"""Knowledge bases: user-supplied documents, indexed and retrievable in a turn.

Runs inside the gateway process. The vector store is embedded and file-backed,
the records are JSON on disk, and the embedding endpoint is read from the
config the operator already filled in -- so a knowledge base adds no service to
a deployment. That is the constraint the package is shaped around: the feature
it replaces needed Redis, a Qdrant instance and a web service to answer a
question about a document.
"""

from raven.knowledge._chunker import ApproxTokenChunker, ChunkerBase
from raven.knowledge._embedding import EmbeddingClient, EmbeddingConfig, EmbeddingError, load_embedding_config
from raven.knowledge._lancedb import LanceDBVectorStore
from raven.knowledge._manager import KnowledgeError, KnowledgeManager, StaleBaseError
from raven.knowledge._parser import ParserBase, TextParser
from raven.knowledge._records import (
    KnowledgeBaseRecord,
    KnowledgeDocumentRecord,
    RecordStore,
)
from raven.knowledge._structure import HeadingAwareChunker, StructuredTextParser
from raven.knowledge._types import (
    Chunk,
    DataBlock,
    DocumentSummary,
    Section,
    TextBlock,
    VectorRecord,
    VectorSearchResult,
)
from raven.knowledge._vector_store import VectorStoreBase

__all__ = [
    "ApproxTokenChunker",
    "Chunk",
    "ChunkerBase",
    "DataBlock",
    "DocumentSummary",
    "EmbeddingClient",
    "EmbeddingConfig",
    "EmbeddingError",
    "HeadingAwareChunker",
    "KnowledgeBaseRecord",
    "KnowledgeDocumentRecord",
    "KnowledgeError",
    "KnowledgeManager",
    "LanceDBVectorStore",
    "ParserBase",
    "RecordStore",
    "Section",
    "StaleBaseError",
    "StructuredTextParser",
    "TextBlock",
    "TextParser",
    "VectorRecord",
    "VectorSearchResult",
    "VectorStoreBase",
    "load_embedding_config",
]
