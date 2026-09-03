"""Pixel work on an extracted figure: trim it, measure it, cut a panel out.

Three source realities drive everything here. Bitmaps pulled out of
"print to PDF" exports arrive as one huge uniform bar (often 60%+ black
letterboxing) around a thin strip of real content; vector renders of paper
figures carry white margins; and product materials ship whole-page
screenshots, while papers ship one figure holding six labelled panels. Placed
as they come, the first two waste most of a slot on border and the last two
shrink past the point where their own text is readable.

So: :func:`autocrop_border` trims a uniform border in place and conservatively
-- any doubt (mixed corner colours, an empty or tiny content box, an
unreadable file) leaves the image untouched. :func:`estimate_panels` and
:func:`detect_panels` find the panels, :func:`segment_page_blocks` finds a
page's content blocks, and :func:`crop_figure` performs the one operation that
rescues either case: cut out the part actually being cited.

Every measurement is fail-soft: an unreadable image reports the value that
warns about nothing, because a failed read is not evidence against a figure.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover -- pillow lives in the render extra
    from PIL import Image

FIGURE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif"})

# Border colour candidate: per-channel median over the four NxN corner
# patches. Corners disagreeing beyond the spread mean "no uniform border".
_CORNER_PATCH = 3
_MAX_CORNER_SPREAD = 16
# Per-channel binarisation tolerance against the border colour; absorbs JPEG
# ringing next to hard content edges.
_DIFF_TOLERANCE = 16
# Breathing room kept around the content box, per side, off the shorter edge.
# It exists so a crop cannot shave an antialiased glyph edge, which a few
# pixels covers; taken off the width instead, a 1800px-wide figure got 36px of
# white added back per side and arrived on the page still framed. A deck sets
# its own margins around a figure and does not need the figure to carry one.
_PAD_FRACTION = 0.005
# Rewriting re-encodes (JPEG generation loss): only crop when at least this
# fraction of the pixel area is border. A tenth of a page is a lot of frame to
# keep for the sake of one re-encode -- an extracted table framed in 5% white
# was left as it came, and that white then scaled up with the table.
_MIN_CROP_AREA_RATIO = 0.04
# A result this small on *both* sides is likelier a detection artefact than a
# figure. One small side is a shape, not an artefact: a letterboxed strip crops
# to something wide and thin, and it is that crop which lets the concerns
# downstream see the strip for what it is. Taking `min` here instead would
# throw away every letterbox trim, and taking `max` alone would let a 30x130
# stray mark eat a 1800x1300 page -- hence both bounds, the second guarded by
# the letterbox span test below.
_MIN_RESULT_PX = 120
_LETTERBOX_SPAN = 0.6
_JPEG_QUALITY = 92

# A crop this small cannot carry a readable panel, so a box that yields it is a
# mistake worth reporting rather than a tiny figure worth keeping.
_MIN_CROP_PX = 32
# A crop that comes back a banner strip, or holding no visible content at all,
# is a mis-aimed box rather than a figure -- the caller is told so instead of
# the deck getting a black bar.
_CROP_MAX_ASPECT = 6.0
_CROP_MIN_ASPECT = 1 / 6.0
_CROP_MIN_INK = 0.004
_ALPHA_CONTENT_THRESHOLD = 8
_FRAME_MARGIN_FRACTION = 0.12
_FRAME_LINE_COVERAGE = 0.60
# Panel detection works on a downscale: separator bands are relative to the
# figure's own size, and full resolution only costs time.
_PANEL_WORK_PX = 400
# A row/column counts as background when only a couple of stray pixels carry
# ink: real gutters between panels are empty, while a sparse line plot still
# puts a few pixels on every interior row and must stay one band.
_PANEL_INK_FRACTION = 0.005
# Gutters between panels are wide; anything narrower is line spacing inside one
# panel.
_PANEL_GAP_FRACTION = 0.06
# Bands holding a sliver of the figure's ink are axis labels and captions, not
# panels.
_PANEL_MIN_INK_SHARE = 0.08
_PANEL_CAP = 12
# Photographs and screenshots differ from almost every background pixel;
# charts and diagrams leave most of the frame empty.
DETAIL_INK_MAX = 0.5
# Ink measurement works on a downscale -- the ratio is scale-invariant.
_INK_WORK_PX = 300
# A box drawn by eye lands near, not on, the gutter between panels: search this
# far for one before cutting where it was asked.
_SNAP_WINDOW_FRACTION = 0.08
# Border strip sampled to tell a clean panel cut from one through a diagram.
_EDGE_STRIP_FRACTION = 0.03
_CUT_EDGE_MIN = 0.01
_CUT_EDGE_MAX = 0.90
# A gutter may carry a stray tick or antialiased pixel and still be a gutter.
_GUTTER_INK_TOLERANCE = 0.02
# Panel detection: a line counts as inked above 1% of its cross-span, and a
# gutter must be at least 1.5% of the figure -- measured at 2-3% on real
# multi-panel paper figures, while line spacing inside a panel is under 1%.
_PANEL_LINE_INK = 0.01
_PANEL_GUTTER_FRACTION = 0.015
_PANEL_MIN_SIZE = 0.08

# Page segmentation (whole-page screenshots). Run-length smoothing closes the
# gaps inside a content block so it comes out as one connected region; the gaps
# are page fractions, measured against real product pages where a headline sits
# ~1% of the page above its image.
_SEG_WORK_PX = 900
_SEG_GAP_X = 0.020
_SEG_GAP_Y = 0.012
_SEG_DIFF = 24
# A block below any of these is page furniture (a nav bar, a caption, a
# button); one above the span cap is the page itself, not a block in it.
_SEG_MIN_AREA = 0.010
_SEG_MIN_SIDE = 0.05
_SEG_MIN_FILL = 0.15
_SEG_MAX_SPAN = 0.92
# A slide wants the subject with air around it, not a shrink-wrapped strip: a
# 12:1 crop of a laptop edge is a real product shot, and padding it back into
# the page's own background makes it placeable.
_SEG_MAX_ASPECT = 2.4
_SEG_MARGIN = 0.015
# Two crops of one photograph is worse than one: measured at 0.83 overlap on a
# real product page, where padding pushed two components of the same image
# together.
_SEG_MAX_OVERLAP = 0.6


def contain_scale(native: tuple[int, int], frame: tuple[float, float]) -> float:
    """How far ``native`` pixels shrink to fit inside ``frame``."""
    native_w, native_h = native
    frame_w, frame_h = frame
    if min(native_w, native_h, frame_w, frame_h) <= 0:
        return 0.0
    return min(frame_w / native_w, frame_h / native_h)


def ink_ratio(path: Path) -> float:
    """Fraction of pixels differing from the border colour.

    Separates line art (charts, diagrams: mostly background) from photographs
    and screenshots (nearly every pixel is content). Reports 1.0 -- the
    photograph end -- for an unreadable image, so nothing is warned about on
    the strength of a failed read.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.load()
            rgb = _flatten_rgb(img)
            background = _uniform_border_color(rgb) or (255, 255, 255)
            data = _scaled_mask(_analysis_mask(img, rgb, background), _INK_WORK_PX).tobytes()
    except Exception:  # noqa: BLE001 -- advisory metric; never fails the ingest
        return 1.0
    return sum(1 for value in data if value) / len(data) if data else 1.0


