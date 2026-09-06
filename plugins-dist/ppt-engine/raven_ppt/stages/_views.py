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

And one concern the fork's host used to hold: page identity. The fork's loop
kept each render's label beside it when an endpoint refused tool-role images
(its ``labelled_images``); the trunk loop's demotion moves the pictures alone,
so a page's number has to survive inside the picture. ``data_uri`` takes an
optional ``label`` and paints it on a strip added BELOW the page -- the page's
own pixels are untouched, so the second reader and the measurements judge the
same page the author built, and the strip reads as apparatus rather than as a
mark on the page.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from raven.utils.images import _IMAGE_TOKEN_CAP, image_pixel_size
from raven_ppt.services.render import LocalDeckRenderer, RenderError

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
        # The PDF already made from this very file is the PDF. A build converted the
        # deck once to measure it and once more to show its pages, 5s each on a 25MB
        # deck; the second is the same file, told apart by the pptx being older.
        made = out_dir / f"{pptx.stem}.pdf"
        try:
            if made.is_file() and made.stat().st_mtime_ns > pptx.stat().st_mtime_ns and made.stat().st_size > 0:
                return made
        except OSError:
            pass
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

    def data_uri(self, png: Path, budget: int | None = None, label: str | None = None) -> str:
        """The image as a data URI, downscaled until it fits its budget.

        ``label`` paints the given text on a strip added below the picture, for
        a render whose identity has to survive a host that separates pictures
        from the text beside them. Added before the fitting, so the budget and
        the pixel cap govern the labelled image the model actually receives.
        """
        raw = png.read_bytes()
        if label:
            raw = _labelled(raw, label)
        return _encoded(raw, budget or self.max_image_bytes)


def _labelled(raw: bytes, label: str) -> bytes:
    """``raw`` with ``label`` painted on a strip added below it.

    A strip rather than an overlay: the page's own pixels are what the second
    reader judges and what the author is answering for, so identity is carried
    on new canvas under the page, not painted over it. Best-effort -- an image
    Pillow cannot open, or a Pillow that is absent, passes through unlabelled,
    which is exactly the fork's behaviour (its loop-side labels are gone here
    either way).
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - Pillow ships with the ppt-engine wheel
        return raw
    try:
        with Image.open(io.BytesIO(raw)) as image:
            page = image.convert("RGB")
    except OSError:
        return raw
    strip = max(18, page.height // 36)
    canvas = Image.new("RGB", (page.width, page.height + strip), (32, 32, 32))
    canvas.paste(page, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((6, page.height + max(2, (strip - 11) // 2)), label, fill=(255, 255, 255))
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _fitted(raw: bytes) -> bytes:
    """``raw`` with its long edge brought under the cap, or ``raw`` unchanged.

    A budget in bytes does not bound pixels, and the two come apart on exactly the
    picture this is most likely to be handed: a 3018x1528 logo of flat colour
    encodes small enough to pass the budget untouched, so it went to the endpoint
    at full width. Anthropic allows 2000px per side once a request carries more
    than twenty images, and a run that had fetched three figures and was reviewing
    its own renders was well past that -- it died on the 28th content block of one
    message, 47 iterations and 13 dollars in, having published nothing.

    The cap is the token formula's, imported rather than restated so the two cannot
    drift: past 1568 the patch count is clamped, so the extra pixels buy no detail
    the model can see and cost bytes and this refusal. Downscaling is silent
    because there is nothing for an author to decide -- the picture the model gets
    is the same picture.
    """
    size = image_pixel_size(raw)
    if size is None:
        # A format the header parser does not read. Passing it through is what
        # happened before this function existed.
        return raw
    if max(size) <= _IMAGE_TOKEN_CAP:
        return raw
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the ppt extra
        return raw
    scale = _IMAGE_TOKEN_CAP / max(size)
    wanted = (max(1, round(size[0] * scale)), max(1, round(size[1] * scale)))
    with Image.open(io.BytesIO(raw)) as image:
        palette = image.mode in ("P", "PA")
        # Resampled in RGB whatever the source is, because LANCZOS over palette
        # *indices* averages numbers that mean nothing. A palette source is then
        # returned to one: it held at most 256 colours to begin with, so the
        # quantisation gives back what it already was, and skipping it is what
        # turns a flat-colour logo into eight times its own bytes -- which the
        # history budget is counted in, so it would trade this refusal for
        # emergency shrinking.
        shrunk = image.convert("RGB").resize(wanted, Image.LANCZOS)
        if palette:
            shrunk = shrunk.quantize(colors=256, method=Image.MEDIANCUT)
        buffer = io.BytesIO()
        shrunk.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _encoded(raw: bytes, budget: int) -> str:
    raw = _fitted(raw)
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
