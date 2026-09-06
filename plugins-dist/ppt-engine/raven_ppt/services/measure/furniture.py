"""Page furniture: what tells a reader where they are and where one group ends.

Two readings that have nothing to do with what a page holds and everything to do with
whether it can be read: whether a page says which page it is, and whether the groups
on it have visible edges.

Every bundled template defines a page-number placeholder on its master, and none of
them puts one on a page. python-pptx does not clone a footer placeholder onto a slide,
so a deck built by cloning prototypes comes out with no page numbers anywhere -- and
nothing measured that, because every check was about what a page holds rather than
about what tells a reader where they are in it.

What it costs is not the number. A body that ends two thirds down an otherwise blank
page reads as unfinished, and a rule across the foot is what says the page ends there
because it was meant to. That is why this is measured over the deck rather than page by
page: one page without a foot is a full-bleed page, and a deck without one is a deck
whose every page trails off.

Reports and never refuses. A deck may be a set of full-bleed pages by design.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.geometry import EMU_PER_INCH, iter_shapes, open_deck

# The share of the page's height, measured from the bottom, that counts as its foot.
# Two things have to land inside it. A template's own footer placeholder sits at 7.01in
# of 7.5in, the last 6.5%; and `ppt_layout.page(footer=True)` reserves 6.48-6.78in, with
# `footer()` writing at 6.58in. At 0.12 the band started at 6.60in and missed the second
# by 0.02in -- so a page whose foot was drawn by our own helper was reported as having
# none, and the test missed it because its fixture wrote the foot at 6.9in. 0.15 puts the
# floor at 6.375in: below the body's own floor at 6.20in and above the reserved strip.
FOOT_SHARE = 0.15
# How many composed pages must carry a foot before the deck reads as having one.
FOOT_SHARE_FLOOR = 0.5
# And how many of a template's own pages before the design reads as using a foot at all,
# which is a different question and takes a different number. The twelve bundled
# templates sit at 0.00 to 0.11 and a real-world template that does use feet at 0.67.
TEMPLATE_FOOT_SHARE = 0.25
# Under this many composed pages there is no habit to report either way.
ENOUGH_PAGES = 5
# Two text clusters count as one row when their vertical spans overlap by this much of
# the shorter one -- columns are drawn to a shared top and a program rounds.
ROW_OVERLAP_SHARE = 0.5
# Centres this much apart are still one row, on top of the share of the shorter height
# the overlap rule allows.
ROW_SLACK_IN = 0.12
# A box this share of the page wide is the page's own copy rather than one column of it.
FULL_WIDTH_SHARE = 0.8
# Left edges this close are the same edge -- a hand-written program rounds, and
# `alignment.FLUSH_SLOP_PT` allows the same 1pt.
SAME_EDGE_IN = 0.05
# And this far apart are two edges a reader reads as two columns. Between the two is the
# miss: near enough to look like one edge, far enough to see it is not.
NEAR_EDGE_IN = 0.5
# How many columns a row needs before it is worth comparing with another. Three: two
# rows of two are a split, and every page has several of those.
COLUMNS_TO_COMPARE = 3


def footer_findings(pptx_path: Path, structural: Sequence[int] = (), prototypes: Path | None = None) -> list[Finding]:
    """The one reading: how many of this deck's composed pages name themselves.

    Silent where the template it was built in has no foot of its own. Measured over the
    twelve bundled templates: eight put text in the bottom band on **no** page at all
    and the other four on one or two of their fifteen to twenty-five, so on a bound deck
    this was asking for a strip the design does not have -- the same mistake `title_row`
    made in asking a cover to put its title in the body row. With no template bound the
    house is ours and the reading stands.
    """
    presentation = open_deck(pptx_path)
    canvas_h = (presentation.slide_height or 0) / EMU_PER_INCH
    if not canvas_h:
        return []
    floor = canvas_h * (1.0 - FOOT_SHARE)
    skipped = set(structural)
    composed: list[int] = []
    footed: list[int] = []
    numbered = 0
    for number, slide in enumerate(presentation.slides, start=1):
        if number in skipped:
            continue
        composed.append(number)
        # Every text in the band and not the first one found: a foot carries a source
        # line on the left and the number on the right, and stopping at the first stops
        # at whichever the script drew first -- which reported a numbered deck as
        # unnumbered.
        has_foot = False
        has_number = False
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_text_frame", False) or shape.top is None:
                continue
            if not shape.text_frame.text.strip():
                continue
            if shape.top / EMU_PER_INCH < floor:
                continue
            has_foot = True
            if "slidenum" in shape.text_frame._txBody.xml:
                has_number = True
        if has_number:
            numbered += 1
        if has_foot:
            footed.append(number)
    if len(composed) < ENOUGH_PAGES:
        return []
    share = len(footed) / len(composed)
    if share < FOOT_SHARE_FLOOR and not _template_foots(prototypes):
        return []
    if share >= FOOT_SHARE_FLOOR:
        return _numbering(composed, footed, numbered)
    absent = [page for page in composed if page not in set(footed)]
    return [
        Finding(
            kind="no_footer",
            severity=Severity.WARNING,
            page=None,
            message=(
                f"{len(absent)} of {len(composed)} composed pages have nothing in the bottom "
                f"{FOOT_SHARE:.0%} of the page (pages {_listed(absent)}). A page whose body ends "
                "above that and puts nothing under it trails off rather than ending, and the "
                "reader has no way to say where they are in the deck. `page(footer=True)` gives "
                "the strip back and `footer(slide, frame.footer, T, note=...)` draws it -- a "
                "hairline across the foot, a note on the left and the page number on the right. "
                "The template's own page-number placeholder lives on its master and is not cloned "
                "onto a slide, so a script that does not draw one ships a deck with none"
            ),
            detail={"absent": absent, "composed": len(composed), "share": round(share, 2)},
        )
    ]


def _template_foots(prototypes: Path | None) -> bool:
    """Whether the template puts copy in the foot on enough of its own pages to ask for one.

    A share of its own and not `FOOT_SHARE_FLOOR`: that floor asks whether a deck has
    made the foot its habit, and this asks whether the design uses one at all. Measured
    over the twelve bundled templates the share runs 0.00 to 0.11 -- eight put nothing
    at the foot of any page, and the other four carry one on one or two of their fifteen
    to twenty-five. A real-world template that does use feet came out at 0.67. Anything
    between those two groups reads the same way on both, and the half a deck is held to
    would suppress the reading on the template that actually asks for a foot.

    Every page counts towards the share, including the cover and the section breaks a
    template naturally leaves footless -- there is no outline for a prototype file, so
    the structural pages a deck excludes cannot be told apart here. That pushes a
    template's share down rather than up, which the distance between 0.11 and 0.67
    absorbs.
    """
    if prototypes is None or not Path(prototypes).is_file():
        return True  # no template, no design to defer to
    try:
        presentation = open_deck(Path(prototypes))
    except Exception:  # noqa: BLE001 -- an unreadable template is not a measurement
        return True
    canvas_h = (presentation.slide_height or 0) / EMU_PER_INCH
    if not canvas_h:
        return True
    if len(presentation.slides) < ENOUGH_PAGES:
        return True  # too few example pages to read a habit off either way
    floor = canvas_h * (1.0 - FOOT_SHARE)
    pages = footed = 0
    for slide in presentation.slides:
        pages += 1
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_text_frame", False) or shape.top is None:
                continue
            if shape.text_frame.text.strip() and shape.top / EMU_PER_INCH >= floor:
                footed += 1
                break
    return bool(pages) and footed / pages >= TEMPLATE_FOOT_SHARE


def _numbering(composed: list[int], footed: list[int], numbered: int) -> list[Finding]:
    """A deck with feet but no numbers in them.

    Separate from the reading above because the remedy is one keyword rather than a
    layout change, and because a foot carrying a source line is doing half the job:
    the page ends visibly and still does not say which page it is.
    """
    if numbered or not footed:
        return []
    return [
        Finding(
            kind="unnumbered_pages",
            severity=Severity.WARNING,
            page=None,
            message=(
                f"all {len(composed)} composed pages carry a foot and none of them carries a page "
                "number. `footer()` writes one as a `slidenum` field, which stays right when a "
                "page is inserted ahead of it -- a digit written into the copy does not. If the "
                "foot was drawn by hand, the field is what `footer(..., number=True)` adds"
            ),
            detail={"composed": len(composed), "footed": len(footed)},
        )
    ]


def _listed(pages: Sequence[int], most: int = 8) -> str:
    shown = [str(page) for page in pages[:most]]
    if len(pages) > most:
        shown.append(f"and {len(pages) - most} more")
    return ", ".join(shown)


def _within(box, cover, slack: float = 0.04) -> bool:
    return (
        cover[0] <= box[0] + slack
        and cover[1] <= box[1] + slack
        and cover[2] >= box[2] - slack
        and cover[3] >= box[3] - slack
    )


def grid_findings(pptx_path: Path, structural: Sequence[int] = ()) -> list[Finding]:
    """Pages laid out on two column grids at once.

    `alignment.flush_drift` compares boxes that *declare* the same left edge and asks
    whether the render kept them there. This asks the other half: two edges the file
    declares 0.12in apart, which no reader reads as a decision. On the page a reader
    called out, a chevron band ran at 0.72 / 4.56 / 8.40 and the copy under it at
    0.84 / 4.80 / 8.76 -- two grids whose steps differ by 0.12in, so the miss grew down
    the page and every column was out by more than the last.

    Only edges that are not inside a panel. A caption indented 0.19in inside the surface
    it sits on is the padding every card has, and comparing it against the card's own
    edge would report the technique rather than the mistake -- which is what the first
    version of this did on a deck whose alignment a reader was happy with.
    """
    presentation = open_deck(pptx_path)
    canvas_w = (presentation.slide_width or 0) / EMU_PER_INCH
    if not canvas_w:
        return []
    skipped = set(structural)
    guilty: dict[int, list[tuple[float, list[float]]]] = {}
    for number, slide in enumerate(presentation.slides, start=1):
        if number in skipped:
            continue
        both = _near_misses(slide, canvas_w)
        if both:
            guilty[number] = both
    if not guilty:
        return []
    pages = sorted(guilty)
    worst = max(
        guilty.items(), key=lambda item: max(abs(here - there) for here, there in zip(item[1][0][1], item[1][1][1]))
    )
    (one_step, one_row), (other_step, other_row) = worst[1]
    drift = max(abs(here - there) for here, there in zip(one_row, other_row))
    return [
        Finding(
            kind="grid_drift",
            severity=Severity.WARNING,
            page=None,
            message=(
                f"{len(pages)} pages put two rows of columns on two grids (pages "
                f"{_listed(pages)}); page {worst[0]} runs {len(one_row)} columns every "
                f"{one_step:.2f}in from {one_row[0]:.2f}in and {len(other_row)} more every "
                f"{other_step:.2f}in from {other_row[0]:.2f}in, out by {drift:.2f}in at the "
                "worst column. Two rows of the same division have to sit on the same edges, or "
                "the miss grows across the page. Take both rows from one `plot.columns(n)` or "
                "one `Box.split`, and put a group's own padding inside its surface rather than "
                "in the column's origin"
            ),
            detail={
                "pages": pages,
                "worst_page": worst[0],
                "steps": [round(one_step, 3), round(other_step, 3)],
                "rows": [[round(edge, 3) for edge in one_row], [round(edge, 3) for edge in other_row]],
                "drift": round(drift, 3),
            },
        )
    ]


def _near_misses(slide, canvas_w: float) -> list[tuple[float, list[float]]]:
    """The two rows of columns on this page that do not line up, or nothing.

    Which is the defect exactly as a reader meets it: two rows one above the other with
    the same number of columns, and the columns out of step. On the page this was written
    for the panels ran 0.72 / 4.56 / 8.40 and the copy under them 0.84 / 4.80 / 8.76, so
    the miss grew 0.12, 0.24, 0.36 across the row.

    Asked as that shape rather than through a proxy, because two proxies each reported a
    designed template for something it does on purpose. "Adjacent left edges 0.05in to
    0.5in apart" cannot tell two grids from indentation -- a badge at the margin, its
    label a quarter inch in, its copy a quarter past that -- and reported nine of the
    twelve bundled templates. "Two page-wide runs at different steps" then read a row of
    five columns and the three-column run inside it as two grids, because every second
    column is a run of its own at twice the pitch, and still reported four. Same count,
    different row, every column near its opposite number: none of those three is
    something a designed page throws up by coincidence.
    """
    rows = [row for row in _rows(slide, canvas_w) if len(row) >= COLUMNS_TO_COMPARE]
    for index, row in enumerate(rows):
        for other in rows[index + 1 :]:
            if len(other) != len(row):
                continue
            offsets = [abs(here - there) for here, there in zip(row, other)]
            # Every column near its opposite number, which is what makes the two one
            # division drawn twice rather than two unrelated rows, and at least one of
            # them off, which is what makes it a miss rather than one grid.
            if max(offsets) > NEAR_EDGE_IN or max(offsets) <= SAME_EDGE_IN:
                continue
            # And the miss has to grow. Two rows at one pitch with the whole of one moved
            # is an indent, which a designed page does on purpose -- five of the twelve
            # bundled templates have one, at 0.17in to 0.47in. Two pitches is what a
            # reader sees as columns that do not line up, and its signature is the gap
            # widening column by column: 0.12, 0.24, 0.36 on the page this is for.
            if not all(later > earlier + SAME_EDGE_IN for earlier, later in zip(offsets, offsets[1:])):
                continue
            return [(row[1] - row[0], list(row)), (other[1] - other[0], list(other))]
    return []


def _rows(slide, canvas_w: float) -> list[list[float]]:
    """The page's rows, each as the left edges in it, in order.

    A row is what a reader sees at one height, so the grouping is on each box's centre
    rather than on overlap: overlap alone lets one tall box weld into itself every row
    it crosses. Panels count as well as copy, because a row of cards and the copy under
    it are exactly the pair this looks for.
    """
    from raven_ppt.services.measure.geometry import is_filled, page_box, shows_picture

    covers = []
    boxes = []
    for shape in iter_shapes(slide.shapes):
        where = page_box(shape)
        if where is None or where.x1 - where.x0 <= 0:
            continue
        box = (where.x0, where.y0, where.x1, where.y1)
        if is_filled(shape) or shows_picture(shape):
            covers.append(box)
            boxes.append(box)
            continue
        if not getattr(shape, "has_text_frame", False) or not shape.text_frame.text.strip():
            continue
        boxes.append(box)
    # A box inside a surface is padded, not misaligned: that padding is what every card
    # has, and it is not the page's grid.
    outer = [
        box
        for box in boxes
        if not any(cover is not box and _within(box, cover) for cover in covers)
        and box[2] - box[0] < canvas_w * FULL_WIDTH_SHARE
    ]
    grouped: list[list[tuple[float, float, float, float]]] = []
    for box in sorted(outer, key=lambda item: (item[1] + item[3]) / 2):
        centre = (box[1] + box[3]) / 2
        if grouped:
            row = grouped[-1]
            shortest = min(min(item[3] - item[1] for item in row), box[3] - box[1])
            near = min(abs(centre - (item[1] + item[3]) / 2) for item in row)
            if shortest > 0 and near <= shortest * (1.0 - ROW_OVERLAP_SHARE) + ROW_SLACK_IN:
                row.append(box)
                continue
        grouped.append([box])
    rows = []
    for row in grouped:
        edges: list[float] = []
        for edge in sorted(box[0] for box in row):
            if not edges or edge - edges[-1] > SAME_EDGE_IN:
                edges.append(edge)
        rows.append(edges)
    return rows
