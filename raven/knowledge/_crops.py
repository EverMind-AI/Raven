"""A picture of where each chunk came from, cut out of its own pages.

A chunk is a few hundred words with a page number on it. That is enough to
open the page and enough to cite, and it is not enough to answer the question
a reader actually has when a retrieved passage looks wrong -- which is whether
the parser read the page correctly. A figure caption swallowed into the
paragraph above it, a table whose columns came out interleaved, a two-column
page read straight across: all of those produce chunk text that looks like
prose and is not what the page says. Seen next to the region it was cut from,
each is obvious at a glance.

So every chunk that knows where it sits gets one image: the boxes it occupies,
cropped out of the rendered pages and stacked. Follows RAGFlow's
``RAGFlowPdfParser.crop`` (Apache-2.0; see NOTICES.md) in what it produces --
one image a chunk, several regions stacked top to bottom -- and not in how it
gets there. RAGFlow writes the coordinates into the chunk's own text as
``@@page\\ttop\\tbottom\\tleft\\tright##`` markers and parses them back out with
a regex, because its chunk is a bare string with nowhere else to keep them.
Here they are already structured, in the ``elements`` spans the parser records,
so the markers would only put text nobody wrote into the thing being embedded.

The other difference is memory. RAGFlow renders every page of a document up
front and holds them for as long as the document is being processed; at 3x an
A4 page is about 13 MB of RGB, so a long PDF is gigabytes of rasters before a
single crop is taken. Here the pages are visited once each, in order, and each
is released before the next is rendered -- so the peak is one page however long
the document.
"""

from __future__ import annotations

import io
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.knowledge.parser import BBOX, ELEMENTS, PAGE_NUMBER

if TYPE_CHECKING:
    from raven.knowledge._types import Chunk

#: What a page is rendered at before it is cut up. Well below the 3x the layout
#: model wants, because this is not an input to anything -- it is a picture for
#: a person to glance at beside the text, and a crop legible at arm's length is
#: all of the job. Three times the pixels would be three times the disk for a
#: thumbnail nobody zooms into.
ZOOM = 1.5

#: The longest a crop's long edge may be. A full-page region at ZOOM is already
#: over this, and scaling it down is what keeps a chunk that swallowed a whole
#: page from costing twenty times what a paragraph costs.
MAX_EDGE = 1000

#: Points of slack around a region before it is cut. A box comes from the
#: glyphs or from a model looking at pixels, and either way it ends exactly on
#: the ink; a crop with no margin reads as a mistake.
MARGIN = 4

#: Pixels of separation between two regions stacked into one image, drawn in
#: the page's own white. Without it a paragraph from the foot of one page and
#: one from the head of the next read as a single run of text.
GAP = 8

#: What the images are written as. WebP at this quality is roughly half the
#: bytes of an equivalent JPEG and, unlike JPEG, does not ring around the sharp
#: black-on-white edges that are most of what a page of text is.
FORMAT = "WEBP"
QUALITY = 80
SUFFIX = ".webp"

#: Below this a region is not a region -- a zero-width box from a parser that
#: knew the page but not the position, or a rule one point tall.
_MIN_SIDE = 2


def regions_of(chunk: "Chunk") -> "list[tuple[int, tuple[float, float, float, float]]]":
    """Every (page, box) a chunk occupies, in reading order.

    Two shapes, because a chunk records its position in one of two places and
    which one says how it was built. A chunk that merged several pieces keeps
    each piece's page and box in its ``elements`` spans, and drops its own box
    when the pieces disagree on the page -- a rectangle spanning two sheets of
    paper is not a location. A chunk that merged nothing has no spans at all
    and carries its one page and one box at the top level.

    Reading only the spans would therefore miss every unmerged chunk, which on
    a document of short sections is most of them; reading only the top level
    would show a merged chunk one page of the several it covers.
    """
    found: list[tuple[int, tuple[float, float, float, float]]] = []
    for span in chunk.metadata.get(ELEMENTS) or []:
        region = _region(span)
        if region is not None:
            found.append(region)
    if found:
        return found
    region = _region(chunk.metadata)
    return [region] if region is not None else []


def _region(holder: "dict[str, Any]") -> "tuple[int, tuple[float, float, float, float]] | None":
    """One page and box out of a span or a chunk's metadata, if it has both."""
    page = holder.get(PAGE_NUMBER)
    box = holder.get(BBOX)
    if not isinstance(page, int) or not isinstance(box, dict):
        return None
    corners = (box.get("x0"), box.get("top"), box.get("x1"), box.get("bottom"))
    if any(corner is None for corner in corners):
        return None
    return page, (float(corners[0]), float(corners[1]), float(corners[2]), float(corners[3]))


