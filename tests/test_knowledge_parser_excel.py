"""Unit tests for the spreadsheet parser: rows, positions, and the two renders."""

from __future__ import annotations

import asyncio
import io
import shutil
import zipfile
from pathlib import Path

import pytest

from raven.knowledge.parser.excel_parser import ExcelParser

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    """A minimal but well-formed workbook, strings written inline.

    Inline rather than shared so the fixture says what a cell holds where the
    cell is; the shared table has its own test.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        entries, rels = [], []
        for index, (name, rows) in enumerate(sheets.items(), start=1):
            entries.append(f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>')
            rels.append(f'<Relationship Id="rId{index}" Target="worksheets/sheet{index}.xml"/>')
            body = ""
            for row_at, row in enumerate(rows, start=1):
                cells = ""
                for col_at, cell in enumerate(row):
                    ref = f"{chr(65 + col_at)}{row_at}"
                    cells += f'<c r="{ref}" t="inlineStr"><is><t>{cell}</t></is></c>' if cell else ""
                body += f'<row r="{row_at}">{cells}</row>'
            package.writestr(
                f"xl/worksheets/sheet{index}.xml",
                f'<worksheet xmlns="{_MAIN}"><sheetData>{body}</sheetData></worksheet>',
            )
        package.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_RELS}"><sheets>{"".join(entries)}</sheets></workbook>',
        )
        package.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels)
            + "</Relationships>",
        )
    return buffer.getvalue()


def _parse(payload: bytes | str, filename: str = "book.xlsx"):
    return asyncio.run(ExcelParser().parse(payload, filename))


def _texts(sections) -> list[str]:
    return [section.content.text for section in sections]


# -- a row is the unit ----------------------------------------------


def test_a_row_carries_the_header_that_names_its_values() -> None:
    """A value without its column name is a number nobody can ask about."""
    book = _xlsx({"Sheet1": [["Region", "Revenue"], ["EU", "1.2M"], ["APAC", "0.9M"]]})

    assert _texts(_parse(book)) == ["Region: EU; Revenue: 1.2M", "Region: APAC; Revenue: 0.9M"]


def test_a_row_says_where_it_came_from() -> None:
    """Sheet, row and the columns it spans -- what a reader needs to find it
    again in the file they uploaded."""
    book = _xlsx({"Sheet1": [["A", "B", "C"], ["", "two", "three"]]})

    metadata = _parse(book)[0].metadata

    assert metadata["sheet_idx"] == 0
    assert metadata["sheet_name"] == "Sheet1"
    assert metadata["row_idx"] == 2, "the row number the spreadsheet shows, not the offset"
    assert metadata["col_idx"] == 2 and metadata["col_end"] == 3


def test_an_empty_cell_does_not_shift_the_ones_after_it() -> None:
    """A sheet stores only the cells that hold something, so the gaps have to
    be put back or a row's third value reads as its second."""
    book = _xlsx({"Sheet1": [["A", "B", "C"], ["one", "", "three"]]})

    assert _texts(_parse(book)) == ["A: one; C: three"]


def test_a_column_with_no_header_keeps_its_value_alone() -> None:
    book = _xlsx({"Sheet1": [["A"], ["one", "loose"]]})

    assert _texts(_parse(book)) == ["A: one; loose"]


def test_every_sheet_is_read_and_numbered() -> None:
    book = _xlsx({"First": [["A"], ["one"]], "Second": [["B"], ["two"]]})

    sections = _parse(book)

    assert [s.metadata["sheet_idx"] for s in sections] == [0, 1]
    assert [s.metadata["sheet_name"] for s in sections] == ["First", "Second"]


def test_a_named_sheet_travels_with_the_row() -> None:
    """A row reading `Region: EU` in a sheet called `2024 Actuals` loses the
    year the moment it is embedded, and the vector is all the search has."""
    book = _xlsx({"2024 Actuals": [["Region"], ["EU"]]})

    assert _texts(_parse(book)) == ["Region: EU -- 2024 Actuals"]


def test_a_sheet_still_called_sheet1_names_nothing() -> None:
    for name in ("Sheet1", "sheet 2", "Sheet"):
        book = _xlsx({name: [["Region"], ["EU"]]})
        assert _texts(_parse(book)) == ["Region: EU"], name


def test_a_sheet_of_one_row_is_read_as_data() -> None:
    """It has no header to label anything with. RAGFlow drops such a sheet; a
    row nobody can search is worse than a row with no labels."""
    book = _xlsx({"Sheet1": [["alpha", "beta"]]})

    assert _texts(_parse(book)) == ["alpha; beta"]
    assert _parse(book)[0].metadata["row_idx"] == 1


