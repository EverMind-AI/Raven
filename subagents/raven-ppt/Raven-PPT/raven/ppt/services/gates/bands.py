"""The band gate: filled colour bars carrying nothing.

This one refuses a deck, which for a matter of taste wants justifying. Prose
could not hold the line. A strip welded to a card edge, or a band across every
title, is the loudest single tell of a generated deck, and it came back in every
deck that was merely asked not to draw one -- so the instruction was replaced by
a measurement on the built file, where a bar is a bar however it was produced.
It is addressed to the design pass rather than the author because the fix is a
design one: let type weight and size establish the hierarchy.

Two shapes are refused and one is allowed, and the allowance is the interesting
part. A narrow accent strip is refused wherever it sits. A full-width band is
refused only when nothing sits on it -- a plane with copy on it is grouping, and
grouping is what a tinted plane is for.

That allowance used to require the band be at the top of the page, the deck's
title row, and a 20-page academic deck is why it does not any more: five
full-width planes carrying formulas, a training schedule and a takeaway row came
back BLOCKING as "a band with nothing on it" while one to six text boxes sat on
each of them. The message was false, the author's only move was to delete good
grouping, and on the same page a plane 0.06in taller passed untouched -- above
`BAND_MAX_HEIGHT_EMU` it is a panel rather than a bar. Carrying copy is now the
whole test, which is what the gate's own first sentence says it is about.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from raven.ppt.contracts.findings import Audience, Finding, Severity
from raven.ppt.services.measure.geometry import (
    EMU_PER_INCH,
    PICTURE,
    Rect,
    has_text,
    is_panel,
    open_deck,
    shape_rect_emu,
    text_boxes_emu,
)
from raven.ppt.services.measure.rendered import RULE_MAX_HEIGHT_PT

# A hairline divider is a legitimate thing and stays legitimate. What is banned
# is a bar: thick enough to read as a block of colour, long enough to run
# alongside content rather than separate it.
#
# Where a rule stops being a rule is `RULE_MAX_HEIGHT_PT`, borrowed rather than
# restated: this gate used to stop at 0.035in (2.5pt) while the measurement module
# called anything under 4.5pt a hairline, and a live deck fell in the gap. Its
# author drew the template's own device -- a 1.05in orange kicker rule under each
# of eight titles, 0.04in tall, correct in the render -- and every one was refused
# as "a filled bar carrying no content", by one eighth of a millimetre. Ordinary
# typographic rules run 0.5pt to 4pt, so the old line cut through the middle of
# legitimate practice; the strips this gate exists for are 0.08in and up.
RULE_MAX_EMU = int(RULE_MAX_HEIGHT_PT * EMU_PER_INCH / 72)
STRIP_MAX_SHORT_EMU = int(0.22 * EMU_PER_INCH)
STRIP_MIN_ASPECT = 6.0
BAND_MIN_WIDTH_FRACTION = 0.85
BAND_MAX_HEIGHT_EMU = int(1.3 * EMU_PER_INCH)
# Half of the text box has to fall inside the band: a title set on a band
# overlaps it almost entirely, while the subtitle below one clips its lower edge
# and is not what the band is carrying.
TITLE_COVERAGE_SHARE = 0.5
# 0.005in. Bars in a series are drawn to a shared baseline and a shared
# thickness, but a hand-written program rounds them, so the grouping is done on
# a grid rather than on equality.
MARK_TOLERANCE_EMU = 4572

_MESSAGE = (
    "a filled bar carrying no content. Let type weight and size establish the hierarchy; if the page "
    "needs a divider, use a hairline in the secondary grey. The one band this deck draws is the one a "
    "title row sits on -- at the top of the page, with the title on it"
)


def band_findings(pptx_path: Path) -> list[Finding]:
    """Filled colour bars on the finished pages."""
    presentation = open_deck(pptx_path)
    slide_w = presentation.slide_width
    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        bars = [
            shape
            for shape in slide.shapes
            if getattr(shape, "shape_type", None) != PICTURE and is_panel(shape) and not has_text(shape)
        ]
        titles = text_boxes_emu(slide)
        marks = data_mark_ids(bars)
        for shape in bars:
            box = shape_rect_emu(shape)
            if box.width <= 0 or box.height <= 0:
                continue
            if shape.shape_id in marks:
                continue  # one of a series of bars: a chart drawn from rectangles
            reason = _refusal(box, slide_w, holds_text(box, titles))
            if reason is None:
                continue
            findings.append(
                Finding(
                    kind="band",
                    severity=Severity.BLOCKING,
                    page=number,
                    audience=Audience.DESIGNER,
                    message=f"{reason}: {_MESSAGE}",
                    detail={
                        "shape": shape.shape_id,
                        "reason": reason,
                        "size_in": f"{box.width / EMU_PER_INCH:.2f}x{box.height / EMU_PER_INCH:.2f}",
                    },
                )
            )
    return findings


def _refusal(box: Rect, slide_w: int, carries_copy: bool) -> str | None:
    """Why this bar is refused, or None if it is allowed.

    Order matters: the strip test runs first, so a narrow vertical accent on a
    title band is still an accent strip and not a title row.
    """
    short, long_ = min(box.width, box.height), max(box.width, box.height)
    if short <= RULE_MAX_EMU:
        return None  # a hairline rule
    if short <= STRIP_MAX_SHORT_EMU and long_ >= STRIP_MIN_ASPECT * short:
        return "an accent strip"
    if box.width >= BAND_MIN_WIDTH_FRACTION * slide_w and box.height <= BAND_MAX_HEIGHT_EMU:
        return None if carries_copy else "a band with nothing on it"
    return None


def holds_text(band: Rect, boxes: Sequence[Rect]) -> bool:
    """Whether a band is the ground under a line of text rather than bare colour.

    Measured as a share of the *text box*, not of the band: a title set on a band
    covers a fraction of it and all of itself.
    """
    return any(box.area > 0 and band.overlap(box) / box.area >= TITLE_COVERAGE_SHARE for box in boxes)


def data_mark_ids(shapes: Sequence[Any]) -> set[int]:
    """Shape ids that belong to a bar series rather than to decoration.

    A chart drawn from rectangles -- which is often the best way to put a compact
    comparison beside a claim -- is geometrically identical to an accent strip.
    What separates them is company: bars share a baseline and a thickness, differ
    in length because that is what encodes the values, and sit one per row, so
    they never occupy the same slot as each other.

    That last clause is load-bearing. Without it a card and the strip welded to
    its edge pass as a series of two -- same height, same left edge, different
    widths -- which is precisely the decoration this is supposed to let the gate
    catch, exempting itself by sitting on top of the thing it decorates.

    It said "one per row" literally at first, and that refused the commonest way to
    draw a bar: a full-length track with the value drawn over it, two shapes to a row.
    A live deck's page had eight such shapes on four rows -- tracks at 2.55in, values
    at 2.26, 2.36, 2.17, 2.20 -- and every one came back BLOCKING as an accent strip,
    on a page the skill itself asks for ("ranking -> sorted horizontal bars"). Two to a
    row is a track and its value; the decoration this catches is one shape on one row,
    which `len(slots) >= 2` still refuses.
    """
    marks: set[int] = set()
    for horizontal in (True, False):
        groups: dict[tuple[int, int], list[Any]] = {}
        for shape in shapes:
            box = shape_rect_emu(shape)
            thickness = int(box.height if horizontal else box.width)
            baseline = int(box.x0 if horizontal else box.y1)
            key = (thickness // MARK_TOLERANCE_EMU, baseline // MARK_TOLERANCE_EMU)
            groups.setdefault(key, []).append(shape)
        for members in groups.values():
            boxes = [shape_rect_emu(member) for member in members]
            lengths = {int(box.width if horizontal else box.height) for box in boxes}
            slots = {int(box.y0 if horizontal else box.x0) for box in boxes}
            if len(members) >= 2 and len(lengths) >= 2 and len(slots) >= 2 and len(members) <= 2 * len(slots):
                marks.update(member.shape_id for member in members)
    return marks
