"""The knowledge-base data model: what a parsed document becomes on its way
into a vector store, and what a search hands back.

The shapes are adopted from AgentScope's ``rag`` package (Apache-2.0; see
NOTICES.md) and kept as adopted: a knowledge base written by the earlier
deployment is still read by this code because the shapes did not move.
Defined here rather than imported because importing AgentScope would put
FastAPI, a message bus and a provider catalogue behind ``raven.knowledge``, and
the package's whole point is that indexing and retrieval run inside the gateway
process.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    """A run of text carried by a section or a chunk."""

    type: Literal["text"] = "text"
    text: str


class DataBlock(BaseModel):
    """Non-text content a parser could not reduce to a string."""

    type: Literal["data"] = "data"
    data: Any = None


class Section(BaseModel):
    """One parsed region of a source document, before chunking."""

    content: TextBlock | DataBlock
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    """One embeddable piece of a document.

    ``chunk_index`` / ``total_chunks`` are the piece's place in its own
    document, which is what lets a retrieved chunk be widened back out to the
    passage around it instead of being shown as an isolated fragment.
    """

    content: TextBlock | DataBlock
    source: str
    chunk_index: int
    total_chunks: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """The chunk's text, or ``""`` for a non-text block."""
        return self.content.text if isinstance(self.content, TextBlock) else ""


class VectorRecord(BaseModel):
    """A chunk plus the vector it was embedded to, as stored."""

    vector: list[float]
    document_id: str
    chunk: Chunk


class VectorSearchResult(BaseModel):
    """One hit. ``score`` is a similarity -- higher is nearer.

    Stated as similarity rather than distance because that is the direction
    every caller already reads: the reranker sorts descending, and a relevance
    floor is a lower bound.
    """

    score: float
    document_id: str
    chunk: Chunk


class DocumentSummary(BaseModel):
    """One document's presence in a collection, without its vectors."""

    document_id: str
    source: str
    chunk_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)