def interior_ink_ratio(path: Path) -> float:
    """Content fraction after excluding an otherwise empty outer frame.

    An extraction that caught a page's decorative rule, or a figure's own
    border, arrives as a large image with nothing in it. Measuring the whole
    frame calls that content; measuring what the frame encloses does not.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.load()
            rgb = _flatten_rgb(img)
            center = rgb.crop(
                (
                    round(rgb.width * 0.4),
                    round(rgb.height * 0.4),
                    round(rgb.width * 0.6),
                    round(rgb.height * 0.6),
                )
            )
            background = _uniform_border_color(center) or _uniform_border_color(rgb) or (255, 255, 255)
            mask = _scaled_mask(_analysis_mask(img, rgb, background), _INK_WORK_PX)
    except Exception:  # noqa: BLE001
        return 1.0
    ratio = _content_ratio_without_blank_frame(mask)
    if ratio >= _CROP_MIN_INK:
        return ratio
    # A flat coloured panel reads as background against itself and scores zero
    # ink. A saturated one is a deliberate block of colour, which is content.
    extrema = center.getextrema()
    if all(high - low <= _DIFF_TOLERANCE for low, high in extrema):
        color = center.resize((1, 1)).getpixel((0, 0))
        if max(color) - min(color) > _DIFF_TOLERANCE:
            return 1.0
    return ratio


def estimate_panels(path: Path) -> int:
    """How many sub-panels a figure holds (1 = a single plot).

    Counts ink bands separated by wide background gutters along both axes.
    An unreadable image reports a single panel.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.load()
            rgb = _flatten_rgb(img)
            background = _uniform_border_color(rgb) or (255, 255, 255)
            mask = _scaled_mask(_analysis_mask(img, rgb, background), _PANEL_WORK_PX)
            width, height = mask.size
            ink = mask.tobytes()
            row_profile = [sum(1 for v in ink[y * width : (y + 1) * width] if v) for y in range(height)]
            col_profile = [sum(1 for y in range(height) if ink[y * width + x]) for x in range(width)]
            rows = _band_count(row_profile, width)
            cols = _band_count(col_profile, height)
    except Exception:  # noqa: BLE001 -- advisory metric; never fails the ingest
        return 1
    return max(1, min(_PANEL_CAP, rows * cols))


