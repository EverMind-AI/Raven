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

Tables are kept as grids, written out as HTML. Word states where its cell
boundaries are and which cells are merged, and that is exactly what a flattened
table throws away: a header standing over two columns has nowhere to go, and a
cell running down three rows is either repeated or lost. A table is also the
one structure a chunk boundary can render meaningless, so it is never split --
it is one element, and the chunker treats it as one piece. See
:func:`raven.knowledge.parser._tables.html_table`.

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

import asyncio
import os
import posixpath
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

from loguru import logger

from raven.knowledge._notes import note
from raven.knowledge._types import Section
from raven.knowledge.parser import BBox, LayoutType, ParserBase
from raven.knowledge.parser._layout import Element, sections_from
from raven.knowledge.parser._tables import Cell, html_table

if TYPE_CHECKING:
    from raven.knowledge._vision import VisionModel

_DOCUMENT_PART = "word/document.xml"
_STYLES_PART = "word/styles.xml"
_RELS_PART = "word/_rels/document.xml.rels"

#: How many figures are described at once. A document is one item in an
#: indexing queue that runs documents one at a time, so this is the whole
#: concurrency against the endpoint -- low enough to stay under a rate limit a
#: reader did not raise, high enough that a report with thirty figures is not
#: thirty round trips end to end.
_FIGURE_CONCURRENCY = 4

#: Characters of the prose either side of a figure sent with it. About a
#: paragraph: enough to name what the picture is of, short enough that the
#: picture stays the subject of the call.
_FIGURE_CONTEXT_CHARS = 600

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


