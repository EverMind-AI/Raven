"""The PDF parser: the text layer where there is one, a model's eyes where not."""

from __future__ import annotations

import asyncio

import pytest

from raven.knowledge._naive_chunker import NaiveChunker
from raven.knowledge._notes import collecting
from raven.knowledge.parser import ELEMENTS, LAYOUT_TYPE, PAGE_NUMBER, LayoutType
from raven.knowledge.parser.pdf_parser import PdfParser


def _pdf(build) -> bytes:
    """A PDF built page by page, as bytes."""
    import pymupdf

    document = pymupdf.open()
    build(document, pymupdf)
    raw = document.tobytes()
    document.close()
    return raw


def _prose(document, pymupdf, *, heading: str = "", body: "list[str] | None" = None) -> None:
    page = document.new_page()
    at = 80
    if heading:
        page.insert_text(pymupdf.Point(60, at), heading, fontsize=20)
        at += 40
    for paragraph in body or []:
        page.insert_text(pymupdf.Point(60, at), paragraph, fontsize=10)
        at += 24


def _ruled_table(page, pymupdf, rows: "list[list[str]]", *, top: float = 300.0) -> None:
    """A table `find_tables` can see: it keys on the ruling lines."""
    xs = [60 + 140 * n for n in range(len(rows[0]) + 1)]
    ys = [top + 30 * n for n in range(len(rows) + 1)]
    for y in ys:
        page.draw_line(pymupdf.Point(xs[0], y), pymupdf.Point(xs[-1], y))
    for x in xs:
        page.draw_line(pymupdf.Point(x, ys[0]), pymupdf.Point(x, ys[-1]))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            page.insert_text(pymupdf.Point(xs[c] + 6, ys[r] + 20), value, fontsize=11)