def crops_for(document: bytes, chunks: "list[Chunk]", ids: list[str]) -> dict[str, bytes]:
    """One image per chunk that knows where it sits, keyed by chunk id.

    A chunk with no positional metadata is absent from the result rather than
    present and empty: a format without pages has nothing to show, and that is
    not the same as a page that came out blank.

    One render per page, in page order, whatever order the chunks are in --
    which is why the work is inverted like this instead of looping over chunks.
    """
    wanted: dict[int, list[tuple[str, int, tuple[float, float, float, float]]]] = defaultdict(list)
    counts: dict[str, int] = {}
    for chunk, chunk_id in zip(chunks, ids, strict=True):
        regions = regions_of(chunk)
        counts[chunk_id] = len(regions)
        for order, (page, box) in enumerate(regions):
            wanted[page].append((chunk_id, order, box))
    if not wanted:
        return {}

    pieces: dict[str, list[tuple[int, Any]]] = defaultdict(list)
    try:
        import pymupdf
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - both are hard dependencies
        logger.warning("knowledge: no crops for this document ({})", exc)
        return {}

    with pymupdf.open(stream=document, filetype="pdf") as opened:
        for number in sorted(wanted):
            if not 1 <= number <= opened.page_count:
                continue
            page = _render(opened[number - 1], pymupdf, Image)
            for chunk_id, order, box in wanted[number]:
                cut = _cut(page, box)
                if cut is not None:
                    pieces[chunk_id].append((order, cut))
            page.close()

    out: dict[str, bytes] = {}
    for chunk_id, held in pieces.items():
        if len(held) != counts[chunk_id]:
            # A region fell off the page or measured nothing. Stacking what is
            # left would show a chunk a picture of part of itself with nothing
            # saying which part, which is worse than showing none of it.
            logger.debug("knowledge: chunk {} has {}/{} regions; no crop", chunk_id, len(held), counts[chunk_id])
            continue
        stacked = _stack([cut for _, cut in sorted(held)], Image)
        if stacked is not None:
            out[chunk_id] = stacked
    return out


def _render(page: Any, pymupdf: Any, image_module: Any) -> Any:
    """One page as an image, at :data:`ZOOM`."""
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=False)
    return image_module.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _cut(page: Any, box: "tuple[float, float, float, float]") -> Any:
    """One region out of a rendered page, or ``None`` when there is none.

    The box is in the page's own points and the image is in pixels, so this is
    where the one scale factor in the module is applied. Clamped to the page:
    a box may legitimately start above the top of it, because a model's idea of
    where a region begins is not bounded by the paper.
    """
    x0, top, x1, bottom = box
    left = max(0, int((x0 - MARGIN) * ZOOM))
    upper = max(0, int((top - MARGIN) * ZOOM))
    right = min(page.width, int((x1 + MARGIN) * ZOOM))
    lower = min(page.height, int((bottom + MARGIN) * ZOOM))
    if right - left < _MIN_SIDE or lower - upper < _MIN_SIDE:
        return None
    return page.crop((left, upper, right, lower))


def _stack(cuts: "list[Any]", image_module: Any) -> "bytes | None":
    """Several regions as one image, top to bottom, scaled to fit the cap.

    Left-aligned rather than centred: the regions came off pages with the same
    margin, so their left edges line up, and centring a narrow one between two
    wide ones invents an indent the document does not have.
    """
    if not cuts:
        return None
    if len(cuts) == 1:
        stacked = cuts[0]
    else:
        width = max(cut.width for cut in cuts)
        height = sum(cut.height for cut in cuts) + GAP * (len(cuts) - 1)
        stacked = image_module.new("RGB", (width, height), (255, 255, 255))
        offset = 0
        for cut in cuts:
            stacked.paste(cut, (0, offset))
            offset += cut.height + GAP

    longest = max(stacked.width, stacked.height)
    if longest > MAX_EDGE:
        scale = MAX_EDGE / longest
        size = (max(1, int(stacked.width * scale)), max(1, int(stacked.height * scale)))
        stacked = stacked.resize(size, image_module.LANCZOS)

    buffer = io.BytesIO()
    stacked.save(buffer, format=FORMAT, quality=QUALITY)
    return buffer.getvalue()


__all__ = ["FORMAT", "MAX_EDGE", "SUFFIX", "ZOOM", "crops_for", "regions_of"]