def detect_panels(path: Path) -> list[tuple[float, float, float, float]]:
    """Sub-panel boxes of a composite figure, as normalised (x0, y0, x1, y1).

    Real paper figures separate their panels by gutters only 2-3% of the figure
    wide, so a model cannot reliably eyeball a crop box -- measured on two ICLR
    figures, guessed boxes sliced panels in half. Detecting the gutters lets a
    panel be cut by index instead. Returns [] for a single panel or an
    unreadable file.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.load()
            rgb = _flatten_rgb(img)
            background = _uniform_border_color(rgb) or (255, 255, 255)
            mask = _analysis_mask(img, rgb, background)
            width, height = mask.size
            data = mask.tobytes()
            col_ink = [sum(1 for y in range(height) if data[y * width + x]) for x in range(width)]
            row_ink = [sum(1 for v in data[y * width : (y + 1) * width] if v) for y in range(height)]
    except Exception:  # noqa: BLE001 -- advisory metric
        return []
    x_bands = _ink_bands(col_ink, height, width)
    y_bands = _ink_bands(row_ink, width, height)
    if len(x_bands) * len(y_bands) < 2:
        return []
    return [(x0 / width, y0 / height, x1 / width, y1 / height) for (y0, y1) in y_bands for (x0, x1) in x_bands]


def edge_cut_share(path: Path, *, sides: frozenset[str] | None = None) -> float:
    """How much of the busiest edge has content running across it.

    Measured per line of the edge strip rather than over its whole area,
    because that is what separates the three cases. A photograph fills every
    line (nothing to cut at -- it has no margins). A crop taken on a layout's
    own margins fills none. A cut *through* something -- a text line, a plot, a
    label -- fills the lines it crosses and leaves the rest quiet, which is the
    sign that a better box was available.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.load()
            rgb = _flatten_rgb(img)
            background = _uniform_border_color(rgb) or (255, 255, 255)
            mask = _analysis_mask(img, rgb, background)
            width, height = mask.size
            data = mask.tobytes()
            strip_y = max(1, round(height * _EDGE_STRIP_FRACTION))
            strip_x = max(1, round(width * _EDGE_STRIP_FRACTION))

            def share(lines) -> float:
                inked = sum(1 for line in lines if line)
                return inked / len(lines) if lines else 0.0

            columns = range(width)
            rows = range(height)
            edge_shares = {
                "top": share([any(data[y * width + x] for y in range(strip_y)) for x in columns]),
                "bottom": share([any(data[y * width + x] for y in range(height - strip_y, height)) for x in columns]),
                "left": share([any(data[y * width + x] for x in range(strip_x)) for y in rows]),
                "right": share([any(data[y * width + x] for x in range(width - strip_x, width)) for y in rows]),
            }
    except Exception:  # noqa: BLE001 -- advisory metric
        return 0.0
    selected = edge_shares.values() if sides is None else (edge_shares[side] for side in sides)
    return max(selected, default=0.0)


