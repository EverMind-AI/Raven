"""What the declared geometry alone can decide.

Two things, and only two. A shape whose declared box leaves the canvas has left
it however the copy inside reflows -- the frame's origin is not something the
renderer negotiates. And a short label in a box narrower than the label needs
wraps into a stacked mess that no render check can see, because nothing overlaps:
the label is simply broken, so it has to be caught before it is drawn.

What used to be here as well, and is deliberately gone: the same module also
measured text-on-text overlap and text running past its card off the *declared*
boxes. Both are now measured on the render (`measure.rendered`), where a word's
position is a fact rather than an assumption about wrapping and line spacing.
Keeping both was keeping two answers to one question, and the declared-geometry
answer is the one whose docstring already admitted it "produced findings on
pages a render shows to be clean".
"""

from __future__ import annotations

from pathlib import Path

from raven.ppt.contracts.findings import Audience, Finding, Severity
from raven.ppt.services.measure.geometry import (
    EMU_PER_INCH,
    EMU_PER_POINT,
    iter_shapes,
    open_deck,
    shape_rect_emu,
)
from raven.ppt.services.measure.width import WidthMeasurer

# Half a point, in EMU: a rule drawn exactly on the margin rounds either way.
EDGE_SLACK_EMU = 6350

# The measurer's font is not the renderer's font, and short labels leave no room
# to wrap gracefully anyway: report only a clear miss.
LABEL_SLACK = 1.05
# A single character cannot wrap, and prose is meant to. What is left between
# those two is label-shaped: a step number, a stat, a column head.
LABEL_MIN_CHARS = 2
LABEL_MAX_CHARS = 24
LABEL_MAX_SPACES = 2
# python-pptx's default left+right inset on a text box, in points.
BOX_SIDE_MARGINS_PT = 14.4


# A line of copy this much wider than its box, with wrapping off, has left the box.
# Two per cent because the measurer's font is not the renderer's.
NO_WRAP_SLACK = 1.02
# And what makes leaving the box a defect: leaving the *page*. Nothing else survived
# measurement. Across six real decks 38 lines spill out of their box, and every one of
# them is a label centred in an anchor box narrower than itself -- "VIS" in a 0.12in box
# inside a coloured circle, rendered dead centre and perfectly legible. A ratio
# threshold was tried at 1.5x and still flagged all of those, because a tiny anchor box
# is how a centred label is placed. So this reports the one case that is never a choice,
# and says nothing on all six decks.


def spilled_copy(pptx_path: Path, measurer: WidthMeasurer | None = None) -> list[Finding]:
    """Copy wider than the box it was put in, in a box that does not wrap.

    The width case every other check declines. `wrapped_labels` skips a box with
    wrapping off, because such a box cannot wrap; `overset_copy` skips it too, and
    measures height anyway; `clipped_copy` only sees copy the renderer refused to
    paint. But a `wrap="none"` box does not clip -- LibreOffice centres the line on the
    box and paints it straight out of both sides.

    Measured on a delivered deck: a page title of 11.68in in a box at 0.92in from the
    left rendered its first glyph at 0.75in, a quarter of an inch outside its own box
    and against the edge of the canvas. Nothing reported it: the box is inside the page,
    the words collide with nothing, and the type is the right size.
    """
    if measurer is None:
        from raven.ppt.services.assets.text_metrics import measurer as font_measurer

        measurer = font_measurer()
    findings: list[Finding] = []
    presentation = open_deck(pptx_path)
    canvas = (presentation.slide_width or 0) / EMU_PER_POINT
    for number, slide in enumerate(presentation.slides, start=1):
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_text_frame", False) or int(shape.width or 0) <= 0:
                continue
            frame = shape.text_frame
            if frame.word_wrap is not False:
                continue  # it wraps, so it is `overset_copy`'s and `wrapped_label`'s
            box = shape_rect_emu(shape)
            box_w = box.width / EMU_PER_POINT - BOX_SIDE_MARGINS_PT
            worst: tuple[float, str] | None = None
            for para in frame.paragraphs:
                text = "".join(run.text for run in para.runs).strip()
                if len(text) < LABEL_MIN_CHARS:
                    continue
                sizes = [run.font.size or para.font.size for run in para.runs]
                size = max((value.pt for value in sizes if value is not None), default=None)
                if size is None:
                    continue
                bold = any(run.font.bold for run in para.runs)
                # A `<a:br/>` inside the paragraph is a line break the author asked for,
                # and python-pptx hands it over as "\n". Measuring the whole paragraph as
                # one line read "Transformer\nDecoder" as 3.04in of copy in a 1.54in box
                # and reported ten such labels on a page where nothing spills.
                for line in text.split("\n"):
                    line = line.strip()
                    if len(line) < LABEL_MIN_CHARS:
                        continue
                    needed = measurer.width(line, round(size), bold)
                    if needed > box_w * NO_WRAP_SLACK and (worst is None or needed > worst[0]):
                        worst = (needed, line)
            if worst is None:
                continue
            needed, text = worst
            # Where it lands: the renderer centres an unwrapped line on its box, so it
            # spills evenly and the page edge is what decides whether a reader loses it.
            spill = (needed - box_w) / 2
            left = box.x0 / EMU_PER_POINT + box_w / 2 - needed / 2
            off_page = left < 0 or left + needed > canvas
            if not off_page:
                continue
            findings.append(
                Finding(
                    kind="spilled_copy",
                    severity=Severity.WARNING,
                    page=number,
                    audience=Audience.DESIGNER,
                    message=(
                        f"'{text[:34]}' sets {needed / 72:.2f}in wide in a {max(box_w, 0.0) / 72:.2f}in box with "
                        f"wrapping off, so it spills {spill / 72:.2f}in past each side and off the edge of the "
                        f"page. Turn wrapping on and give the box the height a second line needs, widen the box, or "
                        "say it shorter -- a box with wrapping off does not clip, it paints straight out of itself"
                    ),
                    detail={
                        "needs_in": round(needed / 72, 2),
                        "box_in": round(max(box_w, 0.0) / 72, 2),
                        "off_page": off_page,
                        "text": text[:60],
                    },
                )
            )
    return findings


