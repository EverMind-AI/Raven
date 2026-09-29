"""The table-structure model: what it finds that a line finder cannot.

Skipped whole when the weights are not installed, which is a checkout where
`make fetch-resources` has not run.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.knowledge.parser import ELEMENTS, LAYOUT_TYPE, LayoutType
from raven.knowledge.parser.deepdoc import _onnx, _pipeline
from raven.knowledge.parser.pdf_parser import PdfParser

pytestmark = pytest.mark.skipif(
    not _onnx.available("layout", "tsr"), reason="the deepdoc layout and table models are not installed"
)

_GRID = [
    ["Region", "Q1", "Q2"],
    ["EU", "1.2M", "1.4M"],
    ["US", "2.4M", "2.6M"],
    ["APAC", "0.8M", "1.1M"],
]


def _document(build):
    import pymupdf

    document = pymupdf.open()
    build(document.new_page(), pymupdf)
    raw = document.tobytes()
    document.close()
    return pymupdf.open(stream=raw, filetype="pdf")


def _ruled(page, pymupdf) -> None:
    """A table drawn with its lines, the kind a line finder can see."""
    page.insert_text(pymupdf.Point(60, 70), "Revenue by region", fontsize=18)
    xs, ys = [60, 200, 340, 480], [110, 145, 180, 215, 250]
    for y in ys:
        page.draw_line(pymupdf.Point(xs[0], y), pymupdf.Point(xs[-1], y))
    for x in xs:
        page.draw_line(pymupdf.Point(x, ys[0]), pymupdf.Point(x, ys[-1]))
    for r, row in enumerate(_GRID):
        for c, value in enumerate(row):
            page.insert_text(pymupdf.Point(xs[c] + 8, ys[r] + 23), value, fontsize=11)


def _unruled(page, pymupdf) -> None:
    """The same table set with whitespace, the way a paper sets one."""
    page.insert_text(pymupdf.Point(60, 70), "Revenue by region", fontsize=18)
    for r, row in enumerate(_GRID):
        for c, value in enumerate(row):
            page.insert_text(pymupdf.Point(60 + c * 140, 130 + r * 35), value, fontsize=11)


# -- what the model is asked ---------------------------------------


def test_the_model_finds_the_rows_and_columns_of_an_unruled_table() -> None:
    """The whole argument for the weights. PyMuPDF's own finder looks for
    ruling lines, so a table set with whitespace is invisible to it; the model
    looks at the table the way a reader does."""
    document = _unruled and _document(_unruled)
    page = document[0]

    assert not list(page.find_tables().tables), "no lines to find, which is the point"

    structure = _pipeline._table_model()([_pipeline.render(page)])[0]
    labels = [box["label"] for box in structure]

    assert labels.count("table row") >= len(_GRID)
    assert labels.count("table column") >= len(_GRID[0])


# -- what it buys --------------------------------------------------


def test_a_table_is_rebuilt_as_a_grid() -> None:
    """The model says where the rows and columns are; the text layer says what
    the cells are. Put back together as a table rather than flattened, because
    a grid is what the thing was and what a model reads it as."""
    sections = asyncio.run(PdfParser().parse(_document(_ruled).tobytes(), "revenue.pdf"))

    text = " ".join(section.content.text for section in sections)
    # The column names as a header row: the reader that follows the ruled lines
    # has always said so, and this one says it too.
    assert "<tr><th>Region</th><th>Q1</th><th>Q2</th></tr>" in text
    assert "<tr><td>EU</td><td>1.2M</td><td>1.4M</td></tr>" in text
    kinds = {span[LAYOUT_TYPE] for section in sections for span in section.metadata[ELEMENTS]}
    assert LayoutType.TABLE in kinds


def test_a_cell_carrying_markup_cannot_break_out_of_its_cell() -> None:
    """The text comes off a page, and the table it lands in is indexed, handed
    to a model and rendered in a panel. Upstream interpolates it raw."""

    def build(page, pymupdf):
        page.insert_text(pymupdf.Point(60, 70), "Revenue by region", fontsize=18)
        xs, ys = [60, 200, 340, 480], [110, 145, 180, 215]
        for y in ys:
            page.draw_line(pymupdf.Point(xs[0], y), pymupdf.Point(xs[-1], y))
        for x in xs:
            page.draw_line(pymupdf.Point(x, ys[0]), pymupdf.Point(x, ys[-1]))
        rows = [["Region", "<b>Q1</b>", "Q2"], ["EU & co", "1.2M", "1.4M"], ["US", "2.4M", "2.6M"]]
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                page.insert_text(pymupdf.Point(xs[c] + 8, ys[r] + 23), value, fontsize=11)

    text = " ".join(s.content.text for s in asyncio.run(PdfParser().parse(_document(build).tobytes(), "r.pdf")))

    assert "&lt;b&gt;Q1&lt;/b&gt;" in text
    assert "<b>" not in text
    assert "EU &amp; co" in text


def test_a_cell_is_not_also_indexed_as_a_line_of_prose() -> None:
    """A table's cells are text boxes too. Indexed twice, a search for a figure
    matches both the row that explains it and a bare number that does not."""
    sections = asyncio.run(PdfParser().parse(_document(_ruled).tobytes(), "revenue.pdf"))

    text = " ".join(section.content.text for section in sections)
    assert text.count("2.4M") == 1


# -- when it is not there ------------------------------------------


def test_a_table_the_model_chokes_on_is_read_as_prose(monkeypatch) -> None:
    """Worse than a grid, better than nothing, and not worth the document."""

    def explode():
        raise RuntimeError("the graph refused this crop")

    monkeypatch.setattr(_pipeline, "_table_model", explode)

    sections = asyncio.run(PdfParser().parse(_document(_ruled).tobytes(), "revenue.pdf"))

    text = " ".join(section.content.text for section in sections)
    assert "2.4M" in text, "the cells were still read"
    assert "Region: EU" not in text, "just not as a grid"


def test_an_absent_model_leaves_the_rest_of_the_layout_working(monkeypatch) -> None:
    """The three models are fetched together, but one of them missing is not a
    reason to stop asking the other two."""
    monkeypatch.setattr(_pipeline, "available", lambda *names: "tsr" not in names and _onnx.available(*names))

    sections = asyncio.run(PdfParser().parse(_document(_ruled).tobytes(), "revenue.pdf"))

    assert [s.metadata.get("heading_path") for s in sections] == [["Revenue by region"]]


def test_both_readers_render_one_table_the_same_way(monkeypatch) -> None:
    """The models are optional, so the same document must not index one way on
    a machine that has them and another way on a machine that does not.

    The two readers find a table's grid by different means -- one asks the
    structure model, the other follows the ruled lines -- and they used to
    disagree about the header row: upstream's rule looks for a header REPEATED
    down a long table by spotting a row that breaks the body's dominant cell
    type, which finds nothing at all when the body is words, so the column
    names came back as body cells. A base built on one machine then read
    differently on the other.
    """
    raw = _document(_ruled).tobytes()

    with_models = asyncio.run(PdfParser().parse(raw, "revenue.pdf"))
    monkeypatch.setattr(_pipeline, "available", lambda *names: False)
    without = asyncio.run(PdfParser().parse(raw, "revenue.pdf"))

    def table_of(sections) -> str:
        text = "\n".join(section.content.text for section in sections)
        return text[text.index("<table>") : text.index("</table>") + len("</table>")]

    assert table_of(with_models) == table_of(without)
    assert "<th>" in table_of(without), "the column names are a header row, not body"
