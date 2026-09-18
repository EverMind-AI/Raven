"""Spreadsheets: one section per row, labelled by the header above it.

Ported in shape from RAGFlow's ``deepdoc.parser.excel_parser`` (Apache-2.0; see
NOTICES.md): a row becomes ``header: value; header: value``, because that is
the unit a question is asked about. A whole sheet in one chunk answers "what is
in this file" and nothing else; a bare row answers nothing at all, since the
numbers have lost the columns that named them.

Read with the standard library rather than with a spreadsheet package. An
``.xlsx`` is a zip of XML like a ``.docx``, a ``.csv`` is text, and the one
format that is neither -- the legacy binary ``.xls`` -- is converted the way a
legacy ``.doc`` is, by the LibreOffice the viewer already needs.

What is not read: formatting, formulas (the stored result is used, which is
what a reader sees), and number formats. A date is held in a sheet as a count
of days and displayed by its format, so a date column arrives here as the
number it is stored as. Reading the format table to render it is the obvious
next step and is not done yet.

``html`` and ``markdown`` render a sheet whole instead, for a surface that
wants to show one rather than search it.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from html import escape
from xml.etree import ElementTree as ET

from raven.knowledge._types import Section, TextBlock
from raven.knowledge.parser import LayoutType, ParserBase, section_metadata
from raven.knowledge.parser._office import converted

#: Rows per table in :meth:`ExcelParser.html`. RAGFlow's number: enough that a
#: sheet is a handful of tables rather than hundreds, small enough that one
#: table is still a thing a browser lays out quickly.
CHUNK_ROWS = 256

#: A cell reference is its column letters and its row number: ``BC12``.
_REF = re.compile(r"([A-Z]+)([0-9]+)")

#: Metadata keys. Where a row sits, which is what a reader needs to find it
#: again in the file they uploaded.
SHEET_IDX = "sheet_idx"
SHEET_NAME = "sheet_name"
ROW_IDX = "row_idx"
COL_IDX = "col_idx"
COL_END = "col_end"


@dataclass
class Sheet:
    """One sheet, as a grid of strings."""

    name: str
    rows: list[list[str]] = field(default_factory=list)

    @property
    def header(self) -> list[str]:
        return self.rows[0] if self.rows else []


class ExcelParser(ParserBase):
    """Read a spreadsheet into one section per row."""

    supported_media_types: list[str] = [
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "text/csv",
    ]

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """The three this reads. ``.csv`` is here rather than with the text
        formats because a row of comma-separated values is a row of a table,
        and reading it as prose indexes the commas."""
        return [".csv", ".xls", ".xlsx"]

    def __init__(self, encoding: str = "utf-8") -> None:
        self.encoding = encoding

    async def parse(self, file: bytes | str, filename: str) -> list[Section]:
        """One section per data row, labelled by the header above it.

        Args:
            file (`bytes | str`): The payload, or a path to it.
            filename (`str`): The source filename, carried into each Section.

        Returns:
            `list[Section]`: A row each, in sheet then row order, carrying
                where they came from: the sheet's index and name, the row's
                number as the spreadsheet counts it, and the first and last
                columns that held anything.

        Raises:
            `FileNotFoundError`: If ``file`` is a path that does not exist.
            `ValueError`: If the payload is not a spreadsheet this can read.
        """
        sheets = await self._sheets(file, filename)
        sections: list[Section] = []
        for sheet_idx, sheet in enumerate(sheets):
            if not sheet.rows:
                continue
            # A sheet of one row has no header to label anything with, so that
            # row is the data rather than the labels for data that is not
            # there. RAGFlow drops such a sheet; a row nobody can search is
            # worse than a row with no labels.
            labelled = len(sheet.rows) > 1
            body = sheet.rows[1:] if labelled else sheet.rows
            first_row = 2 if labelled else 1
            for offset, row in enumerate(body):
                text, first_col, last_col = _row_text(row, sheet.header if labelled else [])
                if not text:
                    continue
                sections.append(
                    Section(
                        content=TextBlock(text=_with_sheet(text, sheet.name)),
                        source=filename,
                        metadata=section_metadata(
                            reading_order=len(sections),
                            layout_type=LayoutType.TABLE,
                            **{
                                SHEET_IDX: sheet_idx,
                                SHEET_NAME: sheet.name,
                                ROW_IDX: first_row + offset,
                                COL_IDX: first_col,
                                COL_END: last_col,
                            },
                        ),
                    )
                )
        return sections

    async def html(self, file: bytes | str, filename: str, chunk_rows: int = CHUNK_ROWS) -> list[str]:
        """Each sheet as HTML tables, its header repeated on every one.

        For a surface that shows a sheet rather than searches it. Chunked by
        rows because one table of fifty thousand rows is a page nothing lays
        out, and because the header has to come with each piece or the columns
        below it are unlabelled.
        """
        tables: list[str] = []
        for sheet in await self._sheets(file, filename):
            if not sheet.rows:
                continue
            caption = sheet.name or filename
            head = "<tr>" + "".join(f"<th>{escape(cell)}</th>" for cell in sheet.header) + "</tr>"
            body = sheet.rows[1:]
            for start in range(0, max(len(body), 1), max(1, chunk_rows)):
                piece = body[start : start + chunk_rows]
                if not piece and start:
                    break
                rows = "".join("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in piece)
                tables.append(f"<table><caption>{escape(caption)}</caption>{head}{rows}</table>")
        return tables

    async def markdown(self, file: bytes | str, filename: str) -> str:
        """Every sheet as a markdown table, under its own heading.

        RAGFlow renders the first sheet; a workbook's second sheet is not less
        of the document than its first, and a heading per sheet costs a line.
        """
        out: list[str] = []
        for sheet in await self._sheets(file, filename):
            if not sheet.rows:
                continue
            width = max(len(row) for row in sheet.rows)
            header = _padded(sheet.header, width)
            out.append(f"## {sheet.name or filename}")
            out.append("| " + " | ".join(_escaped(cell) for cell in header) + " |")
            out.append("| " + " | ".join("---" for _ in range(width)) + " |")
            for row in sheet.rows[1:]:
                out.append("| " + " | ".join(_escaped(cell) for cell in _padded(row, width)) + " |")
            out.append("")
        return "\n".join(out).strip()

    # ── reading ───────────────────────────────────────────────────

    async def _sheets(self, file: bytes | str, filename: str) -> list[Sheet]:
        """The workbook as sheets of strings, whatever it arrived as."""
        raw = _payload(file, filename)
        if raw[:4] == b"PK\x03\x04":
            return _xlsx_sheets(raw, filename)
        if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            # The legacy binary workbook, which nothing here reads: converted
            # by the LibreOffice the viewer already needs, then read as the
            # format it became.
            return _xlsx_sheets(await converted(raw, filename, target="xlsx", suffix=".xls"), filename)
        return [_csv_sheet(raw, filename, self.encoding)]


def _payload(file: bytes | str, filename: str) -> bytes:
    import os

    if isinstance(file, str):
        if not os.path.isfile(file):
            raise FileNotFoundError(f"{filename!r}: no such file: {file!r}")
        with open(file, "rb") as handle:
            return handle.read()
    return file


def _csv_sheet(raw: bytes, filename: str, encoding: str) -> Sheet:
    """A separated-values file as one sheet.

    The dialect is sniffed rather than assumed: a ``.csv`` exported by a
    spreadsheet in a locale that writes decimals with a comma is separated by
    semicolons, and reading it as commas gives one column of joined text.
    """
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        # A spreadsheet writing "csv" on Windows still writes cp1252 as often
        # as not, so that is what the fallback decodes -- as itself. Decoding
        # it as UTF-8 with replacement, which is what this used to do, turns
        # every accented byte into U+FFFD before it is indexed: "Cafe" with an
        # acute accent becomes "Caf" and a replacement mark, permanently and
        # without a word anywhere saying so.
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError as error:
            # Neither encoding reads it, so nothing here knows what the bytes
            # say. Refused rather than mangled: a file indexed as its own
            # replacement characters is worse than one that says it could not
            # be read, because only the second can be acted on.
            raise ValueError(
                f"{filename!r} is not readable as {encoding} or cp1252; save it as UTF-8 and upload it again"
            ) from error
    text = text.lstrip("\ufeff")
    sample = text[:4096]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text), dialect) if any(row)]
    # No name: a separated-values file has one sheet and no name for it, and
    # appending the filename to every row would repeat what `Section.source`
    # already carries into each chunk's metadata.
    return Sheet(name="", rows=rows)


def _xlsx_sheets(raw: bytes, filename: str) -> list[Sheet]:
    """Every sheet of an OOXML workbook, in the order the workbook lists them."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            strings = _shared_strings(package)
            sheets = []
            for name, path in _sheet_parts(package):
                sheets.append(Sheet(name=name, rows=_sheet_rows(package, path, strings)))
            return sheets
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise ValueError(f"Failed to read {filename!r} as a spreadsheet: {exc}") from exc


