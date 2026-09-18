"""Unit tests for naive chunking: delimiters, merging, context and overlap."""

from __future__ import annotations

import asyncio

import pytest

from raven.knowledge._naive_chunker import (
    DEFAULT_DELIMITER,
    NaiveChunker,
    count_tokens,
    has_wrapped_delimiter,
    parse_delimiters,
)
from raven.knowledge._types import DataBlock, Section, TextBlock


def _chunk(sections: list[Section], **kwargs) -> list:
    return asyncio.run(NaiveChunker(**kwargs).chunk(sections))


def _texts(chunks) -> list[str]:
    return [chunk.content.text for chunk in chunks]


def _plain(text: str) -> Section:
    return Section(content=TextBlock(text=text), source="notes.txt", metadata={})


def _with_elements(rows: list[tuple[str, str]]) -> Section:
    """A section whose spans say where each paragraph, table and figure sits."""
    text = "\n\n".join(body for body, _ in rows)
    spans = []
    at = 0
    for index, (body, layout) in enumerate(rows):
        spans.append(
            {
                "reading_order": index,
                "layout_type": layout,
                "char_start": at,
                "char_end": at + len(body),
            }
        )
        at += len(body) + 2
    return Section(content=TextBlock(text=text), source="handbook.docx", metadata={"elements": spans})


# -- the delimiter field -------------------------------------------


def test_bare_characters_are_one_delimiter_each() -> None:
    assert parse_delimiters("\n!?") == ["\n", "!", "?"]


def test_a_backticked_run_is_one_delimiter() -> None:
    """How a reader asks to cut on a blank line rather than on two newlines."""
    assert parse_delimiters("`\n\n`;") == ["\n\n", ";"]
    assert has_wrapped_delimiter("`\n\n`;") is True
    assert has_wrapped_delimiter("\n;") is False


def test_delimiters_come_back_longest_first() -> None:
    """The pattern is an alternation, so a shorter delimiter listed first would
    match inside a longer one and cut twice where one cut was asked for."""
    assert parse_delimiters("`ab`a") == ["ab", "a"]


def test_the_default_carries_both_scripts() -> None:
    """A Chinese full stop ends a sentence as much as a full stop does, and a
    chunker that only knew the latin marks would cut a Chinese document
    nowhere."""
    assert "\u3002" in parse_delimiters(DEFAULT_DELIMITER), "the full-width full stop"
    assert "!" in parse_delimiters(DEFAULT_DELIMITER)


def test_tokens_are_counted_by_the_tokenizer() -> None:
    """Not bytes over four, which reads a CJK character as three quarters of a
    token when it costs about one."""
    assert count_tokens("hello world") == 2
    assert count_tokens("\u5ef6\u8fdf\u5728\u7b2c\u4e8c") > 5 * 0.75


# -- splitting and merging -----------------------------------------


def test_the_delimiter_never_appears_in_a_chunk() -> None:
    """A delimiter is a boundary, not content."""
    chunks = _chunk([_plain("First!Second!Third")], chunk_size=1, separator="!")

    assert _texts(chunks) == ["First", "Second", "Third"]


def test_neighbours_merge_until_the_chunk_is_full() -> None:
    chunks = _chunk([_plain("one!two!three!four")], chunk_size=100, separator="!")

    assert _texts(chunks) == ["one\ntwo\nthree\nfour"]


def test_a_chunk_may_pass_the_size_rather_than_split_a_unit() -> None:
    """The size is read off the chunk *before* the next unit is added, never
    after. So a chunk that is still under the size takes the whole of whatever
    comes next, however large it is, and ends up over.

    That is the trade, and it is the point: cutting the unit to make it fit
    would put half a paragraph in one chunk and half in the next.
    """
    long_one = " ".join(f"word{n}" for n in range(200))
    chunks = _chunk([_plain(f"short!{long_one}")], chunk_size=5, separator="!")

    assert len(chunks) == 1, "a chunk under the size takes the next unit whole"
    assert count_tokens(chunks[0].content.text) > 5 * 20, "and lands far over it"
    assert long_one in chunks[0].content.text


