"""One chain: `.pptx` -> `.pdf` -> one `.png` per page, plus word boxes off the PDF.

That is the whole of what deck authoring needs from rendering, and it is stated as
a whole here because the shape of this package is a decision rather than an
accident.

The predecessor fork carried a general-purpose `raven/rendering/` -- twenty-two
modules: a browser runtime driving headless Chromium, spreadsheet normalisation
through a UNO worker, animated-image decoding, motion classification with
keyframe extraction, per-format detection with support levels, a bundle layout
with manifests and hashes, a path policy, and a container backend that ran the
whole thing in a `boxlite` VM. A deck reaches exactly one path through it:
LibreOffice to PDF, PDF to PNG. Everything else exists for the other formats that
service also renders -- HTML, xlsx, GIF, video -- which deck authoring never
sends it.

Moving it wholesale would have imported all of that: dead branches that still
have to be read when this chain misbehaves, dependencies (`fitz`, `boxlite`) that
a deck never needs, a `RenderConfig` with sixty fields of which four apply, and a
`RenderRequest`/`RenderOutcome` pair whose bundle-relative records every caller
had to walk to find "the PNG for page 3". So this is written from scratch against
the two facts that matter -- LibreOffice's profile locking, and the PDF coordinate
convention -- in about 650 lines of code where that package holds 5200.

What is deliberately *not* here, and where it goes if it is ever wanted: browser
rendering, spreadsheets, animation and motion analysis (none of them describe a
deck); container isolation, format detection, bundle manifests and inline-preview
budgets (the last two belong to a tool's return shape, not to rendering). The slot
route's page previews used to be rasterised straight from compiled SVG with cairo
to skip LibreOffice's seconds during composition; that is the slot backend's own
shortcut and lands with it, not here, because it renders an intermediate rather
than the deliverable.

The extension slot is `DeckRenderer`, the protocol in `service.py`: a full stack,
a container, a remote worker all arrive as another implementation of it, and no
caller changes.
"""

from raven_ppt.services.render.capabilities import RenderCapabilities, available
from raven_ppt.services.render.errors import RenderError, RenderTimeoutError, RenderUnavailableError
from raven_ppt.services.render.office import DEFAULT_CONVERT_TIMEOUT_S, default_concurrency, to_pdf
from raven_ppt.services.render.pdf import (
    DEFAULT_DPI,
    MAX_DPI,
    PAGE_PNG,
    is_page_render,
    page_count,
    page_number,
    page_pixels,
    page_sizes,
    to_pngs,
    unusable_pages,
    word_boxes,
)
from raven_ppt.services.render.service import DeckRenderer, LocalDeckRenderer
from raven_ppt.services.render.sheet import contact_sheet

__all__ = [
    "DEFAULT_CONVERT_TIMEOUT_S",
    "DEFAULT_DPI",
    "MAX_DPI",
    "PAGE_PNG",
    "DeckRenderer",
    "LocalDeckRenderer",
    "RenderCapabilities",
    "RenderError",
    "RenderTimeoutError",
    "RenderUnavailableError",
    "available",
    "contact_sheet",
    "default_concurrency",
    "is_page_render",
    "page_count",
    "page_number",
    "page_pixels",
    "page_sizes",
    "to_pdf",
    "to_pngs",
    "unusable_pages",
    "word_boxes",
]
