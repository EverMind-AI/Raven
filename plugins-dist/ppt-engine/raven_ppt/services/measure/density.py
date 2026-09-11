"""Floors: whether a panel, a body band and a page carry enough to be worth their room.

Density in this engine only ever had a ceiling (design doc D7), and the ceiling turned
out never to have been written -- there is no `MAX_CHARS_PER_PAGE` anywhere, so how
dense a page is has always been settled by measuring the render. What real runs keep
delivering is the other failure: a 4.8in panel holding one line, a body band where
nothing is bigger than the body copy, a content page carrying eighty characters.

Three floors, and each one is a predicate that can be false rather than a noun that is
always present (design doc D20). "Does this page have a card" is answered yes by every
page; "does this card use a third of its height" is answered no by the ones a reader
would call empty. The same distinction is why the anchor below is measured inside the
body band: every page has a title, so a check that counted titles would never fire.

All three are WARNING and none of them refuses a deck. That is D17 taken in advance --
the band gate refused, misjudged three pages, and every patch was another exemption. A
floor is a judgement about how much is enough, so it reports and the author decides,
and the numbers stay revisable as runs land, which is only possible while nothing is
being refused over them.

Every threshold is a module constant, collected into `Floors` so a project can carry
its own set without any predicate here changing. Where each number came from is on the
constant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.bands import band_of
from raven_ppt.services.measure.geometry import (
    EMU_PER_INCH,
    EMU_PER_POINT,
    Rect,
    has_outline,
    has_text,
    is_connector,
    is_filled,
    is_rectangular,
    iter_shapes,
    open_deck,
    page_box,
    page_paragraphs,
    shows_picture,
)
from raven_ppt.services.measure.rendered import CARD_MIN_HEIGHT_PT, CARD_MIN_WIDTH_PT, WORD_IN_CARD_SHARE
from raven_ppt.services.measure.words import by_page
from raven_ppt.services.measure.words import rect as word_rect

_PT_PER_INCH = EMU_PER_INCH / EMU_PER_POINT

if TYPE_CHECKING:
    from raven_ppt.contracts import WordBox
    from raven_ppt.contracts.masters import Bands
    from raven_ppt.services.measure.type_size import Span

# --- container fill -------------------------------------------------------------

# How much of a container's height its content has to occupy. Measured on two real
# decks, 40 containers: a delivered 12-page proposal a reviewer accepted runs 0.18 to
# 0.84 with a median of 0.59, and its one container under 0.35 is the agenda sidebar
# on a structural page. The template it was built from -- 15 pages of untouched
# placeholders -- has four containers at 0.06, 0.06, 0.15 and 0.20, which is the shape
# this is here to catch. The reference figure for this measurement is 0.85 and that is
# a different measurement: a union of y-spans skips the leading between two stacked
# boxes, so a card set solid measures around 0.6 and 0.85 would report every page.
CONTAINER_FILL = 0.35
# The largest single hole inside a container, which is what the eye reads. Total fill
# cannot tell an evenly airy card from one with a gap in the middle: over the 44
# containers of a deck judged sound, the four cards whose last bullet stops an inch
# short of their footnote measure 0.60-0.70in of hole while every other container
# stays under 0.37in, and all of them sit between 0.44 and 0.74 on fill. 0.45in is
# above the widest ordinary leading measured and below the narrowest real hole.
CONTAINER_GAP_IN = 0.45
# A run this tall is a figure or a display line, and the air under one is the
# breathing space a visual anchor is supposed to have -- the reference asks for 40px
# of it. Measured: the 45pt figures in the calibration decks draw runs 0.55-0.62in
# tall, ordinary 14pt body draws 0.17-0.21in.
ANCHOR_INK_IN = 0.35
# And the blank has to be a hole rather than padding. Without this the check reports
# what good decks do on purpose: a one-line row band 0.68in high measures 0.32 because
# the line is centred in it, and the accepted deck's hand-drawn table is seven of them.
# 1.5in is more than two lines of body copy at any size the deck sets -- room something
# was meant to go in. It also sets the smallest container that can ever be reported:
# below 1.5 / (1 - CONTAINER_FILL) = 2.3in tall, no panel can leave that much blank.
CONTAINER_SLACK_IN = 1.5
# A shape counts as sitting in a container when this much of it does. Not "its centre
# is inside": a badge half over the rim is content the container holds, and a caption
# under it is not.
CONTAINED_SHARE = 0.7
# A panel this much of the canvas is the page's ground, not a container on it. Every
# page of both decks measured has exactly one, at 1.00 of the canvas, and its fill is
# the page's own layout rather than anything a reader would call an empty card.
GROUND_SHARE = 0.9

# --- visual anchor --------------------------------------------------------------

# The size at which type stops being copy and starts being an entry point. The deck's
# own body floor is 14pt (`type_size.BODY_FLOOR_PT`) and the accepted deck sets its
# body at 12-18pt, so an anchor is at least half again the floor. On that deck's size
# ladder -- 10 / 12 / 16 / 18 / 22 / 30 / 45 / 72 -- 20pt is the gap between a subhead
# and a heading, and its body band tops out at 22pt or more on ten of twelve pages.
ANCHOR_TYPE_PT = 20.0
# Or a figure large enough to be what the page is. Measured on the same deck: the five
# pages carrying a picture put it at 0.31 to 0.44 of the body band, so a fifth of the
# band sits below every one of them and above any thumbnail.
ANCHOR_FIGURE_SHARE = 0.20
# What counts as one object for that share, when the object is drawn rather than
# placed. Row bands of a hand-drawn table butt against each other: measured on both
# decks, the rows of their table pages are stacked with no gap at all, while a column
# of separate cards leaves 0.16in between them. 0.06in tells those apart.
GRID_GAP_IN = 0.06
# And three bands is a table. Two is a split, and a split anchors nothing.
GRID_ROWS = 3

# --- copy per page --------------------------------------------------------------

# What a page has to say, by what kind of page it is, in characters of copy.
#
# Measured on the delivered proposal, whose eight content pages run 172 to 395
# characters, against the untouched template, whose placeholder pages run 85 to 199.
# The repo's own separate reading agrees on the upper end: `gates/brief.py` records
# content pages across four real decks at 200 to 467 characters, median 250.
#
# A prose page carries the most, because nothing else on it carries anything.
COPY_FLOOR_CONTENT = 150
# A card group trades sentences for labels, so it sits a fifth under the accepted
# deck's thinnest card page (172). It is the floor that catches the template's four
# placeholder card pages, at 85, 108, 115 and 116.
COPY_FLOOR_CARDS = 120
# A data page's figure carries the argument, so what is left is the claim, the reading
# of the figure and the source line -- about ninety characters. The accepted deck's
# figure pages run 182 to 236, so this fires only on a chart nobody read out.
COPY_FLOOR_DATA = 90
# All three are counts of characters on Chinese decks. A latin deck spends more
# characters saying the same thing, so these floors under-report there rather than
# over-report, which is the direction a warning should err in.

# How many card-like panels make a page a card group rather than a page with a panel
# on it. Three is where the accepted deck's card rows start; two panels is a split.
CARD_GROUP = 3

# The three kinds a page can be judged as. The fourth -- furniture -- is the one
# with no floor, so it never reaches a finding and has no name here.
CONTENT, CARDS, DATA = "content", "cards", "data"

# Below its floor and holding more emptied frames than filled ones, a page is not thin
# -- it is unwritten, and that is a different verdict. Eight pages of one live deck went
# out that way, each holding the two lines one call had written over thirteen to
# twenty-six boxes with nothing in them: the route those pages took emptied every text
# the call did not name, and the helper the author then wrote searched the emptied page
# for the words that had just been erased. That route is gone, so the commonest road
# to this page is closed -- the check stays because a program can still write "" into
# every frame it reaches, and a page that arrives blank is worth refusing however it got
# there.
#
# The frame count is what separates the two verdicts, and it is a question rather than
# a threshold. Measured over the ten bundled templates (211 example pages), eleven
# delivered decks and that deck: every page the floor reported on a delivered deck or
# a template kept at least three frames of copy, and the eight kept one or two against
# thirteen or more emptied. Below its floor with the copy it does have outnumbered by
# the boxes it does not fill, a page was cloned to say something the words never
# reached.
EMPTIED = "emptied_page"

# What the plan calls a page when the page is the template's own furniture. The same
# vocabulary the outline gates read, because a page is furniture in one place or in
# neither.
_FURNITURE_MARKERS = (
    "封面",
    "目录",
    "章节",
    "分隔",
    "收尾",
    "封底",
    "结束",
    "尾页",
    "谢谢",
    "cover",
    "agenda",
    "contents",
    "section divider",
    "closing",
)
# And what it calls a page whose subject is a figure. Read as a hint rather than as a
# fact: the built page is asked the same question below, and either answer is enough.
_DATA_MARKERS = ("figure", "chart", "graph", "table", "图", "表", "数据", "曲线")


@dataclass(frozen=True)
class Floors:
    """Every number the three checks below compare against, in one place.

    A dataclass rather than bare reads of the constants so a project can carry its
    own floors -- read one out of `project.json`, pass it in -- without a predicate
    here learning where thresholds come from.
    """

    container_fill: float = CONTAINER_FILL
    container_gap_in: float = CONTAINER_GAP_IN
    container_slack_in: float = CONTAINER_SLACK_IN
    anchor_type_pt: float = ANCHOR_TYPE_PT
    anchor_figure_share: float = ANCHOR_FIGURE_SHARE
    copy_content: int = COPY_FLOOR_CONTENT
    copy_cards: int = COPY_FLOOR_CARDS
    copy_data: int = COPY_FLOOR_DATA

    def copy_floor(self, kind: str) -> int:
        return {CONTENT: self.copy_content, CARDS: self.copy_cards, DATA: self.copy_data}[kind]


DEFAULT_FLOORS = Floors()


def sparse_containers(
    pptx_path: Path,
    words: Sequence[WordBox] | None = None,
    structural: Sequence[int] = (),
    floors: Floors = DEFAULT_FLOORS,
) -> list[Finding]:
    """Cards and panels holding far less than the room they took.

    Where the copy landed is a fact about the render, so hand `words` in and the
    copy's extent is read off it. Both readings are here because they disagree in one
    direction each, and the direction is what decides which is safe to fall back to:
    over the 40 containers of the two decks this was calibrated on -- decks whose text
    boxes are sized to their copy -- the rendered fill came out 0.00 to 0.10 *lower*
    than the declared one, because a word's box is tighter than the frame it was set
    in, so one floor serves both. A box the author did not size to its copy breaks
    that: a 3.4in frame holding a line the renderer never wrapped reads as full off
    the file and as one line off the render. So without a render this under-reports,
    which is the direction a warning should fail in, and `excessive_whitespace` reads
    that page from the render anyway.

    Related to but not the same reading as `rendered.excessive_whitespace`'s
    `empty_panel`, which asks whether a panel's copy stops short of its bottom edge.
    This asks how much of the panel is used at all, counts the pictures and tables in
    it alongside the copy, and reports a hole in the middle the same as one at the
    foot.
    """
    findings: list[Finding] = []
    presentation = open_deck(pptx_path)
    canvas = (presentation.slide_width or 0) / EMU_PER_INCH * (presentation.slide_height or 0) / EMU_PER_INCH
    if canvas <= 0:
        return findings
    painted = by_page(words) if words else {}
    skipped = set(structural)
    for number, slide in enumerate(presentation.slides, start=1):
        if number in skipped:
            continue
        placed = [(shape, page_box(shape)) for shape in iter_shapes(slide.shapes)]
        placed = [(shape, box) for shape, box in placed if box is not None and box.area > 0]
        on_page = [word_rect(word) for word in painted.get(number, ())]
        for shape, box in placed:
            if not _is_container(shape, box, canvas, fillable=True):
                continue
            inside = [(other, inner) for other, inner in placed if other is not shape and _sits_in(box, inner)]
            if not any(_carries_copy(other) for other, _ in inside):
                continue
            held = [inner for other, inner in inside if _is_figure(other) or _is_drawing(other, inner, box)]
            held += _copy_extent(box, inside, on_page)
            used = _covered(box, held) / box.height
            slack = box.height * (1.0 - used)
            hole = _hole(box, held)
            thin = used < floors.container_fill and slack >= floors.container_slack_in
            holed = hole >= floors.container_gap_in
            if not thin and not holed:
                continue
            reason = (
                f"leaves a {hole:.2f}in hole between the things it holds"
                if holed
                else f"gives {used:.0%} of its height to content and leaves {slack:.2f}in of it empty"
            )
            findings.append(
                Finding(
                    kind="sparse_container",
                    severity=Severity.WARNING,
                    page=number,
                    message=(
                        f"a {box.width:.2f}x{box.height:.2f}in panel {reason}, "
                        f"so it reads as a container nobody filled. Cut the panel "
                        f"to what it holds -- card_size() levels a row of cards to their tallest without padding the "
                        f"rest out, and page().holding(*heights) gives the body the run's own height -- or give it "
                        f"the point, figure or number it was drawn for"
                    ),
                    detail={
                        "page": number,
                        "used_share": round(used, 2),
                        "slack_in": round(slack, 2),
                        "panel_in": [round(box.x0, 2), round(box.y0, 2), round(box.width, 2), round(box.height, 2)],
                        "hole_in": round(hole, 2),
                        "floor": floors.container_fill,
                        "gap_floor_in": floors.container_gap_in,
                    },
                )
            )
    return findings


def unanchored_pages(
    pptx_path: Path,
    spans: Sequence[Span] | None,
    bands: Bands | None,
    structural: Sequence[int] = (),
    floors: Floors = DEFAULT_FLOORS,
) -> list[Finding]:
    """Pages whose body offers the eye nowhere to land.

    Inside the body band and nowhere else. A page's title is always the biggest type
    on it, so a check that looked at the whole page would find an anchor every time
    and never be able to say no -- the band is what makes this a predicate rather
    than a census (design doc D20). No bands, no coordinate system, no finding.

    Type sizes come off the render, never off the file: a 22pt heading in a box the
    renderer shrank to fit is set at 13pt on the page, and the reader's eye answers to
    what was painted (design doc D10). No spans means no signal, which is not the same
    as a clean deck, so that is silence too.
    """
    if bands is None or spans is None:
        return []
    body_top, body_bottom = bands.body
    body_area = bands.body_area
    if body_area <= 0:
        return []
    canvas = bands.canvas_w * bands.canvas_h
    skipped = set(structural)
    in_body: dict[int, float] = {}
    for span in spans:
        if band_of(bands, span.y0 / _PT_PER_INCH, span.y1 / _PT_PER_INCH) != "body":
            continue
        in_body[span.page] = max(in_body.get(span.page, 0.0), span.size_pt)
    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        if number in skipped:
            continue
        biggest_type = in_body.get(number, 0.0)
        placed = [(shape, page_box(shape)) for shape in iter_shapes(slide.shapes)]
        placed = [(shape, box) for shape, box in placed if box is not None and box.area > 0]
        objects = [box for shape, box in placed if _is_figure(shape)]
        objects += _grids([box for shape, box in placed if is_filled(shape) and box.area / canvas < GROUND_SHARE])
        figure = max((_clipped_area(box, body_top, body_bottom) for box in objects), default=0.0)
        figure_share = figure / body_area
        if biggest_type >= floors.anchor_type_pt or figure_share >= floors.anchor_figure_share:
            continue
        findings.append(
            Finding(
                kind="no_anchor",
                severity=Severity.WARNING,
                page=number,
                message=(
                    f"nothing in this page's body stands out from its copy: the largest type between the title and "
                    f"the footer is {biggest_type:.0f}pt against the {floors.anchor_type_pt:.0f}pt that reads as an "
                    f"entry point, and the biggest single thing down there -- figure, chart or table -- covers "
                    f"{figure_share:.0%} of the body against {floors.anchor_figure_share:.0%}. Give the page one "
                    f"thing to look at first: set the number or the claim that carries it two steps up the size "
                    f"ladder, or place the figure across the body and let the copy caption it"
                ),
                detail={
                    "page": number,
                    "largest_body_pt": round(biggest_type, 1),
                    "figure_share": round(figure_share, 2),
                    "type_floor_pt": floors.anchor_type_pt,
                    "figure_floor": floors.anchor_figure_share,
                },
            )
        )
    return findings


# How many blocks of copy a body can hold before a reader needs something to group
# them. Two read as a pair from position alone -- a claim and its qualifier, a before
# and an after -- and three stop doing that. Measured over the 105 content pages of the
# eight bundled templates: 104 divide the body with at least one device, the median page
# uses seven, and the pages that use exactly one use a picture. The single page that
# uses none is that set's plainest, a column of headings over a column of paragraphs.
COPY_BLOCKS_BEFORE_DIVIDING = 3


def undivided_bodies(
    pptx_path: Path,
    bands: Bands | None,
    structural: Sequence[int] = (),
) -> list[Finding]:
    """Pages laying several blocks of copy in the body with nothing to separate them.

    A different question from `unanchored_pages`, which asks whether anything on the
    page is big enough for the eye to land on: one display number answers that and
    leaves four paragraphs sitting side by side with nothing between them. This asks
    whether the page's divisions are drawn or only implied by where the boxes were put.

    A device is anything a reader sees between two blocks of copy -- a panel behind
    one of them, a rule, an outlined shape, a picture, a table, a chart. One is
    enough, and the templates say so: the pages that carry a single device carry a
    picture, and a picture beside copy is a division.

    Inside the body band and nowhere else, for the reason `unanchored_pages` gives: a
    title and a footer are furniture, and counting them would find a device on every
    page. No bands, no coordinate system, no finding (design doc D20).
    """
    if bands is None:
        return []
    body_top, body_bottom = bands.body
    canvas = bands.canvas_w * bands.canvas_h
    if canvas <= 0 or body_bottom <= body_top:
        return []
    skipped = set(structural)
    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        if number in skipped:
            continue
        blocks = 0
        devices = 0
        for shape in iter_shapes(slide.shapes):
            box = page_box(shape)
            if box is None:
                continue
            # A divider has extent in one direction only: the column rule on a bundled
            # template is 0.00in wide and 2.20in tall, so an area test drops it.
            if box.x1 - box.x0 <= 0 and box.y1 - box.y0 <= 0:
                continue
            middle = (box.y0 + box.y1) / 2
            if not body_top <= middle <= body_bottom:
                continue
            if _is_figure(shape) or is_connector(shape):
                devices += 1
                continue
            if box.area > 0 and has_text(shape):
                blocks += 1
            if box.area / canvas >= GROUND_SHARE:
                continue
            if is_filled(shape) or has_outline(shape):
                devices += 1
        if devices or blocks < COPY_BLOCKS_BEFORE_DIVIDING:
            continue
        findings.append(
            Finding(
                kind="undivided_body",
                severity=Severity.WARNING,
                page=number,
                message=(
                    f"this page sets {blocks} blocks of copy between the title and the footer and draws "
                    "nothing between them: no panel, no rule, no outline, no figure. Their grouping is "
                    "implied by where the boxes were put, which is a grid a reader has to infer. Draw the "
                    "division: `card_group(slide, frame.body, T, items)` for blocks that are the same kind "
                    'of thing, `plane(slide, region, T, tint="surface")` behind the one that answers the '
                    'page, or `rule(slide, box, T, colour=T["grid"])` between two halves that are not '
                    "cards. One device is enough -- a figure beside the copy counts"
                ),
                detail={"page": number, "copy_blocks": blocks, "floor": COPY_BLOCKS_BEFORE_DIVIDING},
            )
        )
    return findings


def thin_copy(
    pptx_path: Path,
    outline: Any | None,
    prototypes: Path | None = None,
    floors: Floors = DEFAULT_FLOORS,
) -> list[Finding]:
    """Pages carrying less copy than their kind of page needs to say anything.

    Two verdicts, one reading. A page merely under its floor is `thin_copy` and rides
    along with the deck; a page under its floor whose emptied frames outnumber the ones
    carrying anything is `emptied_page` and refuses publication, because it was cloned
    to say something and the words never arrived. See `EMPTIED` for why the frame count
    is what separates them.

    The floor differs by what the page is for, so the plan has to be there: a data
    page whose chart carries the argument and a prose page look the same to a
    character count, and one floor for both is either noise on the first or silence
    on the second. No outline, no page kinds, no findings.

    Furniture is not judged at all. A cover, a contents list, a section divider and a
    closing page are short because that is what they are, and `prototypes` is what
    says which pages those are -- the template's own roles, read from the page the
    plan bound (`services.template.menu`), with the plan's own words as the fallback
    when no template is bound.
    """
    plans = {int(getattr(page, "page", 0)): page for page in getattr(outline, "pages", ()) or ()}
    if not plans:
        return []
    furniture = _furniture(plans, prototypes)
    presentation = open_deck(pptx_path)
    canvas = (presentation.slide_width or 0) / EMU_PER_INCH * (presentation.slide_height or 0) / EMU_PER_INCH
    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        plan = plans.get(number)
        if plan is None or number in furniture:
            continue
        kind = _page_kind(plan, slide, canvas)
        floor = floors.copy_floor(kind)
        chars = sum(len("".join(text.split())) for text in page_paragraphs(slide))
        if chars >= floor:
            continue
        emptied, filled = _frames_emptied_and_filled(slide)
        if emptied > filled:
            findings.append(
                Finding(
                    kind=EMPTIED,
                    severity=Severity.BLOCKING,
                    page=number,
                    message=(
                        f"this page holds {emptied} text frames with nothing in them against {filled} carrying "
                        f"copy, and says {chars} characters where its kind of page needs {floor}. A frame written "
                        f'with "" is emptied, not removed: the box stays on the page and shows nothing. Write '
                        f"every line of this page with `replace_text(slide, 'the words there now', 'yours'), and "
                        f"take what it does not use off it -- `remove_unit` for a spare slot, `drop_shape` "
                        f"for a single shape"
                    ),
                    detail={
                        "page": number,
                        "chars": chars,
                        "floor": floor,
                        "page_kind": kind,
                        "frames_emptied": emptied,
                        "frames_filled": filled,
                    },
                )
            )
            continue
        findings.append(
            Finding(
                kind="thin_copy",
                severity=Severity.WARNING,
                page=number,
                message=(
                    f"this {_kind_name(kind)} carries {chars} characters against the {floor} its kind of page needs "
                    f"to say anything. Say more on it -- the points the plan left in `says`, the reading of what the "
                    f"page shows, the number behind the claim -- or fold it into the page beside it and take a page "
                    f"out of the outline"
                ),
                detail={"page": number, "chars": chars, "floor": floor, "page_kind": kind},
            )
        )
    return findings


def _frames_emptied_and_filled(slide: Any) -> tuple[int, int]:
    """How many of this page's text frames stand empty, and how much of it speaks.

    A table's cells and a chart's own strings count as copy the page carries: a page
    whose argument is a table is not an unwritten page, and counting only text frames
    would call it one.
    """
    emptied = filled = 0
    for shape in iter_shapes(slide.shapes):
        if getattr(shape, "has_text_frame", False):
            if shape.text_frame.text.strip():
                filled += 1
            else:
                emptied += 1
            continue
        if getattr(shape, "has_table", False) or getattr(shape, "has_chart", False):
            filled += 1
    return emptied, filled


def _kind_name(kind: str) -> str:
    return {CONTENT: "content page", CARDS: "card page", DATA: "data page"}[kind]


def _is_container(shape: Any, box: Rect, canvas: float, *, fillable: bool = False) -> bool:
    """A filled shape drawn to hold something, rather than a ground or a rule.

    Its own copy disqualifies it: a shape that holds its text is one thing, and how
    much of itself it uses is a question about the text frame, not about a container.

    `fillable` additionally drops what is not a rectangle -- see `is_rectangular` -- and
    belongs to the reading that asks how full a container is, not to the one that counts
    how many a page has. Three rounded cards make a page of cards; drawn as three
    ellipses they are the same page, and dropping them moved it to `content` and its
    copy floor with it.
    """
    if not is_filled(shape) or _carries_copy(shape):
        return False
    if fillable and not is_rectangular(shape):
        return False
    if box.area / canvas >= GROUND_SHARE:
        return False
    return box.width * _PT_PER_INCH >= CARD_MIN_WIDTH_PT and box.height * _PT_PER_INCH >= CARD_MIN_HEIGHT_PT


def _sits_in(container: Rect, box: Rect) -> bool:
    return box.area > 0 and container.overlap(box) / box.area >= CONTAINED_SHARE


def _carries_copy(shape: Any) -> bool:
    frame = getattr(shape, "text_frame", None)
    return bool(frame is not None and frame.text.strip())


def _copy_extent(container: Rect, inside: Sequence[tuple[Any, Rect]], painted: Sequence[Rect]) -> list[Rect]:
    """Where the container's copy sits: off the render when there is one.

    A page with no rendered words at all is a page nothing could be read from -- a
    render that failed, a page of pictures -- and reading zero copy out of it would
    report every container on it. That is no signal, so the file answers instead.
    """
    if not painted:
        return [box for shape, box in inside if _carries_copy(shape)]
    box_pt = Rect(*(value * _PT_PER_INCH for value in (container.x0, container.y0, container.x1, container.y1)))
    held = [word for word in painted if box_pt.overlap(word) / max(word.area, 1e-9) >= WORD_IN_CARD_SHARE]
    return [Rect(*(value / _PT_PER_INCH for value in (word.x0, word.y0, word.x1, word.y1))) for word in held]


def _is_figure(shape: Any) -> bool:
    return shows_picture(shape) or getattr(shape, "has_table", False) or getattr(shape, "has_chart", False)


# How much of a container something inside it may cover and still count as something
# it holds. Above this the shape is the container's own ground, or a copy of it drawn
# on top, and counting it would report every container as full.
NESTED_SHARE = 0.9


def _is_drawing(shape: Any, box: Rect, container: Rect) -> bool:
    """Something drawn inside a container -- an icon, a badge, a mark -- rather than copy.

    A card's icon is a group of freeform paths, not a picture, so `_is_figure` does not
    see one. On one bundled template's agenda cards the icon is 1.12in of a 3.65in card
    and every one of the four was reported as giving 20% of its height to content: the
    icon a reader spends most of the card looking at was counted as empty.
    """
    if not is_filled(shape) or _carries_copy(shape):
        return False
    return container.area <= 0 or box.area <= container.area * NESTED_SHARE


def _hole(container: Rect, held: Sequence[Rect]) -> float:
    """The widest run of empty height between two things the container holds.

    Only between: the air above the first and below the last is padding, and a card
    that pins its footnote to its floor leaves some there by design. A gap either of
    whose neighbours is tall enough to be a figure or a display line is not counted --
    that air is the breathing space an anchor is meant to have, and counting it
    reported the one page in the calibration deck whose big figure was correct.
    """
    spans: list[list[float]] = []
    for box in sorted(held, key=lambda item: item.y0):
        start, end = max(box.y0, container.y0), min(box.y1, container.y1)
        if end <= start:
            continue
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    widest = 0.0
    for before, after in zip(spans, spans[1:]):
        if before[1] - before[0] >= ANCHOR_INK_IN or after[1] - after[0] >= ANCHOR_INK_IN:
            continue
        widest = max(widest, after[0] - before[1])
    return widest


def _covered(container: Rect, held: Sequence[Rect]) -> float:
    """How much of the container's height its content occupies, holes not counted."""
    spans = [(max(box.y0, container.y0), min(box.y1, container.y1)) for box in held]
    merged: list[list[float]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def _clipped_area(box: Rect, top: float, bottom: float) -> float:
    height = max(min(box.y1, bottom) - max(box.y0, top), 0.0)
    return height * max(box.width, 0.0)


def _grids(panels: Sequence[Rect]) -> list[Rect]:
    """Stacks of aligned bands, each returned as the one object a reader sees.

    A drawn table is what this is for. `native_table` reads a GraphicFrame and a deck
    can draw the same table as a column of filled row bands with the cells set over
    them -- both decks calibrated on do exactly that, and their table page was the one
    the anchor check reported: no type above 16pt, no picture, and a table filling
    three quarters of the body that nothing was counting. The identical page built with
    `ppt_layout.table()` would not have been reported, which is the tell that the
    criterion and not the page was wrong.

    A lone band is not one of these, and deliberately: whether a single panel anchors
    the page depends on what is inside it, which is `sparse_containers`' question, not
    this one. A stack of them is a structure whose reading does not.
    """
    by_column: dict[tuple[float, float], list[Rect]] = {}
    for box in panels:
        by_column.setdefault((round(box.x0, 1), round(box.x1, 1)), []).append(box)
    found: list[Rect] = []
    for column in by_column.values():
        run: list[Rect] = []
        for box in sorted(column, key=lambda item: item.y0):
            if run and box.y0 - run[-1].y1 > GRID_GAP_IN:
                found.extend(_grid_bounds(run))
                run = []
            run.append(box)
        found.extend(_grid_bounds(run))
    return found


def _grid_bounds(run: Sequence[Rect]) -> list[Rect]:
    if len(run) < GRID_ROWS:
        return []
    return [Rect(run[0].x0, run[0].y0, run[0].x1, run[-1].y1)]


def _furniture(plans: dict[int, Any], prototypes: Path | None) -> set[int]:
    """Which planned pages are the template's own furniture."""
    roles: dict[int, str] = {}
    if prototypes is not None and Path(prototypes).is_file():
        from raven_ppt.services.template.menu import menu

        roles = {entry.number: entry.role for entry in menu(Path(prototypes))}
    found = set()
    for number, plan in plans.items():
        prototype = getattr(plan, "prototype", None)
        if prototype is not None and roles.get(int(prototype), ""):
            found.add(number)
            continue
        said = " ".join(
            str(getattr(plan, field, "") or "") for field in ("carries", "section", "layout", "claim")
        ).casefold()
        if any(marker in said for marker in _FURNITURE_MARKERS):
            found.add(number)
    return found


def _page_kind(plan: Any, slide: Any, canvas: float) -> str:
    """Which floor this page answers to.

    The plan is asked first because it says what the page is *for*, and the built page
    second because a plan that named nothing still produced something. Either saying
    "figure" is enough: a page that shows one is a data page whichever of the two
    knows it.
    """
    said = " ".join(str(getattr(plan, field, "") or "") for field in ("carries", "layout")).casefold()
    if getattr(plan, "figures", ()) or any(marker in said for marker in _DATA_MARKERS):
        return DATA
    shapes = list(iter_shapes(slide.shapes))
    if any(_is_figure(shape) for shape in shapes):
        return DATA
    panels = 0
    for shape in shapes:
        box = page_box(shape)
        if box is not None and canvas > 0 and _is_container(shape, box, canvas):
            panels += 1
    return CARDS if panels >= CARD_GROUP else CONTENT
