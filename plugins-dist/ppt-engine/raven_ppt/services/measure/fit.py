"""Whether the copy fits its box, decided before anything is rendered.

Ported from the previous engine's `text_fit`, which owned this and was left
behind. Without it the pipeline could only find out after a render: copy runs
past its box, LibreOffice paints it anyway, and the render checks report the
words that ended up on top of other words -- three runs' worth of collisions
whose actual cause was a box too small for what was put in it.

Measuring it here answers the cause. Greedy wrap at the real width, count the
lines, compare against how many lines the box's height holds. A box that cannot
hold its copy is a finding on the build, in the same round, with the numbers in
it: six lines at 16pt needs 1.51in and the box gives 0.90in.

Two rules carried over from the engine, both load-bearing:

* CJK breaks per character and Latin per word, with a hyphen or slash inside a
  word counting as a break opportunity -- without that last part a narrow column
  renders "Forward-Looking" as "Forward-Loo / king".
* Every line keeps `RENDER_DRIFT_HEADROOM` of the width in reserve, because the
  renderer's metrics are not the measurer's. The engine calibrated the gap
  against LibreOffice 7.4: 0.14% on the longest repro line, so 3% is ~20x the
  observed drift and costs one or two characters a line.

And one rule the port left out, which is what the measurement is worth: the box
alone does not say where the ink lands. A frame's vertical anchor does, and the
two readings of one box differ by most of its height -- `template/house.py`
measured 0.375in between a cloned title and a composed one in the same 0.98in
row. So the copy that does not fit is placed before it is judged. Copy under a
top anchor runs on downward into whatever room is below the frame; copy under a
bottom or middle anchor grows *backwards*, up past its own top edge, which is
never room an author left for it -- the page above the frame is already drawn.
A frame set to shrink its text to fit displaces nothing at all, and its finding
belongs to the type readings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pptx.enum.text import MSO_AUTO_SIZE

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.geometry import (
    EMU_PER_INCH,
    EMU_PER_POINT,
    Rect,
    iter_shapes,
    open_deck,
    shape_rect_pt,
)
from raven_ppt.services.measure.width import WidthMeasurer, is_cjk_char
from raven_ppt.services.template.decompile import inherited_size, page_design

# A line box is this much of the type size, and the first line needs its ascent
# and descent before any leading is added.
LINE_HEIGHT_FACTOR = 1.3
TEXT_ASCENT_FACTOR = 0.85
TEXT_DESCENT_FACTOR = 0.35
RENDER_DRIFT_HEADROOM = 1.03

# Break after one of these inside a word rather than mid-syllable.
_WORD_BREAK_AFTER = frozenset("-–—/")

# Under this many characters a box is a label, and `wrapped_labels` already has
# the label case with its own slack. This check is about copy.
MIN_COPY_CHARS = 24
# How much past its box the copy has to run before it is reported. One line of
# slack: a box sized to its text exactly is normal authoring, and the renderer's
# leading differs from the measurer's by less than a line.
OVERSET_SLACK_LINES = 1

# Where the copy goes when it does not fit, which is the anchor's answer and not
# the box's. `SHRINKS` is a frame the renderer resizes the type inside, so nothing
# is displaced and the size it comes back at is `type_drift`'s reading.
GROWS_DOWN = "down"
GROWS_UP = "up"
GROWS_BOTH = "both"
SHRINKS = "shrinks"
# The anchors that put the copy's last line on the frame's bottom edge, so a line
# it cannot hold is added above the first one.
_GROWS_BACKWARD = {GROWS_UP, GROWS_BOTH}

# What the renderer actually advances per line. `LINE_HEIGHT_FACTOR` above is the
# capacity model and is deliberately generous the other way -- it credits a box with
# fewer lines than it might hold, which is the safe direction for asking whether the
# copy fits and the wrong one for saying where the copy went. These two are measured
# rather than chosen: 24 renders through LibreOffice across six sizes and up to 18
# lines, and `size_pt * (1 + RENDERED_LINE_ADVANCE * (lines - 1))` matched the painted
# ink height to within 0.002in on every one of them whose line count agreed. Past
# roughly ten lines in a 0.56in box LibreOffice clips instead of painting, and the
# model then over-reads -- but copy clipped away is not copy a reader sees either, and
# `clipped_copy` is the row that reads it.
RENDERED_LINE_ADVANCE = 1.2
# A line's ink is not its line box, and the measurer's leading is not the
# renderer's: half a line of 12pt copy at either end is a graze, not a place the
# copy landed.
DISPLACED_GRAZE_PT = 3.0
# And how much of the narrower of the two boxes' widths the displaced copy and the
# copy it reaches have to share before one is over the other rather than beside it.
# Half, which is `box_overflow`'s own answer to the same question about a rendered
# line, and what keeps this off D11's rejected reading: a full-width title box
# overlapping a corner page-number box shares 6% of the title's width and nothing
# at all of a growth direction, so it is not reported here however the boxes lie.
DISPLACED_IN_COLUMN = 0.5


class LineOverflowError(ValueError):
    """A single character is wider than the space, so no wrap can succeed."""


def wrap(
    text: str,
    width_px: float,
    font_px: int,
    *,
    bold: bool = False,
    measurer: WidthMeasurer,
) -> list[str]:
    """Greedy wrap, every line fitting `width_px` with the drift reserve held back."""
    room = width_px / RENDER_DRIFT_HEADROOM
    lines: list[str] = []
    for paragraph in text.split("\n"):
        stripped = paragraph.strip()
        if stripped:
            lines.extend(_wrap_paragraph(measurer, stripped, room, font_px, bold))
    return lines or [""]


def capacity_lines(height_px: float, font_px: int) -> int:
    """How many lines of `font_px` a box `height_px` tall holds."""
    first = (TEXT_ASCENT_FACTOR + TEXT_DESCENT_FACTOR) * font_px
    if height_px < first:
        return 0
    return 1 + int((height_px - first) // (LINE_HEIGHT_FACTOR * font_px))


def needed_height_px(line_count: int, font_px: int) -> float:
    """How tall a box must be to hold `line_count` lines of `font_px`."""
    if line_count <= 0:
        return 0.0
    first = (TEXT_ASCENT_FACTOR + TEXT_DESCENT_FACTOR) * font_px
    return first + (line_count - 1) * LINE_HEIGHT_FACTOR * font_px


@dataclass(frozen=True)
class _Overset:
    """One frame's copy, measured, and where the renderer will put what does not fit."""

    text: str
    size_pt: float
    inherited: bool
    lines: int
    painted: int
    """Lines the renderer will set, measured at its own width -- the placement's count.
    `lines` is the capacity question's count, taken with the drift reserve held back."""
    holds: int
    width_in: float
    height_in: float
    needed_in: float
    box: Rect
    ink: tuple[float, float]
    """(top, bottom) of the copy's own ink, in points down the canvas."""
    growth: str