def _holds_two_lines(shape, frame, size_pt: float) -> bool:
    """Whether this box has the height for a second line of `size_pt`."""
    from raven.ppt.services.measure.fit import capacity_lines

    height = int(shape.height or 0) - ((frame.margin_top or 0) + (frame.margin_bottom or 0))
    if height <= 0:
        return False
    return capacity_lines(height / EMU_PER_INCH * 96, int(round(size_pt * 96 / 72))) >= 2


def off_page_shapes(pptx_path: Path) -> list[Finding]:
    """Shapes whose declared box crosses the edge of the canvas.

    Arithmetic on the built file rather than anyone's judgement: a box 0.2in past
    the bottom edge looks like a design choice in a thumbnail and is a truncated
    sentence on a projector. A design pass that adds to a page until it no longer
    fits has no other way of being told.
    """
    presentation = open_deck(pptx_path)
    width, height = presentation.slide_width, presentation.slide_height
    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        for shape in iter_shapes(slide.shapes):
            box = shape_rect_emu(shape)
            crossed = [
                edge
                for edge, past in (
                    ("left", box.x0 < -EDGE_SLACK_EMU),
                    ("top", box.y0 < -EDGE_SLACK_EMU),
                    ("right", box.x1 > width + EDGE_SLACK_EMU),
                    ("bottom", box.y1 > height + EDGE_SLACK_EMU),
                )
                if past
            ]
            if not crossed:
                continue
            text = shape.text_frame.text.strip() if getattr(shape, "has_text_frame", False) else ""
            findings.append(
                Finding(
                    kind="off_page",
                    severity=Severity.WARNING,
                    page=number,
                    audience=Audience.DESIGNER,
                    message=(
                        f"a shape crosses the {' and '.join(crossed)} edge of the page"
                        + (f" -- it holds {text[:40]!r}" if text else "")
                        + ". Bring it inside the margins"
                    ),
                    detail={"edges": tuple(crossed), "text": text[:40]},
                )
            )
    return findings


def wrapped_labels(pptx_path: Path, measurer: WidthMeasurer | None = None) -> list[Finding]:
    """Short labels set in boxes too narrow to hold them on one line.

    A numbered step drawn as '01' in a box guessed at 0.25in wraps into a stacked
    '0' over '1', and a render check cannot see it: nothing overlaps, the label is
    just broken. Only short label-like text is held to this -- prose is meant to
    wrap -- and the width carries slack for the renderer resolving to a different
    font than the measurer used.
    """
    if measurer is None:
        from raven.ppt.services.assets.text_metrics import measurer as font_measurer

        measurer = font_measurer()
    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_text_frame", False):
                continue
            frame = shape.text_frame
            if frame.word_wrap is False:
                continue
            if int(shape.width or 0) <= 0:
                continue  # no declared width to hold anything to
            # A box narrower than its own margins is not skipped: it is the worst
            # offender, since no label fits in it at all.
            box_w = shape_rect_emu(shape).width / EMU_PER_POINT - BOX_SIDE_MARGINS_PT
            for para in frame.paragraphs:
                text = "".join(run.text for run in para.runs).strip()
                if len(text) < LABEL_MIN_CHARS or len(text) > LABEL_MAX_CHARS or text.count(" ") > LABEL_MAX_SPACES:
                    continue
                sizes = [run.font.size or para.font.size for run in para.runs]
                size = max((value.pt for value in sizes if value is not None), default=None)
                if size is None:
                    continue
                bold = any(run.font.bold for run in para.runs)
                needed = measurer.width(text, round(size), bold)
                if needed <= box_w * LABEL_SLACK:
                    continue
                # A box tall enough for a second line expects to wrap, so wrapping in it
                # is not a broken label. Without this the check has no way to tell a
                # label from body copy in Chinese: the length window and the space count
                # are both about English, and a twenty-character Chinese sentence has no
                # spaces at all -- it reported "解码器结构沿用，改动只在查询定义与时序颈"
                # wrapping to two lines in a three-line column as a defect, on a page a
                # reviewer had just called clean.
                if _holds_two_lines(shape, frame, size):
                    continue
                findings.append(
                    Finding(
                        kind="wrapped_label",
                        severity=Severity.WARNING,
                        page=number,
                        audience=Audience.DESIGNER,
                        message=(
                            f"the label {text!r} needs about {needed / 72:.2f} in but its box gives "
                            f"{max(box_w, 0.0) / 72:.2f} in, so it wraps mid-label -- widen the box or "
                            "shorten the label"
                        ),
                        detail={
                            "label": text,
                            "needs_in": round(needed / 72, 3),
                            "box_in": round(max(box_w, 0.0) / 72, 3),
                        },
                    )
                )
    return findings
