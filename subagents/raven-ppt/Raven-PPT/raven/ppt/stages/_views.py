"""Turning a built deck into things a model and a measurement can read.

Three concerns the render service deliberately does not hold, because they are
about a caller's budget rather than about rendering:

Concurrency. LibreOffice takes seconds and holds a profile directory; the service
is stateless by design, so the limit lives with whoever is asking. Without one, a
review of a twenty-page deck starts twenty conversions.

Blocking calls. The chain is synchronous -- subprocesses and Pillow -- so a stage
inside an event loop has to hand it to a thread or it stops everything else for
the duration.

Byte budget. An image reaching a model is base64 in a request body, and a
1920x1080 PNG is around 400KB before encoding. Twelve of those is a request no
gateway accepts, so a page render is downscaled until it fits and the budget is
stated once here rather than guessed at each call site.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from raven.ppt.services.render import LocalDeckRenderer, RenderError

_log = logging.getLogger(__name__)

# What one picture may cost, encoded. Twelve page renders plus a contact sheet is
# the working case, and a gateway that accepts a 20MB body still pays for it in
# latency on every retry.
MAX_IMAGE_BYTES = 900_000
CONTACT_SHEET_BYTES = 1_600_000


@dataclass
class DeckViews:
    """One deck's renders, on a leash."""

    renderer: object = field(default_factory=LocalDeckRenderer)
    dpi: int = 144
    concurrency: int = 2
    max_image_bytes: int = MAX_IMAGE_BYTES

    def __post_init__(self) -> None:
        self._gate = asyncio.Semaphore(self.concurrency)

    async def pdf(self, pptx: Path, out_dir: Path) -> Path | None:
        """The deck as one PDF, or None when nothing here can convert it.

        None rather than an exception: a deck that cannot be rendered can still
        be built, gated on its content and delivered, and the measurements that
        need a render simply do not run. Refusing the whole call because
        LibreOffice is absent would make an optional dependency a required one.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        async with self._gate:
            try:
                return await asyncio.to_thread(self.renderer.to_pdf, pptx, out_dir)
            except RenderError:
                # Logged, because the caller turns an empty answer into a sentence
                # for the author and cannot tell "LibreOffice is not installed"
                # from "this one conversion failed".
                _log.warning("render: %s did not convert to pdf", pptx.name, exc_info=True)
                return None

    async def pages(self, pptx: Path, out_dir: Path, numbers: Sequence[int] | None = None) -> dict[int, Path]:
        """One PNG per page, keyed by page number. Empty when rendering is absent."""
        pdf = await self.pdf(pptx, out_dir)
        if pdf is None:
            return {}
        return await self.pages_of(pdf, out_dir, numbers)

    async def pages_of(self, pdf: Path, out_dir: Path, numbers: Sequence[int] | None = None) -> dict[int, Path]:
        wanted = list(numbers) if numbers else None
        async with self._gate:
            try:
                pngs = await asyncio.to_thread(self.renderer.to_pngs, pdf, out_dir, self.dpi, wanted)
            except RenderError:
                # The empty dict reaches `ppt_template` as "renders unavailable on
                # this machine", which the author reads as a fact about the host and
                # acts on for the rest of the deck: one live run answered it by
                # reading every example page as code and never looked at a render
                # again, on a host where the pdf and four page renders had just
                # been written. Whatever went wrong here is the only thing that can
                # say whether that sentence is true, and it went nowhere.
                _log.warning("render: %s pages of %s did not render", len(wanted or []), pdf.name, exc_info=True)
                return {}
        return {number: path for number, path in zip(wanted or range(1, len(pngs) + 1), pngs, strict=False)}

    def contact_sheet(self, pngs: Sequence[Path], out: Path, columns: int = 3) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        return self.renderer.contact_sheet(list(pngs), out, columns)

    def data_uri(self, png: Path, budget: int | None = None) -> str:
        """The image as a data URI, downscaled until it fits its budget."""
        return _encoded(png, budget or self.max_image_bytes)


def _encoded(png: Path, budget: int) -> str:
    raw = png.read_bytes()
    if len(base64.b64encode(raw)) <= budget:
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the ppt extra
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    # Two halvings answer any page render against this budget and the loop allows a
    # third; past that it stops rather than shrinking a page to something
    # unreadable, because an image too small to judge is worse than a slightly
    # expensive one.
    with Image.open(io.BytesIO(raw)) as image:
        current = image.convert("RGB")
        for _ in range(3):
            current = current.resize((max(1, current.width // 2), max(1, current.height // 2)), Image.LANCZOS)
            buffer = io.BytesIO()
            current.save(buffer, format="PNG", optimize=True)
            encoded = base64.b64encode(buffer.getvalue())
            if len(encoded) <= budget:
                return "data:image/png;base64," + encoded.decode("ascii")
    return "data:image/png;base64," + encoded.decode("ascii")
