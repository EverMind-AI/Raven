"""Word documents (``.docx``): the body, in the order Word writes it.

Read straight from the OOXML package with the standard library -- a ``.docx``
is a zip of XML, and walking ``word/document.xml`` is both fewer moving parts
than a document object model and the only way to keep the order: a DOM hands
back paragraphs and tables as two separate collections, and rebuilding which
table fell between which paragraphs from those is guesswork. The body's child
order *is* the reading order, so it is read as one pass. This is also why the
package carries no new dependency for Word support, the way
``StructuredTextParser`` reads HTML without a parser library.

What a section is here: a heading and everything under it, up to the next
heading of the same or higher rank -- the same cut ``StructuredTextParser``
makes in Markdown, so a Word document and its exported Markdown index alike.
Paragraphs, tables and figures are elements *inside* a section, each recorded
in ``metadata["elements"]`` with the character range it occupies, so a hit in
the middle of a long section still resolves to the page and box it came from.

Tables are composed the way RAGFlow's docx parser composes them: the header
rows are lifted onto every body row (``Region: EU;Revenue: 1.2M``) instead of
being stated once at the top. A table is the one structure a chunk boundary can
render meaningless -- cut a grid anywhere below its first line and the rows
below have no column names -- and a self-describing row survives the cut. See
:func:`_compose_table`.

What is not read: headers, footers, footnotes and comments. They are not in the
body, they repeat on every page, and indexing them puts a running title in
front of a real answer.

Vertical positions are absent by nature, not by omission. Word stores page
geometry and paragraph indentation, so a paragraph's horizontal band is exact;
where it lands down the page is decided by a layout engine this parser does not
run. ``BBox`` records the edges it knows and leaves ``top`` / ``bottom``
``None`` -- see :class:`~raven.knowledge.parser.BBox`. The page number is read
rather than computed: Word records where it last rendered a page break, which
is the same evidence RAGFlow's docx parser counts pages from. A file no word
processor has ever paginated -- one a report generator wrote straight to disk
-- carries no such marks, and every section of it is reported on page 1. That
is the file saying it has no pages yet, and it is the honest answer; running a
layout engine to invent them is not something a parser can do.
"""

from __future__ import annotations

import os
import re
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
from xml.etree import ElementTree as ET

from raven.knowledge._sections import SECTION_ORDINAL
from raven.knowledge._types import Section, TextBlock
from raven.knowledge.parser import BBox, ElementSpan, LayoutType, ParserBase, section_metadata

_DOCUMENT_PART = "word/document.xml"
_STYLES_PART = "word/styles.xml"

_TWIPS_PER_POINT = 20.0
_EMU_PER_POINT = 12700.0

# Elements whose subtree carries no body text: formatting, revision marks, and
# the two that would actively corrupt the output -- `w:del` holds text a
# reviewer removed, `w:instrText` the field code behind a rendered result.
_SKIPPED_TAGS = frozenset(
    {
        "pPr",
        "rPr",
        "tblPr",
        "tblGrid",
        "trPr",
        "tcPr",
        "sectPr",
        "del",
        "delText",
        "instrText",
        "proofErr",
        "bookmarkStart",
        "bookmarkEnd",
        "commentRangeStart",
        "commentRangeEnd",
        "commentReference",
        "footnoteRef",
        "endnoteRef",
    }
)

_FIGURE_TAGS = frozenset({"drawing", "pict", "object"})


def _local(tag: object) -> str:
    """The tag name without its namespace.

    Matched by local name throughout because OOXML ships in two namespace
    families -- the transitional one every real file uses and the ISO strict
    one Word can still write -- and a parser keyed on the full URI silently
    reads a strict document as empty.
    """
    text = tag if isinstance(tag, str) else ""
    return text.rsplit("}", 1)[-1]


def _attr(node: ET.Element, name: str) -> str | None:
    """An attribute by local name, whichever namespace it arrived in."""
    for key, value in node.attrib.items():
        if _local(key) == name:
            return value
    return None