def test_a_full_chunk_starts_a_new_one() -> None:
    """The other half of the same rule: once a chunk has reached the size, the
    next unit opens a chunk of its own."""
    filler = " ".join(f"word{n}" for n in range(30))
    chunks = _chunk([_plain(f"{filler}!second!third")], chunk_size=10, separator="!")

    assert len(chunks) == 2
    assert chunks[1].content.text == "second\nthird"


def test_a_wrapped_delimiter_makes_every_segment_its_own_chunk() -> None:
    """A reader who spells a delimiter out that carefully is describing the
    pieces they want, not hinting at where to cut."""
    chunks = _chunk([_plain("a;b;c")], chunk_size=1000, separator="`;`")

    assert _texts(chunks) == ["a", "b", "c"]


# -- what stands alone ---------------------------------------------


def test_a_table_is_its_own_chunk() -> None:
    """So a query that matches a table gets the table, and not a paragraph that
    happened to sit beside it."""
    section = _with_elements([("Before.", "text"), ("Region | EU", "table"), ("After.", "text")])

    chunks = _chunk([section], chunk_size=1000, table_context_size=0)

    assert _texts(chunks) == ["Before.", "Region | EU", "After."]


def test_a_figure_is_its_own_chunk_too() -> None:
    section = _with_elements([("Before.", "text"), ("Latency over time", "figure")])

    chunks = _chunk([section], chunk_size=1000, image_context_size=0)

    assert _texts(chunks) == ["Before.", "Latency over time"]


def test_a_data_block_is_passed_through_whole() -> None:
    section = Section(content=DataBlock(data={"kind": "image"}), source="a.png", metadata={})

    chunks = _chunk([section], chunk_size=10)

    assert len(chunks) == 1 and isinstance(chunks[0].content, DataBlock)


def test_a_table_carries_the_prose_around_it_when_asked() -> None:
    """A table on its own embeds as a grid of values with nothing saying what
    they are about."""
    section = _with_elements(
        [
            ("Revenue held up in the second quarter.", "text"),
            ("Region | EU | 1.2M", "table"),
            ("The third quarter is not in yet.", "text"),
        ]
    )

    chunks = _chunk([section], chunk_size=1000, table_context_size=8)
    table = next(text for text in _texts(chunks) if "1.2M" in text)

    assert "second quarter" in table
    assert "third quarter" in table


def test_context_comes_along_by_default() -> None:
    """A table on its own embeds as a grid of values with nothing saying what
    they are about, so the chunker carries some of the prose around it unless
    it is told not to."""
    section = _with_elements([("Around it.", "text"), ("Region | EU", "table")])

    table = next(text for text in _texts(_chunk([section], chunk_size=1000)) if "Region" in text)

    assert "Around it." in table


def test_context_can_be_turned_off() -> None:
    section = _with_elements([("Around it.", "text"), ("Region | EU", "table")])

    chunks = _chunk([section], chunk_size=1000, table_context_size=0)
    table = next(text for text in _texts(chunks) if "Region" in text)

    assert table == "Region | EU"


# -- overlap --------------------------------------------------------


def test_overlap_is_off_by_default() -> None:
    chunks = _chunk([_plain("one!two")], chunk_size=1, separator="!")

    assert _texts(chunks) == ["one", "two"]


def test_overlap_repeats_both_neighbours() -> None:
    """A chunk carries the tail of the one before and the head of the one
    after, so a passage across a boundary is whole in both."""
    chunks = _chunk([_plain("alpha!beta!gamma")], chunk_size=1, separator="!", overlap_size=4)

    assert "alpha" in chunks[1].content.text
    assert "beta" in chunks[1].content.text
    assert "gamma" in chunks[1].content.text
    assert chunks[0].content.text.startswith("alpha"), "the first has nothing before it"
    assert chunks[-1].content.text.endswith("gamma"), "the last has nothing after it"


