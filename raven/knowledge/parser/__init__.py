"""File parsers: raw upload bytes to a list of sections.

A parser handles one format family and preserves boundaries; it never chunks.
``ChunkerBase`` guarantees no chunk spans two sections, so whatever a parser
calls a section is the structure that survives into retrieval.

``ParserBase`` and the ``Section`` shape are adopted from AgentScope
(Apache-2.0; see NOTICES.md). The package layout -- one module per format
family, the format's own reader hidden behind a common call -- follows
RAGFlow's ``deepdoc.parser``.

What this package adds to the adopted base is the positional contract below.
Every section carries :data:`READING_ORDER`, its place in the original file.
Nothing downstream can recover that: search returns hits in score order, and a
section's text does not say where it came from, so a parser that drops the
order drops the document's shape with it. Formats that lay content out on a
page add :data:`PAGE_NUMBER`, :data:`BBOX` and :data:`LAYOUT_TYPE` on top --
that is what lets a hit be cited at a place in a file instead of only quoted,
and what lets a reader tell a heading from a table cell it happens to match.

:data:`ELEMENTS` keeps the two altitudes from fighting each other. Positions
are per paragraph, per row, per figure, while a section has to stay large
enough to embed well -- one section per paragraph would give one chunk per
paragraph, since a chunk never spans sections. So a section spans many elements
and records each one's position against the character range it occupies in the
section text, and a matched span maps back to the page and box it came from.
"""

from __future__ import annotations

import mimetypes
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from raven.knowledge._types import Section

READING_ORDER = "reading_order"
"""Metadata key: the section's 0-based place in its source file."""

PAGE_NUMBER = "page_number"
"""Metadata key: the 1-based page the section starts on, where the format has pages."""

PAGE_END = "page_end"
"""Metadata key: the 1-based page the section ends on. Equals :data:`PAGE_NUMBER`
unless the section runs over a page boundary."""

BBOX = "bbox"
"""Metadata key: a :class:`BBox` as a plain dict, covering the whole section."""

LAYOUT_TYPE = "layout_type"
"""Metadata key: a :class:`LayoutType` value for what the region is."""

ELEMENTS = "elements"
"""Metadata key: a list of :class:`ElementSpan` dicts, in reading order."""

ATOMIC = "atomic"
"""Metadata key: this section is one chunk, whole.

Set by a parser whose sections are already the unit a reader means, and
honoured by every chunker: the section is not split however long it runs, and
nothing is merged into it.

A slide is the case it exists for. A reader says "slide 12"; they do not say
"the third shape of slide 12", and a deck's preview can only scroll to a page
-- so a chunk that is half a slide, or one that spans two, breaks the one thing
the page can do with a hit. Without this the element spans a slide carries are
exactly what the chunker would cut on, which is the opposite of what they are
for here: on a slide they say where each shape sits, not where the text may be
divided.

Deliberately not inferred from a layout type. A spreadsheet row is a table row
and a slide is not a figure; asking "is this section atomic" of the layout
would make one format's answer depend on another's vocabulary."""


class LayoutType(StrEnum):
    """What a parsed region is, as the source file marks it.

    Deliberately coarse. These are the distinctions a reader acts on -- a
    heading names its section, a table cell is not prose, a caption belongs to
    the figure above it -- and every format either states them (a docx style, a
    PDF layout model) or does not. A finer taxonomy would only be guessed.
    """

    TITLE = "title"
    HEADING = "heading"
    TEXT = "text"
    LIST_ITEM = "list_item"
    TABLE = "table"
    FIGURE = "figure"
    CAPTION = "caption"
    QUOTE = "quote"
    CODE = "code"
    TOC = "toc"
    HEADER = "header"
    FOOTER = "footer"
    FOOTNOTE = "footnote"


class BBox(BaseModel):
    """Where a region sits on its page, in points (1/72 inch), origin top-left.

    Points with a top-left origin because that is the frame pdfplumber reports
    ``x0`` / ``x1`` / ``top`` / ``bottom`` in, and PDF is what the other
    formats have to line up with. A docx reader converts its twips and EMU into
    this frame rather than exporting its own units.

    Every field is optional because a flow format genuinely knows only some of
    them. A docx stores page geometry and paragraph indentation, so the
    horizontal band a paragraph occupies is exact, while its vertical position
    exists only once Word has laid the text out -- there is no ``top`` to read.
    ``None`` says unknown; a fabricated number would be indistinguishable from
    a measured one to anything that draws a highlight from it.
    """

    x0: float | None = None
    x1: float | None = None
    top: float | None = None
    bottom: float | None = None

    @property
    def is_empty(self) -> bool:
        """Whether nothing at all is known -- such a box is not worth recording."""
        return self.x0 is None and self.x1 is None and self.top is None and self.bottom is None

    def union(self, other: "BBox") -> "BBox":
        """The smallest box covering both, treating an unknown edge as absent.

        Used to lift element boxes to the section that holds them, where the
        section's own box is only ever the extent of its parts.
        """

        def pick(left: float | None, right: float | None, chooser) -> float | None:
            if left is None:
                return right
            if right is None:
                return left
            return chooser(left, right)

        return BBox(
            x0=pick(self.x0, other.x0, min),
            x1=pick(self.x1, other.x1, max),
            top=pick(self.top, other.top, min),
            bottom=pick(self.bottom, other.bottom, max),
        )