def _find(node: ET.Element, name: str) -> ET.Element | None:
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _findall(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _local(child.tag) == name]


def _first_number(node: ET.Element, *names: str) -> float | None:
    """The first of several attribute spellings that carries a number.

    OOXML renamed the indent attributes (``w:left`` / ``w:right`` became
    ``w:start`` / ``w:end``) and files in the wild use either.
    """
    for name in names:
        value = _number(_attr(node, name))
        if value is not None:
            return value
    return None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class _Page:
    """Page geometry from one ``w:sectPr``, in points."""

    width: float = 612.0
    height: float = 792.0
    left: float = 72.0
    right: float = 72.0
    top: float = 72.0
    bottom: float = 72.0


# US Letter with one-inch margins: what Word applies to a document that states
# no geometry of its own.
_DEFAULT_PAGE = _Page()


@dataclass
class _Style:
    """One entry of ``word/styles.xml``, before inheritance is resolved."""

    name: str = ""
    outline: int | None = None
    numbered: bool = False
    based_on: str | None = None


@dataclass
class _Element:
    """One paragraph, table or figure, with where it sits."""

    layout: LayoutType
    text: str
    page_number: int
    page_end: int
    bbox: BBox
    heading_level: int | None = None


@dataclass
class _Block:
    """A heading and the elements running up to the next heading."""

    level: int | None = None
    title: str = ""
    elements: list[_Element] = field(default_factory=list)


