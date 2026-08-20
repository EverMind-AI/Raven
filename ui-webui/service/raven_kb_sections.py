# -*- coding: utf-8 -*-
"""Give the model the section a hit came from, not just the slice that matched.

Retrieval and reading want different granularities. A small chunk is what makes
a vector search precise -- it is mostly about one thing, so its embedding is not
diluted -- but it is a poor thing to hand a model, because the sentence that
answers the question often needs the two paragraphs around it. Indexing large
and reading large loses the precision; indexing small and reading small loses
the context.

The usual answer is to decouple them: search the small chunks, return the parent
they belong to. Here the parent is already defined -- ``StructuredTextParser``
cuts one section per heading, and every chunk carries the heading path of the
section it came from -- so the parent exists in the index without a second
hierarchy having to be built for it. This module rebuilds a section from its
chunks and collapses hits that share one.

The section text itself is not rebuilt from the chunks -- the chunker stored a
copy when it split one, precisely because reassembling after the fact is unsafe
(see ``SECTION_TEXT``). This module only has to find it and decide when to use
it.

Degrades quietly. A vector store with no scroll access, a document whose chunks
cannot be read back, a section indexed before the chunker started storing it, or
one too large to be worth inlining all leave the original hit untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agentscope.message import TextBlock
from agentscope.rag import Chunk

logger = logging.getLogger("uvicorn.error")

_SCROLL_PAGE = 256

SECTION_TEXT = "section_text"
"""Metadata key: the whole section, carried on the first chunk that came from it.

Defined on the reading side rather than by whichever parser writes it, because
this module has to work over chunks no parser of ours produced -- everything
indexed before a structured parser existed, and every format that has no
headings to cut on. Expansion is a best effort over whatever the payload
happens to carry, so it cannot depend on the writer being installed.

Only present when a section had to be split, and only within
:data:`MAX_SECTION_CHARS`. A reader that wants the section back gets it verbatim
instead of reassembling it from the pieces, which cannot be done safely after
the fact: neighbouring chunks share an overlap window, and recovering where it
starts -- by matching one chunk's tail against the next one's head, or by
searching for a piece in the section text -- lands on the wrong place as soon as
the document repeats itself, which boilerplate and tables routinely do. Both
mistakes drop content silently. One stored copy costs a few KB and cannot be
wrong.
"""

MAX_SECTION_CHARS = 4000
"""Past this, a section stops being context and starts being the document, so
it is not stored for inlining and a hit keeps the chunk that matched."""

SECTION_ORDINAL = "section_ordinal"
"""Metadata key: which section of its document a chunk came from, 0-based.

The section identity. A heading path is not one -- two same-named children of one
parent share it, and a reader keying on the path alone hands a hit in the second
section the text of the first. The boundary is only knowable while the document is
being cut, so the parser records it and :func:`section_key` prefers it.

