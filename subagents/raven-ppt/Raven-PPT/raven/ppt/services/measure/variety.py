"""Whether a deck's composed pages are all the same page.

The one defect in this family that no other measurement can see. Every page can pass
every check -- the type on the ramp, nothing colliding, nothing off the canvas, the
title row where the template puts it -- and the deck still be eleven pages of the same
composition. A delivered 20-page deck was exactly that: of its composed pages, four are
the same eight lines of code, a table over one rounded `plane` in `surface` with three
`points` under it, and the reader's experience of page 18 is that they have already
seen it three times.

Read off the built file rather than off the plan. The plan can say what the page was
meant to be; only the shapes say what it became, and the two disagreeing is precisely
the case a declaration cannot be trusted to report on itself.

A warning and never a refusal. "Varied enough" is not a property a page has -- a series
of pages built the same way so a reader can compare them is good work, and so is a deck
whose material genuinely wants one shape twice. What is reportable is the reading: this
many of your composed pages came out as the same arrangement of the same kinds of
thing. The author decides whether that was the argument or the path of least
resistance.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from raven.ppt.contracts.findings import Finding, Severity
from raven.ppt.services.measure.geometry import (
    EMU_PER_INCH,
    is_panel,
    iter_shapes,
    open_deck,
    page_box,
    shows_picture,
)

# A shape smaller than this is a mark rather than a region: a bullet, a badge, a
# rule, an axis, a timeline stop. Regions are what a page's arrangement is made of.
REGION_MIN_AREA_IN = 0.8
MARK_MAX_AREA_IN = 1.2
# How many marks separate a page that carries a couple of badges from one carrying a
# drawn chart, a timeline or a row of numbered stops. Measured over a 23-page proof
# deck of one page per catalogue entry: the card and figure pages put down 0 or 1, the
# hotspot page 6, the timeline 7 and the modifier page 7.
MANY_MARKS = 5
# Everything wholly inside this share of the page's height is the header, which every
# page of a deck shares by design and which therefore says nothing about how this page
# is composed. Without it the band `heading` paints reads as a panel on all 23 pages of
# the proof deck and the kicker as prose on all 23.
HEADER_SHARE = 0.18
# Two region edges this close are one column, or one row. A page laid out on the grid
# puts its columns on exact multiples of the gutter; one laid out by eye does not, and
# the tolerance is what stops that being counted as a column of its own.
BAND_TOLERANCE_IN = 0.6
# Copy shorter than this is a label on something rather than a region of its own.
TEXT_MIN_CHARS = 12
# Below this many measured pages a share means nothing: three pages of one shape out of
# four is a short deck, not a habit.
MIN_PAGES = 6
# The share of the measured pages one signature has to reach, and the count it has to
# reach with it. Both, because either alone misreads: 60% of six pages is four, which
# is a habit, while 60% of an eight-page deck that is four pages of one shape and four
# of another is nothing at all.
CONCENTRATED = 0.6
CONCENTRATED_PAGES = 4

_DRAWINGML = "http://schemas.openxmlformats.org/drawingml/2006/main"


def layout_variety(pptx_path: Path, structural: list[int] | None = None) -> list[Finding]:
    """One finding when the deck's composed pages nearly all take one shape.

    `structural` names the pages that are the template's own -- a cover, a contents
    list, a divider, a closing. They are meant to be alike, and counting them is how a
    correct deck gets reported for the four pages it was supposed to clone.
    """
    skip = set(structural or ())
    deck = open_deck(pptx_path)
    canvas = (deck.slide_height or 0) / EMU_PER_INCH or 7.5
    pages = [(number, slide) for number, slide in enumerate(deck.slides, start=1) if number not in skip]
    measured = {
        number: signature for number, slide in pages if (signature := page_signature(slide, canvas)) is not None
    }
    if len(measured) < MIN_PAGES:
        return []
    # The fine reading first, because it is the more actionable of the two: naming the
    # grid as well as the kinds says which pages to look at. The coarse one is what
    # catches the deck the fine one talks itself out of.
    found = _concentration(measured)
    reading = "composition"
    if found is None:
        coarse = {
            number: materials for number, slide in pages if (materials := page_materials(slide, canvas)) is not None
        }
        found = _concentration(coarse)
        reading = "materials"
        if found is None:
            return []
        measured = coarse
    shape, count = found
    counted = Counter(measured.values())
    repeated = sorted(number for number, one in measured.items() if one == shape)
    said = _english(shape) if reading == "composition" else f"{shape.replace('+', ' and ')}, arranged differently"
    return [
        Finding(
            kind="layout_variety",
            severity=Severity.WARNING,
            message=(
                f"{count} of this deck's {len(measured)} composed pages came out as the same "
                f"{reading} -- {said} -- on pages {', '.join(str(page) for page in repeated)}. "
                f"The deck has {len(counted)} distinct page structure{'' if len(counted) == 1 else 's'} in it. "
                f"That is a reading and not a "
                f"verdict: a series of pages built alike so a reader can compare them is good work. But if "
                f"those pages are not a series, the shape was the path of least resistance rather than a "
                f"choice -- deck/build/references/layouts.md carries a registry of page structures and "
                f"modifier layers, and more than one modifier on a page is the ordinary case"
            ),
            detail={
                "repeated": repeated,
                "signature": shape,
                "reading": reading,
                "measured": len(measured),
                "distinct": len(counted),
            },
        )
    ]


def _concentration(measured: dict[int, str]) -> tuple[str, int] | None:
    """The one shape most of these pages take, when one of them does."""
    if len(measured) < MIN_PAGES:
        return None
    shape, count = Counter(measured.values()).most_common(1)[0]
    if count < CONCENTRATED_PAGES or count / len(measured) < CONCENTRATED:
        return None
    return shape, count


def page_signature(slide: Any, canvas_h: float = 7.5) -> str | None:
    """What this page is made of and how it is arranged, as one comparable string.

    Three parts, because no one of them tells two pages apart on its own. The kinds say
    what carries the page -- a picture, a table, a tinted panel, a bulleted list, prose.
    The grid says how many columns and rows those regions fall into. The marks say
    whether there is a drawn thing on the page at all: a chart's bars and axis, a
    timeline's stops, a row of numbered badges all arrive as a crowd of small shapes and
    never as one object, so counting them is what separates a chart page from a card page
    that happens to share its grid.

    None for a page with nothing on it worth arranging, which is a cover or a divider
    however it was built.
    """
    read = _read(slide, canvas_h)
    if read is None:
        return None
    kinds, regions, marks = read
    columns = _bands([(box.x0 + box.x1) / 2 for box in regions])
    rows = _bands([(box.y0 + box.y1) / 2 for box in regions])
    drawn = "no marks" if not marks else ("marks" if marks < MANY_MARKS else "many marks")
    return f"{'+'.join(sorted(kinds))} in {columns}x{rows}, {drawn}"


def page_materials(slide: Any, canvas_h: float = 7.5) -> str | None:
    """What this page is made of, with nothing about how it is arranged.

    The blunter of the two readings, and the one a reader agrees with. The signature
    above separates two card pages when one card's copy wraps onto a third line, because
    the row count moves; it separates them again when one of them carries a badge. On a
    live 20-page deck nine pages were the same thing -- a tinted panel with a heading and
    copy in it, in grids of two and three columns -- and the fine reading found seven
    shapes among those nine and reported nothing at all.

    So the grid, the marks, and the difference between a bulleted card and a prose card
    are all dropped here. What is left is the sentence a reader would say: these pages
    are all a panel with words on it.
    """
    read = _read(slide, canvas_h)
    if read is None:
        return None
    kinds, _, _ = read
    return "+".join(sorted({"text" if kind == "points" else kind for kind in kinds}))


def _read(slide: Any, canvas_h: float) -> tuple[set[str], list[Any], int] | None:
    """The kinds on the page, the regions they occupy, and the count of marks.

    One scan for both readings: two scans of the same shapes drift the moment either
    definition of a region moves.
    """
    header = canvas_h * HEADER_SHARE
    kinds: set[str] = set()
    regions: list[Any] = []
    marks = 0
    for shape in iter_shapes(slide.shapes):
        box = page_box(shape)
        if box is None or box.y1 <= header:
            continue
        area = box.width * box.height
        if shows_picture(shape):
            kinds.add("picture")
            regions.append(box)
            continue
        if getattr(shape, "has_table", False):
            kinds.add("table")
            regions.append(box)
            continue
        text = shape.text_frame.text.strip() if getattr(shape, "has_text_frame", False) else ""
        if text:
            if len(text) < TEXT_MIN_CHARS:
                continue
            kinds.add("points" if _bulleted(shape) else "text")
            if area >= REGION_MIN_AREA_IN:
                regions.append(box)
            continue
        if not is_panel(shape):
            continue
        if area < MARK_MAX_AREA_IN:
            marks += 1
            continue
        kinds.add("panel")
        regions.append(box)
    if not kinds or not regions:
        return None
    return kinds, regions, marks


def _bulleted(shape: Any) -> bool:
    """Whether this frame's copy is a real list, which `points` writes and `write` does not."""
    body = shape.text_frame._txBody  # noqa: SLF001 -- python-pptx models no bullet property
    for tag in ("buChar", "buAutoNum"):
        if body.findall(f".//{{{_DRAWINGML}}}{tag}"):
            return True
    return False


def _bands(centres: list[float]) -> int:
    """How many distinct columns (or rows) these centres fall into."""
    bands = 0
    last = None
    for centre in sorted(centres):
        if last is None or centre - last > BAND_TOLERANCE_IN:
            bands += 1
        last = centre
    return bands


def _english(signature: str) -> str:
    kinds, _, rest = signature.partition(" in ")
    return f"{kinds.replace('+', ' and ')}, laid out {rest}"