class DocxParser(ParserBase):
    """Parse a Word document into heading-bounded sections.

    Stateless between calls: every ``parse`` builds its own reader, so one
    instance is safe to share across concurrent agent runs.
    """

    supported_media_types: list[str] = [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """``.docx`` only -- the legacy binary ``.doc`` is a different format
        with a different media type, and nothing here would read it."""
        return [".docx"]

    async def parse(self, file: bytes | str, filename: str) -> list[Section]:
        """Parse the document body into sections, in document order.

        Args:
            file (`bytes | str`):
                The ``.docx`` payload, or a filesystem path to it.
            filename (`str`):
                The source filename, copied into :attr:`Section.source`.

        Returns:
            `list[Section]`:
                One section per heading, plus one for any content before the
                first heading. A document with no headings comes back as a
                single section, which is what the plain-text path would have
                produced.

        Raises:
            `FileNotFoundError`: If ``file`` is a path that does not exist.
            `ValueError`: If the payload is not a readable Word document --
                a renamed file, a corrupt archive, or one Word encrypted.
        """
        source: str | BytesIO
        if isinstance(file, str):
            if not os.path.isfile(file):
                raise FileNotFoundError(f"{filename!r}: no such file: {file!r}")
            source = file
        else:
            source = BytesIO(file)

        try:
            with zipfile.ZipFile(source) as package:
                document = ET.fromstring(package.read(_DOCUMENT_PART))
                styles = _read_styles(package)
        except KeyError as error:
            raise ValueError(
                f"{filename!r} is not a Word document: the package has no {_DOCUMENT_PART}",
            ) from error
        except (zipfile.BadZipFile, ET.ParseError) as error:
            raise ValueError(f"Failed to read {filename!r} as a Word document: {error}") from error

        body = _find(document, "body")
        elements = list(_Reader(styles).read(body)) if body is not None else []
        return _sections_from(elements, filename)


def _read_styles(package: zipfile.ZipFile) -> dict[str, _Style]:
    """Style id -> style, with ``basedOn`` left unresolved.

    Missing or unreadable styles are not fatal: heading detection falls back to
    the outline level and the style id, both of which live in the document
    part itself.
    """
    try:
        root = ET.fromstring(package.read(_STYLES_PART))
    except (KeyError, ET.ParseError):
        return {}

    styles: dict[str, _Style] = {}
    for node in _findall(root, "style"):
        style_id = _attr(node, "styleId")
        if not style_id:
            continue
        name_node = _find(node, "name")
        paragraph = _find(node, "pPr")
        outline = None
        numbered = False
        if paragraph is not None:
            outline_node = _find(paragraph, "outlineLvl")
            if outline_node is not None:
                value = _number(_attr(outline_node, "val"))
                outline = int(value) if value is not None else None
            numbered = _find(paragraph, "numPr") is not None
        based_on = _find(node, "basedOn")
        styles[style_id] = _Style(
            name=(_attr(name_node, "val") or "") if name_node is not None else "",
            outline=outline,
            numbered=numbered,
            based_on=_attr(based_on, "val") if based_on is not None else None,
        )
    return styles


def _resolved(styles: dict[str, _Style], style_id: str | None) -> tuple[str, int | None, bool]:
    """A style's name, outline level and list-ness, following ``basedOn``.

    Word documents routinely define ``Heading1`` as a chain of derived styles,
    so the outline level that identifies a heading often lives two or three
    ancestors up. The walk is bounded because a broken ``basedOn`` cycle in a
    real file would otherwise hang the parse.
    """
    name, outline, numbered = "", None, False
    seen: set[str] = set()
    current = style_id
    while current and current in styles and current not in seen:
        seen.add(current)
        style = styles[current]
        name = name or style.name
        if outline is None:
            outline = style.outline
        numbered = numbered or style.numbered
        current = style.based_on
    return name, outline, numbered


def _heading_level(name: str, style_id: str, outline: int | None) -> int | None:
    """The heading rank of a paragraph style, or ``None`` for body text.

    ``Title`` is rank 0 so that ``Heading 1`` nests under it rather than
    replacing it, which is how a Word document with a cover title reads.
    """
    flattened = "".join(name.lower().split())
    identifier = "".join(style_id.lower().split())
    if "title" in (flattened, identifier):
        return 0
    for candidate in (flattened, identifier):
        if candidate.startswith("heading") and candidate[7:].isdigit():
            return int(candidate[7:])
    if outline is not None and 0 <= outline <= 8:
        return outline + 1
    return None


def _layout_for(name: str, level: int | None, numbered: bool) -> LayoutType:
    """Map a paragraph style to a layout type.

    Only the distinctions Word actually states are read off; anything else is
    body text. Guessing further from the text itself (a short line must be a
    heading, a line of digits must be a caption) is how a parser starts
    inventing structure the document does not have.
    """
    if level == 0:
        return LayoutType.TITLE
    if level is not None:
        return LayoutType.HEADING
    flattened = name.lower()
    if "caption" in flattened:
        return LayoutType.CAPTION
    if "quote" in flattened:
        return LayoutType.QUOTE
    if "code" in flattened or "preformatted" in flattened:
        return LayoutType.CODE
    if flattened.startswith("toc"):
        return LayoutType.TOC
    if "footnote" in flattened or "endnote" in flattened:
        return LayoutType.FOOTNOTE
    if numbered:
        return LayoutType.LIST_ITEM
    return LayoutType.TEXT


class _Reader:
    """One pass over the body, carrying the page count and page geometry.

    A single object rather than free functions because both pieces of state are
    positional: the page number depends on every break seen so far, and the
    geometry on which ``w:sectPr`` the walk has reached.
    """

    def __init__(self, styles: dict[str, _Style]) -> None:
        self._styles = styles
        self._page = 1
        self._geometry = _DEFAULT_PAGE

    def read(self, body: ET.Element) -> Iterator[_Element]:
        children = list(_flatten(body))
        geometries = _geometry_per_child(children)
        for index, child in enumerate(children):
            self._geometry = geometries[index]
            name = _local(child.tag)
            if name == "p":
                yield from self._paragraph(child)
            elif name == "tbl":
                table = self._table(child)
                if table is not None:
                    yield table
            if _starts_new_page(child):
                self._page += 1

    def _paragraph(self, node: ET.Element) -> list[_Element]:
        """A paragraph as zero or more elements: its text, then its figures.

        Figures come after the paragraph's own text rather than at the exact
        offset they sit at, so that an inline image cannot cut a sentence in
        half. Their order relative to each other, and to every other
        paragraph, is preserved.
        """
        properties = _find(node, "pPr")
        style_id = _style_id(properties)
        name, outline, style_numbered = _resolved(self._styles, style_id)
        level = _heading_level(name, style_id or "", outline)
        numbered = style_numbered or (properties is not None and _find(properties, "numPr") is not None)
        bbox = self._band(properties)

        start_page = self._page
        pieces: list[str] = []
        figures: list[ET.Element] = []
        for kind, payload in _events(node):
            if kind == "text":
                pieces.append(payload)
            elif kind == "figure":
                figures.append(payload)
            else:
                self._page += 1
                # A break before any text of this paragraph moves the whole
                # paragraph onto the next page rather than splitting it.
                if not pieces:
                    start_page = self._page

        elements: list[_Element] = []
        text = "".join(pieces).strip()
        if text:
            layout = _layout_for(name, level, numbered)
            elements.append(
                _Element(
                    layout=layout,
                    text=f"- {text}" if layout is LayoutType.LIST_ITEM else text,
                    page_number=start_page,
                    page_end=self._page,
                    bbox=bbox,
                    heading_level=level,
                )
            )
        elements.extend(self._figure(figure, bbox) for figure in figures)
        return elements

    def _figure(self, node: ET.Element, band: BBox) -> _Element:
        """A drawing as an element, with whatever geometry it declares.

        An anchored drawing states its own offset and size and gets a complete
        box; an inline one states only a size, so it keeps the paragraph's left
        edge and gains a real width. Its text is the alt text a document author
        wrote, or the contents of a text box -- never a placeholder, because a
        placeholder would embed and then match queries about nothing.
        """
        return _Element(
            layout=LayoutType.FIGURE,
            text=_figure_text(node),
            page_number=self._page,
            page_end=self._page,
            bbox=_figure_bbox(node, band, self._geometry),
        )

    def _table(self, node: ET.Element) -> _Element | None:
        """A table as one element, its rows composed against their headers.

        One element and not one per row: the section is the unit that gets
        embedded, and every row already names its own columns, so a chunk
        boundary inside the table costs nothing.
        """
        start_page = self._page
        grid = _pad([self._row(row) for row in _findall(node, "tr")])
        text = _compose_table(grid)
        if not text:
            return None
        return _Element(
            layout=LayoutType.TABLE,
            text=text,
            page_number=start_page,
            page_end=self._page,
            bbox=self._band(None),
        )

    def _row(self, node: ET.Element) -> list[str]:
        """One row as one cell per grid column.

        A horizontally merged cell is repeated across the columns it spans, so
        a header that straddles two columns titles both of them and every row
        below still lines up with it.
        """
        cells: list[str] = []
        for cell in _findall(node, "tc"):
            cells.extend([self._cell(cell)] * _grid_span(cell))
        return cells

    def _cell(self, node: ET.Element) -> str:
        """One cell, paragraphs joined -- and page breaks inside it still counted.

        Joined with a space rather than a newline: a composed row is one line,
        and a cell that broke it would strand the half below its own headers.
        """
        parts: list[str] = []
        for child in _flatten(node):
            name = _local(child.tag)
            if name == "p":
                pieces: list[str] = []
                for kind, payload in _events(child):
                    if kind == "text":
                        pieces.append(payload)
                    elif kind == "break":
                        self._page += 1
                text = "".join(pieces).strip()
                if text:
                    parts.append(text)
            elif name == "tbl":
                nested = self._table(child)
                if nested is not None:
                    parts.append(nested.text)
        return " ".join(parts)

    def _band(self, properties: ET.Element | None) -> BBox:
        """The horizontal band a paragraph occupies, in points.

        Exact, and the reason a docx bbox is worth recording at all: the left
        edge is the margin plus the paragraph's own indent, the right edge is
        the page width less the right margin and indent. Both come from the
        file.
        """
        geometry = self._geometry
        left = geometry.left
        right = geometry.width - geometry.right
        indent = _find(properties, "ind") if properties is not None else None
        if indent is not None:
            start = _first_number(indent, "start", "left")
            end = _first_number(indent, "end", "right")
            if start is not None:
                left += start / _TWIPS_PER_POINT
            if end is not None:
                right -= end / _TWIPS_PER_POINT
        return BBox(x0=round(left, 2), x1=round(right, 2))


#: RAGFlow's cell taxonomy, ported pattern for pattern from
#: `deepdoc.parser.docx_parser`. Only one label carries a decision -- `Nu`, a
#: numeric cell -- but the rest have to keep their own labels, because the
#: decision is which label is the most common and lumping the others together
#: would let them outvote it. The Chinese date and quarter forms are written as
#: escapes so this file stays English source (AGENTS.md section 1.3), the way
#: `scripts/check_source_language.py` writes its own CJK ranges.
_CELL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), label)
    for pattern, label in (
        ("^(20|19)[0-9]{2}[\u5e74/-][0-9]{1,2}[\u6708/-][0-9]{1,2}\u65e5*$", "Dt"),
        ("^(20|19)[0-9]{2}\u5e74$", "Dt"),
        ("^(20|19)[0-9]{2}[\u5e74/-][0-9]{1,2}\u6708*$", "Dt"),
        ("^[0-9]{1,2}[\u6708/-][0-9]{1,2}\u65e5*$", "Dt"),
        ("^\u7b2c*[\u4e00\u4e8c\u4e09\u56db1-4]\u5b63\u5ea6$", "Dt"),
        ("^(20|19)[0-9]{2}\u5e74*[\u4e00\u4e8c\u4e09\u56db1-4]\u5b63\u5ea6$", "Dt"),
        ("^(20|19)[0-9]{2}[ABCDE]$", "DT"),
        ("^[0-9.,+%/ -]+$", "Nu"),
        (r"^[0-9A-Z/\._~-]+$", "Ca"),
        (r"^[A-Z]*[a-z' -]+$", "En"),
        (r"^[0-9.,+-]+[0-9A-Za-z/$\uffe5%<>\uff08\uff09()' -]+$", "NE"),
        ("^.{1}$", "Sg"),
    )
)


