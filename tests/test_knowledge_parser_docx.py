"""Unit tests for the Word parser: order, pages, boxes and layout types."""

from __future__ import annotations

import asyncio
import base64
import io
import zipfile

import pytest

from raven.knowledge._types import Section
from raven.knowledge.parser.docx_parser import DocxParser

_NAMESPACES = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
)

_CONTENT_TYPES = (
    '<?xml version="1.0"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument'
    '.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_PACKAGE_RELS = (
    '<?xml version="1.0"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="word/document.xml" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
    "</Relationships>"
)

_STYLES = f"""<w:styles {_NAMESPACES}>
  <w:style w:styleId="Title"><w:name w:val="Title"/></w:style>
  <w:style w:styleId="Heading1"><w:name w:val="heading 1"/>
    <w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>
  <w:style w:styleId="Heading2"><w:name w:val="heading 2"/>
    <w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>
  <w:style w:styleId="Caption"><w:name w:val="caption"/></w:style>
  <w:style w:styleId="Derived"><w:name w:val="Derived Heading"/>
    <w:basedOn w:val="Heading2"/></w:style>
</w:styles>"""

# US Letter, one-inch margins -- the geometry every box in these tests is
# measured against.
_SECTION = '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:left="1440" w:right="1440" w:top="1440" w:bottom="1440"/></w:sectPr>'


