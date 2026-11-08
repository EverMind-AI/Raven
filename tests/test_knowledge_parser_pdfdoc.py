"""The one PDF reader: what a page says, what it draws, and what it looks like.

The fixtures are written with PyMuPDF, which is a dev dependency for exactly
this reason -- the reader under test can read a PDF but not draw one, and a
fixture has to be drawn before it can be read.
"""

from __future__ import annotations

import pytest

from raven.knowledge.parser._pdfdoc import (
    Block,
    Line,
    PasswordProtectedError,
    _grouped,
    _lines_of,
    open_document,
)


def _pdf(build) -> bytes:
    """A PDF built page by page, as bytes."""
    import pymupdf

    document = pymupdf.open()
    build(document, pymupdf)
    raw = document.tobytes()
    document.close()
    return raw


def _one(*, text: str = "Hello world", at: float = 100.0, size: float = 11.0) -> bytes:
    def build(document, pymupdf):
        document.new_page().insert_text(pymupdf.Point(60, at), text, fontsize=size)

    return _pdf(build)


def _word(text: str, x0: float, x1: float, top: float = 0.0, bottom: float = 10.0, size: float = 10.0) -> dict:
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom, "size": size}


def _line(text: str, x0: float, top: float, x1: float, bottom: float) -> Line:
    return Line(text=text, size=10.0, x0=x0, top=top, x1=x1, bottom=bottom)


# -- opening -------------------------------------------------------


def test_a_document_opens_from_bytes_and_from_a_path(tmp_path) -> None:
    raw = _one()
    path = tmp_path / "one.pdf"
    path.write_bytes(raw)

    with open_document(raw) as from_bytes, open_document(path) as from_path:
        assert len(from_bytes) == len(from_path) == 1
        assert from_bytes[0].blocks()[0].lines[0].text == from_path[0].blocks()[0].lines[0].text


def test_a_path_that_is_not_there_is_not_a_read_failure(tmp_path) -> None:
    """Told apart because a caller can do something about one of them."""
    with pytest.raises(FileNotFoundError):
        open_document(tmp_path / "gone.pdf")


def test_something_that_is_not_a_pdf_raises(tmp_path) -> None:
    with pytest.raises(Exception) as caught:  # noqa: B017 - the reader's own type, whatever it is
        open_document(b"this is not a pdf at all")
    assert not isinstance(caught.value, PasswordProtectedError)


def test_a_locked_file_is_told_apart_from_a_broken_one(tmp_path) -> None:
    """The reader wraps every cause in one type, so this is recovered from the
    chain underneath it rather than from the exception itself."""
    import pymupdf

    document = pymupdf.open()
    document.new_page().insert_text(pymupdf.Point(60, 80), "secret")
    path = tmp_path / "locked.pdf"
    document.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    document.close()

    with pytest.raises(PasswordProtectedError):
        open_document(path)


def test_the_pages_are_numbered_from_one() -> None:
    def build(document, pymupdf):
        for index in range(3):
            document.new_page().insert_text(pymupdf.Point(60, 100), f"page {index}", fontsize=11)

    with open_document(_pdf(build)) as document:
        assert document.page_count == len(document) == 3
        assert [page.number for page in document] == [1, 2, 3]
        assert document[1].number == 2


# -- what a page says ----------------------------------------------


def test_a_page_knows_how_big_it_is() -> None:
    def build(document, pymupdf):
        document.new_page(width=400, height=250)

    with open_document(_pdf(build)) as document:
        page = document[0]
        assert (page.width, page.height) == (400.0, 250.0)
        assert page.box == (0.0, 0.0, 400.0, 250.0)


def test_the_words_of_one_line_come_back_as_one_line() -> None:
    with open_document(_one(text="Revenue grew in every region")) as document:
        lines = [line for block in document[0].blocks() for line in block.lines]

    assert [line.text for line in lines] == ["Revenue grew in every region"]
    assert lines[0].size == 11.0


def test_a_line_is_cut_where_the_gap_is_too_wide_to_be_a_space() -> None:
    """Both columns of a two-column page share a baseline, so without this they
    read straight across the gutter -- which puts the right-hand column's words
    inside the left-hand column's region."""
    close = _lines_of([_word("left", 60, 90), _word("column", 94, 130)])
    apart = _lines_of([_word("left", 60, 90), _word("right", 300, 340)])

    assert [line.text for line in close] == ["left column"]
    assert [line.text for line in apart] == ["left", "right"]


def test_a_blocks_size_is_the_one_most_of_its_text_is_set_in() -> None:
    """Weighted by how much is set in it, not by how many runs there are: a
    footnote marker is one run and the sentence around it is one run."""
    (line,) = _lines_of([_word("a", 60, 64, size=22.0), _word("long sentence of body text", 66, 200, size=10.0)])

    assert line.size == 10.0