def _cell_type(text: str) -> str:
    """Which kind of value a cell holds, in RAGFlow's labels.

    The prose fallback counts whitespace-separated words where RAGFlow counts
    tokenizer output. It cannot tell a person's name from other short prose
    (RAGFlow's `Nr`, which needs a POS tagger), and it under-counts a Chinese
    sentence, which carries no spaces. Neither changes a header decision: those
    labels differ from `Nu` either way, and `Nu` is the only label the caller
    tests for.
    """
    for pattern, label in _CELL_PATTERNS:
        if pattern.search(text):
            return label
    words = [word for word in text.split() if len(word) > 1]
    if len(words) > 3:
        return "Tx" if len(words) < 12 else "Lx"
    return "Ot"


def _pad(grid: list[list[str]]) -> list[list[str]]:
    """Square the grid off, so column `j` means the same thing in every row."""
    width = max((len(row) for row in grid), default=0)
    return [row + [""] * (width - len(row)) for row in grid]


def _compose_table(grid: list[list[str]]) -> str:
    """One line per body row, each cell titled by the headers above it.

    RAGFlow's `__compose_table_content`, kept: header rows are not emitted on
    their own, they are lifted onto each body row as `header: cell`, cells are
    joined with `;`, and a table of fewer than two rows composes to nothing at
    all -- a single row has no header to compose against.

    A table whose body is mostly numeric can carry headers further down as
    well (a rate table restating its columns per quarter), and RAGFlow finds
    them by looking for rows that break the numeric pattern. That test is why
    :func:`_cell_type` exists.

    Not kept: RAGFlow returns the rows as a list when the table has more than
    three columns and as one joined string otherwise, which is a chunking
    decision -- how much of a table lands in one retrievable piece. Here the
    chunker owns that, and this returns the text either way.
    """
    if len(grid) < 2:
        return ""

    body = Counter(_cell_type(cell) for row in grid[1:] for cell in row)
    dominant = max(body.items(), key=lambda item: item[1])[0]

    header_rows = [0]
    if dominant == "Nu":
        for index, row in enumerate(grid[1:], start=1):
            types = Counter(_cell_type(cell) for cell in row)
            if max(types.items(), key=lambda item: item[1])[0] != dominant:
                header_rows.append(index)

    lines: list[str] = []
    for index, row in enumerate(grid):
        if index in header_rows:
            continue
        above = _headers_above(header_rows, index)
        cells: list[str] = []
        for column, cell in enumerate(row):
            if not cell:
                continue
            titles: list[str] = []
            for offset in above:
                title = grid[index + offset][column].strip()
                if title and title not in titles:
                    titles.append(title)
            prefix = ",".join(titles)
            cells.append(f"{prefix}: {cell}" if prefix else cell)
        if cells:
            lines.append(";".join(cells))
    return "\n".join(lines)


