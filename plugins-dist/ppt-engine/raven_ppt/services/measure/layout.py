"""What the declared geometry alone can decide.

Three things, and only three. A shape whose declared box leaves the canvas has
left it however the copy inside reflows -- the frame's origin is not something the
renderer negotiates. A box with no height at all cannot show what is in it, for
the same reason and more plainly. And a short label in a box narrower than the
label needs wraps into a stacked mess that no render check can see, because
nothing overlaps: the label is simply broken, so it has to be caught before it is
drawn.

What used to be here as well, and is deliberately gone: the same module also
measured text-on-text overlap and text running past its card off the *declared*
boxes. Both are now measured on the render (`measure.rendered`), where a word's
position is a fact rather than an assumption about wrapping and line spacing.
Keeping both was keeping two answers to one question, and the declared-geometry
answer is the one whose docstring already admitted it "produced findings on
pages a render shows to be clean".
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from raven_ppt.contracts import WordBox
from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.geometry import (
    EMU_PER_INCH,
    EMU_PER_POINT,
    iter_shapes,
    open_deck,
    shape_rect_emu,
    shape_rect_pt,
)
from raven_ppt.services.measure.width import WidthMeasurer
from raven_ppt.services.measure.words import by_page as _by_page
from raven_ppt.services.measure.words import rect as _rect
from raven_ppt.services.template.decompile import inherited_size, page_design

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
        from raven_ppt.services.assets.text_metrics import measurer as font_measurer

        measurer = font_measurer()
    findings: list[Finding] = []
    presentation = open_deck(pptx_path)
    canvas = (presentation.slide_width or 0) / EMU_PER_POINT
    for number, slide in enumerate(presentation.slides, start=1):
        design = None
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
                    # Not stated on the page: the layout or the master says, and a
                    # cloned page states nothing itself. Resolved the way the reference
                    # resolves it, so the two unwrapped blocks that spilled on a
                    # measured page are measured rather than skipped.
                    if design is None:
                        design = page_design(presentation, slide)
                    size = inherited_size(shape, design)
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
    from raven_ppt.services.measure.fit import capacity_lines

    height = int(shape.height or 0) - ((frame.margin_top or 0) + (frame.margin_bottom or 0))
    if height <= 0:
        return False
    return capacity_lines(height / EMU_PER_INCH * 96, int(round(size_pt * 96 / 72))) >= 2


def off_page_shapes(pptx_path: Path) -> list[Finding]:
    """Shapes whose declared box crosses the edge of the canvas.

    Arithmetic on the built file rather than anyone's judgement: a box 0.2in past
    the bottom edge looks like a design choice in a thumbnail and is a truncated
    sentence on a projector, and a thumbnail is what the author is looking at.
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
                    message=(
                        f"a shape crosses the {' and '.join(crossed)} edge of the page"
                        + (f" -- it holds {text[:40]!r}" if text else "")
                        + ". Bring it inside the margins"
                    ),
                    detail={"edges": tuple(crossed), "text": text[:40]},
                )
            )
    return findings


def boxless_copy(pptx_path: Path) -> list[Finding]:
    """Pages holding copy in a box with no height, or no width, to show it in.

    The plainest way for a page to lose its words, and the one nothing measured. A
    live build's page 12 came back as the template's background and its footer, and
    read as an empty page; opened, it held the page's entire copy -- title and all --
    in the closing prototype's title placeholder, declared at 11.88in wide and
    exactly 0in tall. The renderer painted a sliver of it against the top edge.

    Every check that could have caught it looked past it, and each for its own good
    reason: `emptied_page` and `thin_copy` count the characters a page carries and
    found them all; `placeholder_copy` found nothing of the template's left; `off_page`
    measures a box against the canvas edges and this one is inside them; `unreadable`
    reads the ink against the ground under a box that has an area.

    Reported rather than refused. A box with no height shows nothing whatever the copy
    inside it says, and the fix is a number in the author's program -- but the whole
    calibration is one page out of 531, and that page's box was moved by the author
    program's own safe-area constants rather than by anything here (D52).
    """
    presentation = open_deck(pptx_path)
    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        lost: list[str] = []
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_text_frame", False):
                continue
            text = " ".join(shape.text_frame.text.split())
            if not text:
                continue
            # None is "this shape states no geometry", which is not evidence of
            # anything; a stated zero is.
            if (shape.width is not None and shape.width <= 0) or (shape.height is not None and shape.height <= 0):
                lost.append(text)
        if not lost:
            continue
        findings.append(
            Finding(
                kind="boxless_copy",
                severity=Severity.WARNING,
                page=number,
                message=(
                    f"page {number} puts {'a block' if len(lost) == 1 else f'{len(lost)} blocks'} of its copy in a "
                    f"box with no height or no width, so the page shows nothing of it: {lost[0][:60]!r}. "
                    f"Give the box the size its copy needs -- `text_size` and `card_size` say how much that is -- "
                    f"or take the block off the page"
                ),
                detail={"blocks": [text[:120] for text in lost]},
            )
        )
    return findings


