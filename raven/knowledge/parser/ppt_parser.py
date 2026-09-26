"""Slide decks (``.pptx``, and ``.ppt`` through LibreOffice): one slide, one section.

Read straight from the OOXML package with the standard library, for the reason
the Word parser is: a ``.pptx`` is a zip of XML, walking it is fewer moving
parts than a document object model, and it keeps this package free of another
dependency. ``python-pptx`` is in the lockfile only because the deck engine
declares it; nothing in ``raven/`` may assume a plugin's dependency is there.

Follows RAGFlow's ``RAGFlowPptParser`` (Apache-2.0; see NOTICES.md) in what it
reads and how it reads it: shapes in the order they sit on the slide rather
than the order the file lists them, bulleted paragraphs marked by their indent,
tables composed header-onto-row, and group shapes walked through.

A slide is a section and a section is a chunk. That is the point of this
parser, and it is why every section it returns is marked :data:`ATOMIC`: a
deck's page is the unit a reader means -- they say "slide 12", they do not say
"the third paragraph of slide 12" -- so cutting one into pieces gives the index
fragments that no longer correspond to anything the reader can point at, and
merging two puts one chunk in front of a viewer that can only scroll to one
page. The one-to-one is what lets a hit take the preview to the slide it came
from.

Slide order comes from the presentation's own list, never from the part names.
``ppt/slides/slide10.xml`` sorts before ``slide2.xml``, and a deck whose slides
have been reordered or deleted keeps its original numbering in the filenames --
so the order is read where PowerPoint records it, the same way the spreadsheet
parser reads sheet order through the workbook's relationships.

What is not read: speaker notes, which live in their own part and which RAGFlow
does not read either; and pictures, which have no text to give. A slide that is
one image therefore parses to nothing, and is skipped rather than indexed
empty.
"""

from __future__ import annotations

import os
import posixpath
import zipfile
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree as ET

from raven.knowledge._types import Section, TextBlock
from raven.knowledge.parser import (
    BBox,
    ElementSpan,
    LayoutType,
    ParserBase,
    section_metadata,
)
from raven.knowledge.parser._tables import Cell, html_table

_PRESENTATION_PART = "ppt/presentation.xml"
_PRESENTATION_RELS = "ppt/_rels/presentation.xml.rels"

#: English Metric Units per point, the unit every offset and extent on a slide
#: is written in.
_EMU_PER_POINT = 12700.0

#: How far apart two shapes can sit vertically and still count as one row, in
#: EMU. RAGFlow divides the top by ten, which at EMU scale is no tolerance at
#: all -- two shapes a thousandth of a point apart sort as two rows, and which
#: comes first is then decided by a rounding error in whatever drew the deck.
#: Four points is about a line of body text: close enough to mean "side by
#: side", far enough that a real stack is never folded into one row.
_ROW_TOLERANCE = int(4 * _EMU_PER_POINT)

#: Placeholders whose text is the slide's title. Both spellings: a title slide
#: uses the centred one and every other layout uses the plain one, and a deck
#: mixes them freely.
_TITLE_PLACEHOLDERS = frozenset({"title", "ctrTitle"})


def _local(tag: str) -> str:
    """A tag without its namespace.

    Matched by local name throughout, like the Word parser: a deck written by
    Keynote or LibreOffice carries the same element names under its own
    namespace URIs, and pinning the URI would read only what PowerPoint wrote.
    """
    return tag.rsplit("}", 1)[-1]


def _find(node: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in node if _local(child.tag) == name), None)


def _findall(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _local(child.tag) == name]


def _descend(node: ET.Element, name: str) -> ET.Element | None:
    """The first descendant with this local name, at any depth."""
    return next((child for child in node.iter() if _local(child.tag) == name), None)


def _attr(node: ET.Element, name: str) -> str | None:
    """An attribute by local name, whichever namespace it arrived in."""
    for key, value in node.attrib.items():
        if _local(key) == name:
            return value
    return None