def test_two_lines_of_one_paragraph_are_one_block() -> None:
    first, second = _line("first line", 60, 100, 200, 110), _line("second line", 60, 112, 190, 122)

    assert [[line.text for line in group] for group in _grouped([first, second])] == [["first line", "second line"]]


def test_a_paragraph_break_starts_a_new_block() -> None:
    first, second = _line("a paragraph", 60, 100, 200, 110), _line("another", 60, 160, 190, 170)

    assert len(_grouped([first, second])) == 2


def test_two_columns_are_two_blocks_though_their_lines_interleave() -> None:
    """The lines arrive down the page, so they alternate left, right, left: a
    line compared only with the one before it never finds its own column."""
    lines = [
        _line(f"{side} {index}", x0, 100 + index * 12, x0 + 100, 110 + index * 12)
        for index in range(3)
        for side, x0 in (("left", 60.0), ("right", 300.0))
    ]

    groups = _grouped(lines)

    assert len(groups) == 2
    assert {group[0].text for group in groups} == {"left 0", "right 0"}
    assert all(len(group) == 3 for group in groups)


def test_a_picture_is_a_block_with_nothing_to_read() -> None:
    def build(document, pymupdf):
        page = document.new_page()
        page.insert_text(pymupdf.Point(60, 100), "a caption", fontsize=11)
        page.insert_image(
            pymupdf.Rect(300, 400, 420, 500), pixmap=pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20))
        )

    with open_document(_pdf(build)) as document:
        pictures = [block for block in document[0].blocks() if block.picture]

    assert len(pictures) == 1
    assert pictures[0].lines == ()
    assert pictures[0].box == (300.0, 400.0, 420.0, 500.0)


def test_text_drawn_past_the_edge_of_the_page_is_not_read() -> None:
    """A PDF's content stream is not bounded by its page: a line set too long
    carries on past the edge, and the reader never sees the rest. Read whole it
    comes back as a box wider than the page, which is a box no region of that
    page can contain -- so the layout model, which places text by asking which
    region it fell in, drops it."""
    with open_document(_one(text="Alpha " * 40, size=10.0)) as document:
        page = document[0]
        lines = [line for block in page.blocks() for line in block.lines]

    assert lines, "the part that is printed is still read"
    assert max(line.x1 for line in lines) <= page.width + 1
    assert len(lines[0].text) < len("Alpha " * 40), "and the part that is not is not"


def test_a_page_holds_what_reading_it_cost_until_it_is_released() -> None:
    with open_document(_one()) as document:
        page = document[0]
        first = page.blocks()

        assert page.blocks() is first, "asked twice, assembled once"

        page.release()
        assert page.blocks() is not first, "and read again from scratch after"
        assert [line.text for block in page.blocks() for line in block.lines] == ["Hello world"]


# -- tables --------------------------------------------------------


def test_a_ruled_table_is_found_with_its_cells() -> None:
    def build(document, pymupdf):
        page = document.new_page()
        xs, ys = [60, 180, 300], [220, 250, 280]
        for y in ys:
            page.draw_line(pymupdf.Point(xs[0], y), pymupdf.Point(xs[-1], y))
        for x in xs:
            page.draw_line(pymupdf.Point(x, ys[0]), pymupdf.Point(x, ys[-1]))
        for row in range(2):
            for column in range(2):
                page.insert_text(pymupdf.Point(xs[column] + 8, ys[row] + 20), f"r{row}c{column}", fontsize=11)

    with open_document(_pdf(build)) as document:
        tables = document[0].tables()

    assert len(tables) == 1
    assert tables[0].extract() == [["r0c0", "r0c1"], ["r1c0", "r1c1"]]


def test_a_page_whose_tables_cannot_be_found_still_reads_as_prose() -> None:
    """Table finding is heuristic and occasionally throws on a page whose
    drawing commands it cannot make sense of. One page's tables are not worth
    the document."""

    class _Throws:
        page_number = 1

        def find_tables(self):
            raise RuntimeError("cannot make sense of these drawings")

    from raven.knowledge.parser._pdfdoc import Page

    assert Page(_Throws(), 1).tables() == []


# -- what it looks like --------------------------------------------


def test_a_page_renders_at_the_scale_it_is_asked_for() -> None:
    def build(document, pymupdf):
        document.new_page(width=200, height=100)

    with open_document(_pdf(build)) as document:
        page = document[0]
        for scale in (1, 3):
            image = page.render(scale)
            try:
                assert image.size == (200 * scale, 100 * scale)
                assert image.mode == "RGB", "the models were trained on three channels"
            finally:
                image.close()


def test_a_block_carries_the_box_around_every_line_in_it() -> None:
    block = Block(lines=(), x0=1.0, top=2.0, x1=3.0, bottom=4.0)

    assert block.box == (1.0, 2.0, 3.0, 4.0)
    assert _line("x", 1.0, 2.0, 3.0, 4.0).box == (1.0, 2.0, 3.0, 4.0)