def overset_copy(pptx_path: Path, measurer: WidthMeasurer | None = None) -> list[Finding]:
    """Boxes holding more copy than their height can show, measured not rendered.

    Two kinds come out of the one reading, because a box too small for its copy is
    two different pages depending on which way the copy goes. `overset_copy` reports
    the frame; `displaced_copy` refuses the copy that grew backwards out of it and
    landed off the page or on another frame's words.
    """
    if measurer is None:
        from raven_ppt.services.assets.text_metrics import measurer as font_measurer

        measurer = font_measurer()
    presentation = open_deck(pptx_path)
    canvas_pt = (presentation.slide_height or 0) / EMU_PER_POINT
    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        design = None
        occupied = [
            (shape, shape_rect_pt(shape))
            for shape in iter_shapes(slide.shapes)
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip()
        ]
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_text_frame", False):
                continue
            size_pt = _size_pt(shape.text_frame)
            if size_pt is None:
                # Not stated on the page: a cloned template page states nothing of its
                # own, and reading only the runs answered "no size" for every box on it.
                # Resolved the way the reference resolves it, which is what `spilled_copy`
                # already does with the same two calls -- and the boxes this reading was
                # written for are on cloned pages, so without it the check is blind
                # exactly where the copy was replaced.
                if design is None:
                    design = page_design(presentation, slide)
                size_pt = inherited_size(shape, design)
                if size_pt is None:
                    continue
                inherited = True
            else:
                inherited = False
            measured = _measure(shape, slide, size_pt, inherited, measurer)
            if measured is None:
                continue
            landed = _landed(shape, measured, occupied, canvas_pt)
            findings.append(_displaced(number, measured, landed) if landed else _overset(number, measured))
    return findings