class _Vision:
    """A page describer that answers without a network."""

    max_figures = 64

    def __init__(self, text: str = "A scanned invoice for 1,200 euro.", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.seen: list[int] = []

    async def describe(self, image: bytes, *, mime: str = "", context_above: str = "", context_below: str = "") -> str:
        self.seen.append(len(image))
        if self.error is not None:
            raise self.error
        return self.text


def _parse(raw: bytes, model=None, name: str = "report.pdf"):
    return asyncio.run(PdfParser(model).parse(raw, name))


# -- what it claims ------------------------------------------------


def test_it_claims_pdf_and_nothing_else() -> None:
    assert PdfParser.supported_media_types == ["application/pdf"]
    assert PdfParser.supported_extensions() == [".pdf"]


# -- the text layer ------------------------------------------------


def test_a_heading_bounds_a_section() -> None:
    """The same cut the Word and Markdown parsers make, so a document exported
    from one format and indexed from the other lands the same way."""
    raw = _pdf(lambda d, m: _prose(d, m, heading="Quarterly report", body=["Revenue held up.", "Costs did not."]))

    sections = _parse(raw)

    assert len(sections) == 1
    assert sections[0].metadata["heading_path"] == ["Quarterly report"]
    assert "Revenue held up." in sections[0].content.text


def test_two_headings_are_two_sections() -> None:
    def build(document, pymupdf):
        _prose(document, pymupdf, heading="First part", body=["Alpha text here."])
        _prose(document, pymupdf, heading="Second part", body=["Beta text here."])

    sections = _parse(_pdf(build))

    assert [s.metadata["heading_path"] for s in sections] == [["First part"], ["Second part"]]


def test_every_element_says_which_page_it_came_from() -> None:
    """A PDF is read to be cited, and a hit with no page is a hit a reader has
    to go looking for."""

    def build(document, pymupdf):
        _prose(document, pymupdf, heading="Report", body=["Page one text."])
        _prose(document, pymupdf, body=["Page two text."])

    sections = _parse(_pdf(build))

    pages = {span[PAGE_NUMBER] for section in sections for span in section.metadata[ELEMENTS]}
    assert pages == {1, 2}


def test_an_element_carries_the_box_it_was_drawn_in() -> None:
    raw = _pdf(lambda d, m: _prose(d, m, heading="Report", body=["Some prose."]))

    span = _parse(raw)[0].metadata[ELEMENTS][0]

    box = span["bbox"]
    assert box["x0"] > 0 and box["x1"] > box["x0"]
    assert box["bottom"] > box["top"], "measured downward from the top of the page, as a PDF states it"


def test_the_lines_of_a_paragraph_are_joined_into_one() -> None:
    """A PDF breaks a paragraph into a line per printed line. Kept as breaks,
    one sentence indexes as several fragments."""
    long_line = "This sentence is long enough that the page has to break it across two printed lines to fit."
    raw = _pdf(lambda d, m: _prose(d, m, heading="Report", body=[long_line]))

    text = _parse(raw)[0].content.text

    assert long_line.split(".")[0] in text.replace("\n", " ")


# -- tables --------------------------------------------------------


def test_a_table_is_one_element_holding_the_whole_grid() -> None:
    """A PDF states where its cell boundaries are, and `<table>` is the shape
    that keeps them. Flattened to lines a cell that spans two columns has
    nowhere to go, and a header standing over a group of columns has nowhere
    at all -- and a grid is what a model reads a table as."""

    def build(document, pymupdf):
        page = document.new_page()
        page.insert_text(pymupdf.Point(60, 80), "Revenue", fontsize=20)
        _ruled_table(page, pymupdf, [["Region", "Revenue"], ["EU", "1.2M"], ["US", "2.4M"]])

    sections = _parse(_pdf(build))

    tables = [span for s in sections for span in s.metadata[ELEMENTS] if span[LAYOUT_TYPE] == LayoutType.TABLE]
    assert len(tables) == 1
    body = sections[0].content.text
    assert "<table>" in body and "</table>" in body
    assert "<tr><td>Region</td><td>Revenue</td></tr>" in body
    assert "<tr><td>EU</td><td>1.2M</td></tr>" in body


def test_a_header_row_over_a_numeric_body_is_marked_as_one() -> None:
    """Which row is the header is a guess, and RAGFlow's guess is the one kept:
    a row that breaks the body's dominant kind. So it fires where the body is
    numbers and the header is words, and stays quiet on a grid of labels --
    marking an arbitrary row as the header would be worse than marking none."""

    def build(document, pymupdf):
        page = document.new_page()
        page.insert_text(pymupdf.Point(60, 80), "Results", fontsize=20)
        _ruled_table(page, pymupdf, [["Region", "Q1"], ["101", "1.2"], ["202", "2.4"], ["303", "3.6"]])

    body = _parse(_pdf(build))[0].content.text

    assert "<tr><th>Region</th><th>Q1</th></tr>" in body
    assert "<tr><td>101</td><td>1.2</td></tr>" in body


def test_a_table_cell_is_not_also_indexed_as_prose() -> None:
    """The cells are text blocks too. Indexed twice, a search for a value
    answers with a line that has no column name attached."""

    def build(document, pymupdf):
        page = document.new_page()
        page.insert_text(pymupdf.Point(60, 80), "Revenue", fontsize=20)
        _ruled_table(page, pymupdf, [["Region", "Revenue"], ["EU", "1.2M"]])

    text = _parse(_pdf(build))[0].content.text

    assert text.count("1.2M") == 1


# -- a page with no text layer -------------------------------------


def _scanned(document, pymupdf) -> None:
    """A page with a picture on it and no text at all, as a scan is."""
    page = document.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40))
    pixmap.set_rect(pixmap.irect, (240, 240, 240))
    page.insert_image(pymupdf.Rect(60, 60, 400, 400), pixmap=pixmap)


def test_a_page_with_no_text_is_read_by_the_model() -> None:
    """The only path to what a scan says, and the reason there is no OCR
    engine here."""
    vision = _Vision()

    sections = _parse(_pdf(_scanned), vision)

    assert vision.seen, "the page was rendered and sent"
    assert "scanned invoice" in sections[0].content.text
    assert sections[0].metadata[ELEMENTS][0][PAGE_NUMBER] == 1