class ElementSpan(BaseModel):
    """One element of a section: where it came from, and where it landed.

    ``char_start`` / ``char_end`` index the section's own text, so a matched
    span resolves to the element that produced it -- and through the element,
    to a page and a box.
    """

    reading_order: int
    layout_type: LayoutType
    char_start: int
    char_end: int
    page_number: int | None = None
    page_end: int | None = None
    bbox: BBox | None = None


def section_metadata(
    *,
    reading_order: int,
    layout_type: LayoutType | None = None,
    page_number: int | None = None,
    page_end: int | None = None,
    bbox: BBox | None = None,
    elements: list[ElementSpan] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build one section's metadata, dropping what the format does not know.

    Values come out JSON-native: the vector store encodes every metadata value
    with ``json.dumps`` to make it filterable, so a pydantic model left in here
    would fail at insert time rather than at the parser that put it there.
    """
    metadata: dict[str, Any] = {READING_ORDER: reading_order}
    if layout_type is not None:
        metadata[LAYOUT_TYPE] = str(layout_type)
    if page_number is not None:
        metadata[PAGE_NUMBER] = page_number
    if page_end is not None:
        metadata[PAGE_END] = page_end
    if bbox is not None and not bbox.is_empty:
        metadata[BBOX] = bbox.model_dump(exclude_none=True)
    if elements:
        metadata[ELEMENTS] = [element.model_dump(mode="json", exclude_none=True) for element in elements]
    metadata.update({key: value for key, value in extra.items() if value is not None})
    return metadata


class ParserBase(ABC):
    """Abstract base class for file-format parsers.

    Each subclass handles a single file format (or a related family, e.g. all
    plain-text MIME types). Subclasses are typically instantiated once and
    reused across many ``parse()`` calls.

    Subclasses should be stateless or thread-safe -- a single instance may be
    invoked concurrently from multiple agent runs.

    Subclasses must declare :attr:`supported_media_types` so that the
    KnowledgeManager can route uploaded files to the right parser based on
    standard IANA media types (RFC 6838), and must stamp every section they
    return with at least :data:`READING_ORDER` (see :func:`section_metadata`).
    """

    supported_media_types: list[str]
    """Standard IANA media types (RFC 6838) this parser handles,
    e.g. ``["application/pdf"]`` or ``["text/plain", "text/markdown"]``.
    Used by the KnowledgeManager to select a parser for an uploaded file."""

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """Filename extensions (including the leading ``.``) this parser can
        produce uploads for.

        The base implementation derives extensions from
        :attr:`supported_media_types` via
        :func:`mimetypes.guess_all_extensions` -- good enough for clean IANA
        types like ``application/pdf``. Subclasses **should override** this
        when the default reverse-lookup is noisy (``text/plain`` resolves to
        ``.bat`` / ``.c`` / ``.pl`` and a dozen other developer extensions no
        KB user wants in the file picker) or when a media type has no
        registered extension at all (``application/x-yaml`` returns the empty
        list).

        The result is consumed by the front-end's ``<input accept>`` and by the
        client-side filename guard; it is **not** consulted for media-type
        routing -- that always goes through :attr:`supported_media_types`.

        Returns:
            `list[str]`:
                Deduplicated, sorted extensions (each starting with ``.``).
                May be empty when no media type resolves.
        """
        out: set[str] = set()
        for media_type in cls.supported_media_types:
            out.update(mimetypes.guess_all_extensions(media_type))
        return sorted(out)

    @abstractmethod
    async def parse(
        self,
        file: bytes | str,
        filename: str,
    ) -> list[Section]:
        """Parse a file into a list of :class:`Section` objects.

        The ``file`` argument is a union covering the three call sites a parser
        sees in practice:

        - ``bytes`` -- the raw payload, as handed in by HTTP uploads and
          blob-store reads.
        - ``str`` for binary parsers (docx, PDF, PPT, image, ...) -- a
          **filesystem path** to the file to read. The parser opens the path
          itself; callers do not need to read the bytes first.
        - ``str`` for :class:`~raven.knowledge.parser.text_parser.TextParser`
          -- disambiguated at runtime: if the string names an existing file on
          disk it is treated as a path and the file is decoded with the
          configured encoding; otherwise it is treated as pre-decoded text.

        Args:
            file (`bytes | str`):
                The file content or a path to it (see above).
            filename (`str`):
                The original filename (e.g. ``"report.docx"``). Used for error
                messages and copied into each Section's :attr:`Section.source`
                field for downstream display / citation.

        Returns:
            `list[Section]`:
                One Section per natural boundary in the source file, in
                document order, each carrying at least :data:`READING_ORDER` in
                its metadata. For unstructured formats (plain text, image,
                video), a single Section may cover the whole file.

        Raises:
            `TypeError`: If the subclass does not accept the supplied ``file``
                form.
            `FileNotFoundError`: If a binary parser is handed a ``str`` that
                does not name an existing file.
            `ValueError`: If the file cannot be parsed.
        """


__all__ = [
    "ATOMIC",
    "BBOX",
    "BBox",
    "ELEMENTS",
    "ElementSpan",
    "LAYOUT_TYPE",
    "LayoutType",
    "PAGE_END",
    "PAGE_NUMBER",
    "READING_ORDER",
    "ParserBase",
    "section_metadata",
]
