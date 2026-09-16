"""Unit tests for the plain-text parser and the metadata contract it shares."""

from __future__ import annotations

import asyncio

import pytest

from raven.knowledge.parser import BBox, LayoutType, section_metadata
from raven.knowledge.parser.text_parser import TextParser


def _parse(file: bytes | str, filename: str = "notes.txt"):
    return asyncio.run(TextParser().parse(file, filename))


def test_the_whole_file_is_one_section_at_reading_order_zero():
    """Not split: a chunk never spans two sections, so cutting a text file on
    blank lines would make every paragraph its own chunk."""
    sections = _parse("First paragraph.\n\nSecond paragraph.\n")

    assert len(sections) == 1
    assert sections[0].content.text == "First paragraph.\n\nSecond paragraph.\n"
    assert sections[0].metadata["reading_order"] == 0
    assert sections[0].metadata["layout_type"] == "text"


def test_a_string_is_a_path_when_it_names_one_and_text_otherwise(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("From disk.", encoding="utf-8")

    assert _parse(str(path))[0].content.text == "From disk."
    assert _parse("Not a path.")[0].content.text == "Not a path."


def test_undecodable_bytes_are_refused_by_name():
    with pytest.raises(ValueError, match="notes.txt"):
        _parse(b"\xff\xfe\x00 not utf-8")


def test_metadata_carries_only_what_the_format_knows():
    """A key present with a null would read as a measured absence downstream;
    an omitted key reads as the format not having the answer."""
    metadata = section_metadata(
        reading_order=3,
        layout_type=LayoutType.HEADING,
        bbox=BBox(x0=72.0, x1=540.0),
        page_number=2,
    )

    assert metadata == {
        "reading_order": 3,
        "layout_type": "heading",
        "page_number": 2,
        "bbox": {"x0": 72.0, "x1": 540.0},
    }
    assert section_metadata(reading_order=0, bbox=BBox()) == {"reading_order": 0}


def test_a_box_lifts_to_the_widest_edges_of_its_parts():
    box = BBox(x0=72.0, x1=300.0).union(BBox(x0=108.0, x1=540.0, top=90.0))

    assert box.model_dump(exclude_none=True) == {"x0": 72.0, "x1": 540.0, "top": 90.0}