def _headers_above(header_rows: list[int], index: int) -> list[int]:
    """Offsets of the header rows a body row answers to, nearest block only.

    Where several header rows stand apart, only the run of consecutive ones
    closest above the body row titles it -- an earlier block belongs to the
    rows that followed it, not to this one.
    """
    offsets = [row - index for row in header_rows if row < index]
    position = len(offsets) - 1
    while position > 0:
        if offsets[position] - offsets[position - 1] > 1:
            return offsets[position:]
        position -= 1
    return offsets


def _grid_span(cell: ET.Element) -> int:
    """How many grid columns a cell occupies."""
    properties = _find(cell, "tcPr")
    span = _find(properties, "gridSpan") if properties is not None else None
    value = _number(_attr(span, "val")) if span is not None else None
    return max(1, int(value)) if value is not None else 1


def _flatten(node: ET.Element) -> Iterator[ET.Element]:
    """Body children with content controls unwrapped.

    A ``w:sdt`` (a content control -- a date picker, a repeating section, a
    citation field) wraps real paragraphs in two layers of markup. Unwrapping
    it here keeps the walk over the body flat.
    """
    for child in node:
        if _local(child.tag) == "sdt":
            content = _find(child, "sdtContent")
            if content is not None:
                yield from _flatten(content)
        else:
            yield child