def _measure(shape, slide, size_pt: float, inherited: bool, measurer: WidthMeasurer) -> _Overset | None:
    """This frame's copy against its height, and where the overflow goes; None when it fits."""
    frame = shape.text_frame
    text = frame.text.strip()
    if len(text) < MIN_COPY_CHARS or frame.word_wrap is False:
        return None
    width_in = (shape.width - _margins(frame, horizontal=True)) / EMU_PER_INCH
    height_in = (shape.height - _margins(frame, horizontal=False)) / EMU_PER_INCH
    if width_in <= 0 or height_in <= 0:
        return None
    font_px = int(round(size_pt * 96 / 72))
    try:
        lines = wrap(text, width_in * 96, font_px, bold=_bold(frame), measurer=measurer)
    except LineOverflowError:
        lines = [text]
    holds = capacity_lines(height_in * 96, font_px)
    if len(lines) <= holds + OVERSET_SLACK_LINES:
        return None
    needed_in = needed_height_px(len(lines), font_px) / 96
    box = shape_rect_pt(shape)
    top = box.y0 + (frame.margin_top or 0) / EMU_PER_POINT
    bottom = top + height_in * 72
    # Where the ink goes is measured with the renderer's own width and the renderer's
    # own leading, not with the reserve the capacity question is asked under: the
    # reserve exists so a box is never credited with a line it might not fit, and
    # carrying it into the placement would put the copy further out than it goes.
    try:
        painted = wrap(text, width_in * 96 * RENDER_DRIFT_HEADROOM, font_px, bold=_bold(frame), measurer=measurer)
    except LineOverflowError:
        painted = lines
    needed_pt = size_pt * (1 + RENDERED_LINE_ADVANCE * (len(painted) - 1))
    growth = _growth(frame, slide, shape)
    if growth == SHRINKS:
        ink = (top, bottom)
    elif growth == GROWS_UP:
        ink = (bottom - needed_pt, bottom)
    elif growth == GROWS_BOTH:
        middle = (top + bottom) / 2
        ink = (middle - needed_pt / 2, middle + needed_pt / 2)
    else:
        ink = (top, top + needed_pt)
    return _Overset(
        text=text,
        size_pt=size_pt,
        inherited=inherited,
        lines=len(lines),
        painted=len(painted),
        holds=holds,
        width_in=width_in,
        height_in=height_in,
        needed_in=needed_in,
        box=box,
        ink=ink,
        growth=growth,
    )


def _growth(frame, slide, shape) -> str:
    """Which way the copy this frame cannot hold goes.

    `normAutofit` is the renderer shrinking the type instead of moving it, so nothing
    leaves the frame and the size it settles at is what `type_drift` reads. Everything
    else is the anchor's answer, resolved through the layout and the master the way a
    renderer resolves it -- a cloned page declares no anchor of its own, and reading the
    slide alone reports "top" for a title the master anchored to the bottom of its row.

    `spAutoFit` is deliberately not a third case, on two counts. It asks the shape to
    grow to hold its copy, so it looked like the one autofit that would keep the ink
    inside the box -- but LibreOffice ignores it and honours the anchor: rendered, one
    2.4x0.56in box at 20pt put the same three lines at the same y with `spAutoFit` set
    and with it absent, growing 0.17in above the box's own top under a middle anchor and
    0.44in under a bottom one. And the exposure either way is three boxes: of the 184
    `spAutoFit` frames across the ten bundled templates, 181 are anchored to the top,
    where every reading agrees the copy runs on downward.
    """
    if frame.auto_size == MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE:
        return SHRINKS
    # Imported here, not at module scope: `template.house` reads a row's copy capacity
    # through `template.capacity`, which measures it with this module. Measurement is
    # the lower layer of the two and has to stay importable without the template
    # reading that sits on top of it.
    from raven_ppt.services.template.house import title_anchor

    anchor = title_anchor(slide, shape)
    if anchor == "bottom":
        return GROWS_UP
    return GROWS_BOTH if anchor == "middle" else GROWS_DOWN


