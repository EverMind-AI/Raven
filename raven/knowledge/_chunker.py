"""Chunkers: a parser's sections become the pieces that get embedded.

A chunker never merges across a section boundary -- that is what keeps the
structure a parser found (a heading, a page, a slide) from being averaged away
before anything is retrieved.

Adopted from AgentScope's own implementation (Apache-2.0; see NOTICES.md).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from bisect import bisect_right
from itertools import accumulate

from raven.knowledge._types import Chunk, DataBlock, Section, TextBlock


class ChunkerBase(ABC):
    """Abstract base class for chunkers.

    Subclasses implement a specific chunking strategy (by character
    count, by token count, by semantic boundary, etc.).  The
    chunker is configured once at construction time and reused
    across many ``chunk()`` calls within the same knowledge base.

    Subclasses must guarantee:

    - **No cross-Section merging**: every output :class:`Chunk` is
      derived from exactly one input :class:`Section`.
    - **DataBlock pass-through**: a Section whose content is a
      :class:`~agentscope.message.DataBlock` becomes a single Chunk
      with the same content; multimodal data is never sliced.
    - **Continuous indexing**: ``chunk_index`` runs from ``0`` to
      ``total_chunks - 1`` across the entire output list, even
      when the input contains many Sections.
    - **Consistent total_chunks**: every output Chunk carries the
      same ``total_chunks`` value (the length of the output list).
    - **Metadata inheritance**: each output Chunk's ``source`` and
      ``metadata`` are copied from its parent Section.
    """

    @abstractmethod
    async def chunk(self, sections: list[Section]) -> list[Chunk]:
        """Split a list of Sections into Chunks.

        Args:
            sections (`list[Section]`):
                The Sections produced by a :class:`ParserBase`, in
                document order.

        Returns:
            `list[Chunk]`:
                The final chunks, in document order, with
                ``chunk_index`` numbered ``0..N-1`` and
                ``total_chunks`` set to ``N`` on every chunk.
        """


class ApproxTokenChunker(ChunkerBase):
    """A chunker based on an approximate token counting strategy.

    Text sections are sliced into pieces of at most ``chunk_size``
    approximate tokens, with ``overlap`` approximate tokens shared
    between two consecutive pieces.  The token count of a string is
    approximated as ``len(text.encode("utf-8")) // 4``, so no
    tokenizer dependency is required.

    Sections carrying a :class:`~agentscope.message.DataBlock`
    (images, video, etc.) are passed through unchanged as a single
    chunk.

    .. note:: Chunks never span across two input Sections, as
        required by :class:`ChunkerBase`.
    """

    def __init__(self, chunk_size: int = 512, overlap: int = 50) -> None:
        """Initialize the approx token chunker.

        Args:
            chunk_size (`int`, defaults to `512`):
                The maximum number of approximate tokens per chunk.
                Must be a positive integer.
            overlap (`int`, defaults to `50`):
                The number of approximate tokens shared between two
                consecutive chunks.  Must be non-negative and smaller
                than ``chunk_size``.

        Raises:
            `ValueError`:
                If ``chunk_size`` is not positive, or ``overlap`` is
                negative or not smaller than ``chunk_size``.
        """
        if chunk_size <= 0:
            raise ValueError(
                f"chunk_size must be positive, got {chunk_size}.",
            )
        if overlap < 0 or overlap >= chunk_size:
            raise ValueError(
                f"overlap must satisfy 0 <= overlap < chunk_size, got overlap={overlap}, chunk_size={chunk_size}.",
            )

        self.chunk_size = chunk_size
        self.overlap = overlap

    async def chunk(self, sections: list[Section]) -> list[Chunk]:
        """Chunk the input sections into smaller chunks based on an approx
        token counting strategy.

        Args:
            sections (`list[Section]`):
                A list of sections to chunk.

        Returns:
            `list[Chunk]`:
                A list of chunks, with ``chunk_index`` numbered
                ``0..N-1`` and ``total_chunks`` set to ``N`` on every
                chunk.
        """
        chunks: list[Chunk] = []
        for section in sections:
            contents: list[TextBlock | DataBlock]
            if isinstance(section.content, TextBlock):
                contents = [TextBlock(text=piece) for piece in self._split_text(section.content.text)]
            else:
                # DataBlock pass-through: never slice multimodal data
                contents = [section.content]

            chunks.extend(
                Chunk(
                    content=content,
                    source=section.source,
                    chunk_index=0,  # renumbered below
                    total_chunks=0,  # renumbered below
                    metadata=dict(section.metadata),
                )
                for content in contents
            )

        for index, chunk in enumerate(chunks):
            chunk.chunk_index = index
            chunk.total_chunks = len(chunks)

        return chunks

    def _split_text(self, text: str) -> list[str]:
        """Split text into pieces of at most ``chunk_size`` approx tokens.

        Consecutive pieces share approximately ``overlap`` tokens.

        Args:
            text (`str`):
                The text to split.

        Returns:
            `list[str]`:
                The text pieces, in document order.
        """
        if self._approx_count_tokens(text) <= self.chunk_size:
            return [text]

        # Cumulative UTF-8 byte length after each character, so that
        # the byte length of text[i:j] == byte_offsets[j] - byte_offsets[i]
        byte_offsets = [0, *accumulate(len(c.encode("utf-8")) for c in text)]

        chunk_bytes = self.chunk_size * 4
        overlap_bytes = self.overlap * 4

        pieces: list[str] = []
        start = 0
        while start < len(text):
            # The largest end such that the slice fits the byte budget
            end = (
                bisect_right(
                    byte_offsets,
                    byte_offsets[start] + chunk_bytes,
                )
                - 1
            )
            # Always make progress, even for characters whose UTF-8
            # encoding exceeds the budget on their own
            end = max(end, start + 1)
            pieces.append(text[start:end])

            if end >= len(text):
                break

            # Step back by the overlap budget, ensuring forward progress
            next_start = (
                bisect_right(
                    byte_offsets,
                    byte_offsets[end] - overlap_bytes,
                )
                - 1
            )
            start = max(next_start, start + 1)

        return pieces

    @staticmethod
    def _approx_count_tokens(text: str) -> int:
        """The approx count of tokens.

        Args:
            text (`str`):
                The text to be counted.

        Returns:
            `int`:
                The approx count of tokens.
        """
        return len(text.encode("utf-8")) // 4