def test_a_negative_overlap_is_refused() -> None:
    with pytest.raises(ValueError):
        NaiveChunker(overlap_size=-1)


# -- what naive means -----------------------------------------------


def test_chunks_may_span_sections() -> None:
    """The one chunker here that ignores the structure a parser found: that is
    the trade a reader makes when they turn smart chunking off."""
    chunks = _chunk([_plain("first section"), _plain("second section")], chunk_size=1000)

    assert _texts(chunks) == ["first section\nsecond section"]


# -- what a crossed boundary costs, and what is paid ----------------


def _placed(text: str, order: int, page: int, x0: float = 72.0) -> Section:
    """A section that knows where it came from, the way a parser leaves one."""
    return Section(
        content=TextBlock(text=text),
        source="report.docx",
        metadata={
            "reading_order": order,
            "page_number": page,
            "page_end": page,
            "layout_type": "text",
            "bbox": {"x0": x0, "x1": x0 + 400.0},
            "heading_path": ["Handbook", f"Part {order}"],
        },
    )


def test_a_chunk_from_one_section_keeps_that_section_metadata() -> None:
    """The ordinary case, and the one the merging below must not disturb: a
    chunk that crossed nothing is filed exactly where its section was."""
    section = _placed("Revenue held up.", 0, 3)

    chunks = _chunk([section], chunk_size=1000)

    assert chunks[0].metadata == section.metadata


def test_a_merged_chunk_reports_the_pages_it_actually_covers() -> None:
    """It used to report the first section's page as the chunk's page, which
    is not a smaller truth -- it is a false one: the reader is looking at two
    pages and being told a number that is right about the top of it."""
    chunks = _chunk([_placed("Second quarter.", 0, 3), _placed("Third quarter.", 1, 4)], chunk_size=1000)

    assert len(chunks) == 1, "the two sections merged, which is the point of this chunker"
    assert (chunks[0].metadata["page_number"], chunks[0].metadata["page_end"]) == (3, 4)


def test_a_merged_chunk_drops_a_box_that_would_span_pages() -> None:
    """A rectangle across two sheets of paper is not a location, and drawing
    one would put a highlight somewhere nothing was said."""
    chunks = _chunk([_placed("Second quarter.", 0, 3), _placed("Third quarter.", 1, 4)], chunk_size=1000)

    assert "bbox" not in chunks[0].metadata


def test_a_merged_chunk_keeps_a_box_when_the_parts_share_a_page() -> None:
    chunks = _chunk([_placed("Second quarter.", 0, 3), _placed("Third quarter.", 1, 3)], chunk_size=1000)

    assert chunks[0].metadata["bbox"] == {"x0": 72.0, "x1": 472.0}


def test_every_part_of_a_merged_chunk_says_where_it_came_from() -> None:
    """The list the positional metadata was always going to need once chunks
    could cross: each piece addressed by where it sits in *this* chunk, with
    its own page, box and heading path, so a hit in the middle still resolves
    to the place it was written."""
    chunks = _chunk([_placed("Second quarter.", 0, 3), _placed("Third quarter.", 1, 4)], chunk_size=1000)

    text = chunks[0].content.text
    spans = chunks[0].metadata["elements"]
    assert [text[span["char_start"] : span["char_end"]] for span in spans] == [
        "Second quarter.",
        "Third quarter.",
    ]
    assert [span["page_number"] for span in spans] == [3, 4]
    assert [span["heading_path"] for span in spans] == [["Handbook", "Part 0"], ["Handbook", "Part 1"]]


def test_the_spans_survive_the_overlap_being_prepended() -> None:
    """Overlap puts the previous chunk's tail in front of this one's text, so
    an unshifted span points at the neighbour's words instead of its own."""
    sections = [_placed(f"Part {n} text.", n, n + 1) for n in range(4)]

    chunks = _chunk(sections, chunk_size=6, separator="\n", overlap_size=3)

    merged = next(chunk for chunk in chunks if len(chunk.metadata.get("elements", [])) > 1)
    for span in merged.metadata["elements"]:
        quoted = merged.content.text[span["char_start"] : span["char_end"]]
        assert quoted.startswith("Part ") and quoted.endswith("text."), quoted