def _landed(shape, measured: _Overset, occupied: list, canvas_pt: float) -> list[str]:
    """Where copy that grew backwards ended up, in the reader's words; empty when nowhere.

    Only backward growth is asked. Copy running on below its frame is the page's own
    room to give or refuse -- `box_overflow` reports it on the render, and design doc
    D2 keeps it a report because the fix is height taken from a neighbour or a line
    cut. Copy growing up is not that trade: the room above the frame is already spent,
    the author did not ask for it, and the frame's own anchor is what put the copy
    there.
    """
    if measured.growth not in _GROWS_BACKWARD:
        return []
    top, bottom = measured.ink
    box = measured.box
    if box.y1 <= 0 or box.y0 >= canvas_pt:
        return []  # the box itself is off the canvas, which is `off_page`'s finding
    landed: list[str] = []
    if top < -DISPLACED_GRAZE_PT:
        landed.append(f"{-top / 72:.2f}in of it above the top edge of the page")
    if bottom > canvas_pt + DISPLACED_GRAZE_PT:
        landed.append(f"{(bottom - canvas_pt) / 72:.2f}in of it below the bottom edge of the page")
    for side, low, high in _strips(box, top, bottom):
        reached = _reached(shape, box, low, high, occupied)
        if reached is not None:
            landed.append(f"{reached[0] / 72:.2f}in of it over the copy {side} it ({_head(reached[1], 24)!r})")
    return landed


def _strips(box: Rect, top: float, bottom: float) -> list[tuple[str, float, float]]:
    """The bands of ink outside the frame's own box, one per direction it grew into."""
    strips = []
    if top < box.y0 - DISPLACED_GRAZE_PT:
        strips.append(("above", top, box.y0))
    if bottom > box.y1 + DISPLACED_GRAZE_PT:
        strips.append(("below", box.y1, bottom))
    return strips


def _reached(shape, box: Rect, low: float, high: float, occupied: list) -> tuple[float, str] | None:
    """The copy this band of displaced ink is set over, and how deep into it, or None."""
    for other, theirs in occupied:
        if other._element is shape._element:  # noqa: SLF001 -- identity, and python-pptx has no eq
            continue
        across = min(box.x1, theirs.x1) - max(box.x0, theirs.x0)
        if across <= 0 or across / max(min(box.width, theirs.width), 1e-6) < DISPLACED_IN_COLUMN:
            continue
        into = min(high, theirs.y1) - max(low, theirs.y0)
        if into > DISPLACED_GRAZE_PT:
            return (into, other.text_frame.text.strip())
    return None


_GROWTH_NOTE = {
    GROWS_DOWN: "so the copy runs on below the frame",
    GROWS_UP: "and the frame is anchored to the bottom of its box, so the copy grows upward",
    GROWS_BOTH: "and the frame is anchored to the middle of its box, so the copy grows past both edges",
    SHRINKS: "and the frame shrinks its type to fit, so this box comes back under the size its neighbours keep",
}


def _overset(page: int, measured: _Overset) -> Finding:
    return Finding(
        kind="overset_copy",
        severity=Severity.WARNING,
        page=page,
        message=(
            f"'{_head(measured.text)}' wraps to {measured.lines} lines at {measured.size_pt:g}pt in a "
            f"{measured.width_in:.2f}in column, which needs {measured.needed_in:.2f}in of height, and the box "
            f"gives {measured.height_in:.2f}in -- {_GROWTH_NOTE[measured.growth]}. Give it the room "
            f"or give it less to say -- do not answer this by dropping the type size"
        ),
        detail={
            "lines": measured.lines,
            "holds": measured.holds,
            "needs_in": round(measured.needed_in, 2),
            "box_in": round(measured.height_in, 2),
            "size_pt": measured.size_pt,
            "growth": measured.growth,
            "inherited_size": measured.inherited,
        },
    )


