"""The band gate: filled colour bars carrying nothing.

This one reports rather than refuses, which for a matter of taste is the whole
of what it can honestly do. Prose could not hold the line. A strip welded to a
card edge, or a band across every title, is the loudest single tell of a
generated deck, and it came back in every deck that was merely asked not to draw
one -- so the instruction was replaced by a measurement on the built file, where
a bar is a bar however it was produced. The fix is a design one: let type weight
and size establish the hierarchy.

It refused decks until it had been wrong three times, and those three are
recorded below: bar series read as accent strips, planes carrying copy read as
empty bands, and a template's own kicker rule refused by an eighth of a
millimetre. Each was answered with another exemption, and an exemption is an
admission that the measurement does not know what it is looking at. A refusal has
to be right the first time. So the reading stays and the refusal goes (design doc D17). A fourth
misreading followed, as a warning this time -- the thin parts of a stacked column
read as accent strips -- which is the same admission, and the reason a reading is
all this is.

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

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from raven.ppt.contracts.findings import Finding, Severity
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
# a grid rather than on equality. The stack pass spends the same number as a
# distance instead -- see `_keys` -- because a grid disagrees with itself
# wherever a boundary happens to fall between two measurements of one edge.
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
                    severity=Severity.WARNING,
                    page=number,
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
    """Shape ids that belong to a chart rather than to decoration.

    A chart drawn from rectangles -- which is often the best way to put a compact
    comparison beside a claim -- is geometrically identical to an accent strip.
    What separates them is company, and a rectangle drawn as data keeps company of
    one of two kinds: bars standing side by side, or one bar cut into parts. Both
    are read here, in both directions, because a bar runs either way.
    """
    marks: set[int] = set()
    for horizontal in (True, False):
        marks |= _series_ids(shapes, horizontal)
        marks |= _stack_ids(shapes, horizontal)
    return marks


def _series_ids(shapes: Sequence[Any], horizontal: bool) -> set[int]:
    """Shape ids belonging to bars standing side by side.

    Bars share a baseline and a thickness, differ in length because that is what
    encodes the values, and sit one per row, so they never occupy the same slot as
    each other.

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


def _stack_ids(shapes: Sequence[Any], horizontal: bool) -> set[int]:
    """Shape ids belonging to bars that were cut into parts.

    A stacked bar is not a series of bars and `_series_ids` cannot see one: its
    parts do not share a baseline, because each part stands on top of the one
    below it, and they do not share a thickness the way the values run, because
    there the thickness *is* the value. Three columns of three parts from
    `ppt_charts.stacked_bar` came back with a finding on each thin top part --
    2.46 x 0.09in, 2.46 x 0.13in, 2.46 x 0.08in, all "an accent strip" -- on the
    one chart in the set whose whole subject is a part too small to be a bar of
    its own. Only the parts sitting on the axis were exempt, and those by
    accident: they happened to share the axis with each other.

    So the parts are joined back into the bar they were cut from -- one slot, one
    thickness, edges meeting -- and the bars are then asked what `_series_ids`
    asks: two or more of them, in two or more slots, on one baseline, and not all
    the same shape. Congruence stands in for that test's "lengths differ", because
    a 100% stack makes every bar the same length on purpose and puts its values in
    where each one is cut.

    A strip welded to a card edge does not get in by this door. The recorded
    decoration is drawn *over* the card, and an overlap joins nothing: it opens a
    run of its own, exactly as a value drawn over its track does. Laid flush
    against the edge it does join, and then a lone card is one run in one slot,
    and a row of identical cards is a row of identical runs -- refused by the slot
    clause and by the congruence clause, the two the series test already leans on.
    What is left over is a row of cards of *different* heights each wearing the
    same thin strip, and that is the same picture as a stack whose first series is
    constant: bars of different lengths, each with an equal small part at one end.
    No measurement of rectangles tells those two apart, and this one reads them
    both as data.
    """
    marks: set[int] = set()
    measured = []
    for run in _runs(shapes, horizontal):
        span = _span(run)
        measured.append(
            (
                run,
                int(span.height if horizontal else span.width),
                int(span.x0 if horizontal else span.y1),
                int(span.y0 if horizontal else span.x0),
            )
        )
    thicknesses = _keys(item[1] for item in measured)
    baselines = _keys(item[2] for item in measured)
    slots = _keys(item[3] for item in measured)
    groups: dict[tuple[int, int], list[tuple[list[tuple[Rect, Any]], int]]] = {}
    for run, thickness, baseline, slot in measured:
        groups.setdefault((thicknesses[thickness], baselines[baseline]), []).append((run, slot))
    for members in groups.values():
        parts = _keys(int(box.width if horizontal else box.height) for run, _ in members for box, _ in run)
        patterns = {tuple(parts[int(box.width if horizontal else box.height)] for box, _ in run) for run, _ in members}
        held = {slots[slot] for _, slot in members}
        cut = any(len(run) >= 2 for run, _ in members)
        if cut and len(held) >= 2 and len(patterns) >= 2 and len(members) <= 2 * len(held):
            marks.update(shape.shape_id for run, _ in members for _, shape in run)
    return marks