def _rel_id(node: ET.Element) -> str:
    """The relationship this element points through.

    Not ``_attr(node, "id")``, which is the one place in this format where
    matching by local name picks the wrong attribute: a ``sldId`` carries both
    the slide's own number as ``id`` and the relationship as ``r:id``, and the
    two collide on their local name. Read qualified, so the numeric one cannot
    stand in for it -- it resolves to no relationship at all, which is how a
    deck came back with no slides.
    """
    for key, value in node.attrib.items():
        if key.endswith("}id"):
            return value
    return ""


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


@dataclass
class _Shape:
    """One thing on a slide: where it sits, what it says, and what kind it is."""

    text: str
    layout: LayoutType
    #: Top-left in EMU, or ``None`` where the deck states none and no layout
    #: placeholder supplies one.
    x: float | None
    y: float | None
    bbox: BBox
    #: Whether this shape is the slide's title placeholder.
    titles: bool = False

    def order(self) -> tuple[int, float]:
        """The sort key: down the slide in bands, then across.

        Bands rather than exact tops, because two shapes meant to be read side
        by side are never at exactly the same height. Unplaced shapes sort to
        the top, which is RAGFlow's answer too -- and since the sort is stable,
        a slide where nothing states a position keeps the order the file lists,
        which for PowerPoint is title first.
        """
        return (int((self.y or 0.0) // _ROW_TOLERANCE), self.x or 0.0)


class PptParser(ParserBase):
    """Parse a slide deck into one section per slide.

    Stateless between calls: every ``parse`` reads its own package, so one
    instance is safe to share across concurrent runs.
    """

    supported_media_types: list[str] = [
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    ]

    def __init__(self, timeout_s: float | None = None) -> None:
        """
        Args:
            timeout_s (`float | None`):
                How long LibreOffice may take over a legacy ``.ppt``. ``None``
                takes the shared default.
        """
        self.timeout_s = timeout_s

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """The two a reader uploads.

        Overridden because the reverse lookup for ``application/vnd.ms-powerpoint``
        answers with the whole legacy family (``.pot``, ``.pps``, ``.ppa``),
        most of which are templates and add-ins rather than decks.
        """
        return [".ppt", ".pptx"]

    async def parse(self, file: bytes | str, filename: str) -> list[Section]:
        """Parse the deck into sections, one per slide, in presentation order.

        Args:
            file (`bytes | str`): The payload, or a filesystem path to it.
            filename (`str`): The source filename, copied into ``Section.source``.

        Returns:
            `list[Section]`: One section per slide that has any text on it, each
                carrying the slide's 1-based number as its page.

        Raises:
            `FileNotFoundError`: If ``file`` is a path that does not exist.
            `ValueError`: If the payload is not a readable deck, or if a legacy
                ``.ppt`` could not be converted.
        """
        payload = await self._as_pptx(file, filename)
        try:
            with zipfile.ZipFile(BytesIO(payload)) as package:
                slides = [ET.fromstring(package.read(part)) for part in _slide_parts(package)]
                layouts = _layout_offsets(package, _slide_parts(package))
        except KeyError as error:
            raise ValueError(f"{filename!r} is not a slide deck: the package has no {_PRESENTATION_PART}") from error
        except (zipfile.BadZipFile, ET.ParseError) as error:
            raise ValueError(f"Failed to read {filename!r} as a slide deck: {error}") from error

        sections: list[Section] = []
        for index, slide in enumerate(slides):
            shapes = _shapes_on(slide, layouts[index] if index < len(layouts) else {})
            section = _section_from(shapes, filename, number=index + 1)
            if section is not None:
                sections.append(section)
        return sections

    async def _as_pptx(self, file: bytes | str, filename: str) -> bytes:
        """The payload as an OOXML package, converting a legacy deck if needed.

        Decided by the bytes rather than by the name: a ``.pptx`` is a zip and a
        binary ``.ppt`` is not, so a file saved under the wrong extension -- which
        is most of what "my .ppt will not open" turns out to be -- still reads.
        """
        payload = file if isinstance(file, bytes) else None
        if payload is None:
            path = str(file)
            if not os.path.isfile(path):
                raise FileNotFoundError(f"{filename!r}: no such file: {path!r}")
            with open(path, "rb") as handle:
                payload = handle.read()
        if payload[:2] == b"PK":
            return payload

        from raven.knowledge.parser._office import TIMEOUT_S, converted

        return await converted(
            payload,
            filename,
            target="pptx",
            suffix=".ppt",
            timeout_s=self.timeout_s if self.timeout_s is not None else TIMEOUT_S,
        )


def _slide_parts(package: zipfile.ZipFile) -> list[str]:
    """The slide parts, in the order the deck presents them.

    Through the presentation's ``sldIdLst`` and its relationships, never
    through the part names: ``slide10.xml`` sorts before ``slide2.xml``, and a
    deck whose slides were reordered keeps the numbering it was first saved
    with, so the filenames say nothing about the running order.

    A deck with no presentation part is not a deck; the caller reports that.
    Falling back to the sorted part names would be worse than failing, because
    it answers with slides in an order nobody chose.
    """
    presentation = ET.fromstring(package.read(_PRESENTATION_PART))
    listed = _find(presentation, "sldIdLst")
    if listed is None:
        return []
    targets = _relationships(package, _PRESENTATION_RELS, base="ppt")
    parts: list[str] = []
    for entry in _findall(listed, "sldId"):
        part = targets.get(_rel_id(entry))
        if part and part in package.namelist():
            parts.append(part)
    return parts


def _relationships(package: zipfile.ZipFile, part: str, *, base: str) -> dict[str, str]:
    """Relationship id -> package path, for one relationships part."""
    try:
        root = ET.fromstring(package.read(part))
    except (KeyError, ET.ParseError):
        return {}
    targets: dict[str, str] = {}
    for node in root:
        if _local(node.tag) != "Relationship":
            continue
        rel_id, target = _attr(node, "Id"), _attr(node, "Target")
        if not rel_id or not target or (_attr(node, "TargetMode") or "") == "External":
            continue
        targets[rel_id] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(f"{base}/{target}")
    return targets


def _layout_offsets(package: zipfile.ZipFile, slides: list[str]) -> list[dict[str, tuple[float, float]]]:
    """Where each slide's layout puts its placeholders, keyed by placeholder.

    A placeholder that sits where its layout says carries no geometry of its
    own -- which is most title and body placeholders in most decks. Without
    this they all read as unplaced, and a slide whose other shapes *are* placed
    sorts its title after them. PowerPoint resolves this through the layout, so
    this does too.

    Keyed by ``idx`` where the placeholder has one and by ``type`` otherwise,
    which is how a placeholder is matched to its layout.
    """
    offsets: list[dict[str, tuple[float, float]]] = []
    for slide in slides:
        room = posixpath.dirname(slide)
        rels = _relationships(package, f"{room}/_rels/{posixpath.basename(slide)}.rels", base=room)
        layout = next((part for part in rels.values() if "slideLayout" in part), "")
        found: dict[str, tuple[float, float]] = {}
        if layout:
            try:
                root = ET.fromstring(package.read(layout))
            except (KeyError, ET.ParseError):
                root = None
            if root is not None:
                for shape in _shape_nodes(root):
                    key = _placeholder_key(shape)
                    spot = _offset_of(shape)
                    if key and spot is not None:
                        found.setdefault(key, spot)
        offsets.append(found)
    return offsets


def _shape_nodes(root: ET.Element) -> list[ET.Element]:
    """The shapes of a slide or layout, as its shape tree lists them."""
    container = _find(root, "cSld")
    tree = _find(container, "spTree") if container is not None else None
    if tree is None:
        return []
    return [child for child in tree if _local(child.tag) in ("sp", "grpSp", "graphicFrame", "pic")]


def _placeholder_key(shape: ET.Element) -> str:
    """How a placeholder is matched to the layout's copy of it."""
    placeholder = _descend(shape, "ph")
    if placeholder is None:
        return ""
    index = _attr(placeholder, "idx")
    return f"idx:{index}" if index else f"type:{_attr(placeholder, 'type') or 'body'}"


def _offset_of(shape: ET.Element) -> tuple[float, float] | None:
    """A shape's top-left in EMU, or ``None`` when it states none."""
    frame = _descend(shape, "xfrm")
    offset = _find(frame, "off") if frame is not None else None
    if offset is None:
        return None
    x, y = _number(_attr(offset, "x")), _number(_attr(offset, "y"))
    return (x, y) if x is not None and y is not None else None


def _extent_of(shape: ET.Element) -> tuple[float, float] | None:
    frame = _descend(shape, "xfrm")
    extent = _find(frame, "ext") if frame is not None else None
    if extent is None:
        return None
    cx, cy = _number(_attr(extent, "cx")), _number(_attr(extent, "cy"))
    return (cx, cy) if cx is not None and cy is not None else None


def _shapes_on(slide: ET.Element, layout: dict[str, tuple[float, float]]) -> list[_Shape]:
    """Everything on one slide that has text, in the order it is read."""
    shapes = [_read_shape(node, layout) for node in _shape_nodes(slide)]
    placed = [shape for shape in shapes if shape is not None and shape.text]
    return sorted(placed, key=lambda shape: shape.order())


def _read_shape(node: ET.Element, layout: dict[str, tuple[float, float]]) -> _Shape | None:
    """One shape as its text and its place, or ``None`` when it says nothing."""
    name = _local(node.tag)
    spot = _offset_of(node) or layout.get(_placeholder_key(node))
    extent = _extent_of(node)
    x, y = (spot[0], spot[1]) if spot else (None, None)
    box = _box(x, y, extent)

    if name == "grpSp":
        # A group is read through, and its children are sorted among
        # themselves: the group is one thing on the slide, and the order
        # inside it is the order inside it.
        inner = [_read_shape(child, layout) for child in node if _local(child.tag) in ("sp", "grpSp", "graphicFrame")]
        text = "\n".join(shape.text for shape in sorted_shapes(inner))
        return _Shape(text=text, layout=LayoutType.TEXT, x=x, y=y, bbox=box) if text else None

    table = _descend(node, "tbl")
    if table is not None:
        text = html_table(_table_rows(table))
        return _Shape(text=text, layout=LayoutType.TABLE, x=x, y=y, bbox=box) if text else None

    body = _descend(node, "txBody")
    if body is None:
        return None
    text = _body_text(body)
    if not text:
        return None
    # `is not None`, never a truth test: an ElementTree element is falsy when it
    # has no children, and a placeholder never has any -- so `found or default`
    # took the default every time and no deck ever had a title.
    placeholder = _descend(node, "ph")
    kind = _attr(placeholder, "type") if placeholder is not None else None
    titles = (kind or "") in _TITLE_PLACEHOLDERS
    return _Shape(
        text=text,
        layout=LayoutType.TITLE if titles else LayoutType.TEXT,
        x=x,
        y=y,
        bbox=box,
        titles=titles,
    )


def sorted_shapes(shapes: "list[_Shape | None]") -> list[_Shape]:
    """The shapes that said something, in reading order."""
    return sorted((shape for shape in shapes if shape is not None and shape.text), key=lambda shape: shape.order())


def _box(x: float | None, y: float | None, extent: tuple[float, float] | None) -> BBox:
    """A shape's rectangle in points.

    Complete, unlike a Word paragraph's: a slide states absolute coordinates
    for everything on it, so a hit inside a deck can be pointed at rather than
    only quoted.
    """
    if x is None or y is None:
        return BBox()
    width, height = extent or (None, None)
    return BBox(
        x0=x / _EMU_PER_POINT,
        x1=(x + width) / _EMU_PER_POINT if width is not None else None,
        top=y / _EMU_PER_POINT,
        bottom=(y + height) / _EMU_PER_POINT if height is not None else None,
    )


def _table_rows(table: ET.Element) -> "list[list[Cell]]":
    """A slide table as a grid of cells, each carrying what it spans.

    DrawingML says this more directly than Word does: the cell that begins a
    merge carries ``gridSpan`` and ``rowSpan`` itself, and the cells it covers
    are marked ``hMerge`` or ``vMerge`` and hold nothing. So the covered ones
    are dropped rather than emitted empty -- an empty cell in the grid says the
    slide left one blank, which is a different thing from a cell that is part
    of its neighbour.
    """
    grid: list[list[Cell]] = []
    for row in _findall(table, "tr"):
        cells: list[Cell] = []
        for cell in _findall(row, "tc"):
            if _flag(cell, "hMerge") or _flag(cell, "vMerge"):
                continue
            body = _descend(cell, "txBody")
            cells.append(
                Cell(
                    text=_body_text(body).replace("\n", " ").strip() if body is not None else "",
                    colspan=_span(cell, "gridSpan"),
                    rowspan=_span(cell, "rowSpan"),
                )
            )
        grid.append(cells)
    return grid


def _span(cell: ET.Element, name: str) -> int:
    """How many columns or rows a cell covers. One unless it says otherwise."""
    value = cell.get(name)
    if value is None:
        return 1
    try:
        return max(1, int(value))
    except ValueError:
        return 1


def _flag(cell: ET.Element, name: str) -> bool:
    """Whether a boolean cell attribute is set. OOXML writes these as 1/0."""
    return (cell.get(name) or "0").strip().lower() in {"1", "true"}


def _body_text(body: ET.Element) -> str:
    """A text frame's paragraphs, one per line, bullets marked.

    RAGFlow's rendering, kept: a bulleted paragraph is indented by its level
    and prefixed with a dot, so a list reads as a list once the styling is
    gone. A paragraph that carries no bullet is written as itself -- including
    one that turned its bullet off explicitly, which is how a deck writes a
    heading inside a body placeholder.
    """
    lines: list[str] = []
    for paragraph in _findall(body, "p"):
        text = _paragraph_text(paragraph)
        if not text:
            continue
        properties = _find(paragraph, "pPr")
        if properties is not None and _bulleted(properties):
            level = int(_number(_attr(properties, "lvl")) or 0)
            text = f"{'  ' * level}.{text}"
        lines.append(text)
    return "\n".join(lines)


def _bulleted(properties: ET.Element) -> bool:
    """Whether this paragraph draws a bullet.

    Three ways to say yes -- a character, an automatic number, a picture -- and
    one to say no, which wins: ``buNone`` is what a deck writes to turn off a
    bullet the layout would otherwise supply.
    """
    kinds = {_local(child.tag) for child in properties}
    if "buNone" in kinds:
        return False
    return bool(kinds & {"buChar", "buAutoNum", "buBlip"})


def _paragraph_text(paragraph: ET.Element) -> str:
    """One paragraph's text: its runs, its fields, and its line breaks."""
    parts: list[str] = []
    for child in paragraph:
        name = _local(child.tag)
        if name in ("r", "fld"):
            text = _descend(child, "t")
            if text is not None and text.text:
                parts.append(text.text)
        elif name == "br":
            parts.append("\n")
    return "".join(parts).strip()


def _section_from(shapes: list[_Shape], filename: str, *, number: int) -> Section | None:
    """One slide as a section, or ``None`` when it holds no text.

    Skipped rather than kept as an empty section: a slide that is one picture
    has nothing to embed, and a chunk of nothing is a row in the list that no
    query can ever match and that a reader cannot act on. What it costs is the
    numbering -- the chunks of a deck with a picture-only slide run 1, 2, 4 --
    and that is the honest shape, because the page a chunk names is still the
    page it came from.
    """
    if not shapes:
        return None

    pieces: list[str] = []
    spans: list[ElementSpan] = []
    cursor = 0
    for order, shape in enumerate(shapes):
        if cursor:
            pieces.append("\n")
            cursor += 1
        pieces.append(shape.text)
        spans.append(
            ElementSpan(
                reading_order=order,
                layout_type=shape.layout,
                char_start=cursor,
                char_end=cursor + len(shape.text),
                page_number=number,
                page_end=number,
                bbox=shape.bbox if not shape.bbox.is_empty else None,
            )
        )
        cursor += len(shape.text)

    text = "".join(pieces)
    if not text.strip():
        return None
    title = next((shape.text for shape in shapes if shape.titles), "")
    return Section(
        content=TextBlock(text=text),
        source=filename,
        metadata=section_metadata(
            reading_order=number - 1,
            page_number=number,
            page_end=number,
            elements=spans,
            # One slide, one chunk. Without this the spans above are exactly
            # what the chunker cuts on, and a slide would arrive in the index
            # as one piece per shape.
            atomic=True,
            heading_path=[title] if title else None,
        ),
    )


__all__ = ["PptParser"]