def _docx(body: str, styles: str = _STYLES) -> bytes:
    """A minimal but well-formed package holding ``body`` as the document."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", _CONTENT_TYPES)
        package.writestr("_rels/.rels", _PACKAGE_RELS)
        package.writestr(
            "word/document.xml", f"<w:document {_NAMESPACES}><w:body>{body}{_SECTION}</w:body></w:document>"
        )
        package.writestr("word/styles.xml", styles)
    return buffer.getvalue()


def _p(text: str, style: str | None = None, properties: str = "", runs: str = "") -> str:
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    pPr = f"<w:pPr>{style_xml}{properties}</w:pPr>" if style_xml or properties else ""
    return f"<w:p>{pPr}<w:r><w:t>{text}</w:t></w:r>{runs}</w:p>"


def _table(rows: list[list[str]], spans: dict[tuple[int, int], int] | None = None) -> str:
    """A table from its cell texts, with optional horizontal merges."""
    spans = spans or {}
    xml = []
    for r, row in enumerate(rows):
        cells = []
        for c, cell in enumerate(row):
            span = spans.get((r, c))
            properties = f'<w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>' if span else ""
            cells.append(f"<w:tc>{properties}{_p(cell)}</w:tc>")
        xml.append(f"<w:tr>{''.join(cells)}</w:tr>")
    return f"<w:tbl>{''.join(xml)}</w:tbl>"


def _parse(body: str, filename: str = "report.docx", styles: str = _STYLES) -> list[Section]:
    return asyncio.run(DocxParser().parse(_docx(body, styles), filename))


def _texts(sections: list[Section]) -> list[str]:
    return [section.content.text for section in sections]


def test_the_body_reads_in_the_order_word_wrote_it():
    """The interleaving of paragraphs and tables is the document's own order,
    and it is the thing a parser working from two separate collections loses."""
    body = _p("Before the table.") + _table([["Region", "Revenue"], ["EU", "1.2M"]]) + _p("After the table.")
    sections = _parse(body)

    assert len(sections) == 1
    assert sections[0].metadata["reading_order"] == 0
    assert [element["layout_type"] for element in sections[0].metadata["elements"]] == [
        "text",
        "table",
        "text",
    ]
    assert sections[0].content.text.index("Before") < sections[0].content.text.index("EU")
    assert sections[0].content.text.index("EU") < sections[0].content.text.index("After")


def test_each_heading_opens_a_section_numbered_in_reading_order():
    body = (
        _p("Preamble.")
        + _p("Results", style="Heading1")
        + _p("Overall summary.")
        + _p("Latency", style="Heading2")
        + _p("Tail latency held.")
    )
    sections = _parse(body)

    assert [section.metadata["reading_order"] for section in sections] == [0, 1, 2]
    assert [section.metadata.get("heading") for section in sections] == [None, "Results", "Latency"]
    assert sections[2].metadata["heading_path"] == ["Results", "Latency"]
    assert sections[2].metadata["heading_level"] == 2
    assert sections[1].metadata["layout_type"] == "heading"


def test_a_title_holds_the_headings_under_it():
    """``Title`` outranks ``Heading 1`` so a cover title stays in the path
    rather than being replaced by the first section that follows it."""
    body = _p("Quarterly Report", style="Title") + _p("Scope.") + _p("Results", style="Heading1") + _p("Good.")
    sections = _parse(body)

    assert sections[0].metadata["layout_type"] == "title"
    assert sections[1].metadata["heading_path"] == ["Quarterly Report", "Results"]


def test_a_heading_holding_only_subheadings_is_not_indexed_alone():
    body = _p("Parent", style="Heading1") + _p("Child", style="Heading2") + _p("Body.")
    sections = _parse(body)

    assert [section.metadata["heading"] for section in sections] == ["Child"]
    assert sections[0].metadata["heading_path"] == ["Parent", "Child"]


def test_a_document_with_no_headings_is_one_section():
    sections = _parse(_p("One.") + _p("Two."))

    assert len(sections) == 1
    assert sections[0].content.text == "One.\n\nTwo."
    assert sections[0].metadata["reading_order"] == 0


def test_a_rendered_page_break_moves_what_follows_to_the_next_page():
    body = (
        _p("First page.") + _p("Second page.", runs="<w:r><w:lastRenderedPageBreak/></w:r>") + _p("Also second page.")
    )
    sections = _parse(body)
    elements = sections[0].metadata["elements"]

    assert [element["page_number"] for element in elements] == [1, 1, 2]


def test_a_paragraph_split_by_a_page_break_keeps_the_page_it_starts_on():
    body = (
        "<w:p><w:r><w:t>Opening half.</w:t></w:r><w:r><w:lastRenderedPageBreak/>"
        "<w:t> Closing half.</w:t></w:r></w:p>" + _p("Next.")
    )
    elements = _parse(body)[0].metadata["elements"]

    assert elements[0]["page_number"] == 1
    assert elements[0]["page_end"] == 2
    assert elements[1]["page_number"] == 2


def test_a_section_break_starts_a_new_page():
    body = _p("Front matter.", properties='<w:sectPr><w:type w:val="nextPage"/></w:sectPr>') + _p("Chapter one.")
    elements = _parse(body)[0].metadata["elements"]

    assert [element["page_number"] for element in elements] == [1, 2]


def test_a_continuous_section_break_does_not():
    body = _p("Front matter.", properties='<w:sectPr><w:type w:val="continuous"/></w:sectPr>') + _p("Still page one.")
    elements = _parse(body)[0].metadata["elements"]

    assert [element["page_number"] for element in elements] == [1, 1]


def test_indentation_lands_in_the_horizontal_edges():
    """Letter page, one-inch margins: the text column runs 72pt to 540pt, and a
    720-twip indent moves the left edge half an inch in."""
    body = _p("Flush left.") + _p("Indented.", properties='<w:ind w:left="720" w:right="360"/>')
    elements = _parse(body)[0].metadata["elements"]

    assert elements[0]["bbox"] == {"x0": 72.0, "x1": 540.0}
    assert elements[1]["bbox"] == {"x0": 108.0, "x1": 522.0}


def test_the_vertical_edges_stay_unknown_for_flowing_text():
    """Word stores no ``top`` for a paragraph, and inventing one would be
    indistinguishable from a measured position to anything drawing a highlight."""
    elements = _parse(_p("Body text."))[0].metadata["elements"]

    assert "top" not in elements[0]["bbox"]
    assert "bottom" not in elements[0]["bbox"]


def test_an_anchored_drawing_carries_a_complete_box():
    """Offsets are EMU (12700 per point) from the paper edge when the drawing
    positions itself against the page."""
    drawing = (
        "<w:p><w:r><w:drawing><wp:anchor>"
        '<wp:positionH relativeFrom="page"><wp:posOffset>914400</wp:posOffset></wp:positionH>'
        '<wp:positionV relativeFrom="page"><wp:posOffset>1828800</wp:posOffset></wp:positionV>'
        '<wp:extent cx="2540000" cy="1270000"/>'
        '<wp:docPr id="1" name="Picture 1" descr="Latency over time"/>'
        "</wp:anchor></w:drawing></w:r></w:p>"
    )
    elements = _parse(drawing)[0].metadata["elements"]

    assert elements[0]["layout_type"] == "figure"
    assert elements[0]["bbox"] == {"x0": 72.0, "x1": 272.0, "top": 144.0, "bottom": 244.0}


def test_an_inline_drawing_keeps_the_paragraph_band_and_gains_a_real_width():
    drawing = (
        "<w:p><w:r><w:drawing><wp:inline>"
        '<wp:extent cx="1270000" cy="635000"/>'
        '<wp:docPr id="2" name="Picture 2" descr="Architecture"/>'
        "</wp:inline></w:drawing></w:r></w:p>"
    )
    elements = _parse(drawing)[0].metadata["elements"]

    assert elements[0]["bbox"] == {"x0": 72.0, "x1": 172.0}


def test_a_figure_carries_its_alt_text_and_never_a_placeholder():
    """A placeholder would embed and then match queries about nothing."""
    with_alt = (
        '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Picture 1" descr="Revenue by region"/>'
        "</wp:inline></w:drawing></w:r></w:p>"
    )
    without = '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Picture 1"/></wp:inline></w:drawing></w:r></w:p>'

    assert "Revenue by region" in _texts(_parse(with_alt + _p("Body.")))[0]
    assert _texts(_parse(without + _p("Body.")))[0] == "Body."


def test_a_table_lifts_its_headers_onto_every_row():
    """RAGFlow's composition: the header row is not emitted on its own, it
    titles each cell below it, so a row cut out of the table still reads."""
    body = _table([["Region", "Revenue"], ["EU", "1.2M"], ["APAC", ""]])
    sections = _parse(body)
    elements = sections[0].metadata["elements"]

    assert len(elements) == 1
    assert elements[0]["layout_type"] == "table"
    assert sections[0].content.text == "Region: EU;Revenue: 1.2M\nRegion: APAC"


def test_a_numeric_table_finds_the_headers_further_down_it():
    """A row that breaks the numeric pattern is a header for the rows under it.

    Faithful to RAGFlow down to what it costs: the nearest header block wins
    outright, so a column that block leaves blank loses the title the top row
    gave it.
    """
    body = _table(
        [
            ["Metric", "Q1", "Q2"],
            ["Latency", "120", "118"],
            ["Region EU", "", ""],
            ["Latency", "140", "131"],
        ]
    )

    assert _texts(_parse(body))[0] == "Metric: Latency;Q1: 120;Q2: 118\nRegion EU: Latency;140;131"


def test_a_table_with_no_numeric_body_keeps_only_its_first_row_as_the_header():
    body = _table([["Period", "Revenue"], ["Q1", "EU"], ["2024", "1.2M"]])

    assert _texts(_parse(body))[0] == "Period: Q1;Revenue: EU\nPeriod: 2024;Revenue: 1.2M"


def test_a_merged_header_titles_every_column_it_spans():
    """A header across two columns is repeated over both, so the rows below
    line up with it instead of sliding one column left."""
    body = _table([["Region", "Revenue", ""], ["EU", "1.2M", "0.9M"]], spans={(0, 1): 2})

    assert _texts(_parse(body))[0] == "Region: EU;Revenue: 1.2M;Revenue: 0.9M"


def test_a_single_row_table_composes_to_nothing():
    """RAGFlow drops it: one row has no header row to compose against."""
    sections = _parse(_table([["Just a layout box"]]) + _p("Body."))

    assert _texts(sections) == ["Body."]


def test_an_element_span_locates_a_match_back_to_its_page():
    body = _p("Opening paragraph.") + ("<w:p><w:r><w:lastRenderedPageBreak/><w:t>Closing paragraph.</w:t></w:r></w:p>")
    section = _parse(body)[0]
    text = section.content.text

    hit = text.index("Closing")
    element = next(e for e in section.metadata["elements"] if e["char_start"] <= hit < e["char_end"])
    assert element["page_number"] == 2
    assert text[element["char_start"] : element["char_end"]] == "Closing paragraph."


def test_list_items_read_as_a_list():
    body = _p("Alpha.", properties='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>') + _p(
        "Beta.", properties='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    )
    section = _parse(body)[0]

    assert section.content.text == "- Alpha.\n- Beta."
    assert [element["layout_type"] for element in section.metadata["elements"]] == ["list_item", "list_item"]


def test_a_caption_is_marked_as_one():
    elements = _parse(_p("Figure 1: latency.", style="Caption"))[0].metadata["elements"]

    assert elements[0]["layout_type"] == "caption"


def test_a_heading_style_is_found_through_its_ancestors():
    """Word writes derived heading styles routinely, and the outline level that
    identifies one often sits two styles up the ``basedOn`` chain."""
    sections = _parse(_p("Derived section", style="Derived") + _p("Body."))

    assert sections[0].metadata["heading"] == "Derived section"
    assert sections[0].metadata["heading_level"] == 2


def test_deleted_text_and_field_codes_stay_out():
    body = (
        "<w:p><w:r><w:t>Kept.</w:t></w:r>"
        "<w:del><w:r><w:delText> Removed.</w:delText></w:r></w:del>"
        "<w:r><w:instrText> PAGE \\* MERGEFORMAT </w:instrText></w:r>"
        "</w:p>"
    )

    assert _texts(_parse(body)) == ["Kept."]


def test_a_content_control_does_not_hide_the_paragraph_inside_it():
    body = "<w:sdt><w:sdtContent>" + _p("Inside a content control.") + "</w:sdtContent></w:sdt>"

    assert _texts(_parse(body)) == ["Inside a content control."]


def test_a_shape_written_for_two_renderers_is_read_once():
    """Word stores the same text box twice, once per renderer; reading both
    branches would index every text box in the document twice."""
    box = (
        "<w:p><mc:AlternateContent>"
        "<mc:Choice><w:drawing><wp:inline><w:txbxContent>"
        + _p("Callout text.")
        + "</w:txbxContent></wp:inline></w:drawing></mc:Choice>"
        "<mc:Fallback><w:pict><w:txbxContent>" + _p("Callout text.") + "</w:txbxContent></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:p>"
    )

    assert _texts(_parse(box)) == ["Callout text."]


def test_the_page_geometry_of_the_section_a_paragraph_belongs_to_wins():
    """A ``w:sectPr`` describes the section that ends at it, so a landscape
    first section governs the paragraphs before the break, not after."""
    landscape = '<w:sectPr><w:pgSz w:w="15840" w:h="12240"/><w:pgMar w:left="1440" w:right="1440"/></w:sectPr>'
    body = _p("Wide page.", properties=landscape) + _p("Normal page.")
    elements = _parse(body)[0].metadata["elements"]

    assert elements[0]["bbox"]["x1"] == 720.0
    assert elements[1]["bbox"]["x1"] == 540.0


def test_a_section_reports_the_pages_it_spans_and_the_box_around_its_parts():
    body = (
        _p("Results", style="Heading1")
        + _p("Flush.")
        + '<w:p><w:pPr><w:ind w:left="720"/></w:pPr>'
        + "<w:r><w:lastRenderedPageBreak/><w:t>Indented.</w:t></w:r></w:p>"
    )
    section = _parse(body)[0]

    assert section.metadata["page_number"] == 1
    assert section.metadata["page_end"] == 2
    assert section.metadata["bbox"] == {"x0": 72.0, "x1": 540.0}


def test_a_file_that_is_not_a_word_document_is_refused():
    with pytest.raises(ValueError, match="as a Word document"):
        asyncio.run(DocxParser().parse(b"PK\x03\x04 not really", "report.docx"))

    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as package:
        package.writestr("mimetype", "application/vnd.oasis.opendocument.text")
    with pytest.raises(ValueError, match="not a Word document"):
        asyncio.run(DocxParser().parse(empty.getvalue(), "report.docx"))


def test_a_missing_path_is_reported_as_missing():
    with pytest.raises(FileNotFoundError):
        asyncio.run(DocxParser().parse("/nonexistent/report.docx", "report.docx"))


def test_a_path_is_read_from_disk(tmp_path):
    path = tmp_path / "report.docx"
    path.write_bytes(_docx(_p("From disk.")))

    assert _texts(asyncio.run(DocxParser().parse(str(path), "report.docx"))) == ["From disk."]


def test_the_parser_claims_only_the_word_media_type():
    assert DocxParser.supported_extensions() == [".docx"]
    assert DocxParser.supported_media_types == [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]


def test_a_paragraph_survives_chunking_whole() -> None:
    """The element spans the parser records are what the chunker cuts on, so a
    paragraph is never half in one chunk and half in the next. End to end,
    because the guarantee is only worth anything if both halves agree on where
    the paragraphs are."""
    from raven.knowledge._structure import HeadingAwareChunker

    paragraphs = [f"Paragraph {n}. " + " ".join(f"word{i}" for i in range(60)) for n in range(6)]
    body = _p("Handbook", style="Heading1") + "".join(_p(text) for text in paragraphs)

    sections = _parse(body)
    chunks = asyncio.run(HeadingAwareChunker(chunk_size=200, overlap=20).chunk(sections))

    assert len(chunks) > 1, "the document is too big for one chunk, so this is a real test"
    for paragraph in paragraphs:
        assert sum(paragraph in chunk.text for chunk in chunks) == 1, paragraph[:24]


# ── figures described by a model ──────────────────────────────────

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _Vision:
    """A describer that answers without a network, and records its context."""

    max_figures = 64

    def __init__(self, text: str = "A bar chart.", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[bytes, str, str]] = []

    async def describe(self, image: bytes, *, mime: str = "", context_above: str = "", context_below: str = "") -> str:
        self.calls.append((image, context_above, context_below))
        if self.error is not None:
            raise self.error
        return self.text


def _docx_with_picture(body: str, *, parts: dict[str, bytes] | None = None, rels: str | None = None) -> bytes:
    """A package whose document part is ``body`` and which carries a picture."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", _CONTENT_TYPES)
        package.writestr("_rels/.rels", _PACKAGE_RELS)
        package.writestr(
            "word/document.xml", f"<w:document {_NAMESPACES}><w:body>{body}{_SECTION}</w:body></w:document>"
        )
        package.writestr("word/styles.xml", _STYLES)
        package.writestr(
            "word/_rels/document.xml.rels",
            rels
            if rels is not None
            else (
                '<?xml version="1.0"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId7" Target="media/image1.png" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/>'
                "</Relationships>"
            ),
        )
        for name, payload in (parts or {"word/media/image1.png": _PNG}).items():
            package.writestr(name, payload)
    return buffer.getvalue()


