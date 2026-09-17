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

    chunks = _chunk([section], chunk_size=1000)

    assert _texts(chunks) == ["Before.", "Region | EU", "After."]


def test_a_figure_is_its_own_chunk_too() -> None:
    section = _with_elements([("Before.", "text"), ("Latency over time", "figure")])

    assert _texts(_chunk([section], chunk_size=1000)) == ["Before.", "Latency over time"]


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


def test_context_is_off_by_default() -> None:
    section = _with_elements([("Around it.", "text"), ("Region | EU", "table")])

    table = next(text for text in _texts(_chunk([section], chunk_size=1000)) if "Region" in text)

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
