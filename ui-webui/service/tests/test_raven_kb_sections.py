"""Unit tests for rebuilding a section from the chunks that were indexed."""

from __future__ import annotations

import asyncio

from agentscope.message import TextBlock
from agentscope.rag import Chunk
from agentscope.rag._vdb._vector_store import VectorSearchResult
from raven_kb_sections import (
    MAX_SECTION_CHARS,
    SECTION_ORDINAL,
    SECTION_TEXT,
    expand,
    section_key,
)


def _chunk(
    text: str,
    index: int = 0,
    path: list[str] | None = None,
    source: str = "report.md",
    section: str | None = None,
):
    metadata: dict = {"heading_path": path} if path else {}
    if section is not None:
        metadata[SECTION_TEXT] = section
    return Chunk(
        content=TextBlock(text=text),
        source=source,
        chunk_index=index,
        total_chunks=9,
        metadata=metadata,
    )


def _hit(chunk: Chunk, score: float = 0.9, document_id: str = "doc"):
    return VectorSearchResult(score=score, document_id=document_id, chunk=chunk)


class _FakeKb:
    """A knowledge base whose store can be scrolled, like QdrantStore."""

    def __init__(self, chunks):
        self.collection = "kb"
        self.scrolls = 0
        self.vector_store = self._Store(chunks, self)

    class _Store:
        def __init__(self, chunks, owner):
            self._chunks = chunks
            self._owner = owner

        def get_client(self):
            return self

        async def scroll(self, collection_name, scroll_filter, limit, offset, **kwargs):
            self._owner.scrolls += 1
            payloads = [
                type("P", (), {"payload": {"chunk": chunk.model_dump(mode="json")}})() for chunk in self._chunks
            ]
            return payloads, None


class _NoScrollKb:
    """A store with no client to scroll -- expansion must degrade, not fail."""

    collection = "kb"
    vector_store = None


def _expand(pairs):
    return asyncio.run(expand(pairs))


def test_section_key_groups_by_heading_path():
    assert section_key(_chunk("a", path=["A", "B"])) == ("A", "B")
    assert section_key(_chunk("a")) == ()


def test_a_hit_is_replaced_by_its_whole_section():
    path = ["Report", "Range"]
    whole = "## Range\n\nStandard range is 82 km. Cold range is 61 km."
    indexed = [
        _chunk("## Range\n\nStandard range is 82 km. ", 0, path, section=whole),
        _chunk("Report > Range\n\nCold range is 61 km.", 1, path),
    ]
    kb = _FakeKb(indexed)

    passages = _expand([(kb, _hit(indexed[1]))])

    assert len(passages) == 1
    assert passages[0].text == whole
    assert passages[0].heading_path == path


def test_two_hits_in_one_section_collapse_to_one_passage():
    path = ["Report", "Range"]
    whole = "## Range\n\nStandard range is 82 km. Cold range is 61 km."
    indexed = [
        _chunk("## Range\n\nStandard range is 82 km. ", 0, path, section=whole),
        _chunk("Report > Range\n\nCold range is 61 km.", 1, path),
    ]
    kb = _FakeKb(indexed)

    passages = _expand([(kb, _hit(indexed[0], 0.7)), (kb, _hit(indexed[1], 0.9))])

    assert len(passages) == 1
    assert passages[0].score == 0.9


def test_hits_in_different_sections_stay_separate():
    a = _chunk("Range body.", 0, ["Report", "Range"])
    b = _chunk("Braking body.", 1, ["Report", "Braking"])
    kb = _FakeKb([a, b])

    passages = _expand([(kb, _hit(a, 0.9)), (kb, _hit(b, 0.6))])

    assert [p.heading_path[-1] for p in passages] == ["Range", "Braking"]


def test_the_document_is_read_back_once_for_all_its_hits():
    """One scroll per document, not one per hit."""
    a = _chunk("Range body.", 0, ["Report", "Range"])
    b = _chunk("Braking body.", 1, ["Report", "Braking"])
    kb = _FakeKb([a, b])

    _expand([(kb, _hit(a)), (kb, _hit(b))])

    assert kb.scrolls == 1


def test_a_store_without_scroll_access_leaves_every_hit_alone():
    kb = _NoScrollKb()
    first, second = _chunk("First.", 0), _chunk("Second.", 1)

    passages = _expand([(kb, _hit(first, 0.9)), (kb, _hit(second, 0.8))])

    assert [p.text for p in passages] == ["First.", "Second."]


def test_hits_are_not_collapsed_when_the_section_cannot_be_rebuilt():
    """Collapsing without the section would keep one matched chunk and drop the
    rest -- strictly less than what retrieval found."""
    kb = _NoScrollKb()
    first, second = _chunk("First.", 0, ["A"]), _chunk("Second.", 1, ["A"])

    passages = _expand([(kb, _hit(first, 0.9)), (kb, _hit(second, 0.8))])

    assert len(passages) == 2


