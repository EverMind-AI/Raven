"""PDF files: the text layer where there is one, and a model's eyes where not.

A PDF is not a document format so much as a drawing format. It records glyphs
at coordinates, and whether those glyphs are a heading, a table cell or a page
footer is something a reader infers from how they look. So there are two ways
to read one here, and which runs depends on what is installed.

The geometric way infers structure from how the page is set: a block set larger
than the body text is a heading, a run of cells that line up is a table. That
needs nothing but the file, and it degrades exactly where a template does --
a caption set in the body size reads as prose, a running header as a paragraph.

The other way asks deepdoc's models, in :mod:`.deepdoc._pipeline`: one says
what each region of the page is, one reads a page that carries no text at all,
and one rebuilds a table's rows from a picture of it. That is the half a PDF
genuinely does not state, and `make fetch-resources` is what turns it on.

A page neither of them can read -- a scan, with the recogniser not installed --
is rendered and handed to the vision model (see :mod:`raven.knowledge._vision`),
and a page nobody can read says so on its row rather than being indexed as
nothing.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.knowledge._notes import note
from raven.knowledge._types import Section
from raven.knowledge.parser import BBox, LayoutType, ParserBase
from raven.knowledge.parser._layout import Element, sections_from
from raven.knowledge.parser._tables import html_table, pad_grid

if TYPE_CHECKING:
    from raven.knowledge._vision import VisionModel

#: How much bigger than the body text a run has to be set to read as a heading.
#: Measured rather than absolute: a paper sets its body at 9pt and a report at
#: 12, and a fixed threshold calls every line of one a heading and none of the
#: other. Two steps, so a document with one heading size still gets a hierarchy
#: rather than everything at level 1.
_HEADING_RATIO = (1.5, 1.2)

#: Below this many characters, a page that also carries a picture is treated as
#: a scan of one. Not zero: a scanned page routinely carries a few characters
#: of header junk or a watermark from whatever produced it, and indexing those
#: instead of the page is worse than indexing nothing.
#:
#: The picture is half the rule, and the half that matters. A page with nine
#: characters and no image is a section divider or a title page -- sparse, not
#: scanned -- and sending it to a model would cost a call to be told what the
#: text layer already said.
_TEXT_LAYER_MIN = 24

#: What a page is rendered at before it is handed to a model. 150 dpi against
#: the 72 a PDF states, which is the point where body text at 9pt stays legible
#: to a vision model without the image being mostly paper.
_RENDER_SCALE = 150 / 72

#: Pages described at once. Same reasoning as the figure limit in the Word
#: parser: one document at a time reaches here, so this is the whole
#: concurrency against the endpoint.
_PAGE_CONCURRENCY = 4


class PdfParser(ParserBase):
    """Parse a PDF into heading-bounded sections, in reading order."""

    supported_media_types: list[str] = ["application/pdf"]

    def __init__(
        self, model: "VisionModel | None" = None, *, layout: bool | None = None, ocr: bool | None = None
    ) -> None:
        """
        Args:
            model (`VisionModel | None`):
                What reads a page that neither the text layer nor the
                recogniser could. ``None`` resolves the configured ``vision``
                pin per parse, so configuring one reaches the next upload
                without a restart. Unset, such a page is reported as unread
                rather than silently indexed as nothing.
            layout (`bool | None`):
                Whether to ask the deepdoc models what each region of a page
                is. ``None`` (the default) means "when the weights are
                installed" -- so fetching them turns it on and an install that
                skipped them still reads PDFs, geometrically. ``False`` forces
                the geometric path even where they are present, which is what
                a test comparing the two wants.
            ocr (`bool | None`):
                Whether a page with no text layer is read by the recogniser.
                ``None`` means "when its weights are installed". ``False``
                sends such a page to the vision model instead, which is the
                slower and more expensive of the two and the only one that can
                say what a photograph on the page shows.
        """
        self._model = model
        self._layout = layout
        self._ocr = ocr

    @classmethod
    def supported_extensions(cls) -> list[str]:
        return [".pdf"]

    async def parse(self, file: bytes | str, filename: str) -> list[Section]:
        """Parse the document into sections, in the order a reader would read.

        Raises:
            `ValueError`: if the payload is not a readable PDF -- a renamed
                file, a truncated download, or one encrypted with a password.
        """
        import pymupdf

        try:
            if isinstance(file, str):
                if not Path(file).is_file():
                    raise FileNotFoundError(f"{filename!r}: no such file: {file!r}")
                document = pymupdf.open(file)
            else:
                document = pymupdf.open(stream=BytesIO(file), filetype="pdf")
        except FileNotFoundError:
            raise
        except Exception as error:
            raise ValueError(f"Failed to read {filename!r} as a PDF: {error}") from error

        with document:
            if document.needs_pass:
                raise ValueError(f"{filename!r} is password-protected, so nothing in it can be read")
            body = _body_size(document)
            by_model = self._layout_reader()
            elements: list[Element] = []
            unread: list[int] = []
            for number, page in enumerate(document, start=1):
                found = by_model(page, number) if by_model is not None else None
                if found is None:
                    found = _page_elements(page, number, body)
                if found:
                    elements.extend(found)
                else:
                    unread.append(number)
            if unread:
                elements.extend(await self._describe(document, unread, filename))

        if not elements:
            note(f"nothing could be read out of {filename!r}: it has no text layer and no page could be described.")
        return sections_from(elements, filename)

    def _layout_reader(self) -> Any:
        """The model-driven page reader, or ``None`` to read geometrically.

        Resolved per parse rather than held: fetching the weights should reach
        the next upload, and so should deleting them.
        """
        if self._layout is False:
            return None
        from raven.knowledge.parser.deepdoc import _pipeline

        if not _pipeline.usable():
            if self._layout:
                # Asked for by name and not installed: that is worth saying,
                # because the alternative is a reader wondering why a document
                # came out the way it did.
                note(
                    "the deepdoc layout model is not installed, so this file was read geometrically; "
                    "run `make fetch-resources` to install it."
                )
            return None
        read = False if self._ocr is False else _pipeline.readable()
        if self._ocr and not read:
            note(
                "the deepdoc text recognizer is not installed, so pages with no text layer were not read here; "
                "run `make fetch-resources` to install it."
            )

        def elements(page: Any, number: int) -> "list[Element] | None":
            return _pipeline.elements(page, number, ocr=read)

        return elements

    async def _describe(self, document: Any, pages: list[int], filename: str) -> list[Element]:
        """Read the pages that carry no text, by looking at them.

        A scanned PDF is an image of a document, and this is the only path to
        what it says. Ordered back into place by the caller: each element
        carries its page number, and pages were walked in order.
        """
        model = self._model
        if model is None:
            from raven.knowledge._vision import load_vision_model

            model = load_vision_model()
        if model is None:
            note(
                f"{len(pages)} page(s) of {filename!r} have no text layer and were not read: "
                "no vision model is configured (Settings, Default models)."
            )
            return []

        limit = asyncio.Semaphore(_PAGE_CONCURRENCY)
        refusals: list[str] = []
        capped = pages[: model.max_figures]
        if len(pages) > len(capped):
            note(f"only the first {len(capped)} of {len(pages)} unreadable pages of {filename!r} were described.")

        async def describe(number: int) -> "tuple[int, str, BBox]":
            page = document[number - 1]
            box = _box(page.rect)
            image = page.get_pixmap(matrix=_matrix(), alpha=False).tobytes("png")
            async with limit:
                try:
                    return number, await model.describe(image, mime="image/png"), box
                except Exception as exc:  # noqa: BLE001 - one page, not the document
                    logger.warning("knowledge: page {} of {} could not be described: {}", number, filename, exc)
                    refusals.append(str(exc))
                    return number, "", box

        described = await asyncio.gather(*(describe(number) for number in capped))
        if refusals:
            note(f"{len(refusals)} of {len(capped)} pages of {filename!r} could not be described: {refusals[0]}")
        return [
            Element(layout=LayoutType.TEXT, text=text.strip(), page_number=number, page_end=number, bbox=box)
            for number, text, box in described
            if text.strip()
        ]


def _matrix() -> Any:
    import pymupdf

    return pymupdf.Matrix(_RENDER_SCALE, _RENDER_SCALE)


def _box(rect: Any) -> BBox:
    """A PyMuPDF rectangle as the box this package records.

    Both measure in points from the top-left, so this is a rename rather than
    a conversion -- and writing it once is what keeps it that way.
    """
    return BBox(x0=float(rect.x0), x1=float(rect.x1), top=float(rect.y0), bottom=float(rect.y1))


def _body_size(document: Any) -> float:
    """The size the document sets its body text at.

    The most common size across the first few pages, weighted by how much text
    is set in it -- not the mean, which a title page full of large type drags
    upward, and not the minimum, which is a footnote. Sampled rather than
    exhaustive: a document sets its body once, and reading three hundred pages
    to learn it is three hundred pages of work for one number.
    """
    seen: Counter[float] = Counter()
    for page in list(document)[:8]:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = (span.get("text") or "").strip()
                    if text:
                        seen[round(float(span.get("size") or 0), 1)] += len(text)
    return seen.most_common(1)[0][0] if seen else 0.0


def _page_elements(page: Any, number: int, body: float) -> list[Element]:
    """One page as elements, or an empty list when it carries no text.

    Tables first, because a table's cells are also text blocks: the rows are
    composed into one element and the blocks inside the table's area are then
    dropped, or every cell would be indexed twice -- once as a line of prose
    with no column name, and once inside the row that explains it.
    """
    elements: list[Element] = []
    covered: list[Any] = []
    for table in _tables(page):
        # As HTML, the way the model-driven path composes one and the way
        # RAGFlow indexes every PDF table. A PDF states where its cell
        # boundaries are, which is the thing a flattened row throws away.
        grid = pad_grid([[(cell or "").strip() for cell in row] for row in table.extract()])
        text = html_table(grid)
        if not text:
            continue
        covered.append(table.bbox)
        elements.append(
            Element(
                layout=LayoutType.TABLE,
                text=text,
                page_number=number,
                page_end=number,
                bbox=_box(_rect(table.bbox)),
            )
        )

    characters = 0
    pictures = 0
    for block in page.get_text("dict").get("blocks", []):
        box = _rect(block.get("bbox") or (0, 0, 0, 0))
        if block.get("type") == 1:
            pictures += 1
            # An image. Its bytes are not read: a figure inside a PDF has no
            # caption of its own to index, and describing every one of them is
            # the figure path the Word parser has -- worth having here too, and
            # not worth guessing at before the layout model lands.
            elements.append(
                Element(layout=LayoutType.FIGURE, text="", page_number=number, page_end=number, bbox=_box(box))
            )
            continue
        if any(_inside(box, area) for area in covered):
            continue
        text, size = _block_text(block)
        if not text:
            continue
        characters += len(text)
        elements.append(
            Element(
                layout=LayoutType.HEADING if _heading_level(size, body) else LayoutType.TEXT,
                text=text,
                page_number=number,
                page_end=number,
                bbox=_box(box),
                heading_level=_heading_level(size, body),
            )
        )

    if pictures and characters < _TEXT_LAYER_MIN and not any(e.layout is LayoutType.TABLE for e in elements):
        # A picture of a page rather than a page: reported as unread so it
        # reaches the vision path, rather than as a handful of stray
        # characters that say nothing about what is on it.
        return []
    # Back into the order a reader reads them in. The tables were found first
    # -- they have to be, so the blocks inside them can be dropped -- which
    # otherwise puts every table on the page ahead of every paragraph on it.
    # That is not a cosmetic difference: sections are cut at headings, so a
    # heading that sorts after the table it introduces ends up a heading with
    # nothing under it, and a heading with nothing under it is dropped.
    #
    # Top then left, which is reading order for the single-column pages this
    # path is for. A two-column page needs to know where the columns are, and
    # that is what the layout model is for.
    elements.sort(key=lambda e: (e.bbox.top if e.bbox.top is not None else 0.0, e.bbox.x0 or 0.0))
    return elements


def _tables(page: Any) -> list[Any]:
    """The tables PyMuPDF finds, or none when it cannot look.

    Table finding is heuristic and occasionally throws on a page whose drawing
    commands it cannot make sense of. One page's tables are not worth the
    document.
    """
    try:
        return list(page.find_tables().tables)
    except Exception as exc:  # noqa: BLE001 - the prose on the page is still readable
        logger.debug("pdf: table detection failed on a page ({})", exc)
        return []


def _rect(bbox: Any) -> Any:
    import pymupdf

    return bbox if hasattr(bbox, "x0") else pymupdf.Rect(*bbox)


def _inside(inner: Any, outer: Any) -> bool:
    """Whether a block sits within a table's area, with a point of slack.

    Slack because the two boxes are measured differently: a table's bounds come
    from its ruling lines and a block's from its glyphs, so a cell's text can
    sit a hair outside the grid drawn around it.
    """
    area = _rect(outer)
    return inner.x0 >= area.x0 - 1 and inner.x1 <= area.x1 + 1 and inner.y0 >= area.y0 - 1 and inner.y1 <= area.y1 + 1


def _block_text(block: Any) -> tuple[str, float]:
    """A block's text, and the size most of it is set in."""
    lines: list[str] = []
    sizes: Counter[float] = Counter()
    for line in block.get("lines", []):
        parts = []
        for span in line.get("spans", []):
            text = span.get("text") or ""
            if text.strip():
                sizes[round(float(span.get("size") or 0), 1)] += len(text.strip())
            parts.append(text)
        joined = "".join(parts).strip()
        if joined:
            lines.append(joined)
    if not lines:
        return "", 0.0
    # Joined with spaces rather than newlines: a PDF breaks a paragraph into a
    # line per printed line, and keeping those breaks indexes a sentence as
    # several fragments.
    return " ".join(lines), (sizes.most_common(1)[0][0] if sizes else 0.0)


def _heading_level(size: float, body: float) -> int | None:
    """Which heading level a run set at ``size`` reads as, or ``None``.

    Size alone, deliberately. Bold body text is not a heading and a PDF has no
    way to say that it is; inferring a hierarchy from weight as well produces
    one that changes halfway through a document, which is worse than a flat one
    that does not.
    """
    if body <= 0 or size <= 0:
        return None
    for level, ratio in enumerate(_HEADING_RATIO, start=1):
        if size >= body * ratio:
            return level
    return None


__all__ = ["PdfParser"]