def test_only_the_pages_that_need_it_are_sent() -> None:
    """A model call per page of a readable document would be a bill for
    nothing: the text layer is already there."""

    def build(document, pymupdf):
        _prose(document, pymupdf, heading="Report", body=["This page has text on it."])
        _scanned(document, pymupdf)

    vision = _Vision()
    sections = _parse(_pdf(build), vision)

    assert len(vision.seen) == 1, "the page with text was not sent"
    assert "scanned invoice" in " ".join(s.content.text for s in sections)


def test_no_vision_model_leaves_a_note_rather_than_a_silent_gap(monkeypatch) -> None:
    """A scanned page indexed as nothing looks exactly like a page that said
    nothing."""
    monkeypatch.setattr("raven.knowledge._vision.load_vision_model", lambda: None)

    with collecting() as notes:
        sections = _parse(_pdf(_scanned))

    assert sections == []
    assert any("no vision model is configured" in n for n in notes)


def test_a_page_the_model_refuses_costs_only_that_page() -> None:
    def build(document, pymupdf):
        _prose(document, pymupdf, heading="Report", body=["This page has text on it."])
        _scanned(document, pymupdf)

    with collecting() as notes:
        sections = _parse(_pdf(build), _Vision(error=RuntimeError("rate limited")))

    assert "This page has text on it." in sections[0].content.text
    assert any("rate limited" in n for n in notes)


# -- what it refuses -----------------------------------------------


def test_bytes_that_are_not_a_pdf_say_so() -> None:
    with pytest.raises(ValueError, match="as a PDF"):
        _parse(b"this is not a pdf at all")


def test_a_password_protected_file_says_which(tmp_path) -> None:
    """Not "could not parse": the reader can do something about a password."""
    import pymupdf

    document = pymupdf.open()
    document.new_page().insert_text(pymupdf.Point(60, 80), "secret")
    path = tmp_path / "locked.pdf"
    document.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    document.close()

    with pytest.raises(ValueError, match="password-protected"):
        asyncio.run(PdfParser().parse(str(path), "locked.pdf"))


def test_a_path_that_is_not_there_is_not_a_parse_failure(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        asyncio.run(PdfParser().parse(str(tmp_path / "gone.pdf"), "gone.pdf"))


# -- end to end ----------------------------------------------------


def test_a_parsed_pdf_chunks_with_its_positions_intact() -> None:
    """The contract the whole thing is for: a chunk of this document can still
    be pointed at the page it came from."""

    def build(document, pymupdf):
        _prose(document, pymupdf, heading="Handbook", body=["Alpha " * 40])
        _prose(document, pymupdf, heading="Appendix", body=["Beta " * 40])

    sections = _parse(_pdf(build))
    chunks = asyncio.run(NaiveChunker(chunk_size=4096).chunk(sections))

    assert len(chunks) == 1, "both sections fit one chunk, which is what merging metadata is for"
    spans = chunks[0].metadata[ELEMENTS]
    assert {span[PAGE_NUMBER] for span in spans} == {1, 2}
    assert chunks[0].metadata["page_number"] == 1
    assert chunks[0].metadata["page_end"] == 2


def test_a_sparse_page_is_not_mistaken_for_a_scan() -> None:
    """A section divider has nine characters on it and no picture. Sent to a
    model, it costs a call to be told what the text layer already said."""

    def build(document, pymupdf):
        page = document.new_page()
        page.insert_text(pymupdf.Point(60, 300), "Chapter 3", fontsize=24)

    vision = _Vision()
    sections = _parse(_pdf(build), vision)

    assert vision.seen == [], "nothing was sent"
    assert "Chapter 3" in sections[0].content.text


def test_a_picture_page_with_a_caption_is_still_read_as_a_scan() -> None:
    """The other half of the rule: a scan routinely carries a watermark or a
    header from whatever produced it, and indexing those instead of the page
    is worse than indexing nothing."""

    def build(document, pymupdf):
        _scanned(document, pymupdf)
        document[0].insert_text(pymupdf.Point(60, 500), "page 1", fontsize=8)

    vision = _Vision()
    sections = _parse(_pdf(build), vision)

    assert vision.seen, "the page was sent"
    assert "scanned invoice" in sections[0].content.text