def test_a_section_with_no_positions_merges_without_inventing_any() -> None:
    """Plain text has no page and no box, and a merged chunk of it must not
    acquire one."""
    chunks = _chunk([_plain("first section"), _plain("second section")], chunk_size=1000)

    assert "page_number" not in chunks[0].metadata
    assert "bbox" not in chunks[0].metadata
    assert len(chunks[0].metadata["elements"]) == 2, "but it still says what went in"


# -- which section each part came from ------------------------------


def _from_section(text: str, ordinal: int, page: int = 1) -> Section:
    """A section as a structured parser leaves one: identified, and placed."""
    return Section(
        content=TextBlock(text=text),
        source="handbook.docx",
        metadata={
            "reading_order": ordinal,
            "section_ordinal": ordinal,
            "page_number": page,
            "page_end": page,
            "layout_type": "text",
            "heading_path": ["Handbook", f"Part {ordinal}"],
        },
    )


def test_every_part_names_the_section_it_was_cut_from() -> None:
    """`section_ordinal` is *the* section identity -- a heading path is not one,
    because two same-named children of a parent share it. Carried only on the
    chunk, every part after the first was attributed to a section it never came
    from, and a hit widened back to "its" section would be handed the wrong
    text with nothing saying so."""
    chunks = _chunk([_from_section("Third.", 3), _from_section("Seventh.", 7)], chunk_size=1000)

    spans = chunks[0].metadata["elements"]
    assert [span["section_ordinal"] for span in spans] == [3, 7]


def test_a_chunk_spanning_sections_claims_none_of_them() -> None:
    """Because it does not have one. Keeping the first part's is the
    mis-attribution the key exists to prevent, and the parts each carry their
    own for a reader that needs it."""
    chunks = _chunk([_from_section("Third.", 3), _from_section("Seventh.", 7)], chunk_size=1000)

    assert "section_ordinal" not in chunks[0].metadata


def test_a_chunk_built_inside_one_section_keeps_that_section() -> None:
    """The common case, and the one the rule above must not cost: several
    pieces of one section merging is still that section's chunk."""
    section = _with_elements([("First para.", "text"), ("Second para.", "text")])
    section.metadata["section_ordinal"] = 4

    chunks = _chunk([section], chunk_size=1000)

    assert chunks[0].metadata["section_ordinal"] == 4
    assert {span["section_ordinal"] for span in chunks[0].metadata["elements"]} == {4}


def test_a_section_with_no_ordinal_falls_back_to_its_reading_order() -> None:
    """A spreadsheet writes one section per row and no section ordinal, so the
    reading order is the only thing that says which row a piece was. Twenty
    rows in a chunk with no way to tell them apart is the same loss under
    another name."""
    rows = [
        Section(
            content=TextBlock(text=f"Region: R{n}"),
            source="sales.csv",
            metadata={"reading_order": n, "layout_type": "table", "row_idx": n + 2},
        )
        for n in range(3)
    ]

    chunks = _chunk(rows, chunk_size=1000)

    assert [span["section_ordinal"] for span in chunks[0].metadata["elements"]] == [0, 1, 2]


def test_a_section_that_cannot_say_where_it_came_from_is_not_given_a_guess() -> None:
    """A span with no origin is honest about not knowing; one carrying an
    invented ordinal is not."""
    chunks = _chunk([_plain("first"), _plain("second")], chunk_size=1000)

    assert all("section_ordinal" not in span for span in chunks[0].metadata["elements"])


def test_the_origin_does_not_leak_into_a_chunk_that_merged_nothing() -> None:
    """A chunk from one section carries that section's metadata unchanged, and
    tracking where parts came from must not add a key to it."""
    section = _from_section("Alone.", 5)

    chunks = _chunk([section], chunk_size=1000)

    assert chunks[0].metadata == section.metadata