def test_an_oversized_section_falls_back_to_the_matched_chunk():
    """A headingless file is one section; inlining it would bury the answer. The
    chunker declines to store one this big, and a stale copy is refused here."""
    big = [
        _chunk("head", 0, section="x" * (MAX_SECTION_CHARS + 10)),
        _chunk("tail", 1),
    ]
    kb = _FakeKb(big)

    passages = _expand([(kb, _hit(big[1]))])

    assert passages[0].text == "tail"


def test_a_section_that_was_never_split_keeps_its_own_chunk():
    """One chunk already is the section; there is nothing to expand to."""
    only = _chunk("## Range\n\nStandard range is 82 km.", 0, ["Report", "Range"])
    kb = _FakeKb([only])

    passages = _expand([(kb, _hit(only))])

    assert passages[0].text == "## Range\n\nStandard range is 82 km."


def test_passages_keep_the_order_they_were_given():
    """Re-sorting by score here silently undid the reranker upstream, which had
    already put the best passage first with a lower vector score."""
    a = _chunk("Reranked first.", 0, ["A"])
    b = _chunk("Higher vector score.", 1, ["B"])
    kb = _FakeKb([a, b])

    passages = _expand([(kb, _hit(a, 0.2)), (kb, _hit(b, 0.95))])

    assert [p.text for p in passages] == ["Reranked first.", "Higher vector score."]


def test_documents_are_not_confused_with_each_other():
    """Two documents can hold the same heading path; a section must not be
    stitched out of both."""
    shared = ["Report"]
    mine = _chunk("Mine.", 0, shared, source="a.md")
    kb = _FakeKb([mine])

    passages = _expand([(kb, _hit(mine, 0.9, document_id="doc-a"))])

    assert passages[0].source == "a.md"
    assert passages[0].text == "Mine."


def test_two_stored_copies_under_one_heading_path_are_not_expanded():
    """A repeated heading under one parent -- two `### Example` under one
    `## Usage` -- gives both sections the same full path, and the lookup returned
    whichever stored copy it reached first. A hit in the second section was then
    replaced by the first section's text: content and citation disagreeing, with
    the other hits in the group collapsed into it and gone.

    Only the two-stored-copies shape is caught. A same-path sibling that was
    never split stores nothing, so it is invisible to the guard and its hit is
    still handed the other section's text -- see the comment on `_whole_section`
    for why no signal on this side closes that, and what the writer owes instead.
    """
    path = ["Usage", "Example"]
    first = _chunk("first body", 0, path=path, section="FIRST SECTION")
    second = _chunk("second body", 1, path=path, section="SECOND SECTION")
    kb = _FakeKb([first, second])

    passages = asyncio.run(expand([(kb, _hit(second))]))

    assert len(passages) == 1
    assert passages[0].text == "second body", "expanded to a section it did not come from"
    assert "FIRST SECTION" not in passages[0].text


def test_one_section_under_a_repeated_heading_still_expands():
    """The guard must not cost the ordinary case: a single stored copy answering
    to the path is unambiguous even when the heading text is a common one."""
    path = ["Usage", "Example"]
    only = _chunk("body", 0, path=path, section="THE WHOLE SECTION")
    kb = _FakeKb([only, _chunk("elsewhere", 1, path=["Other"], section="OTHER")])

    passages = asyncio.run(expand([(kb, _hit(only))]))

    assert passages[0].text == "THE WHOLE SECTION"


def test_an_ordinal_keyed_chunk_is_still_cited_by_its_headings():
    """`section_key` and the citation path were one function. Once the identity
    became an ordinal, a citation read `1` instead of `Usage > Example` -- and no
    existing test saw it, because they all build chunks with a path and no ordinal,
    where the two happen to coincide."""
    chunk = _chunk("body", 0, path=["Usage", "Example"])
    chunk.metadata[SECTION_ORDINAL] = 1
    kb = _FakeKb([chunk])

    passages = asyncio.run(expand([(kb, _hit(chunk))]))

    assert passages[0].heading_path == ["Usage", "Example"]
    assert section_key(chunk) == (1,), "the identity should still be the ordinal"


def test_two_same_path_sections_with_ordinals_do_not_collapse():
    """The shape no reading-side guard could close, now closed by the writer: the
    two sections have distinct identities, so a hit in the second is neither
    grouped with the first nor handed its text."""
    path = ["Usage", "Example"]
    first = _chunk("first body", 0, path=path, section="FIRST SECTION")
    first.metadata[SECTION_ORDINAL] = 1
    second = _chunk("second body", 1, path=path)
    second.metadata[SECTION_ORDINAL] = 2
    kb = _FakeKb([first, second])

    passages = asyncio.run(expand([(kb, _hit(second))]))

    assert passages[0].text == "second body"
    assert "FIRST SECTION" not in passages[0].text
    assert passages[0].heading_path == path
