"""Whether a deck spent its layout budget, read off the pages it built.

`variety` asks whether one arrangement dominates a whole deck. These are the three
readings underneath that, and each is a budget rather than a taste: a page next to a
page of the same shape, a deck that reaches for a row of equal cards again and again,
and a deck whose every page divides its body into equal parts.

Read off the file and not off the plan, for the reason `variety` gives: the plan says
what a page was meant to be and the shapes say what it became, and a declaration
cannot be trusted to report on itself. The plan field that names a layout is a free
string whose only check is that the token appears in a catalogue -- it has never been
compared with the page.

All three report and none refuses. Repeating a shape can be the argument -- two pages
built alike so a reader can compare them is good work -- and the reading is what is
worth having: this page came out the same shape as the one before it. The author
decides whether that was deliberate.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.geometry import EMU_PER_INCH, open_deck
from raven_ppt.services.measure.variety import _read, page_signature

# Two regions count as one row when their vertical spans overlap by this much of the
# shorter one. Cards in a row are drawn to a shared top, but a hand-written program
# rounds, and a caption tucked under one of them is not part of the row.
ROW_OVERLAP_SHARE = 0.6
# Widths this close are the same width. Measured over the four card rows of two decks:
# siblings in a row come out within 0.02in of each other, and the nearest unequal pair
# a deck deliberately drew was 0.59in apart.
SAME_WIDTH_IN = 0.08
# What counts as the page's own ground rather than a region on it.
GROUND_WIDTH_SHARE = 0.95
GROUND_HEIGHT_SHARE = 0.6
# Centres this much apart are still one row, on top of the share of the shorter height
# the overlap rule already allows. Cards drawn to a shared top differ by rounding; a
# caption tucked under one of them is further off than this.
ROW_SLACK_IN = 0.12
# A row of this many equal cards is the arrangement the budget is about. Two is a
# split, which is a different shape and has its own uses.
CARDS_IN_A_ROW = 3
# The share of the page width a row of cards must span to be the body's division
# rather than a strip of chips inside one card. Six 1.31in labels along the top of an
# agenda page are a row of equal things and are not what this budget is about.
CARD_ROW_SPAN_SHARE = 0.6
# How many pages of a deck may be a row of equal cards before it reads as the shape
# the deck reaches for by default. The reference this was drawn from allows two.
EQUAL_CARD_PAGES = 2
# And what share of the composed pages. The absolute budget alone punishes a long deck
# for having three comparison pages out of twenty: measured over twelve published
# templates, the share sits at or under 29% for eleven of them and jumps to 41-43% for
# the ones that reach for the row whatever the page is about.
EQUAL_CARD_SHARE = 0.35
# The share of composed pages that must divide their body unequally. Below this a deck
# is a stack of grids whatever its content was.
ASYMMETRIC_SHARE = 0.4
# Under this many composed pages there is no budget to speak of: a four-page deck with
# two grids is not a deck with a habit.
ENOUGH_PAGES = 6
# How far two regions may sit apart across pages and still be the same place. Measured
# on the pair a reader does read as one page twice: same three columns to the hundredth
# of an inch, 1.6in apart vertically.
SAME_PLACE_IN = 0.12


def quota_findings(pptx_path: Path, structural: Sequence[int] = ()) -> list[Finding]:
    """Every budget reading for this deck."""
    return [
        *repeated_layout(pptx_path, structural),
        *equal_card_habit(pptx_path, structural),
        *symmetry_habit(pptx_path, structural),
    ]


def repeated_layout(pptx_path: Path, structural: Sequence[int] = ()) -> list[Finding]:
    """Pages that came out the same shape as the page before them.

    Adjacent and not deck-wide, which is what makes this a different reading from
    `layout_variety`: a deck may return to a shape later without a reader noticing,
    and cannot put two of them side by side without one.

    The signature is the sieve and the geometry is the test. `page_signature` names what
    carries a page and the grid it falls into, and on a deck whose every region is a
    tinted panel the first half says nothing -- two pages then match on the grid alone,
    which a wide figure over three cards and a dark figure beside a five-row list can do
    by coincidence.
    """
    shapes = _signatures(pptx_path, structural)
    placed = _regions_by_page(pptx_path)
    findings: list[Finding] = []
    for (before, first), (after, second) in zip(shapes, shapes[1:]):
        if after != before + 1 or first != second:
            continue
        if not _same_columns(placed.get(before, ()), placed.get(after, ())):
            continue
        findings.append(
            Finding(
                kind="repeated_layout",
                severity=Severity.WARNING,
                page=after,
                message=(
                    f"this page came out the same arrangement as page {before} -- both are "
                    f"{first}. A reader meeting the same shape twice in a row reads the second as "
                    "the first one again. Give one of them a different division of the body: the "
                    "layout catalogue's asymmetric entries exist for the page that would otherwise "
                    "repeat its neighbour"
                ),
                detail={"page": after, "same_as": before, "signature": first},
            )
        )
    return findings


def equal_card_habit(pptx_path: Path, structural: Sequence[int] = ()) -> list[Finding]:
    """A deck that lays its body out as a row of equal cards more than it should.

    One page of it is a comparison. Every third page of it is the shape a program
    reaches for when it has a list and no argument about the list.
    """
    pages = _card_rows(pptx_path, structural)
    composed = _composed(pptx_path, structural)
    if len(pages) <= EQUAL_CARD_PAGES or not composed:
        return []
    share = len(pages) / composed
    if share < EQUAL_CARD_SHARE:
        return []
    return [
        Finding(
            kind="equal_card_habit",
            severity=Severity.WARNING,
            page=None,
            message=(
                f"{len(pages)} of {composed} composed pages lay their body out as a row of equal "
                f"cards ({share:.0%}, pages {', '.join(str(page) for page in pages)}). Past "
                f"{EQUAL_CARD_SHARE:.0%} the row of cards is what the deck does rather than what "
                "one page says. The content that keeps arriving as three equal boxes is usually a "
                "figure with an insight, a route against a route, or a list against a picture"
            ),
            detail={
                "pages": list(pages),
                "composed": composed,
                "share": round(share, 2),
                "budget": EQUAL_CARD_PAGES,
                "floor": EQUAL_CARD_SHARE,
            },
        )
    ]


def _composed(pptx_path: Path, structural: Sequence[int]) -> int:
    """How many pages of this deck carry a composition rather than page furniture."""
    total = len(open_deck(pptx_path).slides)
    return total - sum(1 for page in set(structural) if 1 <= page <= total)


def symmetry_habit(pptx_path: Path, structural: Sequence[int] = ()) -> list[Finding]:
    """A deck whose pages nearly all divide their body into equal parts."""
    divided = _divisions(pptx_path, structural)
    if len(divided) < ENOUGH_PAGES:
        return []
    uneven = [page for page, equal in divided.items() if not equal]
    share = len(uneven) / len(divided)
    if share >= ASYMMETRIC_SHARE:
        return []
    return [
        Finding(
            kind="symmetry_habit",
            severity=Severity.WARNING,
            page=None,
            message=(
                f"{len(uneven)} of {len(divided)} composed pages divide their body unequally "
                f"({share:.0%}); below {ASYMMETRIC_SHARE:.0%} a deck reads as one grid restated. "
                "An unequal division is what gives a page a subject: a picture against a column of "
                "copy, a figure against its reading, a wide route against a narrow one"
            ),
            detail={
                "uneven_pages": uneven,
                "measured": len(divided),
                "share": round(share, 2),
                "floor": ASYMMETRIC_SHARE,
            },
        )
    ]


def _signatures(pptx_path: Path, structural: Sequence[int]) -> list[tuple[int, str]]:
    presentation = open_deck(pptx_path)
    canvas_h = (presentation.slide_height or 0) / EMU_PER_INCH
    skipped = set(structural)
    found: list[tuple[int, str]] = []
    for number, slide in enumerate(presentation.slides, start=1):
        if number in skipped:
            continue
        signature = page_signature(slide, canvas_h)
        if signature is not None:
            # Without the mark count: how many badges and rules a page carries is not
            # its arrangement. Two pages of "a claim block over three equal cards"
            # differed only in that tail, and a reader meeting them back to back sees
            # one shape twice.
            found.append((number, signature.split(",")[0]))
    return found


def _regions_by_page(pptx_path: Path) -> dict[int, tuple]:
    presentation = open_deck(pptx_path)
    canvas_h = (presentation.slide_height or 0) / EMU_PER_INCH
    found: dict[int, tuple] = {}
    for number, slide in enumerate(presentation.slides, start=1):
        read = _read(slide, canvas_h)
        if read is not None:
            found[number] = tuple(read[1])
    return found


def _same_columns(first: Sequence, second: Sequence) -> bool:
    """Whether these two pages put the same number of things in the same columns.

    Columns and not rows: what a reader recognises across two pages is the vertical
    division, and the pair this was measured on holds its three cards at the same three
    left edges 1.6in further down. Requiring the rows to match as well would let a deck
    restate a page by sliding it.
    """
    if len(first) != len(second) or not first:
        return False
    ordered = (
        sorted(first, key=lambda box: (round(box.y0, 1), box.x0)),
        sorted(second, key=lambda box: (round(box.y0, 1), box.x0)),
    )
    return all(
        abs(here.x0 - there.x0) <= SAME_PLACE_IN and abs(here.width - there.width) <= SAME_PLACE_IN
        for here, there in zip(*ordered)
    )


def _rows_of(regions: Sequence, canvas_w: float = 13.333, canvas_h: float = 7.5) -> list[list]:
    """The page's regions grouped into the rows a reader sees them in.

    Grounds come out first. A shape the width of the page and most of its height is
    what the page is painted on, and it overlaps every row vertically -- reading it as
    a region collapsed every page in the calibration set into one row of everything.

    Grouped on the centre of each span rather than on overlap with whichever region
    sorted first: a tall picture beside a stack of cards overlaps all of them, and the
    stack is still three rows.
    """
    kept = [
        box
        for box in regions
        if not (box.width >= canvas_w * GROUND_WIDTH_SHARE and box.height >= canvas_h * GROUND_HEIGHT_SHARE)
    ]
    rows: list[list] = []
    for box in sorted(kept, key=lambda item: (item.y0 + item.y1) / 2):
        centre = (box.y0 + box.y1) / 2
        if rows:
            row = rows[-1]
            shortest = min(min(item.height for item in row), box.height)
            near = min(abs(centre - (item.y0 + item.y1) / 2) for item in row)
            if shortest > 0 and near <= shortest * (1.0 - ROW_OVERLAP_SHARE) + ROW_SLACK_IN:
                row.append(box)
                continue
        rows.append([box])
    return rows


def _card_rows(pptx_path: Path, structural: Sequence[int]) -> list[int]:
    presentation = open_deck(pptx_path)
    canvas_h = (presentation.slide_height or 0) / EMU_PER_INCH
    canvas_w = (presentation.slide_width or 0) / EMU_PER_INCH
    skipped = set(structural)
    pages: list[int] = []
    for number, slide in enumerate(presentation.slides, start=1):
        if number in skipped:
            continue
        read = _read(slide, canvas_h)
        if read is None:
            continue
        _kinds, regions, _marks = read
        for row in _rows_of(regions, canvas_w, canvas_h):
            if len(row) < CARDS_IN_A_ROW:
                continue
            if _cards_in(_outermost(row), canvas_w) >= CARDS_IN_A_ROW:
                pages.append(number)
                break
    return pages


def _outermost(row: Sequence) -> list:
    """The row with its nested regions dropped.

    A card and the text frame inside it are both regions, and counting both reads a row
    of three cards as six boxes of two alternating widths -- so the row is never uniform
    and its widest matching group is three, by accident, whatever the page looks like.
    What a reader counts is the outer boxes.
    """
    return [
        box
        for box in row
        if not any(
            other is not box and other.x0 <= box.x0 and other.x1 >= box.x1 and other.width > box.width for other in row
        )
    ]


def _cards_in(row: Sequence, canvas_w: float) -> int:
    """How many cards the widest same-width group in this row holds.

    Zero unless that group spans `CARD_ROW_SPAN_SHARE` of the page: three equal boxes
    are a row of cards when they divide the body and a strip of labels when they sit
    inside something else, and the width they cover between them is what tells those
    apart.
    """
    widths = sorted(box.width for box in row)
    best = 0
    for index, width in enumerate(widths):
        group = [other for other in widths[index:] if other - width <= SAME_WIDTH_IN]
        if sum(group) >= canvas_w * CARD_ROW_SPAN_SHARE:
            best = max(best, len(group))
    return best


def _divisions(pptx_path: Path, structural: Sequence[int]) -> dict[int, bool]:
    """page -> whether its widest row divides the body into equal parts.

    The widest row and not every row: a page's shape is the division a reader takes
    from it, and that is the one that spans the body. A page with one region divides
    nothing and is not counted either way.

    Nested regions come out here for the same reason they do in `_card_rows`: a row of
    equal cards each holding a narrower text frame reads as two alternating widths, so
    the most regular division a page can have would be reported as its least.
    """
    presentation = open_deck(pptx_path)
    canvas_h = (presentation.slide_height or 0) / EMU_PER_INCH
    canvas_w = (presentation.slide_width or 0) / EMU_PER_INCH
    skipped = set(structural)
    divided: dict[int, bool] = {}
    for number, slide in enumerate(presentation.slides, start=1):
        if number in skipped:
            continue
        read = _read(slide, canvas_h)
        if read is None:
            continue
        _kinds, regions, _marks = read
        rows = [_outermost(row) for row in _rows_of(regions, canvas_w, canvas_h)]
        rows = [row for row in rows if len(row) > 1]
        if not rows:
            continue
        widest = max(rows, key=lambda row: sum(box.width for box in row))
        widths = [box.width for box in widest]
        divided[number] = max(widths) - min(widths) <= SAME_WIDTH_IN
    return divided
