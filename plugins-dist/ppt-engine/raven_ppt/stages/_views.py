"""Turning a built deck into things a model and a measurement can read.

Three concerns the render service deliberately does not hold, because they are
about a caller's budget rather than about rendering:

Concurrency. LibreOffice takes seconds and holds a profile directory; the service
is stateless by design, so the limit lives with whoever is asking. Without one, a
review of a twenty-page deck starts twenty conversions. How many is a fact about
the box rather than about the deck, so the number comes from
``render.default_concurrency`` and only an operator's own setting overrides it.

Blocking calls. The chain is synchronous -- subprocesses and Pillow -- so a stage
inside an event loop has to hand it to a thread or it stops everything else for
the duration.

Byte budget. An image reaching a model is base64 in a request body, and a
1920x1080 page render is 230KB to 1.1MB as a PNG, the top of that range being the
dark photographic pages. Twelve of those is a request no gateway accepts, so the
picture is JPEG by the time it is encoded, then downscaled if it still does not
fit, and both the format and the budget are stated once here rather than guessed
at each call site.

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
from typing import Any

from raven.utils.images import _IMAGE_TOKEN_CAP, detect_image_mime, image_pixel_size
from raven_ppt.services.render import LocalDeckRenderer, RenderError, default_concurrency, sheet

_log = logging.getLogger(__name__)

# What one picture may cost, encoded. Twelve page renders plus a contact sheet is
# the working case, and a gateway that accepts a 20MB body still pays for it in
# latency on every retry.
MAX_IMAGE_BYTES = 900_000
# A sheet is twenty pages, so it is budgeted as twenty pages and not as one. The old
# 1_600_000 had no reader and would have been the worst possible number: measured, the
# largest template's sheet at four columns should carry 600px cells, and halving it to
# fit 1.6MB took it to 300px -- smaller cells than the same template gets on a sheet
# half the size, which is the one outcome a byte budget must not produce.
#
# Measured through this encoder on the eight bundled templates at 96dpi: a template's
# own sheet is 0.98MB to 1.57MB of base64, and the largest sheet the engine builds --
# the 36 borrowable reference pages at six columns -- is 2.50MB. So this is 2.4x
# headroom on the worst case, against the 7x MAX_IMAGE_BYTES gives a page render. A
# sheet always takes the JPEG branch, because the tiler composes in RGB and nothing
# downstream of it can see through.
CONTACT_SHEET_BYTES = 6_000_000
# How many pixels a sheet may reach before it is downscaled. `_IMAGE_TOKEN_CAP` is the
# right number for a picture whose subject fills the frame -- past it the patch count is
# clamped and the extra pixels buy no detail. A sheet's subject is one cell, a twentieth
# of the frame, so the cap has to be reckoned per cell: clipping a sheet's long edge to
# 1568 leaves 232-523px cells, which measures 82% top-1 against 90% for the same sheet
# at four columns and a 3072px edge (and against 90% for twenty separate renders). The
# number is `sheet.DEFAULT_MAX_EDGE`, imported rather than restated so a sheet the tiler
# was willing to build is never a sheet this encoder shrinks.
CONTACT_SHEET_PIXELS = sheet.DEFAULT_MAX_EDGE

# What the model is handed, and the quality it is handed at. A page render is a
# picture of type, so the quality was chosen on whether small copy survives it and
# not on a size target: measured on real pages at 1:1, 96dpi CJK body copy and
# white type on a saturated fill are indistinguishable from the PNG at 85, while 75
# visibly softens the same strokes. `_compressed` leaves chroma unsubsampled for the
# same reason -- these pages are coloured type on coloured fills, where 4:2:0 puts a
# fringe on every stroke and dulls a one-pixel accent rule. Over 97 pages of five
# real decks that is 2.5x fewer bytes than PNG, 4.6x on the dark photographic ones
# whose page renders made a 16.8MB request; it also keeps a dark page under
# MAX_IMAGE_BYTES, which as a PNG it missed by enough to be halved twice and reach
# the model at 784x453.
#
# This is the model's side only. A measurement reads the renders `pages_of` writes,
# which stay PNG: `measure.contrast` opens them to decide ink against ground,
# `render.pdf._unusable` counts their colours, `template.theme` samples them for the
# ground, and `render.sheet` composes them. No measurement goes through `data_uri`.
MODEL_IMAGE_MIME = "image/jpeg"
MODEL_IMAGE_QUALITY = 85


@dataclass
class DeckViews:
    """One deck's renders, on a leash."""

    renderer: object = field(default_factory=LocalDeckRenderer)
    dpi: int = 144
    concurrency: int = field(default_factory=default_concurrency)
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

    def contact_sheet(
        self,
        pngs: Sequence[Path],
        out: Path,
        columns: int = sheet.DEFAULT_COLUMNS,
        *,
        labels: Sequence[str] | None = None,
    ) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        return self.renderer.contact_sheet(list(pngs), out, columns, labels=labels)

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

    def sheet_uri(self, png: Path) -> str:
        """A contact sheet as a data URI, on the sheet's budget and the sheet's cap.

        Its own door rather than an argument on ``data_uri`` because the two pictures
        are budgeted on different things and only one of them is a page: a page is
        judged as itself and the per-picture cap is the token formula's, while a sheet
        is judged one cell at a time and the same cap would take a 768px cell down to
        392px. ``data_uri`` is what a page still comes through -- ``ppt_review`` reads
        one page per call and needs it at full size -- and that path is untouched.
        """
        return _encoded(png.read_bytes(), CONTACT_SHEET_BYTES, cap=CONTACT_SHEET_PIXELS)


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