def crop_figure(
    src: Path,
    box: tuple[float, float, float, float],
    dst: Path,
    *,
    autocrop: bool = True,
    snap: bool = True,
    reject_cut_edges: bool = False,
    cut_edge_notes: list[str] | None = None,
) -> tuple[int, int]:
    """Write the ``(x0, y0, x1, y1)`` fraction of ``src`` to ``dst``.

    Coordinates are fractions of the source figure with the origin top-left.
    Returns the written ``(width, height)``. Raises ``ValueError`` with a
    message meant for the caller's model when the box is degenerate, out of
    range, or would yield an unusably small image. ``dst`` is written
    atomically, so a rejected crop leaves whatever was there before.
    """
    from PIL import Image

    x0, y0, x1, y1 = (float(v) for v in box)
    for name, value in (("x0", x0), ("y0", y0), ("x1", x1), ("y1", y1)):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"crop box values are fractions of the figure: {name}={value} is outside 0..1")
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"crop box must satisfy x0 < x1 and y0 < y1 (got x0={x0}, x1={x1}, y0={y0}, y1={y1})")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{dst.stem}.", suffix=".png", dir=dst.parent, delete=False) as handle:
        staged = Path(handle.name)
    try:
        with Image.open(src) as img:
            img.load()
            width, height = img.size
            left, top = round(x0 * width), round(y0 * height)
            right, bottom = round(x1 * width), round(y1 * height)
            if min(right - left, bottom - top) < _MIN_CROP_PX:
                raise ValueError(
                    f"crop box yields {right - left}x{bottom - top}px from a {width}x{height}px figure — "
                    "too small to read; widen the box"
                )
            if snap:
                rgb = _flatten_rgb(img)
                background = _uniform_border_color(rgb) or (255, 255, 255)
                mask = _analysis_mask(img, rgb, background)
                left, top, right, bottom = _snap_box_to_gutters(mask, (left, top, right, bottom))
            cropped = img.crop((left, top, right, bottom))
            size = cropped.size
            cropped.save(staged, format="PNG")
        _reject_unusable_crop(staged, size)
        if reject_cut_edges or cut_edge_notes is not None:
            _report_cut_edges(
                staged,
                moved=(("left", left > 0), ("top", top > 0), ("right", right < width), ("bottom", bottom < height)),
                notes=cut_edge_notes,
            )
        if autocrop:
            trimmed = autocrop_border(staged)
            if min(trimmed) > 0:
                size = trimmed
            _reject_unusable_crop(staged, size)
        os.replace(staged, dst)
        return size
    finally:
        staged.unlink(missing_ok=True)


def _report_cut_edges(staged: Path, *, moved, notes: list[str] | None) -> None:
    share = edge_cut_share(staged, sides=frozenset(side for side, was_moved in moved if was_moved))
    if not _CUT_EDGE_MIN < share < _CUT_EDGE_MAX:
        return
    note = (
        f"an edge of this crop has ink across {share:.0%} of it, so the cut may run through a line of "
        "text, a plot or a label rather than along a margin. Look at the crop: if something is sliced in "
        "half, crop again on the gap the figure already has"
    )
    # Warning, not refusal, when the caller asked to be told. Only a clean
    # gutter or a solid fill clears the band this measures, and a figure
    # printed above a caption has neither -- so refusing turned "look at it and
    # nudge the edge" into guessing at a number the caller could not see. It
    # has eyes; the crop goes back with the warning attached.
    if notes is not None:
        notes.append(note)
    else:
        raise ValueError(note)


def _reject_unusable_crop(dst: Path, size: tuple[int, int]) -> None:
    """Report a crop that is too small, a strip, or holds no content.

    A box aimed slightly off a panel comes back as a sliver of gutter or a band
    of letterbox black. Placed, it reads as a defect on the slide, and nothing
    downstream can tell it from a deliberate figure -- so the crop fails here,
    where the caller can still re-aim the box.
    """
    width, height = size
    aspect = width / height if height else 0.0
    problem = ""
    if min(width, height) < _MIN_CROP_PX:
        problem = "too small to read after trimming"
    elif aspect > _CROP_MAX_ASPECT or (aspect and aspect < _CROP_MIN_ASPECT):
        problem = f"aspect {aspect:.1f}:1 — a strip, not a panel; the box is far off in one dimension"
    else:
        ink = interior_ink_ratio(dst)
        if ink < _CROP_MIN_INK:
            problem = (
                f"{ink:.1%} of it carries any content — the box landed on a margin or a flat "
                "background band rather than the panel"
            )
    if not problem:
        return
    raise ValueError(f"crop yields {width}x{height}px and {problem}; look at the figure again and re-aim the box")