def test_a_shared_string_is_read_from_its_table() -> None:
    """The common case in a real workbook: repeated text is stored once."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_RELS}"><sheets>'
            '<sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        package.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        package.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="{_MAIN}"><si><t>Region</t></si><si><r><t>E</t></r><r><t>U</t></r></si></sst>',
        )
        package.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{_MAIN}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>1</v></c></row>'
            "</sheetData></worksheet>",
        )

    assert _texts(_parse(buffer.getvalue())) == ["Region: EU"], "a string split into runs is still one string"


def test_numbers_keep_the_spelling_the_sheet_stored() -> None:
    """A count of 3 is not improved by being indexed as 3.0."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_RELS}"><sheets>'
            '<sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        package.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        package.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{_MAIN}"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Count</t></is></c></row>'
            '<row r="2"><c r="A2"><v>3.0</v></c></row>'
            '<row r="3"><c r="A3" t="b"><v>1</v></c></row>'
            "</sheetData></worksheet>",
        )

    assert _texts(_parse(buffer.getvalue())) == ["Count: 3", "Count: TRUE"]


# -- separated values -----------------------------------------------


def test_a_csv_is_read_as_the_table_it_is() -> None:
    body = b"Region,Revenue\nEU,1.2M\nAPAC,0.9M\n"

    assert _texts(_parse(body, "book.csv")) == ["Region: EU; Revenue: 1.2M", "Region: APAC; Revenue: 0.9M"]


def test_a_csv_row_is_not_labelled_with_its_filename() -> None:
    """`Section.source` already carries it into every chunk."""
    assert "book.csv" not in _texts(_parse(b"A,B\n1,2\n", "book.csv"))[0]


def test_semicolons_are_a_separator_where_a_locale_says_so() -> None:
    """A spreadsheet in a locale that writes decimals with a comma exports
    semicolons, and reading it as commas gives one column of joined text."""
    body = b"Region;Revenue\nEU;1,2\n"

    assert _texts(_parse(body, "book.csv")) == ["Region: EU; Revenue: 1,2"]


def test_a_byte_order_mark_is_not_part_of_the_first_header() -> None:
    body = "﻿Region,Revenue\nEU,1.2M\n".encode()

    assert _texts(_parse(body, "book.csv")) == ["Region: EU; Revenue: 1.2M"]


# -- the two renders, kept for a surface that shows a sheet ----------


def test_markdown_renders_every_sheet_under_its_own_heading() -> None:
    book = _xlsx({"First": [["A", "B"], ["1", "2"]], "Second": [["C"], ["3"]]})

    out = asyncio.run(ExcelParser().markdown(book, "book.xlsx"))

    assert "## First" in out and "## Second" in out
    assert "| A | B |" in out and "| 1 | 2 |" in out


def test_markdown_escapes_a_pipe_that_would_end_a_column() -> None:
    book = _xlsx({"Sheet1": [["A"], ["one|two"]]})

    assert "one\\|two" in asyncio.run(ExcelParser().markdown(book, "book.xlsx"))


def test_html_repeats_the_header_on_every_chunk() -> None:
    """A table of fifty thousand rows is a page nothing lays out, and a piece
    without the header is a grid of unlabelled values."""
    rows = [["A", "B"]] + [[str(n), "x"] for n in range(5)]
    book = _xlsx({"Sheet1": rows})

    tables = asyncio.run(ExcelParser().html(book, "book.xlsx", chunk_rows=2))

    assert len(tables) == 3
    assert all("<th>A</th><th>B</th>" in table for table in tables)
    assert all(table.startswith("<table><caption>Sheet1</caption>") for table in tables)


def test_html_escapes_what_would_otherwise_be_markup() -> None:
    """The cell below holds the three characters `<b>` -- the fixture writes
    them as XML entities because that is how a cell holds them. What comes back
    must be escaped again, or a spreadsheet decides what the page renders."""
    book = _xlsx({"Sheet1": [["A"], ["&lt;b&gt;"]]})

    table = asyncio.run(ExcelParser().html(book, "book.xlsx"))[0]

    assert "<td>&lt;b&gt;</td>" in table
    assert "<td><b></td>" not in table


# -- what it claims, and what it refuses ----------------------------


def test_the_parser_claims_the_three_spreadsheet_types() -> None:
    assert ExcelParser.supported_extensions() == [".csv", ".xls", ".xlsx"]
    assert "text/csv" in ExcelParser.supported_media_types


def test_a_missing_path_is_reported_as_missing() -> None:
    with pytest.raises(FileNotFoundError):
        _parse("/nonexistent/book.xlsx")


def test_a_zip_that_is_not_a_workbook_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("mimetype", "application/zip")

    with pytest.raises(ValueError, match="as a spreadsheet"):
        _parse(buffer.getvalue())


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice is not installed here")
def test_a_legacy_workbook_is_converted_and_read(tmp_path) -> None:
    """The one format with no reader here. Against the real converter, because
    the stubs elsewhere prove the wiring and only this proves the argv."""
    from raven.utils.office import convert, find_soffice

    (tmp_path / "book.csv").write_text("Region,Revenue\nEU,1.2M\n")
    staged = tmp_path / "out"
    staged.mkdir()
    convert(tmp_path / "book.csv", staged, executable=find_soffice(), timeout_s=120.0, target="xls")
    legacy = next(Path(staged).glob("*.xls"))

    assert _texts(_parse(legacy.read_bytes(), "book.xls")) == ["Region: EU; Revenue: 1.2M -- book"]