def _displaced(page: int, measured: _Overset, landed: list[str]) -> Finding:
    anchor = "bottom" if measured.growth == GROWS_UP else "middle"
    return Finding(
        kind="displaced_copy",
        severity=Severity.BLOCKING,
        page=page,
        message=(
            f"'{_head(measured.text)}' sets {measured.painted} lines at {measured.size_pt:g}pt in a "
            f"{measured.width_in:.2f}in column and the box shows {measured.holds}, and the frame is anchored to "
            f"the {anchor} of its box -- so the lines it cannot hold are set above the first one, putting "
            f"{' and '.join(landed)}. Anchor the frame to the top of its box, or place() it where the copy "
            "is to go: either leaves the box, the copy and every neighbour as they are. Widening or "
            "shortening also works and neither is required -- what is not an answer is leaving the copy "
            "somewhere the page is already drawn"
        ),
        detail={
            "lines": measured.lines,
            "holds": measured.holds,
            "needs_in": round(measured.needed_in, 2),
            "box_in": round(measured.height_in, 2),
            "size_pt": measured.size_pt,
            "painted_lines": measured.painted,
            "growth": measured.growth,
            "inherited_size": measured.inherited,
            "landed": list(landed),
        },
    )


def _wrap_paragraph(measurer: WidthMeasurer, text: str, room: float, font_px: int, bold: bool) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in _tokenize(text):
        if token == " ":  # noqa: S105 -- a text token, not a credential
            if current:
                current += " "
            continue
        candidate = current + token
        if measurer.width(candidate.rstrip(), font_px, bold) <= room:
            current = candidate
            continue
        if current.strip():
            lines.append(current.rstrip())
            current = ""
            if measurer.width(token, font_px, bold) <= room:
                current = token
                continue
        rest = token
        while rest:
            prefix = _longest_fitting(measurer, rest, room, font_px, bold)
            if prefix == 0:
                raise LineOverflowError(f"{rest[0]!r} at {font_px}px is wider than {room:.0f}px")
            if prefix == len(rest):
                current, rest = rest, ""
            else:
                lines.append(rest[:prefix])
                rest = rest[prefix:]
    if current.strip():
        lines.append(current.rstrip())
    return lines


def _tokenize(text: str) -> list[str]:
    """Latin words, single CJK characters, explicit spaces."""
    tokens: list[str] = []
    word: list[str] = []
    for char in text:
        if char == " ":
            if word:
                tokens.append("".join(word))
                word = []
            tokens.append(" ")
        elif is_cjk_char(char):
            if word:
                tokens.append("".join(word))
                word = []
            tokens.append(char)
        elif char in _WORD_BREAK_AFTER and word:
            word.append(char)
            tokens.append("".join(word))
            word = []
        else:
            word.append(char)
    if word:
        tokens.append("".join(word))
    return tokens


def _longest_fitting(measurer: WidthMeasurer, text: str, room: float, font_px: int, bold: bool) -> int:
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if measurer.width(text[:mid], font_px, bold) <= room:
            low = mid
        else:
            high = mid - 1
    return low


def _size_pt(frame) -> float | None:
    """The largest size any run in the frame states, or None when none does."""
    sizes = [
        run.font.size.pt
        for para in frame.paragraphs
        for run in para.runs
        if run.font.size is not None and run.text.strip()
    ]
    return max(sizes) if sizes else None


def _bold(frame) -> bool:
    return any(run.font.bold for para in frame.paragraphs for run in para.runs)


def _margins(frame, *, horizontal: bool) -> int:
    if horizontal:
        return (frame.margin_left or 0) + (frame.margin_right or 0)
    return (frame.margin_top or 0) + (frame.margin_bottom or 0)


def _head(text: str, limit: int = 34) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
