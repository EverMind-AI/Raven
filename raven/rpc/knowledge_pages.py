"""Whole pages of a knowledge document, rendered for a reader to look at.

The panel beside a chunk list used to be the browser's own PDF viewer in a
frame. That viewer reads where to open from the URL once, when it loads, and
answers to nothing afterwards -- so following a reader from chunk to chunk
meant loading the file again on every press -- and nothing can be drawn on top
of it, because it is another document in another frame with its own painting.

Pages as images fix both. The panel is then a column of pictures this page
owns: scrolling to one is a scroll, and the region a chunk was cut from can be
covered with a translucent box laid over the picture, which is the one thing a
reader checking a chunk actually wants to see.

The images are rendered once and kept. A page is immutable for the life of its
document -- the bytes behind a document id never change, because re-uploading a
file makes a new document -- so the cache needs no invalidation beyond being
dropped when the document is.

Which PDF belongs to which document is not this module's question -- it is
handed one and told which page. That keeps it out of the knot the preview
module sits in (tests/test_import_cycle_budget.py counts it), and leaves it a
renderer rather than a second place that knows what a document is.

Rendered at :data:`raven.knowledge._crops.ZOOM`, the same scale the crops use,
and deliberately the same number: the client places a highlight by dividing a
region's points by the page's points, and a second scale factor in the system
would be a second chance to be half a page out.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from raven.knowledge._crops import FORMAT, QUALITY, SUFFIX, ZOOM

#: The most pages one document is drawn as. A reader scrolling a column of
#: pictures is reading a document, not auditing a corpus, and the column is
#: built up front -- so this is the ceiling on how much layout one open costs.
#: Past it the whole document falls back to the frame, which pages lazily
#: itself and shows all of it.
MAX_PAGES = 400


class PagesUnavailableError(RuntimeError):
    """This PDF has no such page."""


async def pdf_of(document_id: str, blob: Path, name: str) -> "Path | None":
    """The PDF a document is drawn from, or ``None`` where it has none.

    Itself for a PDF, LibreOffice's rendering for an office file, and nothing
    for a text file or a note. ``None`` rather than an error because having no
    pages is an ordinary thing for a document to be: the reader's side frames
    the file instead, which is what every format had before pages.

    A slide deck is pages once it has been converted, which is what makes a
    deck's chunks locatable at all -- the parser numbered the slides, and slide
    N is page N of the rendering.

    Takes the name rather than the record so that this module needs nothing
    that knows what a document is -- which is what keeps it out of the knot
    `knowledge_preview` sits in (tests/test_import_cycle_budget.py counts it).
    """
    from raven.rpc import pdf_preview

    named = Path(name or "document")
    if named.suffix.lower() == ".pdf":
        return blob
    if not pdf_preview.is_renderable(named):
        return None
    return await pdf_preview.pdf_for_stored(document_id, blob, name)


def cache_dir() -> Path:
    from raven.config.paths import get_cache_dir

    return get_cache_dir() / "knowledge-pages"


def forget(document_id: str) -> None:
    """Drop every rendered page of one document.

    Never fails: the row is gone either way, and a reader who cannot be rid of
    a document because of a cache file is worse off than one whose cache is
    swept a week later.
    """
    import shutil

    if not document_id:
        return
    root = cache_dir()
    # Resolved and checked to be a child of the directory this module owns,
    # rather than tested for separators: `..` contains none and names the
    # parent, which is every rendered page of every document.
    try:
        held = (root / document_id).resolve()
        if held.parent != root.resolve():
            return
    except OSError:
        return
    try:
        shutil.rmtree(held, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - rmtree already swallows these
        logger.debug("knowledge: could not drop the rendered pages of {} ({})", document_id, exc)


def sizes(pdf: Path) -> "list[dict[str, Any]]":
    """Every page's number and size, in the points its regions are measured in.

    The size is what the reader's side divides by: a region is a box in points
    and a picture is a box of pixels, and a page that does not say how big it
    is cannot have anything placed on it.

    A document longer than :data:`MAX_PAGES` answers with nothing rather than
    with its first four hundred. The reader's side reads an empty list as "this
    has no pages" and frames the file, which shows all of it -- where a
    truncated column would show most of it and say nothing about the rest, and
    a chunk cut from page five hundred would have nowhere to be marked.
    """
    import pymupdf

    out: list[dict[str, Any]] = []
    with pymupdf.open(pdf) as opened:
        if opened.page_count > MAX_PAGES:
            logger.debug("knowledge: {} pages is past the ceiling; framing it instead", opened.page_count)
            return []
        for number in range(1, opened.page_count + 1):
            rect = opened[number - 1].rect
            out.append({"number": number, "width": float(rect.width), "height": float(rect.height)})
    return out


def _path_for(document_id: str, page: int) -> Path:
    return cache_dir() / document_id / f"{page}{SUFFIX}"


def _render(pdf: Path, page: int, out: Path) -> None:
    """One page, written where it will be found again."""
    import io

    import pymupdf
    from PIL import Image

    with pymupdf.open(pdf) as opened:
        if not 1 <= page <= opened.page_count:
            raise PagesUnavailableError(f"no page {page}")
        pixmap = opened[page - 1].get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=False)
        drawn = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    buffer = io.BytesIO()
    drawn.save(buffer, format=FORMAT, quality=QUALITY)
    drawn.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Written beside and moved into place: two readers asking for the same page
    # at once would otherwise each serve the other's half-written file.
    scratch = out.with_name(f"{out.name}.{id(buffer):x}.part")
    scratch.write_bytes(buffer.getvalue())
    scratch.replace(out)


async def image_for(document_id: str, pdf: Path, page: int) -> Path:
    """One rendered page, from the cache or freshly drawn into it.

    Keyed by the document rather than by the PDF, because for an office file
    the PDF is a rendering in a shared cache and the document is what a reader
    asked about -- and what a delete has to be able to sweep.

    Off the event loop: rendering an A4 page is tens of milliseconds of C, and
    a reader scrolling a long document asks for several at once.
    """
    held = _path_for(document_id, page)
    if held.is_file():
        return held
    await asyncio.to_thread(_render, pdf, page, held)
    return held


__all__ = ["MAX_PAGES", "PagesUnavailableError", "cache_dir", "forget", "image_for", "pdf_of", "sizes"]