def _style_id(properties: ET.Element | None) -> str | None:
    if properties is None:
        return None
    style = _find(properties, "pStyle")
    return _attr(style, "val") if style is not None else None


def _events(node: ET.Element) -> Iterator[tuple[str, Any]]:
    """Walk a paragraph in document order, yielding text, breaks and figures.

    Ordered rather than collected because the page number depends on where a
    break falls relative to the text around it, which a set of ``w:t`` nodes
    no longer says.
    """
    for child in node:
        name = _local(child.tag)
        if name in _SKIPPED_TAGS:
            continue
        if name == "t":
            yield "text", child.text or ""
        elif name == "tab":
            yield "text", "\t"
        elif name == "cr":
            yield "text", "\n"
        elif name == "noBreakHyphen":
            yield "text", "-"
        elif name == "br":
            if _attr(child, "type") == "page":
                yield "break", None
            else:
                yield "text", "\n"
        elif name == "lastRenderedPageBreak":
            yield "break", None
        elif name in _FIGURE_TAGS:
            yield "figure", child
        elif name == "AlternateContent":
            # Word writes the same shape twice, once per renderer. Reading both
            # branches would index every text box in the document twice.
            branch = _find(child, "Choice")
            if branch is None:
                branch = _find(child, "Fallback")
            if branch is not None:
                yield from _events(branch)
        else:
            yield from _events(child)


