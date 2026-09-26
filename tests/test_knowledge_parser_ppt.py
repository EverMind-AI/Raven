"""Unit tests for the slide parser: order, shapes, tables, and one chunk a slide."""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from raven.knowledge._naive_chunker import NaiveChunker
from raven.knowledge._structure import HeadingAwareChunker
from raven.knowledge._types import Section
from raven.knowledge.parser import ATOMIC, ELEMENTS, PAGE_NUMBER, READING_ORDER, LayoutType
from raven.knowledge.parser.ppt_parser import PptParser

_NS = (
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


def _text_shape(text: str, *, x: int = 0, y: int = 0, placeholder: str = "", bullets: bool = False) -> str:
    """One text box on a slide, at a stated point."""
    ph = f'<p:nvSpPr><p:nvPr><p:ph type="{placeholder}"/></p:nvPr></p:nvSpPr>' if placeholder else ""
    frame = f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="100000" cy="50000"/></a:xfrm></p:spPr>'
    paragraphs = "".join(
        f"<a:p>{'<a:pPr lvl="1"><a:buChar char="-"/></a:pPr>' if bullets else ''}<a:r><a:t>{line}</a:t></a:r></a:p>"
        for line in text.split("\n")
    )
    return f"<p:sp>{ph}{frame}<p:txBody>{paragraphs}</p:txBody></p:sp>"


def _table_shape(rows: "list[list[str | tuple[str, str]]]", *, x: int = 0, y: int = 0) -> str:
    """A table shape. A cell given as a pair carries that raw attribute string,
    which is how a merge is written: `gridSpan="2"` on the cell that begins it
    and `hMerge="1"` on each cell it covers."""

    def one(cell: "str | tuple[str, str]") -> str:
        text, marks = cell if isinstance(cell, tuple) else (cell, "")
        return f"<a:tc {marks}><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></a:tc>"

    grid = "".join("<a:tr>" + "".join(one(cell) for cell in row) + "</a:tr>" for row in rows)
    return (
        f'<p:graphicFrame><p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="100000" cy="50000"/></p:xfrm>'
        f"<a:graphic><a:graphicData><a:tbl>{grid}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )


def _deck(slides: list[str], *, order: list[int] | None = None, layouts: dict[str, str] | None = None) -> bytes:
    """A package holding these slides, presented in ``order``.

    ``order`` indexes ``slides`` and defaults to the order given. It exists
    because the running order is the one thing about a deck that cannot be read
    off the part names.
    """
    order = list(range(len(slides))) if order is None else order
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        entries = "".join(f'<p:sldId id="{256 + at}" r:id="rId{at + 2}"/>' for at in order)
        package.writestr(
            "ppt/presentation.xml",
            f"<p:presentation {_NS}><p:sldIdLst>{entries}</p:sldIdLst></p:presentation>",
        )
        rels = "".join(
            f'<Relationship Id="rId{at + 2}" Target="slides/slide{at + 1}.xml" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"/>'
            for at in range(len(slides))
        )
        package.writestr(
            "ppt/_rels/presentation.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Target="slideMasters/slideMaster1.xml" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"/>'
            f"{rels}</Relationships>",
        )
        for at, body in enumerate(slides):
            package.writestr(
                f"ppt/slides/slide{at + 1}.xml",
                f"<p:sld {_NS}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>",
            )
            if layouts:
                package.writestr(
                    f"ppt/slides/_rels/slide{at + 1}.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Target="../slideLayouts/slideLayout1.xml" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"/>'
                    "</Relationships>",
                )
        for name, body in (layouts or {}).items():
            package.writestr(name, f"<p:sldLayout {_NS}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sldLayout>")
    return buffer.getvalue()


def _parse(payload: bytes, name: str = "deck.pptx") -> list[Section]:
    return asyncio.run(PptParser().parse(payload, name))


def _texts(sections: list[Section]) -> list[str]:
    return [section.content.text for section in sections]


# -- what it claims -------------------------------------------------


def test_it_claims_both_deck_formats() -> None:
    assert PptParser.supported_extensions() == [".ppt", ".pptx"]
    assert "application/vnd.ms-powerpoint" in PptParser.supported_media_types


# -- one slide, one section -----------------------------------------


def test_each_slide_is_a_section_numbered_from_one() -> None:
    sections = _parse(_deck([_text_shape("First slide"), _text_shape("Second slide")]))

    assert _texts(sections) == ["First slide", "Second slide"]
    assert [section.metadata[PAGE_NUMBER] for section in sections] == [1, 2]
    assert [section.metadata[READING_ORDER] for section in sections] == [0, 1]


def test_the_running_order_comes_from_the_deck_not_the_filenames() -> None:
    """`slide10.xml` sorts before `slide2.xml`, and a deck whose slides were
    reordered keeps the numbering it was first saved with -- so the part names
    say nothing about what a reader sees first."""
    slides = [_text_shape(f"Slide {n}") for n in range(3)]

    sections = _parse(_deck(slides, order=[2, 0, 1]))

    assert _texts(sections) == ["Slide 2", "Slide 0", "Slide 1"]
    assert [section.metadata[PAGE_NUMBER] for section in sections] == [1, 2, 3]


def test_a_slide_with_nothing_on_it_is_skipped() -> None:
    """A slide that is one picture has nothing to embed, and a chunk of nothing
    is a row no query can match. The pages that remain still name themselves
    correctly, which is what the preview needs."""
    sections = _parse(_deck([_text_shape("First"), "<p:pic/>", _text_shape("Third")]))

    assert _texts(sections) == ["First", "Third"]
    assert [section.metadata[PAGE_NUMBER] for section in sections] == [1, 3]


def test_a_deck_with_no_slides_parses_to_nothing() -> None:
    assert _parse(_deck([])) == []


# -- the order shapes are read in -----------------------------------


def test_shapes_are_read_down_the_slide_then_across() -> None:
    """The order they sit in, not the order the file lists them: a deck writes
    shapes in creation order, and nobody builds a slide top to bottom."""
    body = (
        _text_shape("bottom", x=0, y=4_000_000)
        + _text_shape("top right", x=5_000_000, y=0)
        + _text_shape("top left", x=0, y=0)
    )

    assert _texts(_parse(_deck([body]))) == ["top left\ntop right\nbottom"]


def test_shapes_within_a_line_of_each_other_count_as_one_row() -> None:
    """Two shapes meant to be read side by side are never at exactly the same
    height, and sorting on the exact top puts the right-hand one first whenever
    it sits a rounding error higher."""
    body = _text_shape("right", x=5_000_000, y=0) + _text_shape("left", x=0, y=20_000)

    assert _texts(_parse(_deck([body]))) == ["left\nright"]


def test_a_slide_that_states_no_positions_keeps_its_own_order() -> None:
    body = "".join(_text_shape(word) for word in ("one", "two", "three"))

    assert _texts(_parse(_deck([body]))) == ["one\ntwo\nthree"]


def test_a_placeholder_takes_its_position_from_the_layout() -> None:
    """Most title and body placeholders carry no geometry of their own -- it
    lives in the layout. Read as unplaced, a title sorts after every shape that
    does state a position, which on a real deck is most of them."""
    slide = (
        '<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        "<p:txBody><a:p><a:r><a:t>The title</a:t></a:r></a:p></p:txBody></p:sp>"
    ) + _text_shape("body text", x=0, y=3_000_000)
    layout = (
        '<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="0" y="500000"/><a:ext cx="100000" cy="50000"/></a:xfrm></p:spPr>'
        "<p:txBody><a:p/></p:txBody></p:sp>"
    )

    sections = _parse(_deck([slide], layouts={"ppt/slideLayouts/slideLayout1.xml": layout}))

    assert _texts(sections) == ["The title\nbody text"]


# -- what a shape says ----------------------------------------------


def test_a_bulleted_paragraph_is_marked_and_indented() -> None:
    """RAGFlow's rendering: once the styling is gone a list has to still read
    as a list."""
    sections = _parse(_deck([_text_shape("first\nsecond", bullets=True)]))

    assert sections[0].content.text == "  .first\n  .second"


def test_a_table_is_kept_as_a_grid() -> None:
    """The shape the Word and PDF parsers keep too: a slide states where its
    cell boundaries are, and that is what a flattened table throws away."""
    body = _table_shape([["Region", "Revenue"], ["EU", "1.2M"], ["US", "3.4M"]])

    assert _texts(_parse(_deck([body]))) == [
        "<table>\n"
        "<tr><th>Region</th><th>Revenue</th></tr>\n"
        "<tr><td>EU</td><td>1.2M</td></tr>\n"
        "<tr><td>US</td><td>3.4M</td></tr>\n"
        "</table>"
    ]


def test_a_merged_cell_spans_instead_of_leaving_a_blank() -> None:
    """DrawingML puts the span on the cell that begins the merge and marks the
    cells it covers as carrying nothing. Read straight through, those covered
    cells come out as blanks -- a grid saying the slide left a cell empty,
    which is a different claim from a cell being part of its neighbour."""
    body = _table_shape(
        [
            [("Revenue", 'gridSpan="2"'), ("", 'hMerge="1"')],
            [("EU", 'rowSpan="2"'), "1.2M"],
            [("", 'vMerge="1"'), "1.4M"],
        ]
    )

    text = _texts(_parse(_deck([body])))[0]

    assert '<th colspan="2">Revenue</th>' in text
    assert '<td rowspan="2">EU</td>' in text
    assert "<tr><td>1.4M</td></tr>" in text, "the covered cell is gone, not blank"


def test_a_group_is_read_through() -> None:
    inner = _text_shape("inside one", x=0, y=0) + _text_shape("inside two", x=0, y=1_000_000)
    body = (
        f'<p:grpSp><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></a:xfrm></p:grpSpPr>{inner}</p:grpSp>'
    )

    assert _texts(_parse(_deck([body]))) == ["inside one\ninside two"]


def test_the_title_placeholder_becomes_the_heading_path() -> None:
    body = _text_shape("Quarterly review", placeholder="ctrTitle") + _text_shape("body", y=2_000_000)

    sections = _parse(_deck([body]))

    assert sections[0].metadata["heading_path"] == ["Quarterly review"]


def test_a_slide_with_no_title_carries_no_heading() -> None:
    """Rather than borrowing its first line, which is a caption as often as a
    title and would be cited as one."""
    assert "heading_path" not in _parse(_deck([_text_shape("just some text")]))[0].metadata


# -- where each shape sits ------------------------------------------


def test_every_shape_records_its_own_box_and_page() -> None:
    """Complete boxes, unlike a Word paragraph's: a slide states absolute
    coordinates for everything on it, so a hit can be pointed at."""
    body = _text_shape("top", x=127_000, y=254_000) + _text_shape("under", x=0, y=2_540_000)

    spans = _parse(_deck([body]))[0].metadata[ELEMENTS]

    assert [span["page_number"] for span in spans] == [1, 1]
    # 127000 EMU is 10pt, and the shape is 100000 EMU wide. Rounded because the
    # conversion is a division and the exact tail is not the point.
    box = spans[0]["bbox"]
    assert (round(box["x0"], 3), round(box["top"], 3)) == (10.0, 20.0)
    assert (round(box["x1"], 3), round(box["bottom"], 3)) == (17.874, 23.937)


def test_a_table_span_says_it_is_a_table() -> None:
    body = _text_shape("intro") + _table_shape([["a", "b"], ["1", "2"]], y=1_000_000)

    spans = _parse(_deck([body]))[0].metadata[ELEMENTS]

    assert [span["layout_type"] for span in spans] == [LayoutType.TEXT, LayoutType.TABLE]


def test_the_spans_address_the_section_text() -> None:
    body = _text_shape("first", y=0) + _text_shape("second", y=1_000_000)
    section = _parse(_deck([body]))[0]

    quoted = [section.content.text[span["char_start"] : span["char_end"]] for span in section.metadata[ELEMENTS]]

    assert quoted == ["first", "second"]


# -- one slide, one chunk -------------------------------------------


def test_a_slide_is_marked_as_one_chunk() -> None:
    assert _parse(_deck([_text_shape("anything")]))[0].metadata[ATOMIC] is True


def test_a_long_slide_is_still_one_chunk() -> None:
    """The guarantee the parser exists under. A reader says "slide 12"; half a
    slide is not something they can point at, and the preview beside the list
    can only scroll to a page."""
    body = _text_shape(". ".join(f"sentence {n}" for n in range(200)))
    sections = _parse(_deck([body]))

    chunks = asyncio.run(NaiveChunker(chunk_size=16).chunk(sections))

    assert len(chunks) == 1


def test_two_slides_are_never_merged_into_one_chunk() -> None:
    """The other half: a chunk holding two slides puts one piece in front of a
    viewer that can only show one page."""
    sections = _parse(_deck([_text_shape("first"), _text_shape("second")]))

    chunks = asyncio.run(NaiveChunker(chunk_size=4096).chunk(sections))

    assert [chunk.content.text for chunk in chunks] == ["first", "second"]


def test_a_slide_takes_no_overlap_from_its_neighbours() -> None:
    """Overlap repeats the neighbours at each end, which on a deck would put
    the previous slide's words into this slide's chunk and make the page it
    names a lie about half its text."""
    sections = _parse(_deck([_text_shape(f"slide {n} text") for n in range(3)]))

    chunks = asyncio.run(NaiveChunker(chunk_size=4096, overlap_size=20).chunk(sections))

    assert [chunk.content.text for chunk in chunks] == ["slide 0 text", "slide 1 text", "slide 2 text"]


def test_the_page_travels_to_the_chunk() -> None:
    """Which is what the preview reads to scroll: the chunk has to carry the
    slide number, not merely have been cut from it."""
    sections = _parse(_deck([_text_shape("one"), _text_shape("two"), _text_shape("three")]))

    chunks = asyncio.run(NaiveChunker(chunk_size=4096).chunk(sections))

    assert [chunk.metadata[PAGE_NUMBER] for chunk in chunks] == [1, 2, 3]


def test_the_structural_chunker_keeps_the_slide_whole_too() -> None:
    """Which chunker a base runs is a setting, and a guarantee a parser relies
    on cannot depend on a setting."""
    body = _text_shape(". ".join(f"sentence {n}" for n in range(200)))

    chunks = asyncio.run(HeadingAwareChunker(chunk_size=16, overlap=4).chunk(_parse(_deck([body]))))

    assert len(chunks) == 1


# -- the awkward payloads -------------------------------------------


def test_a_file_that_is_not_a_deck_says_so() -> None:
    with pytest.raises(ValueError, match="slide deck"):
        _parse(b"PK\x03\x04 and then nonsense")


def test_a_zip_with_no_presentation_part_says_so() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("docProps/app.xml", "<Properties/>")

    with pytest.raises(ValueError, match="presentation"):
        _parse(buffer.getvalue())


def test_a_path_is_read_from_disk(tmp_path) -> None:
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(_deck([_text_shape("from disk")]))

    assert _texts(asyncio.run(PptParser().parse(str(deck), "deck.pptx"))) == ["from disk"]


def test_a_missing_path_is_reported_as_missing(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        asyncio.run(PptParser().parse(str(tmp_path / "gone.pptx"), "gone.pptx"))