def _runs(shapes: Sequence[Any], horizontal: bool) -> list[list[tuple[Rect, Any]]]:
    """The shapes joined into the bars they were cut from, the long way along.

    A part of a stack sits in the slot its neighbours sit in, at their thickness,
    with its edge against theirs. Anything that overlaps its neighbour instead --
    a value drawn over its track, a strip laid on a card -- is not a cut and opens
    a run of its own, which is what keeps `_stack_ids` off the decoration.
    """
    measured = []
    for shape in shapes:
        box = shape_rect_emu(shape)
        measured.append(
            (box, shape, int(box.height if horizontal else box.width), int(box.y0 if horizontal else box.x0))
        )
    thicknesses = _keys(item[2] for item in measured)
    slots = _keys(item[3] for item in measured)
    lanes: dict[tuple[int, int], list[tuple[Rect, Any]]] = {}
    for box, shape, thickness, slot in measured:
        lanes.setdefault((thicknesses[thickness], slots[slot]), []).append((box, shape))
    runs: list[list[tuple[Rect, Any]]] = []
    for lane in lanes.values():
        lane.sort(key=lambda member: (member[0].x0, member[0].x1) if horizontal else (member[0].y0, member[0].y1))
        run: list[tuple[Rect, Any]] = []
        for box, shape in lane:
            if run:
                head = run[-1][0]
                start, finish = (box.x0, head.x1) if horizontal else (box.y0, head.y1)
                if abs(start - finish) > MARK_TOLERANCE_EMU:
                    runs.append(run)
                    run = []
            run.append((box, shape))
        runs.append(run)
    return runs


def _span(run: Sequence[tuple[Rect, Any]]) -> Rect:
    """The uncut bar a run stands for."""
    return Rect(
        min(box.x0 for box, _ in run),
        min(box.y0 for box, _ in run),
        max(box.x1 for box, _ in run),
        max(box.y1 for box, _ in run),
    )


def _keys(values: Iterable[float]) -> dict[float, int]:
    """One key per measurement, shared by anything within a tolerance of it.

    Not `value // MARK_TOLERANCE_EMU`, which is how this module groups elsewhere.
    Dividing puts two numbers a single EMU apart in different groups whenever the
    boundary falls between them, and it does: the middle column of a three-column
    stack came back on its own, its parts joined correctly and then grouped away
    from the other two columns, because the page's axis lands at 5687568 EMU under
    one column and 5687567 under the next -- and 5687568 is 1244 tolerances to the
    EMU. A hand-written program rounds; nothing here should turn a rounding into a
    different answer.
    """
    keys: dict[float, int] = {}
    group, previous = 0, None
    for value in sorted(set(values)):
        if previous is not None and value - previous > MARK_TOLERANCE_EMU:
            group += 1
        keys[value] = group
        previous = value
    return keys