def _local(tag: object) -> str:
    text = tag if isinstance(tag, str) else ""
    return text.rsplit("}", 1)[-1]


def _attr(node: ET.Element, name: str) -> str | None:
    for key, value in node.attrib.items():
        if _local(key) == name:
            return value
    return None


def _text_of(node: ET.Element) -> str:
    """Every ``t`` under a node, joined -- a string split into styled runs is
    still one string."""
    return "".join(part.text or "" for part in node.iter() if _local(part.tag) == "t")


def _shared_strings(package: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(package.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [_text_of(item) for item in root if _local(item.tag) == "si"]


def _sheet_parts(package: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Sheet names with the part each one's rows live in.

    Through the relationships rather than by guessing ``sheet1.xml``: the file
    names follow the order sheets were created, not the order they are shown
    in, and a workbook whose sheets have been reordered reads backwards if the
    numbering is trusted.
    """
    book = ET.fromstring(package.read("xl/workbook.xml"))
    rels = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
    targets = {
        _attr(rel, "Id"): _attr(rel, "Target") for rel in rels if _local(rel.tag) == "Relationship" and _attr(rel, "Id")
    }

    parts: list[tuple[str, str]] = []
    for sheets in (node for node in book.iter() if _local(node.tag) == "sheets"):
        for sheet in sheets:
            name = _attr(sheet, "name") or ""
            target = targets.get(_attr(sheet, "id") or "")
            if not target:
                continue
            path = target.lstrip("/")
            parts.append((name, path if path.startswith("xl/") else f"xl/{path}"))
    return parts


def _sheet_rows(package: zipfile.ZipFile, path: str, strings: list[str]) -> list[list[str]]:
    """One sheet's cells as a rectangle of strings.

    A spreadsheet stores only the cells that hold something, so a row's cells
    are placed by their own references and the gaps filled -- otherwise a row
    with an empty second column reads as if its third value were its second.
    """
    try:
        root = ET.fromstring(package.read(path))
    except KeyError:
        return []

    rows: list[list[str]] = []
    for row in (node for node in root.iter() if _local(node.tag) == "row"):
        cells: dict[int, str] = {}
        for at, cell in enumerate(node for node in row if _local(node.tag) == "c"):
            column = _column_of(_attr(cell, "r"), at)
            value = _cell_value(cell, strings)
            if value:
                cells[column] = value
        if not cells:
            rows.append([])
            continue
        rows.append([cells.get(index, "") for index in range(1, max(cells) + 1)])
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _column_of(reference: str | None, fallback: int) -> int:
    """``C`` is 3. Falls back to the cell's position when there is no
    reference, which a minimal writer may omit."""
    match = _REF.match(reference or "")
    if not match:
        return fallback + 1
    column = 0
    for char in match.group(1):
        column = column * 26 + (ord(char) - 64)
    return column


def _cell_value(cell: ET.Element, strings: list[str]) -> str:
    kind = _attr(cell, "t") or "n"
    if kind == "inlineStr":
        return _text_of(cell).strip()
    value = next((child.text or "" for child in cell if _local(child.tag) == "v"), "")
    if kind == "s":
        try:
            return strings[int(value)].strip()
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if value == "1" else "FALSE"
    # Numbers keep the spelling the sheet stored, minus a trailing ".0" that
    # is an artefact of storing every number as a float: a row counted "3" is
    # not improved by being indexed as "3.0".
    if value.endswith(".0"):
        return value[:-2]
    return value.strip()


def _row_text(row: list[str], header: list[str]) -> tuple[str, int, int]:
    """One row as ``header: value; header: value``, with the columns it spans.

    A value without its column name is a number nobody can ask about, so the
    header travels with it. A column with no header keeps its value alone
    rather than gaining an empty label.
    """
    fields: list[str] = []
    first_col = last_col = 0
    for index, cell in enumerate(row):
        if not cell.strip():
            continue
        column = index + 1
        first_col = column if not first_col else first_col
        last_col = column
        label = header[index].strip() if index < len(header) else ""
        fields.append(f"{label}: {cell}" if label else cell)
    return "; ".join(fields), first_col or 1, last_col or 1


def _with_sheet(text: str, sheet: str) -> str:
    """Name the sheet in the row, unless the name says nothing.

    RAGFlow's rule, and the reason for it is retrieval: a row reading
    ``Region: EU; Revenue: 1.2M`` in a sheet called ``2024 Actuals`` loses the
    year the moment it is embedded, and the vector is all the search has. A
    sheet still called ``Sheet1`` names nothing, so it is left out.
    """
    name = sheet.strip()
    if not name or re.fullmatch(r"sheet\s*\d*", name, re.IGNORECASE):
        return text
    return f"{text} -- {name}"


def _padded(row: list[str], width: int) -> list[str]:
    return list(row) + [""] * (width - len(row))


def _escaped(cell: str) -> str:
    """A pipe in a cell would end the column it is in."""
    return cell.replace("|", "\\|").replace("\n", " ")