def segment_page_blocks(path: Path, *, limit: int = 3) -> list[tuple[float, float, float, float]]:
    """Content blocks inside a whole-page screenshot, as fraction boxes.

    A page screenshot placed whole is unreadable, and asking a model to eyeball
    a crop box out of one is where product decks lose their product shots. The
    page's own layout says where its blocks are: smooth the non-background
    pixels into regions, keep the ones big and dense enough to carry a slide,
    and pad each back out to a placeable shape.

    Largest first and capped, so ingest stays a fixed cost. Returns [] when the
    image cannot be read or holds no separable block.
    """
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as img:
            img.load()
            full = _flatten_rgb(img)
        scale = min(1.0, _SEG_WORK_PX / max(full.size))
        small = full.resize((max(1, round(full.width * scale)), max(1, round(full.height * scale))))
        rgb = np.asarray(small)
        height, width, _ = rgb.shape
        background = _dominant_color(rgb)
        mask = np.abs(rgb.astype(np.int16) - np.array(background)).max(axis=2) > _SEG_DIFF
        smeared = _smear(mask, max(1, round(width * _SEG_GAP_X)), max(1, round(height * _SEG_GAP_Y)))
        blocks = []
        for left, top, right, bottom in _component_boxes(smeared):
            box_w, box_h = right - left, bottom - top
            if box_w * box_h < _SEG_MIN_AREA * width * height:
                continue
            if box_w < _SEG_MIN_SIDE * width or box_h < _SEG_MIN_SIDE * height:
                continue
            if box_w > _SEG_MAX_SPAN * width and box_h > _SEG_MAX_SPAN * height:
                continue
            if mask[top:bottom, left:right].mean() < _SEG_MIN_FILL:
                continue
            blocks.append((box_w * box_h, (left / width, top / height, right / width, bottom / height)))
    except Exception:  # noqa: BLE001 -- best effort; ingest keeps the whole page
        return []
    blocks.sort(key=lambda item: -item[0])
    return _distinct([_padded(box) for _, box in blocks], limit)


def stitch_vertical(parts: list[Path], dst: Path) -> tuple[int, int]:
    """Stack ``parts`` top to bottom into ``dst``; return its ``(w, h)``.

    Parts are scaled to the widest one so a band that decoded a pixel narrower
    does not leave a ragged edge. Returns ``(0, 0)`` when any part is
    unreadable -- the caller then keeps the parts as separate figures.
    """
    from PIL import Image

    try:
        images = []
        for part in parts:
            img = Image.open(part)
            img.load()
            images.append(_flatten_rgb(img))
    except Exception:  # noqa: BLE001 -- fall back to the unstitched parts
        return (0, 0)
    width = max(img.width for img in images)
    scaled = [
        img if img.width == width else img.resize((width, max(1, round(img.height * width / img.width))))
        for img in images
    ]
    canvas = Image.new("RGB", (width, sum(img.height for img in scaled)), (255, 255, 255))
    offset = 0
    for img in scaled:
        canvas.paste(img, (0, offset))
        offset += img.height
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dst, format="PNG")
    return canvas.size


def autocrop_border(path: Path) -> tuple[int, int]:
    """Trim a uniform border around the image at ``path``, in place.

    Returns the post-processing ``(width, height)`` -- the original size
    whenever no crop applies. Fail-soft by contract: any error leaves the file
    byte-identical and returns the original size, or ``(0, 0)`` when the image
    cannot even be opened (callers keep their own dims then).
    """
    size = (0, 0)
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.load()
            size = img.size
            alpha = _alpha_content_mask(img)
            box = (
                _padded_content_box(alpha.getbbox(), img.size) if alpha is not None else _content_box(_flatten_rgb(img))
            )
            if box is None:
                return size
            cropped = img.crop(box)
            _rewrite(cropped, path, img.format)
            return cropped.size
    except Exception:  # noqa: BLE001 -- cosmetic step; never fails the ingest
        return size


def _content_box(rgb: Image.Image) -> tuple[int, int, int, int] | None:
    """Padded content bbox to keep, or ``None`` when cropping is unsafe or not
    worth a re-encode."""
    border = _uniform_border_color(rgb)
    if border is None:
        # A frame is the easy case. A figure whose panels sit off-centre in the
        # region it was cut from has white down one side only: its corners
        # disagree, and this used to give up -- one paper figure kept 264 blank
        # pixels of its 1801 that way. Paper white is a background whether or
        # not it surrounds the content, so fall back to it and let the content
        # bbox decide which sides actually trim. An image with no white margin
        # yields its own bounds and is left alone by the area threshold below.
        border = (255, 255, 255)
    bbox = _ink_mask(rgb, border).getbbox()
    if bbox is None:  # solid-colour image: nothing to anchor a crop on
        return None
    return _padded_content_box(bbox, rgb.size)