def _figure_text(node: ET.Element) -> str:
    """Alt text, or a text box's own paragraphs, or nothing."""
    for descendant in node.iter():
        if _local(descendant.tag) == "docPr":
            alt = (_attr(descendant, "descr") or "").strip()
            if alt:
                return alt
    parts: list[str] = []
    for descendant in node.iter():
        if _local(descendant.tag) != "txbxContent":
            continue
        for paragraph in _flatten(descendant):
            if _local(paragraph.tag) != "p":
                continue
            text = "".join(payload for kind, payload in _events(paragraph) if kind == "text").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _figure_bbox(node: ET.Element, band: BBox, geometry: _Page) -> BBox:
    """A drawing's box, from the offsets and extent it declares.

    Both are stated in EMU against a named origin (``page`` means the paper
    edge, everything else the text area), so the conversion is arithmetic
    rather than layout. A drawing that positions itself by alignment
    (``wp:align``) rather than by offset states no coordinate at all and keeps
    the paragraph's band.
    """
    extent = next((child for child in node.iter() if _local(child.tag) == "extent"), None)
    width = (_number(_attr(extent, "cx")) or 0.0) / _EMU_PER_POINT if extent is not None else 0.0
    height = (_number(_attr(extent, "cy")) or 0.0) / _EMU_PER_POINT if extent is not None else 0.0

    anchor = next((child for child in node.iter() if _local(child.tag) == "anchor"), None)
    if anchor is None:
        x0 = band.x0
        return BBox(
            x0=x0,
            x1=round(x0 + width, 2) if x0 is not None and width else band.x1,
        )

    x0 = _offset(anchor, "positionH", 0.0, geometry.left)
    top = _offset(anchor, "positionV", 0.0, geometry.top)
    return BBox(
        x0=x0 if x0 is not None else band.x0,
        x1=round(x0 + width, 2) if x0 is not None and width else band.x1,
        top=top,
        bottom=round(top + height, 2) if top is not None and height else None,
    )


def _offset(anchor: ET.Element, axis: str, page_origin: float, text_origin: float) -> float | None:
    position = _find(anchor, axis)
    if position is None:
        return None
    offset = _find(position, "posOffset")
    value = _number((offset.text or "").strip()) if offset is not None and offset.text else None
    if value is None:
        return None
    origin = page_origin if _attr(position, "relativeFrom") == "page" else text_origin
    return round(origin + value / _EMU_PER_POINT, 2)


def _geometry_per_child(children: list[ET.Element]) -> list[_Page]:
    """The page geometry in force at each body child.

    A ``w:sectPr`` describes the section that *ends* at it, so a paragraph
    takes the next one at or after itself, and the body's trailing ``w:sectPr``
    covers everything after the last section break. Resolved in one backward
    pass because a long document has thousands of paragraphs and at most a
    handful of section breaks.
    """
    geometries: list[_Page] = [_DEFAULT_PAGE] * len(children)
    current = _DEFAULT_PAGE
    for index in range(len(children) - 1, -1, -1):
        properties = _section_properties(children[index])
        if properties is not None:
            current = _page_geometry(properties)
        geometries[index] = current
    return geometries


def _section_properties(child: ET.Element) -> ET.Element | None:
    """The ``w:sectPr`` a body child carries, at body or paragraph level."""
    if _local(child.tag) == "sectPr":
        return child
    if _local(child.tag) != "p":
        return None
    properties = _find(child, "pPr")
    return _find(properties, "sectPr") if properties is not None else None