def _picture(rel_id: str = "rId7", alt: str = "") -> str:
    description = f' descr="{alt}"' if alt else ""
    return (
        f'<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Picture 1"{description}/>'
        f'<a:graphic><a:graphicData><a:blip r:embed="{rel_id}"/></a:graphicData></a:graphic>'
        "</wp:inline></w:drawing></w:r></w:p>"
    )


def test_a_figure_is_described_by_the_model() -> None:
    """The point of the whole path: an image in a document has no text, so
    without this the section around it is indexed as if the picture were not
    there."""
    vision = _Vision()

    sections = asyncio.run(DocxParser(vision).parse(_docx_with_picture(_picture()), "report.docx"))

    assert "A bar chart." in sections[0].content.text
    assert vision.calls[0][0] == _PNG, "the picture out of the package, not a placeholder"


def test_the_prose_around_a_figure_goes_with_it() -> None:
    """A figure rarely explains itself: the sentence introducing it names what
    it is of, and without that a model describes a bar chart as a bar chart."""
    vision = _Vision()
    body = _p("Revenue held up in the second quarter.") + _picture() + _p("The third quarter is not in yet.")

    asyncio.run(DocxParser(vision).parse(_docx_with_picture(body), "report.docx"))

    _, above, below = vision.calls[0]
    assert above == "Revenue held up in the second quarter."
    assert below == "The third quarter is not in yet."


