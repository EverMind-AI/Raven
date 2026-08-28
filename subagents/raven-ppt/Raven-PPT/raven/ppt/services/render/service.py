"""The one object the rest of the capability holds to see a deck.

Callers depend on `DeckRenderer`, the protocol, and are handed
`LocalDeckRenderer`, the implementation that shells out to LibreOffice and reads
the PDF in-process. The split is the extension slot: the predecessor's render
service could also drive a browser, normalise spreadsheets, capture motion and run
the whole conversion inside a container, and if any of that is ever wanted here
again it arrives as a second implementation of this protocol rather than as an
argument threaded through every call site.

**These calls block, for seconds.** LibreOffice on a real deck takes single-digit
seconds cold; rasterising forty pages takes a few more. A stage must therefore
reach them through `asyncio.to_thread`, not call them directly on the event loop --
an agent whose loop is parked for eleven seconds stops answering. Threads are safe
here by construction: every conversion is its own process with its own profile
directory and its own temporary space, and nothing in this package holds mutable
state between calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from raven.ppt.contracts.rendered import PageSize, WordBox
from raven.ppt.services.render import office, sheet
from raven.ppt.services.render import pdf as pdf_reader
from raven.ppt.services.render.capabilities import RenderCapabilities, available


class DeckRenderer(Protocol):
    """pptx -> pdf -> png, plus what can be measured off the pdf."""

    def available(self) -> RenderCapabilities: ...

    def to_pdf(self, pptx: Path, out_dir: Path) -> Path: ...

    def to_pngs(self, pdf: Path, out_dir: Path, dpi: int | None = ..., pages: list[int] | None = ...) -> list[Path]: ...

    def word_boxes(self, pdf: Path) -> dict[int, list[WordBox]]: ...

    def page_sizes(self, pdf: Path) -> dict[int, PageSize]: ...

    def page_count(self, pdf: Path) -> int: ...

    def contact_sheet(self, pngs: list[Path], out: Path, columns: int = ...) -> Path: ...


@dataclass(frozen=True)
class LocalDeckRenderer:
    """The local chain: LibreOffice for the conversion, PDFium or poppler for the rest.

    Frozen and cheap to construct, so a caller with no configuration to pass can
    build one where it needs it rather than threading an instance through.
    """

    soffice: str | None = None
    convert_timeout_s: float = office.DEFAULT_CONVERT_TIMEOUT_S
    poppler_timeout_s: float = pdf_reader.DEFAULT_POPPLER_TIMEOUT_S
    dpi: int = pdf_reader.DEFAULT_DPI
    contact_sheet_max_edge: int = sheet.DEFAULT_MAX_EDGE

    def available(self) -> RenderCapabilities:
        return available(soffice=self.soffice)

    def to_pdf(self, pptx: Path, out_dir: Path) -> Path:
        return office.to_pdf(pptx, out_dir, soffice=self.soffice, timeout_s=self.convert_timeout_s)

    def to_pngs(
        self,
        pdf: Path,
        out_dir: Path,
        dpi: int | None = None,
        pages: list[int] | None = None,
    ) -> list[Path]:
        return pdf_reader.to_pngs(pdf, out_dir, dpi or self.dpi, pages, timeout_s=self.poppler_timeout_s)

    def word_boxes(self, pdf: Path) -> dict[int, list[WordBox]]:
        return pdf_reader.word_boxes(pdf, timeout_s=self.poppler_timeout_s)

    def page_sizes(self, pdf: Path) -> dict[int, PageSize]:
        return pdf_reader.page_sizes(pdf, timeout_s=self.poppler_timeout_s)

    def page_count(self, pdf: Path) -> int:
        return pdf_reader.page_count(pdf, timeout_s=self.poppler_timeout_s)

    def contact_sheet(self, pngs: list[Path], out: Path, columns: int = sheet.DEFAULT_COLUMNS) -> Path:
        return sheet.contact_sheet(pngs, out, columns, max_edge=self.contact_sheet_max_edge)