def _fitted(raw: bytes, cap: int = _IMAGE_TOKEN_CAP) -> bytes:
    """``raw`` with its long edge brought under the cap, or ``raw`` unchanged.

    A budget in bytes does not bound pixels, and the two come apart on exactly the
    picture this is most likely to be handed: a 3018x1528 logo of flat colour
    encodes small enough to pass the budget untouched, so it went to the endpoint
    at full width. Anthropic allows 2000px per side once a request carries more
    than twenty images, and a run that had fetched three figures and was reviewing
    its own renders was well past that -- it died on the 28th content block of one
    message, 47 iterations and 13 dollars in, having published nothing.

    The default cap is the token formula's, imported rather than restated so the two
    cannot drift: past 1568 the patch count is clamped, so the extra pixels buy no
    detail the model can see and cost bytes and this refusal. Downscaling is silent
    because there is nothing for an author to decide -- the picture the model gets
    is the same picture.

    A caller passes its own cap only when the frame is not the subject. A contact
    sheet is the one such picture here, and `CONTACT_SHEET_PIXELS` says why.
    """
    size = image_pixel_size(raw)
    if size is None:
        # A format the header parser does not read. Passing it through is what
        # happened before this function existed.
        return raw
    if max(size) <= cap:
        return raw
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the ppt extra
        return raw
    scale = cap / max(size)
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


def _sees_through(raw: bytes) -> bool:
    """True when the picture carries a pixel the reader is meant to see through.

    Transparency is the thing a keyed illustration is judged on -- whether the
    green screen came out cleanly -- and JPEG has nowhere to put it, so such a
    picture stays a PNG. Asked of the bytes the caller handed over rather than of
    the fitted ones, because fitting converts to RGB and would answer no for a
    picture that arrived with an alpha channel.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the ppt-engine wheel
        return False
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.mode in ("LA", "PA", "RGBA"):
                return image.getchannel("A").getextrema()[0] < 255
            return image.mode == "P" and "transparency" in image.info
    except OSError:
        return False


def _compressed(image: Any, mime: str) -> bytes:
    buffer = io.BytesIO()
    if mime == "image/png":
        image.save(buffer, format="PNG", optimize=True)
    else:
        image.save(buffer, format="JPEG", quality=MODEL_IMAGE_QUALITY, subsampling=0, optimize=True, progressive=True)
    return buffer.getvalue()


def _uri(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def _encoded(raw: bytes, budget: int, cap: int = _IMAGE_TOKEN_CAP) -> str:
    mime = "image/png" if _sees_through(raw) else MODEL_IMAGE_MIME
    raw = _fitted(raw, cap)
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the ppt extra
        return _uri(detect_image_mime(raw) or "image/png", raw)
    if mime != "image/png":
        try:
            with Image.open(io.BytesIO(raw)) as image:
                raw = _compressed(image.convert("RGB"), mime)
        except OSError:
            # A picture Pillow will not open is sent as it arrived, labelled by its
            # own magic bytes: the alternative is no picture at all.
            return _uri(detect_image_mime(raw) or "image/png", raw)
    encoded = base64.b64encode(raw)
    if len(encoded) <= budget:
        return f"data:{mime};base64," + encoded.decode("ascii")
    # Two halvings answer any page render against this budget and the loop allows a
    # third; past that it stops rather than shrinking a page to something
    # unreadable, because an image too small to judge is worse than a slightly
    # expensive one.
    with Image.open(io.BytesIO(raw)) as image:
        current = image.convert("RGB")
        for _ in range(3):
            current = current.resize((max(1, current.width // 2), max(1, current.height // 2)), Image.LANCZOS)
            encoded = base64.b64encode(_compressed(current, mime))
            if len(encoded) <= budget:
                return f"data:{mime};base64," + encoded.decode("ascii")
    return f"data:{mime};base64," + encoded.decode("ascii")