def test_the_alt_text_stays_in_front_of_the_description() -> None:
    """One is a label somebody chose and the other is what is in the picture."""
    sections = asyncio.run(DocxParser(_Vision()).parse(_docx_with_picture(_picture(alt="Figure 4")), "report.docx"))

    assert sections[0].content.text.startswith("Figure 4")
    assert "A bar chart." in sections[0].content.text


def test_a_described_figure_is_its_own_chunk() -> None:
    """The span the parser already recorded is what makes it one: a figure is
    never split on a delimiter and never merged with the paragraph beside it."""
    from raven.knowledge._naive_chunker import NaiveChunker

    vision = _Vision(text="The chart shows revenue. " * 200)
    body = _p("Before.") + _picture() + _p("After.")

    sections = asyncio.run(DocxParser(vision).parse(_docx_with_picture(body), "report.docx"))
    chunks = asyncio.run(NaiveChunker(chunk_size=64, image_context_size=0).chunk(sections))

    assert sum(chunk.text.startswith("The chart shows revenue.") for chunk in chunks) == 1


def test_an_undescribed_picture_is_reported_rather_than_only_logged() -> None:
    """The document is indexed either way. What the note carries is that part
    of it is not in the index, which a row saying ready cannot say."""
    from raven.knowledge._notes import collecting

    vision = _Vision(error=RuntimeError("rate limited"))
    body = _p("Revenue held up.") + _picture()

    with collecting() as notes:
        sections = asyncio.run(DocxParser(vision).parse(_docx_with_picture(body), "report.docx"))

    assert sections[0].content.text == "Revenue held up.", "the text still parsed"
    assert "1 of 1 pictures" in notes[0]
    assert "rate limited" in notes[0], "the endpoint's own words, not a category"