def _page_geometry(properties: ET.Element) -> _Page:
    size = _find(properties, "pgSz")
    margins = _find(properties, "pgMar")

    def twips(node: ET.Element | None, name: str, fallback: float) -> float:
        value = _number(_attr(node, name)) if node is not None else None
        return value / _TWIPS_PER_POINT if value is not None else fallback

    return _Page(
        width=twips(size, "w", _DEFAULT_PAGE.width),
        height=twips(size, "h", _DEFAULT_PAGE.height),
        left=twips(margins, "left", _DEFAULT_PAGE.left),
        right=twips(margins, "right", _DEFAULT_PAGE.right),
        top=twips(margins, "top", _DEFAULT_PAGE.top),
        bottom=twips(margins, "bottom", _DEFAULT_PAGE.bottom),
    )


def _starts_new_page(child: ET.Element) -> bool:
    """Whether a section break here pushes what follows onto a new page.

    Only a paragraph-level ``w:sectPr`` counts: the body's trailing one ends
    the document, and a ``continuous`` break keeps the same page.
    """
    if _local(child.tag) != "p":
        return False
    section = _section_properties(child)
    if section is None:
        return False
    kind = _find(section, "type")
    return kind is None or _attr(kind, "val") != "continuous"


def _sections_from(elements: list[_Element], filename: str) -> list[Section]:
    """Group elements into heading-bounded sections, in document order."""
    blocks: list[_Block] = [_Block()]
    for element in elements:
        if element.heading_level is not None:
            blocks.append(_Block(level=element.heading_level, title=element.text))
        blocks[-1].elements.append(element)

    stack: list[tuple[int, str]] = []
    sections: list[Section] = []
    for block in blocks:
        if block.level is not None:
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.title))

        body = block.elements[1:] if block.level is not None else block.elements
        # A heading whose only content is itself -- a parent holding nothing
        # but subheadings -- would index as a chunk of its own title. The name
        # is not lost: its children carry it in their heading path.
        if not block.elements or (block.level is not None and not body):
            continue

        text, spans = _lay_out(block.elements)
        if not text.strip():
            continue

        path = [title for _, title in stack]
        box = BBox()
        for element in block.elements:
            box = box.union(element.bbox)
        lead = block.elements[0]
        sections.append(
            Section(
                content=TextBlock(text=text),
                source=filename,
                metadata=section_metadata(
                    reading_order=len(sections),
                    layout_type=lead.layout,
                    page_number=lead.page_number,
                    page_end=block.elements[-1].page_end,
                    bbox=box,
                    elements=spans,
                    heading=path[-1] if path else None,
                    heading_path=path or None,
                    heading_level=block.level,
                    **{SECTION_ORDINAL: len(sections)},
                ),
            )
        )
    return sections


def _lay_out(elements: list[_Element]) -> tuple[str, list[ElementSpan]]:
    """Join a section's elements and record where each one landed.

    Consecutive list items are kept one line apart so a list reads as a list;
    everything else is separated by a blank line. ``reading_order`` on a span
    is the element's place in the section, not in the document -- the section's
    own reading order says where the section is, and the two together locate
    the element.

    An element with no text of its own -- an unlabelled image -- still gets a
    span, an empty one at the offset it sat at. Its page and box are what the
    span was for; giving it a placeholder to occupy would put words into the
    document that nobody wrote.
    """
    parts: list[str] = []
    spans: list[ElementSpan] = []
    cursor = 0
    previous: _Element | None = None
    for index, element in enumerate(elements):
        if cursor and element.text:
            separator = (
                "\n"
                if previous is not None
                and previous.layout is LayoutType.LIST_ITEM
                and element.layout is LayoutType.LIST_ITEM
                else "\n\n"
            )
            parts.append(separator)
            cursor += len(separator)
        parts.append(element.text)
        spans.append(
            ElementSpan(
                reading_order=index,
                layout_type=element.layout,
                char_start=cursor,
                char_end=cursor + len(element.text),
                page_number=element.page_number,
                page_end=element.page_end,
                bbox=element.bbox if not element.bbox.is_empty else None,
            )
        )
        cursor += len(element.text)
        previous = element
    return "".join(parts), spans