def wrapped_labels(
    pptx_path: Path, measurer: WidthMeasurer | None = None, words: Sequence[WordBox] | None = None
) -> list[Finding]:
    """Short labels set in boxes too narrow to hold them on one line.

    A numbered step drawn as '01' in a box guessed at 0.25in wraps into a stacked
    '0' over '1', and a render check cannot see it: nothing overlaps, the label is
    just broken. Only short label-like text is held to this -- prose is meant to
    wrap -- and the width carries slack for the renderer resolving to a different
    font than the measurer used.

    `words` is the render, and where there is one it has the last word. `_em_width`
    is a class average per character, not a font metric, and on figures it reads
    high: it over-measured eight digit-and-percent labels on one chart page by
    about 13%, where `LABEL_SLACK` allows 5, and every one of them was on a single
    line in the render. Raising the slack would only trade that for a number
    guessed the other way, so the prediction is kept as what finds candidates and
    the render decides which of them actually broke. Without a render the
    prediction stands alone, which is still better than the check not running --
    that is the case this was written for, a box guessed at 0.25in.
    """
    if measurer is None:
        from raven_ppt.services.assets.text_metrics import measurer as font_measurer

        measurer = font_measurer()
    findings: list[Finding] = []
    painted = _by_page(words) if words is not None else None
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        on_page = painted.get(number, ()) if painted is not None else None
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_text_frame", False):
                continue
            frame = shape.text_frame
            if frame.word_wrap is False:
                continue
            if int(shape.width or 0) <= 0:
                continue  # no declared width to hold anything to
            drawn = _drawn_lines(shape, on_page) if on_page is not None else None
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
                if drawn is not None and _set_on_one_line(text, drawn):
                    continue
                findings.append(
                    Finding(
                        kind="wrapped_label",
                        severity=Severity.WARNING,
                        page=number,
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


# A word's top within this many points of the line's is on that line. The renderer's
# own figure, shared with the render-side checks rather than guessed again here.
_SAME_LINE_PT = 4.0

# How much of a word has to sit inside a box for the box to own it. Half: a word
# straddling two boxes belongs to whichever holds more of it, and at exactly half
# either answer is as good.
_WORD_IN_BOX_SHARE = 0.5


def _drawn_lines(shape: Any, on_page: Sequence[WordBox]) -> list[str]:
    """What the renderer actually set inside this shape, one string per line.

    Whitespace is dropped rather than normalised: the comparison this feeds is
    "did these characters end up on one line", and `pdftotext` splits a run into
    words wherever it likes -- a label the file holds as one string comes back as
    two or three word boxes on the same line.
    """
    box = shape_rect_pt(shape)
    if box.area <= 0:
        return []
    inside = [word for word in on_page if box.overlap(_rect(word)) / max(_rect(word).area, 1e-6) >= _WORD_IN_BOX_SHARE]
    lines: list[list[WordBox]] = []
    for word in sorted(inside, key=lambda word: (word.y0, word.x0)):
        if lines and abs(word.y0 - lines[-1][0].y0) <= _SAME_LINE_PT:
            lines[-1].append(word)
        else:
            lines.append([word])
    return ["".join("".join(word.text.split()) for word in line) for line in lines]


def _set_on_one_line(text: str, drawn: Sequence[str]) -> bool:
    """Whether the render put this label on a single line after all.

    True only on positive evidence. A shape whose words the render did not report --
    an empty list, a label the extractor transcribed differently, a glyph it dropped
    -- is not evidence of anything, and the file's prediction stands.
    """
    wanted = "".join(text.split())
    return bool(wanted) and any(wanted in line for line in drawn)