def test_an_unconfigured_model_is_reported_too() -> None:
    """The most common of the three, and the one with a fix: a reader who never
    set a vision model has no way to know their figures are being skipped."""
    from raven.knowledge._notes import collecting

    with collecting() as notes:
        asyncio.run(DocxParser(None).parse(_docx_with_picture(_picture()), "report.docx"))

    assert "no vision model is configured" in notes[0]
    assert "Settings" in notes[0]


def test_a_document_with_no_pictures_reports_nothing() -> None:
    """A note on every Word file would be noise, and the mark it draws on the
    row would stop meaning anything."""
    from raven.knowledge._notes import collecting

    with collecting() as notes:
        asyncio.run(DocxParser(None).parse(_docx(_p("Revenue held up.")), "report.docx"))

    assert notes == []


def test_the_figure_cap_says_so_rather_than_stopping_quietly() -> None:
    """Otherwise a capped run looks like a model that had nothing to say about
    the last hundred pictures."""
    from raven.knowledge._notes import collecting

    vision = _Vision()
    vision.max_figures = 2

    with collecting() as notes:
        asyncio.run(DocxParser(vision).parse(_docx_with_picture("".join(_picture() for _ in range(5))), "r.docx"))

    assert "only the first 2 of 5 pictures" in notes[0]