def _padded_content_box(
    bbox: tuple[int, int, int, int] | None, size: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    if bbox is None:
        return None
    width, height = size
    pad = round(min(width, height) * _PAD_FRACTION)
    left = max(bbox[0] - pad, 0)
    top = max(bbox[1] - pad, 0)
    right = min(bbox[2] + pad, width)
    bottom = min(bbox[3] + pad, height)
    kept_w, kept_h = right - left, bottom - top
    if max(kept_w, kept_h) < _MIN_RESULT_PX:
        return None
    if min(kept_w, kept_h) < _MIN_RESULT_PX:
        # A thin result is trusted only in letterbox shape -- content spanning
        # most of the image with uniform bands above and below. Thin and short
        # of that is a stray mark the detector latched onto, and cropping to it
        # would destroy the image in place.
        span = kept_w / width if kept_w >= kept_h else kept_h / height
        if span < _LETTERBOX_SPAN:
            return None
    if width * height - kept_w * kept_h < _MIN_CROP_AREA_RATIO * width * height:
        return None
    return (left, top, right, bottom)


def _uniform_border_color(rgb: Image.Image) -> tuple[int, int, int] | None:
    """Median colour of the four corner patches, or ``None`` when the corners
    disagree (no uniform border to trim)."""
    width, height = rgb.size
    n = min(_CORNER_PATCH, width, height)
    patches = (
        rgb.crop((0, 0, n, n)),
        rgb.crop((width - n, 0, width, n)),
        rgb.crop((0, height - n, n, height)),
        rgb.crop((width - n, height - n, width, height)),
    )
    corners = []
    for patch in patches:
        pixels = [patch.getpixel((x, y)) for y in range(patch.height) for x in range(patch.width)]
        corners.append(tuple(int(median(px[c] for px in pixels)) for c in range(3)))
    for c in range(3):
        values = [corner[c] for corner in corners]
        if max(values) - min(values) > _MAX_CORNER_SPREAD:
            return None
    return tuple(int(median(corner[c] for corner in corners)) for c in range(3))


def _rewrite(cropped: Image.Image, path: Path, fmt: str | None) -> None:
    """Persist the crop atomically in the source format; JPEG at a fixed high
    quality, everything else with default (lossless) encoder args."""
    if not fmt:
        raise ValueError(f"unknown source image format for {path.name}")
    kwargs: dict[str, int] = {}
    if fmt == "JPEG":
        kwargs["quality"] = _JPEG_QUALITY
        if cropped.mode not in ("RGB", "L", "CMYK"):
            cropped = _flatten_rgb(cropped)
    tmp = path.with_name(path.name + ".autocrop.tmp")
    try:
        cropped.save(tmp, format=fmt, **kwargs)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _flatten_rgb(img: Image.Image) -> Image.Image:
    from PIL import Image

    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return img.convert("RGB")


def _ink_mask(rgb: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    from PIL import Image, ImageChops

    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, background))
    channels = diff.split()
    mask = channels[0]
    for channel in channels[1:]:
        mask = ImageChops.lighter(mask, channel)
    return mask.point(lambda v: 255 if v > _DIFF_TOLERANCE else 0)


def _alpha_content_mask(img: Image.Image) -> Image.Image | None:
    if img.mode not in ("RGBA", "LA") and "transparency" not in img.info:
        return None
    alpha = img.convert("RGBA").getchannel("A")
    low, high = alpha.getextrema()
    if low == high:
        return None
    return alpha.point(lambda value: 255 if value > _ALPHA_CONTENT_THRESHOLD else 0)