Absent on every chunk indexed before the structured parser, and on documents with
no headings, where there is one section and nothing to disambiguate. Defined here
with :data:`SECTION_TEXT` for the same reason: this side has to read chunks no
parser of ours produced, so it cannot depend on the writer being installed.
"""


@dataclass
class Passage:
    """What the model is shown for one hit: a section, or a lone chunk."""

    score: float
    source: str
    heading_path: list[str]
    text: str


def _text_of(chunk: Chunk) -> str:
    return chunk.content.text if isinstance(chunk.content, TextBlock) else ""


def section_key(chunk: Chunk) -> tuple:
    """Identify the section a chunk belongs to, within its document.

    Prefers :data:`SECTION_ORDINAL`, which is unique per document and is the only
    thing that separates two same-named siblings; the heading path is a fallback
    for chunks indexed before the parser recorded one. A document is indexed in
    one pass, so its chunks are either all ordinal-keyed or none are -- the two
    kinds do not mix within one document and cannot mis-group against each other.

    Chunks with neither -- everything indexed before the structured parser, and
    documents with no headings -- all answer to one key, which is correct: they
    really did come from a single unstructured section.
    """
    ordinal = chunk.metadata.get(SECTION_ORDINAL)
    if isinstance(ordinal, int) and not isinstance(ordinal, bool):
        return (ordinal,)
    return tuple(heading_path_of(chunk))


def heading_path_of(chunk: Chunk) -> list[str]:
    """The ancestor headings of a chunk, for a citation.

    Kept apart from :func:`section_key`. That one is an identity and is allowed to
    be opaque -- it is an ordinal whenever the parser recorded one -- while this is
    the human-readable path a reader is shown. They were the same function once,
    and the moment the identity stopped being the path a citation started reading
    "1" instead of "Usage > Example".
    """
    path = chunk.metadata.get("heading_path")
    return [str(part) for part in path] if isinstance(path, list) else []


async def _document_chunks(knowledge_base: Any, document_id: str) -> list[Chunk]:
    """Read every chunk of one document back out of the vector store.

    Goes through the Qdrant client rather than ``VectorStoreBase``, whose
    abstract surface is insert / delete / search / list_documents and has no way
    to ask for the records of a document. A store without that client is not an
    error here -- the caller simply does not expand.
    """
    store = knowledge_base.vector_store
    get_client = getattr(store, "get_client", None)
    if get_client is None:
        return []

    from qdrant_client import models

    scroll_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="document_id",
                match=models.MatchValue(value=document_id),
            ),
        ],
    )
    client = get_client()
    chunks: list[Chunk] = []
    offset: Any = None
    while True:
        points, offset = await client.scroll(
            collection_name=knowledge_base.collection,
            scroll_filter=scroll_filter,
            limit=_SCROLL_PAGE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        chunks.extend(Chunk.model_validate(point.payload["chunk"]) for point in points)
        if offset is None:
            return chunks


async def _whole_section(
    knowledge_base: Any,
    document_id: str,
    path: tuple,
    cache: dict[tuple, list[Chunk]],
) -> str | None:
    """The full text of one section, or ``None`` when there is none to use.

    ``None`` covers every case where the section cannot stand in for the chunks
    that matched: no scroll access, nothing read back, a section that was never
    split (its chunk already is the section), one indexed before the chunker
    stored it, or one too large to inline. The caller falls back to the matched
    chunks.
    """
    cache_key = (id(knowledge_base), document_id)
    if cache_key not in cache:
        try:
            cache[cache_key] = await _document_chunks(knowledge_base, document_id)
        except Exception as exc:  # noqa: BLE001 - the matched chunks still work
            logger.warning("knowledge: cannot read back document %s: %s", document_id, exc)
            cache[cache_key] = []

    stored = set()
    for chunk in cache[cache_key]:
        if section_key(chunk) != path:
            continue
        whole = chunk.metadata.get(SECTION_TEXT)
        if isinstance(whole, str) and whole.strip() and len(whole) <= MAX_SECTION_CHARS:
            stored.add(whole)

    # A heading path is not a unique section id: one parent with two same-named
    # children -- two `### Example` under one `## Usage` -- gives both the same
    # full path. Returning the first copy found hands a hit in the second section
    # the text of the first, cited as that heading, with the group's other hits
    # collapsed into it and gone. Content and citation disagreeing is worse than
    # no expansion.
    #
    # This catches only the ambiguity it can see: two *stored copies* under one
    # path. Ambiguity is a property of the sections, and a same-path sibling that
    # was never split, or was too large to inline, stores nothing -- both normal
    # states per SECTION_TEXT above -- so it is invisible here and its hit still
    # gets the other section's text.
    #
    # No signal on this side closes that. Chunk-index contiguity is the obvious
    # candidate and does not work: same-path siblings are adjacent by
    # construction, so one section of four chunks and two adjacent sections of
    # three and one produce an identical layout. Checking that the stored copy
    # contains the matched chunk fails too, because a split section's later
    # chunks carry a heading prefix and so do not appear in it verbatim; reading
    # around that would mean reproducing the writer's prefix format here, which
    # is the coupling SECTION_TEXT is defined on this side to avoid.
    #
    # The writer therefore has to record a section identity that survives a
    # repeated heading -- an ordinal, or the index of the section's first chunk.
    # That is required, not an optimisation, and this guard is not a substitute
    # for it.
    if len(stored) > 1:
        logger.warning(
            "knowledge: %s has %d sections under %s; keeping the matched chunks",
            document_id,
            len(stored),
            " > ".join(path) or "(no heading)",
        )
        return None
    return next(iter(stored), None)


async def expand(pairs: list[tuple[Any, Any]]) -> list[Passage]:
    """Turn ranked hits into section-sized passages, keeping the given order.

    The caller's order is authoritative and is never re-sorted here. It used to
    be, by ``score``, which was harmless while the vector score was the only
    ranking -- and silently undid the reranker the moment one was added, since
    ``Passage.score`` still carries the vector score. A collapsed group takes
    the position of its best-ranked member, which is where it first appears.

    ``pairs`` carries each hit next to the knowledge base it came from, which is
    what makes the sibling lookup possible; a flattened result list has no way
    back to the collection holding it.

    Hits sharing a section collapse into one passage scored by their best hit --
    three matches in one section is one thing worth reading, not three. They
    collapse **only** when the whole section was recovered, though: without it,
    merging would keep one matched chunk and silently drop the others.
    """
    grouped: dict[tuple, list[Any]] = {}
    for knowledge_base, hit in pairs:
        if not _text_of(hit.chunk).strip():
            continue
        grouped.setdefault(
            (id(knowledge_base), hit.document_id, section_key(hit.chunk)),
            [],
        ).append((knowledge_base, hit))

    cache: dict[tuple, list[Chunk]] = {}
    passages: list[Passage] = []
    for (_, document_id, path), members in grouped.items():
        knowledge_base = members[0][0]
        whole = await _whole_section(knowledge_base, document_id, path, cache)
        if whole is None:
            passages.extend(
                Passage(
                    score=hit.score,
                    source=hit.chunk.source,
                    heading_path=heading_path_of(hit.chunk),
                    text=_text_of(hit.chunk),
                )
                for _, hit in members
            )
            continue
        # Every member of a group came from one section, so any of them carries
        # the path. Read it off the chunk rather than off the group key: the key
        # is an ordinal once the parser records one.
        passages.append(
            Passage(
                score=max(hit.score for _, hit in members),
                source=members[0][1].chunk.source,
                heading_path=heading_path_of(members[0][1].chunk),
                text=whole,
            ),
        )

    return passages
