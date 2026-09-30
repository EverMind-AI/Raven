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
    chunk_id: str = ""
    """What addresses this piece for the life of the index.

    Derived from the content, so the same text in the same document is the same
    piece across a rebuild -- see ``chunk_id`` in ``_manager``. Empty on rows
    written before ids existed; those answer searches like any other row and
    cannot be acted on one at a time until the document is reindexed."""

    enabled: bool = True
    """Whether this piece may be retrieved at all.

    Off is not a soft ranking penalty: a disabled piece is filtered out of
    search and never reaches the agent, which is the only reading under which
    turning one off is worth doing."""

    manual: bool = False
    """Whether a person wrote this piece rather than a parser cutting it.

    Recorded so the panel can say so. It buys no protection: reindexing a
    document deletes every piece of it, this one included."""


class StoredChunk(BaseModel):
    """A chunk as the index holds it: the piece, plus what can be done to it.

    Separate from :class:`Chunk` because the two answer to different owners. A
    Chunk is what a parser and a chunker produced, and nothing about storage
    belongs in it; this is that piece once the store has given it an identity
    and a state a reader can change.
    """

    chunk_id: str
    chunk: Chunk
    enabled: bool = True
    manual: bool = False


class VectorSearchResult(BaseModel):
    """One hit. ``score`` runs the same direction whatever found it: higher is
    nearer.

    Stated that way rather than as a distance because it is the direction every
    caller already reads: the reranker sorts descending, and a relevance floor
    is a lower bound. What the number *means* is :attr:`retrieval`'s business,
    and the two scales are not comparable by value -- see :func:`_merged`.
    """

    score: float
    document_id: str
    chunk: Chunk
    chunk_id: str = ""
    retrieval: Literal["vector", "keyword"] = "vector"
    """How this hit was found, and therefore what ``score`` is.

    ``vector`` is a cosine similarity in 0..1; ``keyword`` is a BM25 score on
    the index's own scale, which is unbounded and not calibrated to the first.
    Carried on the hit rather than inferred by the caller because a base falls
    back to keywords per search, and a merged result set can hold both -- a
    reader shown 8.4 beside 0.62 under one heading called "similarity" is being
    told something false about both."""
    """Which stored piece this was, when the store knows.

    Empty for rows written before ids existed. A hit that cannot be named is
    still a hit worth reading; it just cannot be acted on one at a time."""


class DocumentSummary(BaseModel):
    """One document's presence in a collection, without its vectors."""

    document_id: str
    source: str
    chunk_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)