def _analysis_mask(img: Image.Image, rgb: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    from PIL import ImageChops

    mask = _ink_mask(rgb, background)
    alpha = _alpha_content_mask(img)
    if alpha is not None:
        mask = ImageChops.lighter(mask, alpha)
    return mask


def _scaled_mask(mask: Image.Image, max_px: int) -> Image.Image:
    from PIL import Image

    scale = min(1.0, max_px / max(mask.size))
    if scale >= 1.0:
        return mask
    size = (max(1, round(mask.width * scale)), max(1, round(mask.height * scale)))
    return mask.resize(size, Image.Resampling.NEAREST)


def _content_ratio_without_blank_frame(mask: Image.Image) -> float:
    data = mask.tobytes()
    if not data:
        return 1.0
    interior = _frame_interior_box(mask)
    if interior is None:
        return sum(bool(value) for value in data) / len(data)
    inner_data = mask.crop(interior).tobytes()
    return sum(bool(value) for value in inner_data) / len(data)


def _frame_interior_box(mask: Image.Image) -> tuple[int, int, int, int] | None:
    """What an otherwise empty outer frame encloses, or None if there is no frame.

    A frame reaches all four margins, leaves the middle empty, and draws lines
    that run most of the way across. Rounded corners and broken rules both had
    to pass, which is why coverage is per line rather than a corner test.
    """
    width, height = mask.size
    bbox = mask.getbbox()
    if bbox is None:
        return None
    left, top, right, bottom = bbox
    margin_x = round(width * _FRAME_MARGIN_FRACTION)
    margin_y = round(height * _FRAME_MARGIN_FRACTION)
    if left > margin_x or top > margin_y or right < width - margin_x or bottom < height - margin_y:
        return None
    data = mask.tobytes()
    core = (
        round(width * 0.45),
        round(height * 0.45),
        max(round(width * 0.55), round(width * 0.45) + 1),
        max(round(height * 0.55), round(height * 0.45) + 1),
    )
    if mask.crop(core).getbbox() is not None:
        return None

    span_w = right - left
    span_h = bottom - top
    row_floor = span_w * _FRAME_LINE_COVERAGE
    col_floor = span_h * _FRAME_LINE_COVERAGE
    row_ink = [sum(bool(value) for value in data[y * width + left : y * width + right]) for y in range(height)]
    col_ink = [sum(bool(data[y * width + x]) for y in range(top, bottom)) for x in range(width)]
    middle_x = (left + right) // 2
    middle_y = (top + bottom) // 2
    top_lines = [y for y in range(top, middle_y) if row_ink[y] >= row_floor]
    bottom_lines = [y for y in range(middle_y, bottom) if row_ink[y] >= row_floor]
    left_lines = [x for x in range(left, middle_x) if col_ink[x] >= col_floor]
    right_lines = [x for x in range(middle_x, right) if col_ink[x] >= col_floor]
    if not all((top_lines, bottom_lines, left_lines, right_lines)):
        return None
    inner = (max(left_lines) + 1, max(top_lines) + 1, min(right_lines), min(bottom_lines))
    if inner[0] >= inner[2] or inner[1] >= inner[3]:
        return None
    return inner


def _band_count(profile: list[int], span: int) -> int:
    """Number of ink bands along one axis, given per-line ink counts."""
    ink_threshold = max(1, round(span * _PANEL_INK_FRACTION))
    gap_threshold = max(2, round(len(profile) * _PANEL_GAP_FRACTION))
    bands: list[int] = []
    current = 0
    gap = 0
    for value in profile:
        if value >= ink_threshold:
            if gap >= gap_threshold and current:
                bands.append(current)
                current = 0
            gap = 0
            current += value
        else:
            gap += 1
    if current:
        bands.append(current)
    if not bands:
        return 1
    total = sum(bands)
    return max(1, sum(1 for band in bands if band >= total * _PANEL_MIN_INK_SHARE))


def _ink_bands(profile: list[int], cross: int, span: int) -> list[tuple[int, int]]:
    """Contiguous runs of inked lines, split on gutters wide enough to be a
    panel boundary rather than line spacing."""
    ink_floor = max(1, round(cross * _PANEL_LINE_INK))
    gutter = max(4, round(span * _PANEL_GUTTER_FRACTION))
    bands: list[tuple[int, int]] = []
    start = None
    gap = 0
    for index, value in enumerate(profile):
        if value > ink_floor:
            if start is None:
                start = index
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= gutter:
                bands.append((start, index - gap))
                start = None
                gap = 0
    if start is not None:
        bands.append((start, len(profile) - 1))
    return [(a, b) for a, b in bands if b - a > span * _PANEL_MIN_SIZE]


def _background_lines(mask: Image.Image) -> tuple[list[bool], list[bool]]:
    """Which rows and columns carry no ink -- the gutters between panels."""
    width, height = mask.size
    data = mask.tobytes()
    row_limit = max(1, round(width * _GUTTER_INK_TOLERANCE))
    col_limit = max(1, round(height * _GUTTER_INK_TOLERANCE))
    rows = [sum(1 for v in data[y * width : (y + 1) * width] if v) <= row_limit for y in range(height)]
    cols = [sum(1 for y in range(height) if data[y * width + x]) <= col_limit for x in range(width)]
    return rows, cols


def _snap_edge(clear: list[bool], position: int, *, window: int, outward: int) -> int:
    """Nearest gutter to ``position`` within ``window``, preferring the side
    that keeps content (outward), so a box drawn by eye lands between panels
    instead of through one."""
    for distance in range(window + 1):
        for direction in (outward, -outward):
            candidate = position + distance * direction
            if 0 <= candidate < len(clear) and clear[candidate]:
                return candidate
    return position


def _snap_box_to_gutters(mask: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    rows, cols = _background_lines(mask)
    left, top, right, bottom = box
    win_x = max(2, round(mask.width * _SNAP_WINDOW_FRACTION))
    win_y = max(2, round(mask.height * _SNAP_WINDOW_FRACTION))
    return (
        _snap_edge(cols, left, window=win_x, outward=-1),
        _snap_edge(rows, top, window=win_y, outward=-1),
        _snap_edge(cols, min(right, mask.width - 1), window=win_x, outward=1) + 1,
        _snap_edge(rows, min(bottom, mask.height - 1), window=win_y, outward=1) + 1,
    )


def _dominant_color(rgb) -> list[int]:
    """The page's most common colour, quantised.

    Keyed on the whole image rather than its corners: a browser screenshot
    carries a white chrome strip above a black page, and taking the corner
    makes the entire page read as content.
    """
    import numpy as np

    # int32 before the arithmetic, not after: a uint8 channel times 64 wraps,
    # and the winning "colour" comes back magenta on any page whose ground is
    # not black -- two real product brochures segmented to nothing.
    flat = (rgb.reshape(-1, 3) // 16).astype(np.int32)
    codes = flat[:, 0] * 4096 + flat[:, 1] * 64 + flat[:, 2]
    values, counts = np.unique(codes, return_counts=True)
    winner = int(values[counts.argmax()])
    return [(winner // 4096) * 16 + 8, ((winner // 64) % 64) * 16 + 8, (winner % 64) * 16 + 8]


def _smear(mask, gap_x: int, gap_y: int):
    """Run-length smoothing: close gaps shorter than the given spans so the
    parts of one content block join up into a single region."""
    horizontal = mask.copy()
    for _ in range(gap_x):
        horizontal[:, 1:] |= horizontal[:, :-1]
    for _ in range(gap_x):
        horizontal[:, :-1] |= horizontal[:, 1:]
    vertical = mask.copy()
    for _ in range(gap_y):
        vertical[1:, :] |= vertical[:-1, :]
    for _ in range(gap_y):
        vertical[:-1, :] |= vertical[1:, :]
    return horizontal & vertical


def _component_boxes(mask) -> list[tuple[int, int, int, int]]:
    """Bounding box of every 4-connected region in a boolean mask."""
    import numpy as np

    height, width = mask.shape
    seen = np.zeros((height, width), dtype=bool)
    boxes: list[tuple[int, int, int, int]] = []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        stack = [(int(y0), int(x0))]
        seen[y0, x0] = True
        top = bottom = int(y0)
        left = right = int(x0)
        while stack:
            y, x = stack.pop()
            top, bottom = min(top, y), max(bottom, y)
            left, right = min(left, x), max(right, x)
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        boxes.append((left, top, right + 1, bottom + 1))
    return boxes


def _padded(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Give a block breathing room, and open a strip out to a sane aspect."""
    x0, y0, x1, y1 = box
    x0, y0 = max(0.0, x0 - _SEG_MARGIN), max(0.0, y0 - _SEG_MARGIN)
    x1, y1 = min(1.0, x1 + _SEG_MARGIN), min(1.0, y1 + _SEG_MARGIN)
    width, height = x1 - x0, y1 - y0
    if height > 0 and width / height > _SEG_MAX_ASPECT:
        grow = (width / _SEG_MAX_ASPECT - height) / 2
        y0, y1 = max(0.0, y0 - grow), min(1.0, y1 + grow)
    elif width > 0 and height / width > _SEG_MAX_ASPECT:
        grow = (height / _SEG_MAX_ASPECT - width) / 2
        x0, x1 = max(0.0, x0 - grow), min(1.0, x1 + grow)
    return (x0, y0, x1, y1)


def _distinct(boxes: list[tuple[float, float, float, float]], limit: int) -> list[tuple[float, float, float, float]]:
    """Drop blocks that cover what a bigger one already covers.

    Padding a block out to a placeable shape can push it over its neighbour,
    and two crops of one photograph is worse than one: the planner reads them
    as two figures and can put the same image on two slides. Largest wins,
    since it is the one with the whole subject.
    """
    kept: list[tuple[float, float, float, float]] = []
    for box in boxes:
        area = (box[2] - box[0]) * (box[3] - box[1])
        redundant = False
        for other in kept:
            wide = max(0.0, min(box[2], other[2]) - max(box[0], other[0]))
            high = max(0.0, min(box[3], other[3]) - max(box[1], other[1]))
            smaller = min(area, (other[2] - other[0]) * (other[3] - other[1]))
            if smaller and wide * high / smaller > _SEG_MAX_OVERLAP:
                redundant = True
                break
        if not redundant:
            kept.append(box)
        if len(kept) == limit:
            break
    return kept