def test_no_vision_model_leaves_the_document_exactly_as_it_was(monkeypatch) -> None:
    """The asymmetry with an uploaded picture, and it is deliberate: a document
    is worth indexing whether or not its figures were described."""
    monkeypatch.setattr("raven.knowledge._vision.load_vision_model", lambda: None)
    body = _p("Revenue held up.") + _picture(alt="Figure 4")

    sections = asyncio.run(DocxParser().parse(_docx_with_picture(body), "report.docx"))

    assert sections[0].content.text == "Revenue held up.\n\nFigure 4"


def test_a_figure_that_cannot_be_described_costs_only_its_description() -> None:
    vision = _Vision(error=RuntimeError("rate limited"))
    body = _p("Revenue held up.") + _picture(alt="Figure 4")

    sections = asyncio.run(DocxParser(vision).parse(_docx_with_picture(body), "report.docx"))

    assert sections[0].content.text == "Revenue held up.\n\nFigure 4"


def test_a_relationship_pointing_nowhere_is_not_a_failure() -> None:
    """A package can name a part it does not hold. The document is still
    readable; that one figure has no picture."""
    vision = _Vision()
    sections = asyncio.run(
        DocxParser(vision).parse(_docx_with_picture(_p("Revenue held up.") + _picture(rel_id="rId99")), "report.docx")
    )

    assert vision.calls == []
    assert sections[0].content.text == "Revenue held up."


def test_an_externally_linked_picture_is_not_fetched() -> None:
    """``r:link`` points at the author's disk or a URL. Neither is in the
    package, and a parser is no place to start fetching one."""
    vision = _Vision()
    rels = (
        '<?xml version="1.0"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId7" Target="https://example.com/chart.png" TargetMode="External" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/>'
        "</Relationships>"
    )

    asyncio.run(DocxParser(vision).parse(_docx_with_picture(_picture(), rels=rels), "report.docx"))

    assert vision.calls == []


def test_the_figure_limit_stops_a_document_costing_a_call_a_picture() -> None:
    """Past the cap a figure keeps whatever text it had. Stopping quietly would
    look like a model that had nothing to say."""
    vision = _Vision()
    vision.max_figures = 2
    body = "".join(_picture(alt=f"Figure {n}") for n in range(5))

    asyncio.run(DocxParser(vision).parse(_docx_with_picture(body), "report.docx"))

    assert len(vision.calls) == 2
