"""Knowledge bases: user-supplied documents, indexed and retrievable in a turn.

Runs inside the gateway process. The vector store is embedded and file-backed,
the records are JSON on disk, and the embedding endpoint is read from the
config the operator already filled in -- so a knowledge base adds no service to
a deployment. That is the constraint the package is shaped around: the feature
it replaces needed Redis, a Qdrant instance and a web service to answer a
question about a document.
"""

from raven.knowledge._chunker import ApproxTokenChunker, ChunkerBase
from raven.knowledge._embedding import (
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    SiliconFlowEmbeddingClient,
    embedding_client,
    load_embedding_config,
)
from raven.knowledge._lancedb import LanceDBVectorStore
from raven.knowledge._manager import (
    DuplicateBaseNameError,
    KnowledgeError,
    KnowledgeManager,
    SearchOutcome,
    StaleBaseError,
    supported_extensions,
)
from raven.knowledge._records import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_SEPARATOR,
    DEFAULT_TOP_K,
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
from raven.knowledge.parser import BBox, ElementSpan, LayoutType, ParserBase
from raven.knowledge.parser.doc_parser import LegacyDocParser
from raven.knowledge.parser.docx_parser import DocxParser
from raven.knowledge.parser.excel_parser import ExcelParser
from raven.knowledge.parser.text_parser import TextParser

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_SEPARATOR",
    "DEFAULT_TOP_K",
    "ApproxTokenChunker",
    "BBox",
    "Chunk",
    "ChunkerBase",
    "DataBlock",
    "DocumentSummary",
    "DocxParser",
    "ElementSpan",
    "EmbeddingClient",
    "ExcelParser",
    "EmbeddingConfig",
    "EmbeddingError",
    "HeadingAwareChunker",
    "KnowledgeBaseRecord",
    "KnowledgeDocumentRecord",
    "KnowledgeError",
    "KnowledgeManager",
    "LanceDBVectorStore",
    "LegacyDocParser",
    "LayoutType",
    "ParserBase",
    "RecordStore",
    "SearchOutcome",
    "SiliconFlowEmbeddingClient",
    "Section",
    "DuplicateBaseNameError",
    "StaleBaseError",
    "StructuredTextParser",
    "embedding_client",
    "supported_extensions",
    "TextBlock",
    "TextParser",
    "VectorRecord",
    "VectorSearchResult",
    "VectorStoreBase",
    "load_embedding_config",
]