class DocxParser(ParserBase):
    """Parse a Word document into heading-bounded sections.

    Stateless between calls: every ``parse`` builds its own reader, so one
    instance is safe to share across concurrent agent runs.
    """

    supported_media_types: list[str] = [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]

    def __init__(self, model: "VisionModel | None" = None) -> None:
        """
        Args:
            model (`VisionModel | None`):
                What describes the pictures in the document. ``None`` (the
                default) resolves the configured ``vision`` pin per parse, so
                configuring one reaches the next upload without a restart.
                Unset there too, the document still parses -- a figure keeps
                whatever its author wrote as alt text, which is what this
                parser did before it could see at all.
        """
        self._model = model

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
                body = _find(document, "body")
                elements = list(_Reader(styles).read(body)) if body is not None else []
                # Read inside the package, before anything is awaited: the
                # descriptions are fetched concurrently below, and several
                # coroutines reading one ZipFile is not a thing to rely on.
                pictures = _pictures(package, elements)
        except KeyError as error:
            raise ValueError(
                f"{filename!r} is not a Word document: the package has no {_DOCUMENT_PART}",
            ) from error
        except (zipfile.BadZipFile, ET.ParseError) as error:
            raise ValueError(f"Failed to read {filename!r} as a Word document: {error}") from error

        await _describe_figures(elements, pictures, filename, model=self._model)
        return sections_from(elements, filename)


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

    def read(self, body: ET.Element) -> Iterator[Element]:
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

    def _paragraph(self, node: ET.Element) -> list[Element]:
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

        elements: list[Element] = []
        text = "".join(pieces).strip()
        if text:
            layout = _layout_for(name, level, numbered)
            elements.append(
                Element(
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

    def _figure(self, node: ET.Element, band: BBox) -> Element:
        """A drawing as an element, with whatever geometry it declares.

        An anchored drawing states its own offset and size and gets a complete
        box; an inline one states only a size, so it keeps the paragraph's left
        edge and gains a real width. Its text is the alt text a document author
        wrote, or the contents of a text box -- never a placeholder, because a
        placeholder would embed and then match queries about nothing.
        """
        return Element(
            layout=LayoutType.FIGURE,
            text=_figure_text(node),
            page_number=self._page,
            page_end=self._page,
            bbox=_figure_bbox(node, band, self._geometry),
            ref=_figure_rel_id(node),
        )

    def _table(self, node: ET.Element) -> Element | None:
        """A table as one element, kept as a grid.

        One element and not one per row: the section is the unit that gets
        embedded, and a chunk boundary inside a table would cut a grid in half.

        A single-row table is not a table. Word documents use one-row tables as
        layout boxes -- a bordered callout, a header band -- far more often than
        as data, and indexing those as tables puts furniture in the index
        wearing a grid's clothes. That rule came from `compose_table`, which
        answered nothing for a table it could find no header in; it is stated
        here now, because HTML would happily render one.
        """
        start_page = self._page
        rows = [self._row(row) for row in _findall(node, "tr")]
        if len(rows) < 2:
            return None
        text = html_table(_spanned(rows))
        if not text:
            return None
        return Element(
            layout=LayoutType.TABLE,
            text=text,
            page_number=start_page,
            page_end=self._page,
            bbox=self._band(None),
        )

    def _row(self, node: ET.Element) -> "list[_TableCell]":
        """One row, each cell carrying what it spans and whether it continues.

        Not one entry per grid column: a cell that straddles two columns is one
        cell with a `colspan` of two, because HTML can say that. The flat form
        this used to produce had to repeat it instead -- there was nowhere to
        record a span -- and repeating it into a grid that can say it once
        would put the same value in two cells.
        """
        return [
            _TableCell(text=self._cell(cell), colspan=_grid_span(cell), continues=_merges_up(cell))
            for cell in _findall(node, "tc")
        ]

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


@dataclass(frozen=True)
class _TableCell:
    """One cell as the reader found it, before the rows are stitched together.

    ``continues`` is a vertical merge that is not its own start: Word writes
    the text once, in the cell that begins the merge, and marks every cell
    below it as a continuation carrying nothing. Which is why it cannot be
    resolved a row at a time -- the row underneath has to reach the row above.
    """

    text: str
    colspan: int = 1
    continues: bool = False


def _spanned(rows: "list[list[_TableCell]]") -> "list[list[Cell]]":
    """The rows with vertical merges folded into the cell that begins them.

    A continuation cell is dropped and the cell it continues grows a
    ``rowspan``. Matched by grid column rather than by position in the row,
    because a row with a merge in it has fewer cells than the grid has columns
    and counting cells would drift one column further right on every row.
    """
    out: list[list[Cell]] = []
    started: dict[int, tuple[int, int]] = {}
    for row in rows:
        built: list[Cell] = []
        column = 0
        for cell in row:
            if cell.continues and column in started:
                at, index = started[column]
                held = out[at][index]
                out[at][index] = Cell(held.text, held.colspan, held.rowspan + 1)
            else:
                started[column] = (len(out), len(built))
                built.append(Cell(cell.text, cell.colspan))
            column += cell.colspan
        out.append(built)
    return out


def _merges_up(cell: ET.Element) -> bool:
    """Whether this cell continues a vertical merge begun above it.

    ``w:vMerge`` with no ``w:val``, or with ``continue``, is a continuation;
    ``restart`` begins one and is an ordinary cell as far as this is concerned.
    """
    properties = _find(cell, "tcPr")
    merge = _find(properties, "vMerge") if properties is not None else None
    if merge is None:
        return False
    return (_attr(merge, "val") or "continue").lower() != "restart"


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


def _figure_rel_id(node: ET.Element) -> str:
    """Which picture a drawing draws, as the relationship id it names.

    ``a:blip/@r:embed`` for an embedded picture, and nothing for a drawing that
    is not one -- a text box, a chart, a shape. ``r:link`` is deliberately not
    read: it points at a file on the author's disk or a URL, neither of which
    is in the package, so there would be nothing to describe.
    """
    for descendant in node.iter():
        if _local(descendant.tag) != "blip":
            continue
        embedded = _attr(descendant, "embed")
        if embedded:
            return embedded
    return ""


def _read_relationships(package: zipfile.ZipFile) -> dict[str, str]:
    """Relationship id -> the part it points at, as a package path.

    Targets are written relative to the part that declares them, which for
    ``word/document.xml`` means relative to ``word/``. An absolute target (one
    written with a leading slash) is already a package path and keeps its
    shape without it.
    """
    try:
        root = ET.fromstring(package.read(_RELS_PART))
    except (KeyError, ET.ParseError):
        return {}

    targets: dict[str, str] = {}
    for node in root:
        if _local(node.tag) != "Relationship":
            continue
        rel_id, target = _attr(node, "Id"), _attr(node, "Target")
        if not rel_id or not target or (_attr(node, "TargetMode") or "") == "External":
            continue
        targets[rel_id] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(f"word/{target}")
    return targets


def _pictures(package: zipfile.ZipFile, elements: list[Element]) -> dict[int, bytes]:
    """The bytes behind each figure, keyed by its index in ``elements``.

    Read eagerly for the figures the walk actually found, rather than for every
    image part in the package: a Word file carries the header logo and every
    icon a theme ships, and none of them is in the body.
    """
    wanted = [(index, element.ref) for index, element in enumerate(elements) if element.ref]
    if not wanted:
        return {}
    targets = _read_relationships(package)
    pictures: dict[int, bytes] = {}
    for index, rel_id in wanted:
        part = targets.get(rel_id)
        if not part:
            continue
        try:
            pictures[index] = package.read(part)
        except KeyError:
            # A relationship naming a part the package does not hold. The
            # document is still readable; this one figure has no picture.
            logger.debug("docx: {} points at {!r}, which is not in the package", rel_id, part)
    return pictures


async def _describe_figures(
    elements: list[Element],
    pictures: dict[int, bytes],
    filename: str,
    *,
    model: "VisionModel | None",
) -> None:
    """Fill each figure's text in with what a model sees in it, in place.

    Optional by design, and the asymmetry with the image parser is deliberate:
    an upload that is nothing but a picture has nothing to index without a
    description, while a document is worth indexing whether or not its figures
    were described. So no vision model, a model that refuses, one picture that
    will not decode -- each of those costs the figures their description and
    nothing else.

    The prose on either side goes with the picture, because a figure rarely
    explains itself: the sentence introducing it names what it is of, and
    without that a model describes a bar chart as a bar chart.

    Every picture that ends up undescribed leaves a note (see
    ``raven.knowledge._notes``), which the manager writes onto the document.
    The file is still indexed and still searchable; what the note says is that
    part of it is not in the index, which is not otherwise discoverable -- the
    row says ready and the answers are merely worse.
    """
    if not pictures:
        return
    if model is None:
        from raven.knowledge._vision import load_vision_model

        model = load_vision_model()
    if model is None:
        note(
            f"{len(pictures)} picture(s) in this file were not read: no vision model is configured "
            "(Settings, Default models)."
        )
        return

    ordered = sorted(pictures)
    if len(ordered) > model.max_figures:
        logger.warning(
            "knowledge: {} holds {} figures and the limit is {}; the rest keep their captions",
            filename,
            len(ordered),
            model.max_figures,
        )
        note(f"only the first {model.max_figures} of {len(ordered)} pictures in this file were read.")
        ordered = ordered[: model.max_figures]

    limit = asyncio.Semaphore(_FIGURE_CONCURRENCY)
    refusals: list[str] = []

    async def describe(index: int) -> tuple[int, str]:
        async with limit:
            try:
                return index, await model.describe(
                    pictures[index],
                    context_above=_text_around(elements, index, -1),
                    context_below=_text_around(elements, index, 1),
                )
            except Exception as exc:  # noqa: BLE001 - one figure, not the document
                logger.warning("knowledge: a figure in {} could not be described: {}", filename, exc)
                refusals.append(str(exc))
                return index, ""

    for index, description in await asyncio.gather(*(describe(index) for index in ordered)):
        if not description:
            continue
        # The author's own alt text first, the model's reading after it: one is
        # a label somebody chose and the other is what is in the picture, and
        # the label is the better thing for an eye to land on.
        existing = elements[index].text.strip()
        elements[index].text = f"{existing}\n{description}" if existing else description

    if refusals:
        # One note for the lot, carrying the first reason: a reader acts on
        # what the endpoint said, and it says the same thing forty times.
        note(f"{len(refusals)} of {len(ordered)} pictures in this file could not be read: {refusals[0]}")


def _text_around(elements: list[Element], index: int, step: int) -> str:
    """The nearest prose on one side of a figure, bounded.

    Nearest rather than everything in the section: the paragraph beside a
    figure is what refers to it, and three pages of unrelated text would push
    that out of the model's attention rather than adding to it.
    """
    at = index + step
    while 0 <= at < len(elements):
        element = elements[at]
        if element.layout is not LayoutType.FIGURE and element.text.strip():
            return element.text.strip()[:_FIGURE_CONTEXT_CHARS]
        at += step
    return ""


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
