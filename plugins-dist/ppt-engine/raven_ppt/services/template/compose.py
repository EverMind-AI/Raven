"""Building a page out of one the template already drew.

A template's visual elements live on its example slides and not on its layouts, so a
page built with `add_slide(layout)` gets the template's placeholders and almost none
of its design: covers come out right, because covers are decorated on the layout, and
content pages leave the template entirely.

The operations here are the ones that let an author work the way the design
actually supports: take the example page closest to what this page has to say,
replace its words and its pictures, clear a space in it for something of your own,
delete what is left over. Each is something python-pptx has no API for, and each
has a way of going quietly wrong that a reader of the resulting file would not
connect to its cause:

Cloning copies shape XML that refers to relationships by id. The new slide has no
such relationships, so every picture on the copy resolves to nothing -- verified:
`no relationship with key 'rId4'`. The references have to be re-pointed as the copy
is made.

Replacing a picture by deleting the frame and adding a new one loses the crop, the
outline, the shadow and the z-order the template chose. Swapping the image behind
the existing frame keeps all of it.

Replacing text by assigning to `.text` discards the run properties, so a heading
comes back at the body size in the body colour. Writing into the first run and
clearing the rest keeps what the template set.

Drawing into a cloned page means clearing a space in it first, and for a while
there was no way to say that here. A live program wrote its own two sweeps
instead -- one keyed on whether a shape's text was Chinese, the other on whether
the shape was bigger than 1.0x0.7in -- and against the page it ran on those kept
every arrow, every number label and, because a rule has one zero dimension, every
connector; two charts came out drawn on top of them. `clear_region` is that
operation, and it names what it took.
"""

# Where a template keeps its design, measured on twenty real templates: 85% of the
# visual elements are on the example slides and not on the layouts -- 29 per template
# against 5.

from __future__ import annotations

import atexit
import copy
import io
import math
import re
import warnings
from difflib import SequenceMatcher
from pathlib import Path
from typing import NamedTuple

from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

# How far a picture's proportions may differ from its frame's before fitting it stops
# being a fit. A template's portrait photo slot is around 0.6 wide-to-tall and a paper's
# architecture figure is around 2.4: contained inside that slot the figure becomes a
# strip a quarter of the frame's height with empty space above and below it, which is
# what a live page did to Figure 2. Past this ratio the page needs rearranging, and only
# the author can decide how.
FIT_RATIO_LIMIT = 2.0

# Attributes that name a relationship inside copied shape XML.
_REL_ATTRS = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed",
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}link",
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
)


def clone_page(presentation, prototype, *rest):
    """A new slide at the end of `presentation`, holding a copy of `prototype`.

    The prototype may come from the same presentation or another one; either way
    every relationship its shapes refer to is carried across and re-pointed, which
    is the part that breaks when this is written by hand.

    **The copy arrives carrying the prototype's words.** Replace them with
    `replace_text`, one call per line the page says, and do not add a text
    box over the top: the prototype's own placeholder copy stays underneath yours, so
    the page reads "单击此处添加长一点的副标题" under your own text.
    `placeholder_copy` and `template_underlay` refuse that at the gate.
    """
    if rest:
        # Python's own "takes 2 positional arguments but 3 were given" says nothing
        # about the third, and the third is always the same mistake: the page number,
        # which belongs to `prototype`. A live build spent a round finding that and
        # then corrected six call sites at once.
        given = ", ".join(repr(one) for one in rest)
        raise TypeError(
            f"clone_page(presentation, prototype) takes no page number; got {given} as well. "
            f"The page belongs to the prototype: clone_page(presentation, prototype(source, {rest[0]!r}))"
        )
    slide = presentation.slides.add_slide(_layout_in(presentation, prototype))
    for existing in list(slide.shapes):
        existing._element.getparent().remove(existing._element)

    tree = slide._element.spTree
    for element in prototype._element.spTree:
        # The group's own properties belong to the tree that already exists.
        if element.tag.endswith(("}nvGrpSpPr", "}grpSpPr")):
            continue
        copied = copy.deepcopy(element)
        _repoint(copied, prototype.part, slide.part)
        tree.append(copied)
    return slide


def bundled(name: str):
    """A bundled template by its file name without `.pptx`, example pages intact.

    For borrowing a page the bound template has no equivalent of: `prototype(bundled(
    "gold_panel_year_end_summary"), 13)` is the S-curve of pills, whichever template the
    deck is built in. The clone lands on the deck's own layout of the same name and its
    theme colours resolve to the deck's, so what comes across is the arrangement and not
    the source's look -- measured on four such clones rendered beside their sources.
    Record it in the plan as `borrowed` beside `prototype`, so the checks that read the
    plan know which file the page came from.
    """
    import os

    folder = os.environ.get("PPT_BUNDLED_TEMPLATES", "")
    if not folder or not Path(folder).is_dir():
        raise RuntimeError(
            "PPT_BUNDLED_TEMPLATES is not set, so no bundled template can be opened here; "
            "this program is meant to run under ppt_build, which sets it"
        )
    stem = str(name or "").strip().removesuffix(".pptx")
    path = Path(folder) / f"{stem}.pptx"
    if not path.is_file():
        shipped = ", ".join(sorted(p.stem for p in Path(folder).glob("*.pptx")))
        raise FileNotFoundError(f"no bundled template is called {stem!r}; the ones that ship are {shipped}")
    from pptx import Presentation

    return Presentation(str(path))


def prototype(template, number: int):
    """The template's page `number`, counting from 1 the way the reference counts.

    Named for the outline field that names the same thing (`prototype: 8`) rather than
    `page`, which `ppt_layout` already exports for the regions of a page an author is
    drawing. One skill cannot ask for `from ppt_layout import page` and
    `from ppt_template import page` on two lines of the same program.

    `template.slides[7]` is page 8 and every reference, menu line and outline field
    calls it 8, so every call site does the arithmetic and one of them gets it wrong.
    One did: a deck named prototype 8 for two of its pages, cloned `slides[6]`, and
    was built on page 7 -- which the prototype check then refused, correctly and
    unhelpfully, on two otherwise finished pages.
    """
    slides = list(template.slides)
    if not 1 <= number <= len(slides):
        raise IndexError(f"this template ships {len(slides)} pages, so there is no page {number}")
    return slides[number - 1]


# The name this had for one afternoon, kept working. A program written against the
# older reference imports it, and an ImportError in the middle of a build costs a round
# to learn a rename that changes nothing about what the function does.
page = prototype


def shape_at(slide, number: int):
    """The slide's shape `number`, in the numbering the reference prints.

    One numbering, two spellings of it: `# [5]` above a shape in the reference and
    `shape_at(slide, 5)` on the cloned page mean the same shape -- counting from 1 over
    every shape on the page, groups walked into.

    It exists because a template page is a starting point rather than a form.
    `clone_page` returns the slide and the next thing an author wants is usually an
    adjustment to it -- move the frame a landscape figure went into, close the hole two
    deleted units left, take a panel out from over a picture -- and each of those needs
    a handle on one shape.
    """
    every = list(_all_shapes(slide.shapes))
    if not 1 <= number <= len(every):
        raise IndexError(
            f"this page holds {len(every)} shapes, so there is no shape {number}. It holds: "
            + "; ".join(f"[{index}] {_describe(shape)}" for index, shape in enumerate(every, start=1))
        )
    return every[number - 1]


def page_position(shape) -> tuple[float, float]:
    """Where `shape` sits on the page, in inches, with its groups resolved.

    A shape inside a group states its position in the *group's* coordinate space, and
    a group states an offset and an extent against a child offset and child extent it
    may scale by. So `shape.left` is not where the shape is, and comparing it against
    a number read off the template's own render finds nothing.

    Four authors wrote a walker for this in their own build scripts; one worked the
    transform out and three compared `shape.left` directly, which is four of their
    twenty build failures -- `no shape near (1.56, 2.47)` against a shape that was
    exactly there on the page.
    """
    left = top = 0.0
    scale_x = scale_y = 1.0
    for parent, child in _group_chain(shape):
        offset, extent = parent
        child_offset, child_extent = child
        step_x = extent[0] / child_extent[0] if child_extent[0] else 1.0
        step_y = extent[1] / child_extent[1] if child_extent[1] else 1.0
        left += (offset[0] - child_offset[0] * step_x) * scale_x
        top += (offset[1] - child_offset[1] * step_y) * scale_y
        scale_x *= step_x
        scale_y *= step_y
    return (
        left + (shape.left or 0) / EMU_PER_INCH * scale_x,
        top + (shape.top or 0) / EMU_PER_INCH * scale_y,
    )


class PageBox(NamedTuple):
    """Where a shape is drawn, as two corners in page inches.

    Two corners and not a corner-and-a-size, so it is the same reading as
    `ppt_layout.Box` and drops straight into a call that wants a region:
    `clear_region(slide, page_box(shape_at(slide, 3)))` clears exactly where that
    shape was. The engine's own `services/measure/geometry.page_box` answers in the
    same shape, and one page box meaning two different things in one codebase is
    the mistake `Box` was given two named constructors to prevent.
    """

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    # The docstring above promises this reads the same as `ppt_layout.Box`, and that
    # box answers to `.x` and `.y` as well as `.w` and `.h`. Without these two a
    # program that reads a corner off one box and hands it to another gets half its
    # names back and an AttributeError for the rest: a live run's helper did
    # `round(b.y, 3), round(b.x, 3)` once, in one place, and every page of a
    # twenty-page deck failed to draw. `Box` was given the same pair for the same
    # reason; the promise was the part that did not travel.
    @property
    def x(self) -> float:
        return self.x0

    @property
    def y(self) -> float:
        return self.y0


def page_box(shape) -> PageBox:
    """Where `shape` is drawn and how big it is drawn, in page inches.

    `page_position` with the extent as well, because a group scales what is inside
    it: on a real template page a chart declared 6.80x2.64in is drawn 6.31x4.01in by
    the group holding it. A rule written against the declared size is out by half the
    height, which is how a region test passes over the thing it was aimed at -- and
    two hand-written sweeps in one live program tested `shape.width` directly.
    """
    left = top = 0.0
    scale_x = scale_y = 1.0
    for parent, child in _group_chain(shape):
        offset, extent = parent
        child_offset, child_extent = child
        step_x = extent[0] / child_extent[0] if child_extent[0] else 1.0
        step_y = extent[1] / child_extent[1] if child_extent[1] else 1.0
        left += (offset[0] - child_offset[0] * step_x) * scale_x
        top += (offset[1] - child_offset[1] * step_y) * scale_y
        scale_x *= step_x
        scale_y *= step_y
    x0 = left + (shape.left or 0) / EMU_PER_INCH * scale_x
    y0 = top + (shape.top or 0) / EMU_PER_INCH * scale_y
    return PageBox(
        x0,
        y0,
        x0 + (shape.width or 0) / EMU_PER_INCH * scale_x,
        y0 + (shape.height or 0) / EMU_PER_INCH * scale_y,
    )


# How much of the *box* has to lie in a shape before that shape is what the box
# sits in rather than something in it. A template draws a chart inside a card, and
# the card holds the page's arrangement: on the same five pages the card scores 0.97
# to 1.00 of the box while covering 0.42 to 0.69 of itself, so the two readings
# together separate the card from the chart, which covers 1.00 of both.
THE_BOX_SITS_IN_IT = 0.9


def shapes_in(container, box, *, share: float = 0.5, with_text: bool = False):
    """Every shape drawn inside `box`, groups walked into, in reading order.

    The plural of `shape_near`, and the finder the vocabulary was missing. `shape_near`
    answers "the shape at this point" and `shape_saying` answers "the shape with these
    words"; nothing answered "the shapes in this space", so an author that had to clear
    room for a chart wrote its own sweep -- and a hand-written sweep tests what its
    author thought to test. One live program keyed one sweep on whether the text was
    Chinese and the other on whether the shape was bigger than 1.0x0.7in, and against
    the page it ran on that kept every arrow (each 1.27x0.68in, under the height bar),
    every number label ("01".."05", not Chinese) and, because a horizontal rule is
    0.00in tall and a vertical one 0.00in wide, every connector. All of it came out
    on top of the charts it had cleared room for.

    `box` is a box that says which reading it is: a `ppt_layout.Box` -- `Box.corners(x0,
    y0, x1, y1)` or `Box.at(x, y, w=, h=)` -- or the one `page_box(shape)` hands back.
    Four bare numbers are refused, because the `(left, top, width, height)` a template
    reference prints and the two corners a `Box` holds are the same four numbers and
    nothing in them says which was meant.

    `share` is how much of a shape has to lie in the box before the box is about that
    shape. Half, because furniture straddles an edge, and half is measured rather than
    picked: over the region a chart occupies on five template pages and the body region
    a live deck drew into, what is inside the box scores 0.58 to 1.00 of itself and what
    belongs to the rest of the page scores 0.05 to 0.42, and no page in the set puts
    anything between 0.42 and 0.58.

    A shape the box sits inside is not in the box: that is the card a template draws
    its chart in, and it carries the arrangement the page was cloned for. It is left
    where it is, and `clear_region` says that it did so.

    `share=0` asks the other question -- everything that touches the box at all -- which
    is what to ask of a band you mean to write in rather than empty.
    """
    region = _as_region(box)
    found = []
    for shape in _all_shapes(container.shapes):
        if with_text and not (getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip()):
            continue
        if _inside(shape, region, share):
            found.append(shape)
    return found


def _inside(shape, region, share: float) -> bool:
    """Whether `shape` is drawn in `region` rather than merely near or around it."""
    try:
        drawn = page_box(shape)
    except ValueError:
        # Inside a flipped or rotated group, so it has no position to compare
        # against -- see `_group_chain`. Left alone rather than guessed at.
        return False
    left, top, right, bottom = region
    area = max(0.0, right - left) * max(0.0, bottom - top)
    mine = _share_of_shape(drawn, region)
    # `share=0` reads as "anything that touches this box at all", which is the question
    # an author asks of a band it wants to write in rather than clear.
    if mine <= 0.0 or mine < share:
        return False
    # The box covers this shape and this shape covers the box: they are the same
    # rectangle, which is what a region taken off a chart's own box is, so it goes.
    # A box inside a shape that reaches well past it is the card the box sits in, and
    # that card is the arrangement the page was cloned for, so the card stays.
    #
    # Only when the caller is clearing. `share=0` is the other question -- what is over
    # this band at all -- and there the card is part of the answer, not an exception to
    # it: a foot that is the template's own is exactly what that call is looking for.
    if share > 0 and area and _overlap(drawn, region) / area >= THE_BOX_SITS_IN_IT:
        if mine < THE_BOX_SITS_IN_IT:
            return False
    return True


# Said once, and it shows the two readings rather than asserting which is right, because
# the whole point is that four numbers cannot be told apart. `ppt_layout.Box.at` refuses
# positional numbers for this same reason and in nearly these same words.
_A_REGION_SAYS_WHICH = (
    "a region is a box that says which of its two readings it is -- a ppt_layout.Box, or the "
    "PageBox that page_box(shape) hands back -- and this does not: {given}{spelled} Say which: "
    "Box.corners(x0, y0, x1, y1) for two corners, Box.at(x, y, w=, h=) for a corner and a size, "
    "or page_box(shape) for the box a shape is drawn in."
)


def _region_readings(given) -> str:
    """The two rectangles four numbers could be, so the caller can see which it wanted."""
    if len(given) != 4:
        return ""
    left, top, third, fourth = given
    corners = f"({left:g}, {top:g}) to ({third:g}, {fourth:g})"
    size = f"({left:g}, {top:g}) to ({left + third:g}, {top + fourth:g})"
    return f" -- as corners that is the box {corners}, as a size the box {size}."


def _as_region(box) -> tuple[float, float, float, float]:
    """`box` as (left, top, right, bottom) in inches, from a box that says which it is.

    Two conventions meet here and four bare numbers cannot say which: `ppt_layout.Box`
    and `page_box` are two corners, while every python-pptx call in the same script --
    and every box a template reference prints -- is a corner and a size. So an object
    that knows is asked, and four bare numbers are refused rather than guessed at.

    Guessing is what this did until it was measured. A bare tuple was read as corners
    unless its last two numbers were smaller than the first two, on the stated theory
    that a size always is. On a 13.33x7.5in canvas most sizes are larger than most
    origins, so the reading treated as the exception was the common one: `(1, 1, 4, 3)`
    meaning a corner and a size became the region (1,1)-(4,3) rather than (1,1)-(5,4).
    A shape at (4.2, 3.2) was then invisible to `shapes_in`, so `clear_region` neither
    removed it nor named it in `left_standing` -- the author was told the space was
    clear, drew into it, and landed on the furniture anyway, which is the one failure
    this pair of calls exists to prevent.

    Refused rather than reported, because there is nothing to report: the size reading
    always contains the corner reading, so "the other reading would have caught more"
    is true of every bare tuple and separates nothing. The ambiguity can only be
    removed at the call, which is why `Box` was given two named constructors.
    """
    if hasattr(box, "x0") and hasattr(box, "y1"):
        return (float(box.x0), float(box.y0), float(box.x1), float(box.y1))
    try:
        values = tuple(float(value) for value in box)
    except (TypeError, ValueError):
        values = ()
    raise ValueError(_A_REGION_SAYS_WHICH.format(given=values or repr(box), spelled=_region_readings(values)))


def _overlap(drawn, region) -> float:
    across = max(0.0, min(drawn[2], region[2]) - max(drawn[0], region[0]))
    down = max(0.0, min(drawn[3], region[3]) - max(drawn[1], region[1]))
    return across * down


def _share_of_shape(drawn, region) -> float:
    """How much of this shape the region covers, 0 to 1.

    A connector is the reason this is not simply an area ratio. A horizontal rule is
    0.00in tall and a vertical one 0.00in wide, so its area is zero, every area ratio
    over it is zero or undefined, and it survives any region test written the obvious
    way -- which is what left a leader line lying across a redrawn chart. A shape with
    one live dimension is measured along that dimension.
    """
    x0, y0, x1, y1 = drawn
    left, top, right, bottom = region
    width, height = x1 - x0, y1 - y0
    across = max(0.0, min(x1, right) - max(x0, left))
    down = max(0.0, min(y1, bottom) - max(y0, top))
    if width > 0 and height > 0:
        return (across * down) / (width * height)
    if width > 0:
        return across / width if top <= y0 <= bottom else 0.0
    if height > 0:
        return down / height if left <= x0 <= right else 0.0
    return 1.0 if (left <= x0 <= right and top <= y0 <= bottom) else 0.0


def shape_near(container, left: float, top: float, tol: float = 0.08, *, with_text: bool = False):
    """The shape whose top-left corner is within `tol` inches of (`left`, `top`).

    The other half of `page_position`: the numbers an author has are the ones the
    template reference prints, which are positions on the page, and this is what turns
    one of those back into a handle. `with_text=True` skips the panels and pictures a
    text box sits on, which is the reading an author usually wants at a coordinate.

    A companion to `shape_at`, which takes the same page's shapes by number. Both
    exist because a template page is a starting point rather than a form.
    """
    every = list(_all_shapes(container.shapes))
    unplaceable = 0
    considered = []
    for index, shape in enumerate(every, start=1):
        if with_text and not (getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip()):
            continue
        at = _where(shape)
        if at is None:
            unplaceable += 1
            continue
        if abs(at[0] - left) <= tol and abs(at[1] - top) <= tol:
            return shape
        considered.append((index, shape, at))
    raise KeyError(
        f"no shape within {tol:g}in of ({left:g}, {top:g}) on this page"
        + (" carrying text" if with_text else "")
        + "."
        + _nearby(left, top, tol, considered)
        + " Positions are on the page, groups resolved -- `shape.left` inside a group is not one. "
        + (
            f"{unplaceable} of them sit in a group the template flipped or rotated and have no position "
            "to compare against; take those by their copy with `shape_saying`. "
            if unplaceable
            else ""
        )
        + "For every shape in a space rather than the one at a point, `shapes_in(container, box)`; to "
        + "empty that space, `clear_region(container, box)` -- both take a ppt_layout.Box or a "
        + "page_box(shape), not four bare numbers. The page holds: "
        + "; ".join(_listed(index, shape) for index, shape in enumerate(every, start=1))
    )


def _where(shape) -> tuple[float, float] | None:
    """`page_position`, or None where the shape has no position to compare against."""
    try:
        return page_position(shape)
    except ValueError:
        return None


def _listed(index: int, shape) -> str:
    at = _where(shape)
    where = f"({at[0]:.2f}, {at[1]:.2f})" if at is not None else "inside a flipped or rotated group"
    return f"[{index}] {_describe(shape)} at {where}"


def shape_saying(container, prefix: str):
    """The first shape whose copy starts with `prefix`, groups walked into.

    `replace_text(slide, old, new)` finds a block by the same words; this is the single
    handle for the adjustment that comes after. Its refusal lists the copy the page
    actually holds, because the string an author is matching against is usually the
    template's and usually not quite what it remembered.

    Reaching for it at all is usually the wrong move: `replace_text` writes the words
    without ever handing the shape back, and a second pass over the page is a second
    place for it to go wrong. What this is for is the adjustment `replace_text` has no
    argument for -- moving, resizing or dropping the block whose words it knows.
    """
    said = []
    holders = []
    for shape in _all_shapes(container.shapes):
        if not getattr(shape, "has_text_frame", False):
            continue
        text = shape.text_frame.text.strip()
        if not text:
            continue
        if text.startswith(prefix):
            return shape
        said.append(text[:40])
        holders.append(shape)
    raise KeyError(
        f"no shape on this page starts with {prefix!r}."
        + _near_says([(prefix, holders)], head=True)
        + " Its copy reads: "
        + "; ".join(repr(one) for one in said)
        + ". A prefix has to match from the first character of the block, accents and spacing included; name the"
        " block with `replace_text(slide, old, new)` keyed on the words it holds now rather than looking"
        " for it here"
    )


# The size the readability floor is set at, which `measure.type_size` refuses under.
# Stated here rather than imported: this module is projected beside the author's
# script and runs without the package around it.
BODY_FLOOR_PT = 14.0
# And the length of copy the floor applies to. `measure.type_size` holds a caption, a
# kicker or a chart mark to 10.8pt instead, so lifting everything from twelve characters
# up flattened 53 runs across the bundled templates that the check already accepts --
# which is the size ladder `type_drift` and `type_scale` measure.
COPY_CHARS = 20


def raise_type(slide, floor: float = BODY_FLOOR_PT, min_chars: int = COPY_CHARS) -> int:
    """Lift a cloned page's small copy to the readability floor, and give it the room.

    A template sets its demo copy at whatever suits the demo, and cloning brings that
    size along: three of four live decks shipped body copy at 10.8 to 12pt and were
    told so by `type_floor`, per box, page after page. Two of their authors wrote this
    function for themselves under the same name with the same 14pt default, and only
    one of them did the second half -- type raised in a box sized for the smaller type
    overflows it, which is why `type_floor`'s own message asks for both.

    Returns how many boxes it touched. Runs of fewer than `min_chars` are left alone:
    a number, a unit or a two-character label is set small on purpose, and lifting
    those is what turns a designed size ladder into one flat size.

    **It cannot fix the other half of `type_floor`.** That check reads the size off the
    render, and copy comes out under the floor two ways: stated small, which this
    lifts, or stated at the floor and shrunk to fit by the box's own autofit, which
    this does not touch because nothing in that box is under the floor to raise. Measured across
    two live decks: one page had six boxes of the first kind and another had none of it
    and one of the second. The second needs a bigger box rather than a bigger size --
    `ppt_layout.fits` and `text_size` are what say how much bigger.
    """
    from pptx.enum.text import MSO_AUTO_SIZE
    from pptx.util import Pt

    touched = 0
    for shape in _all_shapes(slide.shapes):
        if not getattr(shape, "has_text_frame", False):
            continue
        frame = shape.text_frame
        if len(frame.text.strip()) < min_chars:
            continue
        listed = _list_size(frame)
        lifted = False
        for para in frame.paragraphs:
            bare = 0
            for run in para.runs:
                size = run.font.size
                if size is None:
                    bare += 1
                elif size.pt < floor:
                    run.font.size = Pt(floor)
                    lifted = True
            if not bare:
                continue
            # A run that states no size takes one from its paragraph or from the body's
            # own list style, and the check this answers to reads the size off the render
            # -- so a size the file never writes on a run is still a size it reports.
            # 106 runs across the twelve bundled templates inherit theirs this way, and
            # reading only the run level left every one of them where it was.
            stated = para.font.size
            inherited = stated.pt if stated is not None else listed
            if inherited is not None and inherited < floor:
                para.font.size = Pt(floor)
                lifted = True
        if not lifted:
            continue
        # The template's own autofit is what shrank the copy in the first place, and
        # merely turning it off leaves the lifted type running out of a box drawn for
        # the smaller size -- measured 0.48in past the bottom of a 1.2in box, with
        # nothing in the file to say so. Growing the shape is the half `type_floor`'s
        # message asks for and the only one reachable without font metrics: a box that
        # then meets its neighbour is a collision the render-side checks report, which
        # copy running out of its box is not. `word_wrap` is left as the template set
        # it -- a one-line label it set `wrap="none"` on is a label, not a paragraph.
        frame.auto_size = MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT
        touched += 1
    return touched


def _list_size(frame) -> float | None:
    """The size this body's own list style states, in points, or None if it states none."""
    level = frame._txBody.find(f"{{{_A}}}lstStyle/{{{_A}}}lvl1pPr/{{{_A}}}defRPr")
    size = level.get("sz") if level is not None else None
    return int(size) / 100.0 if size else None


def _group_chain(shape):
    """(parent offset+extent, child offset+extent) for each group above `shape`, outermost first."""
    chain = []
    element = shape._element.getparent()
    while element is not None and element.tag == f"{{{_P}}}grpSp":
        frame = element.find(f"{{{_P}}}grpSpPr/{{{_A}}}xfrm")
        if frame is None:
            break
        if frame.get("rot") or frame.get("flipH") == "1" or frame.get("flipV") == "1":
            raise ValueError(
                "this shape sits inside a group the template flipped or rotated, so where it is drawn is "
                "not what its offsets say -- measured 1.25in out on a flipped group, fifteen times the "
                "tolerance a search runs at. Take the handle by its copy instead: shape_saying(container, "
                "prefix) reads the text, which a flip does not move"
            )
        offset, extent = frame.find(f"{{{_A}}}off"), frame.find(f"{{{_A}}}ext")
        child_offset, child_extent = frame.find(f"{{{_A}}}chOff"), frame.find(f"{{{_A}}}chExt")
        if None in (offset, extent, child_offset, child_extent):
            break
        chain.append(
            (
                (
                    (int(offset.get("x")) / EMU_PER_INCH, int(offset.get("y")) / EMU_PER_INCH),
                    (int(extent.get("cx")) / EMU_PER_INCH, int(extent.get("cy")) / EMU_PER_INCH),
                ),
                (
                    (int(child_offset.get("x")) / EMU_PER_INCH, int(child_offset.get("y")) / EMU_PER_INCH),
                    (int(child_extent.get("cx")) / EMU_PER_INCH, int(child_extent.get("cy")) / EMU_PER_INCH),
                ),
            )
        )
        element = element.getparent()
    chain.reverse()
    return chain


EMU_PER_INCH = 914400.0


def _layout_in(presentation, prototype):
    """The target's own layout for this prototype, matched by name.

    Handing `add_slide` a layout that belongs to another package relates the new
    slide to a part that package owns, and saving then writes that layout -- and
    its master, and its theme -- into the file a second time under the name it
    already has. The result is a zip holding two entries called
    `ppt/slideLayouts/slideLayout9.xml`, which PowerPoint offers to repair.

    Cloning across packages is the normal case here, not the exotic one: the pages
    worth reusing are in the user's original and the deck is built in the prepared
    copy. Both come from the same file, so the layout names match.
    """
    theirs = prototype.slide_layout
    ours = [layout for master in presentation.slide_masters for layout in master.slide_layouts]
    if any(layout._element is theirs._element for layout in ours):
        return theirs
    for layout in ours:
        if layout.name == theirs.name:
            return layout
    return ours[0] if ours else theirs


def _repoint(element, source_part, target_part) -> None:
    """Re-attach every relationship the copied XML refers to, by id.

    Walks the copy rather than the original, so nested shapes -- a picture inside a
    group, which is where templates keep most of theirs -- are covered too.
    """
    for node in element.iter():
        for attribute in _REL_ATTRS:
            old = node.get(attribute)
            if not old:
                continue
            try:
                related = source_part.rels[old]
            except KeyError:
                continue
            node.set(attribute, _carried(related, target_part))


def _carried(related, target_part) -> str:
    """The same relationship, remade against the target, and the id it now has."""
    if related.is_external:
        return target_part.relate_to(related.target_ref, related.reltype, is_external=True)
    blob = getattr(related.target_part, "blob", None)
    if blob is not None and related.reltype.endswith("/image"):
        # Rebuilt from the bytes rather than pointed at the source's part. The two
        # packages number their media independently, so carrying the part across
        # brings a `/ppt/media/image1.png` into a file that already has one, and
        # the saved zip holds two entries under that name. Going through the
        # target's own image collection also means a picture cloned twice is
        # stored once.
        try:
            _, relationship = target_part.get_or_add_image_part(io.BytesIO(blob))
        except Exception:  # noqa: BLE001 -- see below; the picture is worth more than the tidier package
            # A deck may legally hold an EMF or a WMF, and nothing here decodes
            # one. Carrying the part is the weaker path -- it is what risks the
            # collision above -- but the alternative is losing the whole page to
            # an exception, which is what this did on 1 of 28 real templates.
            return target_part.relate_to(related.target_part, related.reltype)
        return relationship
    return target_part.relate_to(related.target_part, related.reltype)


def replace_picture(
    shape,
    image: Path,
    fit: str = "contain",
    *,
    anchor: str = "centre",
    trim=None,
    zoom: float = 1.0,
    alpha: float | None = None,
    box=None,
):
    """Swap the image behind a picture frame, keeping the frame.

    Deleting the frame and adding a new one is the obvious way and it loses what
    the template chose: the crop, the border, the shadow, the position in the
    z-order. The frame is the design; only the pixels are the content.

    A template's frame is almost never the aspect ratio of the figure going into it, so
    something has to give, and which one matters:

    * `fit="contain"` (the default) shrinks the frame to the picture's proportions and
      centres it there. The whole figure is visible.
    * `fit="cover"` keeps the frame exactly and crops the picture to it. Right for a
      photograph, wrong for a figure: a 3x3 grid of qualitative results went into a
      portrait slot under cover and lost its left and right columns, which is citing
      evidence the page does not show.
    * `fit="stretch"` distorts to fill, which is visible in any screenshot with type in
      it. There for the rare frame drawn to the picture.

    Contain is the default because on this route the pictures come from the sources --
    plots, tables, qualitative grids -- and losing part of one is a provenance problem, not
    a layout one. Reach for cover when the picture is decoration.

    Three arguments decide *which* pixels a cover fit keeps, and without them an author
    wrote its own swap: thirty lines of PIL and hand-edited XML, past the checks here.

    * `anchor` is the side the crop keeps -- "centre" (the default), "top", "bottom",
      "left" or "right". A photograph whose subject is along the top loses it to a
      centred crop: `anchor="top"` keeps the lettering on the archway.
    * `trim` cuts shares off the source's own edges *before* the fit, as
      (left, right, top, bottom). A screenshot with a progress bar along the bottom is
      `trim=(0, 0, 0, 0.08)`, and nothing has to be written to a file to do it.
    * `zoom` is a multiple of the scale that just covers the frame, so `zoom=1.6`
      shows 1/1.6 of what the fit would -- a detail made legible at the size the frame
      has. Under 1 is refused: a cover that does not cover is `fit="contain"`.

    `trim` applies to contain as well, where it cuts the source and the frame then
    gives way to what is left. `anchor` and `zoom` are cover's own and are ignored
    there -- contain shows the whole picture, so there is no window to place.

    `alpha` washes the new picture to a share of itself, the way `backdrop` does. It
    is for the frame that is the page: six of the eight bundled templates keep a
    picture the size of the canvas on a layout, a soft texture the type reads over,
    and a photograph swapped in at full strength drowns every title on that layout.
    `alpha=0.1` keeps it the texture the template meant; the shares between 0.12 and
    0.80 are the fog `washed_backdrop` reports, and a photograph meant to be seen goes
    in at full strength under a plane of ink with light type, as `backdrop` lays them.
    `None` leaves whatever wash the frame had.

    `box` -- (left, top, width, height) in inches, or a `ppt_layout.Box` -- reshapes the
    frame first, as `place` would, so the fit is computed against where the figure goes.

    A template's illustration is not always a picture: the cartoon on a section page is
    as often a group of a dozen freeforms, drawn in PowerPoint, and there is no blip to
    swap. Handed such a shape -- or a list of shapes that make one drawing -- this puts
    a picture where the drawing was: the drawing's own box (or `box`), the drawing's
    place in the z-order, fitted the same way, and the drawing removed. A shape inside a
    group takes the drawing it is part of with it -- the outermost group around it that
    holds no text, so a cartoon goes whole and the card it decorates stays. Returns the
    new picture frame on that route, so the caller can still reach it; `None` when a
    frame was kept.
    """
    targets = list(shape) if isinstance(shape, (list, tuple)) else [shape]
    if not targets:
        raise ValueError("nothing to replace: `replace_picture` was handed an empty list")
    if len(targets) > 1 or _blip_fill(targets[0]) is None:
        _refuse_what_is_not_a_picture(targets)
        return _picture_in_place_of(targets, image, fit, anchor=anchor, trim=trim, zoom=zoom, alpha=alpha, box=box)
    shape = targets[0]
    if box is not None:
        place(shape, box)
    # Read before the blip is swapped: what the frame held is the whole question.
    _check_cut_out(shape, image)
    _, relationship = shape.part.get_or_add_image_part(str(image))
    fill = _blip_fill(shape)
    blip = fill.find(f"{{{_A}}}blip")
    if blip is None:
        raise ValueError("that shape has no image to replace")
    blip.set(f"{{{_R}}}embed", relationship)
    if alpha is not None:
        _wash(blip, alpha)
    if fit not in ("cover", "contain"):
        return
    if fit == "contain" and shape.shape_type == MSO_SHAPE_TYPE.PICTURE and _clipped(shape):
        # A frame cut to a curve, an arc or a slant is the page's design, and contain
        # shrinks it to the picture's proportions: a 13.35in wave-edged frame on a gold
        # section page came back 7.56in wide, off its swoosh, with the photograph
        # sitting in a plain rectangle beside the panel it was drawn to complete.
        # Cover keeps the frame and crops the picture into it, which is what a shaped
        # frame asks for; an author who wants contain there says so and gets it.
        warnings.warn(
            f"{getattr(shape, 'name', 'this frame')!r} is a shaped picture frame ({_geometry(shape)}), so the "
            f"picture was fitted with cover: the frame keeps its shape and the picture is cropped into it. "
            'Pass fit="cover" to say so, or anchor/trim/zoom to choose which part shows.',
            stacklevel=2,
        )
        fit = "cover"
    _check_shape(shape, image, fit, trim)
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        _fit(shape, image, fit, anchor, trim, zoom)
    else:
        # A shape *filled* with a picture crops through the fill's source rectangle
        # rather than through a picture frame's crop attributes, and it has no frame to
        # shrink -- so "contain" has nowhere to put the letterboxing and both fits become
        # the same cover crop. Without this the template's own stretch survives and a
        # figure swapped into a rounded panel comes out distorted, which is visible in
        # any screenshot with type in it.
        _fill_crop(shape, fill, image, anchor, trim, zoom)


# What a drawing made of shapes may be, for `replace_picture` to put a picture in
# its place: a group, a hand-drawn outline, a preset shape. A text box or a placeholder
# is neither -- an index that lands on one is a miscount, not an illustration.
_DRAWN = frozenset({MSO_SHAPE_TYPE.GROUP, MSO_SHAPE_TYPE.FREEFORM, MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.LINE})


def _is_drawing(shape) -> bool:
    """Whether a shape without a blip is an illustration a picture may stand in for."""
    if getattr(shape, "is_placeholder", False):
        return False
    if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        return False
    return getattr(shape, "shape_type", None) in _DRAWN


def _outermost(shape):
    """The whole drawing `shape` is part of: the outermost group around it that holds no text.

    A cartoon is a group of freeforms and a member of it means the cartoon. But on a
    template page everything sits one group down -- the card, its icon, its heading and
    its copy in one group, the four cards in another -- and climbing to the top there
    swapped a whole page's content for one picture. A group with words in it is content
    the drawing sits in, not the drawing.
    """
    element = shape._element
    while True:
        parent = element.getparent()
        if parent is None or parent.tag != f"{{{_P}}}grpSp" or _holds_text(parent):
            break
        element = parent
    if element is shape._element:
        return shape
    return _shape_for(shape.part.slide, element)


def _holds_text(element) -> bool:
    """Whether a group carries words, or a text box drawn to carry them."""
    if any((node.text or "").strip() for node in element.iter(f"{{{_A}}}t")):
        return True
    return any(node.get("txBox") == "1" for node in element.iter(f"{{{_P}}}cNvSpPr"))


def _refuse_what_is_not_a_picture(targets) -> None:
    """Refuse a target that is neither a picture nor a drawing a picture may replace.

    The check used to sit in the call that resolved a shape by number, and went with it
    when that call was removed (D41): a numbered target reached here unexamined, so an
    index off by one turned a card's body copy into a photograph and said nothing. The
    numbering is what makes this worth a refusal rather than a caller's problem -- two
    models each aimed at a page's third shape when the picture was its second.
    """
    wrong = [one for one in targets if _blip_fill(one) is None and not _is_drawing(one)]
    if not wrong:
        return
    named = "; ".join(_describe(one) for one in wrong)
    page = ""
    try:
        every = list(_all_shapes(targets[0].part.slide.shapes))
    except Exception:  # noqa: BLE001 -- not on a slide, so there is no page to list
        every = []
    if every:
        page = " The page holds: " + "; ".join(
            f"[{index}] {_describe(one)}" for index, one in enumerate(every, start=1)
        )
    raise ValueError(
        f"a picture cannot stand in for {named}: it holds no image and it is not an illustration -- a text box, "
        "a placeholder or a panel with words in it is a miscount, not a drawing. Name the frame or the drawing "
        f"the page actually has, or add a picture of your own with `slide.shapes.add_picture`.{page}"
    )


def _picture_in_place_of(targets, image: Path, fit: str, *, anchor, trim, zoom, alpha, box):
    """A picture where a drawing made of shapes was, fitted into its box at its depth."""
    owner = getattr(targets[0].part, "slide", None)
    if owner is None:
        raise ValueError(
            "that drawing is on a layout, not on the page; a layout's shapes cannot be swapped for a picture -- "
            "cover it with `backdrop` or `shapes.add_picture` on the page instead"
        )
    whole: list = []
    for target in targets:
        top = _outermost(target)
        if not any(top._element is done._element for done in whole):
            whole.append(top)
    tree = whole[0]._element.getparent()
    if any(shape._element.getparent() is not tree for shape in whole):
        raise ValueError("those shapes are not on the same page, so one picture cannot stand in for them")
    left = min(int(shape.left) for shape in whole)
    top = min(int(shape.top) for shape in whole)
    right = max(int(shape.left + shape.width) for shape in whole)
    bottom = max(int(shape.top + shape.height) for shape in whole)
    if right <= left or bottom <= top:
        raise ValueError("that drawing has no extent to put a picture into; pass `box`")
    depth = min(tree.index(shape._element) for shape in whole)
    picture = owner.shapes.add_picture(str(image), left, top, right - left, bottom - top)
    picture._element.getparent().remove(picture._element)
    tree.insert(depth, picture._element)
    picture.name = getattr(whole[0], "name", "") or "illustration"
    if box is not None:
        place(picture, box)
    if fit not in ("cover", "contain", "stretch"):
        raise ValueError(f"fit must be cover, contain or stretch, not {fit!r}")
    _check_shape(picture, image, fit, trim)
    if fit != "stretch":
        _fit(picture, image, fit, anchor, trim, zoom)
    if alpha is not None:
        _wash(picture._element.blipFill.find(f"{{{_A}}}blip"), alpha)
    for shape in whole:
        tree.remove(shape._element)
    return picture


_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _geometry(shape) -> str:
    """The preset a shape's geometry names, or `custom` for a hand-drawn outline."""
    element = shape._element
    if element.find(f".//{{{_A}}}custGeom") is not None:
        return "custom"
    preset = element.find(f".//{{{_A}}}prstGeom")
    return str(preset.get("prst")) if preset is not None else "rect"


def _clipped(shape) -> bool:
    """Whether a picture frame is cut to anything but a plain rectangle."""
    return _geometry(shape) not in ("rect",)


def _blip_fill(shape):
    """The `blipFill` this shape shows its image through, whichever spelling it uses.

    A template's photograph is as often a rounded rectangle filled with one as it is a
    picture frame -- that is how a designer gets a soft corner on a photo -- and the two
    spell it differently: `p:pic/p:blipFill` against `p:sp/p:spPr/a:blipFill`. This only
    knew the first, so an author told by `template_picture` to replace the photograph on
    its contents page got `AttributeError: blipFill`.
    """
    element = shape._element
    own = getattr(element, "blipFill", None)
    if own is not None:
        return own
    properties = element.find(f"{{{_P}}}spPr")
    return None if properties is None else properties.find(f"{{{_A}}}blipFill")


# Where a cover crop keeps its window when the picture and the frame disagree.
# Centre is the only reading this had, and a live author wrote thirty lines of PIL and
# XML to get the other one: a night-market photograph whose archway lettering is along
# the top came out with the lettering cut, so the program pre-cropped the file itself
# with a `top_bias` of its own and swapped the blip by hand -- past `_check_shape`,
# past the stale-`fillRect` cleanup, and past every measurement this module makes.
_PICTURE_ANCHORS = {
    "centre": (0.5, 0.5),
    "top": (0.5, 0.0),
    "bottom": (0.5, 1.0),
    "left": (0.0, 0.5),
    "right": (1.0, 0.5),
}


def _trimmed(trim) -> tuple[float, float, float, float]:
    """`trim` as four shares of the source's own edges, refusing what cannot be cut."""
    if trim is None:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        left, right, top, bottom = (float(share) for share in trim)
    except (TypeError, ValueError):
        raise ValueError(
            f"trim is four shares of the source's edges -- (left, right, top, bottom) -- not {trim!r}. "
            "A screenshot with a progress bar along the bottom is trim=(0, 0, 0, 0.08)"
        ) from None
    if min(left, right, top, bottom) < 0 or left + right >= 1 or top + bottom >= 1:
        raise ValueError(
            f"trim=({left:g}, {right:g}, {top:g}, {bottom:g}) leaves no picture: each is a share of the "
            "source's own width or height, and the two on an axis have to come to less than 1"
        )
    return (left, right, top, bottom)


def _cover_crop(frame: float, size: tuple[int, int], anchor: str, trim, zoom: float):
    """The four crop shares a cover fit needs, against the source's own edges.

    One function for both crop paths -- a picture frame's `crop_*` attributes and a
    fill's `srcRect` -- because they were two copies of the same arithmetic and only
    one of them ever got a fix.

    Every share returned is of the *original* source, which is what both paths take
    and what makes `trim` and the fit's own crop add rather than compose: they are
    cuts off the same rectangle.

    `zoom` is a multiple of the scale that just covers the frame. 1.0 is the largest
    window that still fills it, which is the fit itself; 1.6 shows 1/1.6 of that
    window, which is how a detail in a photograph is made legible at the size the
    frame has. Under 1.0 there is no cover, so it is refused rather than letterboxed
    silently.
    """
    if anchor not in _PICTURE_ANCHORS:
        raise ValueError(f"anchor is one of {', '.join(sorted(_PICTURE_ANCHORS))}, not {anchor!r}")
    if not zoom or zoom <= 0:
        raise ValueError(f"zoom is a multiple of the fit, so {zoom!r} is not one")
    if zoom < 1:
        raise ValueError(
            f"zoom={zoom:g} would leave the frame part empty, which a cover fit cannot do. Either "
            'fit="contain", which shrinks the frame to the picture, or hand a smaller box'
        )
    left0, right0, top0, bottom0 = _trimmed(trim)
    width, height = size
    across, down = 1 - left0 - right0, 1 - top0 - bottom0
    picture = (width * across) / (height * down)
    keep_w, keep_h = (frame / picture, 1.0) if picture > frame else (1.0, picture / frame)
    keep_w, keep_h = keep_w / zoom, keep_h / zoom
    if keep_w > 1 or keep_h > 1:
        raise ValueError(f"zoom={zoom:g} asks for more picture than there is at this crop")
    share_x, share_y = _PICTURE_ANCHORS[anchor]
    off_x, off_y = (1 - keep_w) * share_x, (1 - keep_h) * share_y
    return (
        left0 + off_x * across,
        1 - (left0 + (off_x + keep_w) * across),
        top0 + off_y * down,
        1 - (top0 + (off_y + keep_h) * down),
    )


def _fill_crop(shape, fill, image: Path, anchor: str = "centre", trim=None, zoom: float = 1.0) -> None:
    """Crop the fill's source to the shape's proportions, so nothing stretches.

    The crop is stated against the frame, so any inset the template fitted its own
    photograph with has to go with the image it was cut for. Left behind, the fill
    states its fit twice and the two disagree: a live template photograph came out with
    a 23.9% crop for the frame and a -32% `fillRect` for a box half again as wide, and
    `figure_distortion` read the pair as a 1.64x stretch that no renderer put on the
    page.
    """
    size = _picture_size(image)
    if size is None or not shape.width or not shape.height:
        return
    from lxml import etree

    for stale in fill.findall(f"{{{_A}}}srcRect"):
        fill.remove(stale)
    stretch = fill.find(f"{{{_A}}}stretch")
    if stretch is not None:
        for stale in stretch.findall(f"{{{_A}}}fillRect"):
            stretch.remove(stale)
    left, right, top, bottom = _cover_crop(shape.width / shape.height, size, anchor, trim, zoom)
    rect = etree.SubElement(fill, f"{{{_A}}}srcRect")
    for side, share in (("l", left), ("r", right), ("t", top), ("b", bottom)):
        if share > 0:
            rect.set(side, str(int(round(share * 100000))))
    fill.insert(list(fill).index(fill.find(f"{{{_A}}}blip")) + 1, rect)


def _check_shape(shape, image: Path, how: str, trim=None) -> None:
    """Warn about a picture whose proportions the frame cannot hold either way.

    A portrait slot and a landscape figure is a layout decision, not a fitting one:
    contained, the figure is a strip in the middle of an empty frame; covered, most of it
    is cropped away. So the numbers are stated -- `place(shape, (left, top, width,
    height))` reshapes the frame, another prototype may have a landscape slot, and
    `drop` plus a shape of your own is always available.

    Stated as a warning, not raised. Raising stopped the whole build for one picture:
    three of the ten script crashes across two measured runs were this refusal, each
    costing a round and hiding every later page's failure behind it -- and one was a 1.5
    photograph aimed at a page-wide banner, which is a crop a designer makes on purpose.
    The picture is placed the way the caller asked; the warning reaches the author as
    the build's `warnings`, and the render shows what the crop did.
    """
    size = _picture_size(image)
    if size is None or not shape.width or not shape.height:
        return
    width, height = size
    frame = shape.width / shape.height
    # The trimmed picture, because that is the one being fitted. A landscape screenshot
    # trimmed to its portrait panel is the shape of the panel, and judging the file
    # would refuse the very cut that made it fit.
    left, right, top, bottom = _trimmed(trim)
    picture = (width * (1 - left - right)) / (height * (1 - top - bottom))
    off = max(frame / picture, picture / frame)
    if off <= FIT_RATIO_LIMIT:
        return
    import warnings

    what = (
        "sits as a strip in an otherwise empty frame"
        if how == "contain"
        else f"loses about {1 - 1 / off:.0%} of the figure to the crop"
    )
    warnings.warn(
        f"{Path(image).name} is {width}x{height}"
        + (f", trimmed to {picture:.2f}" if any((left, right, top, bottom)) else "")
        + f" ({picture:.2f} wide-to-tall) and this frame is "
        f"{shape.width / 914400:.2f}x{shape.height / 914400:.2f}in ({frame:.2f}) -- {off:.1f}x apart. "
        f"{'Contained' if how == 'contain' else 'Cropped'}, it {what}. Placed as asked; look at the render. "
        "If the figure matters, give the frame the box it needs -- replace_picture(shape, image, box=(left, "
        "top, width, height)) in inches, or place(shape_at(slide, n), box) afterwards -- or clone a prototype "
        "whose picture slot runs the other way, or drop this frame and add a picture of your own."
        + _frames_on_page(shape),
        stacklevel=3,
    )


def _frames_on_page(shape) -> str:
    """Every picture frame on this shape's page, so a refusal can be answered by picking one.

    A landscape figure aimed at a 6.05x7.50in frame has been aimed at the page's
    full-height backdrop photograph rather than at its figure slot. Numbers about the
    frame that was named do not answer that; the frames on the page and their
    proportions do, and that is one walk of the same page.
    """
    try:
        every = list(_all_shapes(shape.part.slide.shapes))
    except Exception:  # noqa: BLE001 -- not on a slide, so there is no page to describe
        return ""
    lines = []
    for index, other in enumerate(every, start=1):
        if getattr(other, "shape_type", None) != MSO_SHAPE_TYPE.PICTURE or not other.height:
            continue
        mine = " <- this one" if other._element is shape._element else ""
        lines.append(
            f"[{index}] {other.width / 914400:.2f}x{other.height / 914400:.2f}in "
            f"({other.width / other.height:.2f}){mine}"
        )
    return " This page's picture frames: " + ", ".join(lines) + "." if lines else ""


# The share of a picture's pixels that are fully transparent before it is a cut-out --
# an illustration floating on the page's own ground rather than a photograph in a frame.
# The bundled templates' cartoons measure 0.58 to 0.81; a photograph with a soft vignette
# stays well under.
CUT_OUT_SHARE = 0.25


def _is_cut_out(shape) -> bool:
    """Whether a picture frame holds a cut-out: an image mostly transparent, on the page's ground."""
    if getattr(shape, "shape_type", None) != MSO_SHAPE_TYPE.PICTURE:
        return False
    try:
        blob = shape.image.blob
    except Exception:  # noqa: BLE001 -- a frame whose image part is missing is not a cut-out
        return False
    share = _transparent_share(io.BytesIO(blob))
    return share is not None and share >= CUT_OUT_SHARE


def _transparent_share(source) -> float | None:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover -- Pillow ships with the route
        return None
    try:
        with Image.open(source) as opened:
            if opened.mode not in ("RGBA", "LA", "P") and "transparency" not in opened.info:
                return 0.0
            alpha = opened.convert("RGBA").getchannel("A")
            histogram = alpha.histogram()
            return histogram[0] / float(alpha.width * alpha.height)
    except Exception:  # noqa: BLE001 -- an unreadable image is the caller's problem
        return None


def _check_cut_out(shape, image: Path | str) -> None:
    """Warn when an opaque picture takes a cut-out's box.

    A template's cartoon is a transparent PNG floating on the page's own ground, and its
    box runs wherever the drawing does -- up into the title row, over the band. Swapping
    a photograph into that box keeps the box: on a live page a rectangular photograph then
    hugged the title and sat over the band the cartoon had merely peeked over. The picture
    is placed as asked; the author is told what the slot was and the two ways out.
    """
    if not _is_cut_out(shape):
        return
    share = _transparent_share(image)
    if share is None or share >= CUT_OUT_SHARE:
        return
    import warnings

    left, top = (shape.left or 0) / 914400, (shape.top or 0) / 914400
    warnings.warn(
        f"{getattr(shape, 'name', 'this frame')!r} held a cut-out illustration on the page's own ground "
        f"({(shape.width or 0) / 914400:.1f}x{(shape.height or 0) / 914400:.1f}in at {left:.2f}, {top:.2f}), and "
        f"{Path(image).name} is an opaque picture: in the cut-out's box it lands on whatever the drawing floated over "
        "-- a title row, a band. Either give the photograph a box of its own, clear of the copy "
        "(replace_picture(shape, image, box=(left, top, width, height)) or place(shape, box)), or fill the slot with a "
        "cut-out: ppt_generate_image(..., transparent=true).",
        stacklevel=3,
    )


def _picture_size(image: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover -- Pillow ships with the route
        return None
    try:
        with Image.open(image) as opened:
            return opened.size
    except Exception:  # noqa: BLE001 -- an unreadable image is the caller's problem
        return None


def _fit(shape, image: Path, how: str, anchor: str = "centre", trim=None, zoom: float = 1.0) -> None:
    """Crop the frame's content ("cover") or shrink the frame to the picture ("contain")."""
    size = _picture_size(image)
    if size is None:
        return
    width, height = size
    if not (width and height and shape.width and shape.height):
        return
    shape.crop_left = shape.crop_right = shape.crop_top = shape.crop_bottom = 0
    frame = shape.width / shape.height
    cut = _trimmed(trim)
    if how == "cover":
        left, right, top, bottom = _cover_crop(frame, size, anchor, trim, zoom)
        shape.crop_left, shape.crop_right = left, right
        shape.crop_top, shape.crop_bottom = top, bottom
        return
    # contain: the frame gives way, and it gives way about its own centre so the
    # composition around it does not shift. What was trimmed off the source is cut
    # first, and the frame then gives way to what is left rather than to the file.
    if any(cut):
        shape.crop_left, shape.crop_right, shape.crop_top, shape.crop_bottom = cut
    picture = (width * (1 - cut[0] - cut[1])) / (height * (1 - cut[2] - cut[3]))
    if picture > frame:
        tall = int(shape.width / picture)
        shape.top = int(shape.top + (shape.height - tall) / 2)
        shape.height = tall
    elif picture < frame:
        wide = int(shape.height * picture)
        shape.left = int(shape.left + (shape.width - wide) / 2)
        shape.width = wide


def layout_pictures(slide) -> list:
    """The pictures a page inherits from its layout, largest first.

    A template's photograph is not always on the page: several bundled templates carry
    the cover's, the section page's and the closing page's on the *layout*, so every
    page built on it shows the same picture and nothing on the page itself can be
    handed to `replace_picture` -- a swap on the cloned page never reaches
    it, and a live deck shipped with the template's photographs on every section page
    for that reason. These are those shapes. `replace_picture(layout_pictures(slide)[0],
    image, "cover")` changes the picture for every page on that layout at once, which
    is what a house photograph should do. One the size of the page is the page's
    background with type over it: `alpha=0.1` keeps it a texture, and a photograph meant
    to be seen wants a plane of ink and light type over it, the way `backdrop` lays them
    -- a wash between the two, 0.12 to 0.80, is the fog `washed_backdrop` reports.
    """
    found = [shape for shape in _every_shape(slide.slide_layout.shapes) if _blip_fill(shape) is not None]
    found.sort(key=lambda shape: -((shape.width or 0) * (shape.height or 0)))
    return found


# Where a backdrop's picture may sit between invisible and opaque. Under 0.05 nothing
# shows and the call was a mistake; 1.0 is the photograph as it is, which is a figure and
# not a backdrop, but an author who wants a full-bleed picture behind a title over a dark
# wash of its own is allowed it.
BACKDROP_ALPHA_MIN = 0.05
# As far as a wash goes before it stops being texture and starts being fog: under this
# the page's own ground and type still carry the page.
BACKDROP_TEXTURE_MAX = 0.12


def backdrop(
    slide,
    image,
    *,
    alpha: float = 1.0,
    scrim: float | None = 0.62,
    ink=None,
    light_type: bool = True,
    box=None,
    anchor: str = "centre",
    trim=None,
    zoom: float = 1.0,
):
    """A photograph behind everything on the page, dimmed under a plane of ink, the type set light.

    The one generated picture that never poses as evidence: a cover, a section page or a
    closing page wants atmosphere more than a figure, and the templates' own photographs
    are placeholders. Full-bleed by default, or into `box` -- (left, top, width, height)
    in inches, or a `ppt_layout.Box` -- and cover-cropped to it, so a 4:3 render behind a
    16:9 page loses its top and bottom rather than stretching; `anchor`, `trim` and
    `zoom` place the window the way `replace_picture` does.

    The default is the form that reads: the photograph at full strength, a plane of the
    theme's ink over it at `scrim` (0.62, measured against a reference cover that works --
    a night market at full strength under a dark plane, white title), and every run of type the plane covers set
    to the theme's light colour, except runs already in a saturated accent, which keep it.
    Type outside a `box` sits on the page's own ground and is left as the template set it. A
    photograph washed to 30% on a white page under black type reads as fog -- a live
    cover and a closing page came out that way -- and this is the alternative that does
    not. `ink` names the plane's colour when the theme's is wrong for the picture.

    `alpha` is the picture's share of itself, and with `scrim=None` (or 0) nothing is
    laid over it and the type is left alone: that is the texture form, for a faint
    picture behind a template's own ground, and it wants `alpha` at 0.12 or under. The
    shares between texture and full strength are the ones the render will report as
    fog (`washed_backdrop`). The wash is the picture's own (`alphaModFix`), so nothing
    is added to the z-order but the picture -- and, with a scrim, the one plane above it.

    Returns the picture shape. The contrast reading (§10) is taken off the pixels, so
    type that does not carry over the picture comes back as unreadable type: look at the
    render.
    """
    from pptx.util import Inches

    path = Path(image)
    if not path.is_file():
        raise ValueError(f"backdrop wants a picture file, and {image!r} is not one")
    share = _alpha_share(alpha)
    plane_share = _scrim_share(scrim)
    if box is None:
        left, top = 0.0, 0.0
        width, height = _canvas_of(slide)
    else:
        left, top, width, height = _as_size(box, "a box for backdrop")
    picture = slide.shapes.add_picture(str(path), Inches(left), Inches(top), Inches(width), Inches(height))
    picture.name = "backdrop"
    _fit(picture, path, "cover", anchor, trim, zoom)
    _wash(picture._element.find(f"{{{_P}}}blipFill/{{{_A}}}blip"), share)
    # Behind everything: the two bookkeeping children of the shape tree come first, and
    # the first drawn shape after them is the lowest on the page.
    tree = picture._element.getparent()
    tree.remove(picture._element)
    tree.insert(2, picture._element)
    if plane_share:
        dark, light = _ink_and_light(slide)
        plane = _plane(slide, left, top, width, height, ink if ink is not None else dark, plane_share)
        plane.name = "backdrop scrim"
        tree.remove(plane._element)
        tree.insert(3, plane._element)
        if light_type:
            _lighten_type(slide, light, keep={picture._element, plane._element}, within=(left, top, width, height))
    return picture


def _scrim_share(scrim) -> float:
    """`scrim` as the plane's share, 0 for none; refusing what is not a share."""
    if scrim is None:
        return 0.0
    try:
        share = float(scrim)
    except (TypeError, ValueError):
        raise ValueError(f"scrim is the plane's share of ink over the picture, 0..1, not {scrim!r}") from None
    if not (0.0 <= share <= 1.0):
        raise ValueError(f"scrim={share:g} is outside 0..1")
    return share


def _plane(slide, left: float, top: float, width: float, height: float, colour, share: float):
    """A rectangle of `colour` at `share` opacity, no outline, no shadow, no text."""
    from lxml import etree
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    plane = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
    plane.fill.solid()
    plane.fill.fore_color.rgb = _rgb_of(colour)
    plane.line.fill.background()
    plane.shadow.inherit = False
    colour_node = plane._element.spPr.find(f"{{{_A}}}solidFill/{{{_A}}}srgbClr")
    if colour_node is not None and share < 1.0:
        opacity = etree.SubElement(colour_node, f"{{{_A}}}alpha")
        opacity.set("val", str(int(round(share * 100000))))
    if plane.has_text_frame:
        plane.text_frame.text = ""
    return plane


def _ink_and_light(slide) -> tuple[str, str]:
    """The theme's dark and light colours (`dk1`, `lt1`), or black and white without a theme."""
    from raven_ppt.services.template.inventory import _theme_colours, _theme_root

    try:
        scheme = dict(_theme_colours(_theme_root(slide.part.package.presentation_part.presentation)))
    except Exception:  # noqa: BLE001 -- a slide outside a package has no theme to read
        scheme = {}
    return scheme.get("dk1", "#111111"), scheme.get("lt1", "#FFFFFF")


def _lighten_type(slide, colour, *, keep, within=None) -> None:
    """Every run of type under the plane set to `colour`, except runs in a saturated accent.

    The scrim turns the page dark where it lies, and the template set its type for a
    light page. A run the author or the template already coloured with an accent -- a
    kicker, a numeral -- is the one thing the page says with colour, so it stays. `within`
    is the plane, (left, top, width, height) in inches: a shape that does not reach it
    sits on the page's own light ground, where light type would vanish -- a boxed
    photograph across the lower half turned a title at the top white. A grouped
    shape's frame is in its group's child space, so it is carried to page space
    through the group's transform before the test (`_into_group`).
    """
    from pptx.util import Inches

    light = _rgb_of(colour)
    plane = None
    if within is not None:
        left, top, width, height = within
        plane = (Inches(left), Inches(top), Inches(left + width), Inches(top + height))
    pending = [(shape, _PAGE_SPACE) for shape in slide.shapes if shape._element not in keep]
    while pending:
        shape, place = pending.pop()
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            # Never pruned on its own frame: that frame is the untransformed one, and a
            # turned group lies somewhere else on the page. Its children are judged
            # one by one, each carried through the turn; the group carries no type.
            inside = _into_group(shape, place)
            pending.extend((child, inside) for child in shape.shapes)
            continue
        if plane is not None and not _reaches(shape, plane, place):
            continue
        if not getattr(shape, "has_text_frame", False) or not shape.has_text_frame:
            continue
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                if not run.text.strip():
                    continue
                current = _run_rgb(run)
                if current is not None and _saturated(current):
                    continue
                run.font.color.rgb = light


#: Page space itself: a frame read off a top-level shape needs no carrying. An affine
#: map (a, b, c, d, e, f): (x, y) -> (a*x + b*y + e, c*x + d*y + f).
_PAGE_SPACE = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _then(outer, inner):
    """The affine map that applies `inner` first and `outer` after."""
    a1, b1, c1, d1, e1, f1 = inner
    a2, b2, c2, d2, e2, f2 = outer
    return (
        a2 * a1 + b2 * c1,
        a2 * b1 + b2 * d1,
        c2 * a1 + d2 * c1,
        c2 * b1 + d2 * d1,
        a2 * e1 + b2 * f1 + e2,
        c2 * e1 + d2 * f1 + f2,
    )


def _into_group(group, place):
    """The map from `group`'s child space to the page, given the map from its own.

    A group draws its children in a space of its own (`a:chOff`/`a:chExt`), places that
    space in its frame (`a:off`/`a:ext`), then mirrors (`flipH`/`flipV`) and turns
    (`rot`, in 60000ths of a degree, clockwise) the frame about its centre -- so a
    child's frame says nothing about the page until all of it is applied. A caption
    grouped at page (1, 5) read as its child-space numbers and stayed dark over a
    lower-half scrim; a mirrored group put its text on the other side of the page from
    where the numbers said. A group without a transform, or with a degenerate child
    extent, carries its parent's map unchanged.
    """
    import math

    try:
        xfrm = group._element.grpSpPr.xfrm
        off, ext, choff, chext = xfrm.off, xfrm.ext, xfrm.chOff, xfrm.chExt
    except AttributeError:
        return place
    if None in (off, ext, choff, chext) or not chext.cx or not chext.cy:
        return place
    kx, ky = ext.cx / chext.cx, ext.cy / chext.cy
    local = (kx, 0.0, 0.0, ky, off.x - choff.x * kx, off.y - choff.y * ky)
    cx, cy = off.x + ext.cx / 2, off.y + ext.cy / 2
    if xfrm.get("flipH") in ("1", "true"):
        local = _then((-1.0, 0.0, 0.0, 1.0, 2 * cx, 0.0), local)
    if xfrm.get("flipV") in ("1", "true"):
        local = _then((1.0, 0.0, 0.0, -1.0, 0.0, 2 * cy), local)
    try:
        turn = int(xfrm.get("rot") or 0)
    except ValueError:
        turn = 0
    if turn:
        theta = math.radians(turn / 60000)
        cos, sin = math.cos(theta), math.sin(theta)
        local = _then((cos, -sin, sin, cos, cx - cx * cos + cy * sin, cy - cx * sin - cy * cos), local)
    return _then(place, local)


def _reaches(shape, plane, place=_PAGE_SPACE) -> bool:
    """Whether the shape's frame, carried to page space by `place`, overlaps `plane`.

    `plane` is (left, top, right, bottom) in EMU; the frame is the box around its four
    carried corners, which is what a turned frame occupies. A shape with no frame of
    its own -- a placeholder the layout never positioned -- is taken to reach it: the
    page-sized default is the case the reading was made for.
    """
    left, top, width, height = (getattr(shape, name, None) for name in ("left", "top", "width", "height"))
    if None in (left, top, width, height):
        return True
    a, b, c, d, e, f = place
    corners = [
        (a * x + b * y + e, c * x + d * y + f)
        for x, y in ((left, top), (left + width, top), (left, top + height), (left + width, top + height))
    ]
    xs, ys = [x for x, _ in corners], [y for _, y in corners]
    return min(xs) < plane[2] and max(xs) > plane[0] and min(ys) < plane[3] and max(ys) > plane[1]


def _run_rgb(run):
    """A run's explicit RGB colour, or None for a theme colour or none at all."""
    try:
        colour = run.font.color
        if colour is None or colour.type is None:
            return None
        return colour.rgb
    except (AttributeError, TypeError):
        return None


def _saturated(rgb) -> bool:
    """Whether a colour reads as an accent rather than as ink or paper."""
    channels = (int(str(rgb)[0:2], 16), int(str(rgb)[2:4], 16), int(str(rgb)[4:6], 16))
    return max(channels) - min(channels) >= 70 and max(channels) >= 110


def wash(shape, alpha: float):
    """Set a picture's transparency: `alpha` is the picture's share of itself.

    Any picture on the page -- a frame the template drew, one `replace_picture`
    filled, one `add_picture` placed, a rounded panel filled with a photograph -- and
    the same share `backdrop` and `replace_picture(alpha=...)` take: 1 is the picture as
    it is (under a scrim, the way a cover carries one), 0.12 or under is texture behind
    the page's own ground, and the shares between read as fog under type. Written
    into the blip's own `alphaModFix`, replacing whatever wash the picture carried, so
    nothing is added to the page and the frame keeps its crop, border and place. Returns
    the shape. A shape with no picture in it is refused: a solid fill has its own
    transparency and this is not it.
    """
    fill = _blip_fill(shape)
    blip = fill.find(f"{{{_A}}}blip") if fill is not None else None
    if blip is None:
        raise ValueError(
            f"wash wants a picture, and {getattr(shape, 'name', shape)!r} shows none -- it takes a picture frame "
            "or a shape filled with one; a solid fill is not washed this way"
        )
    _wash(blip, alpha)
    return shape


def _alpha_share(alpha) -> float:
    """`alpha` as a share of the picture, refusing what is not a wash."""
    try:
        share = float(alpha)
    except (TypeError, ValueError):
        raise ValueError(f"alpha is the picture's share of itself, between 0 and 1, not {alpha!r}") from None
    if not (BACKDROP_ALPHA_MIN <= share <= 1.0):
        raise ValueError(
            f"alpha={share:g} is outside {BACKDROP_ALPHA_MIN:g}..1: 1 is the photograph as it is (under a scrim, "
            f"the way a cover carries one), {BACKDROP_TEXTURE_MAX:g} or under is texture behind the page's own ground"
        )
    return share


def _wash(blip, alpha) -> None:
    """Set a blip's transparency (`alphaModFix`), replacing any it carried."""
    from lxml import etree

    share = _alpha_share(alpha)
    for stale in blip.findall(f"{{{_A}}}alphaModFix"):
        blip.remove(stale)
    fix = etree.Element(f"{{{_A}}}alphaModFix")
    fix.set("amt", str(int(round(share * 100000))))
    # `alphaModFix` precedes `extLst` in a blip's children; anything else already
    # there is an effect the author did not ask for.
    extension = blip.find(f"{{{_A}}}extLst")
    if extension is not None:
        extension.addprevious(fix)
    else:
        blip.append(fix)


def _canvas_of(slide) -> tuple[float, float]:
    """The page's (width, height) in inches, off the presentation the slide belongs to."""
    try:
        presentation = slide.part.package.presentation_part.presentation
        return (presentation.slide_width / 914400, presentation.slide_height / 914400)
    except Exception:  # noqa: BLE001 -- a slide outside a package answers the default canvas
        return (13.333, 7.5)


def replace_text(target, text: str, new: str | None = None) -> None:
    """Write new words into a shape, keeping how the template set them.

    Two forms, because both are what an author reaches for:

        replace_text(shape, "新文字")                # this shape
        replace_text(slide, "单击此处添加标题", "新文字")  # whatever on this page holds that

    Finding the shape is the tedious half of the operation and the page already knows
    how, so the second form takes the page: without it, `replace_text(slide, old, new)`
    answers `takes 2 positional arguments but 3 were given`. Groups are searched, since
    that is where a template keeps its text.

    Assigning to `.text` drops every run property, so a heading returns at body size in
    body colour -- the page keeps its geometry and loses its typography, which reads as
    a worse bug than a missing page because it looks deliberate.

    **One word in the accent, on a cloned page.** `text` may be a sequence of
    `ppt_layout.Run` instead of a string, and then each piece is set as its own run:

        replace_text(shape, [Run("\u8bbf\u5ba2\u4e2d\u7ea6 "), Run("84%", bold=True, colour=ACCENT_INK), Run(" \u5230\u8bbf\u8fc7")])

    Whatever a piece does not state is the template's, because every piece is a copy of
    the run the template put there -- so the line keeps its face, its size and its
    colour and one word of it does not. Without this a cloned page could not emphasise
    anything: the plain-string path puts the whole line in run 0 and deletes the rest,
    and one run carries one colour. Pass a list of sequences for several paragraphs.

    **The box keeps the geometry the template drew it with, and says so when the new
    copy needs more of it.** Nothing here moves, grows or re-sizes a frame: longer copy
    wraps to more lines in the same box, and where the box cannot show them the program
    is told at the end of its run -- how many lines the box shows, how many this copy
    needs, how many the copy it replaced took, and where the overflow goes (down the
    page, up off the top of it where the frame is bottom-anchored, or into a type size
    a step under the template's where the frame shrinks its text to fit). Answer it with
    fewer words or with `place`; the deck's own checks report the consequences after the
    render, which is a whole deck later.
    """
    shape = target
    if new is not None:
        every = list(_all_shapes(target.shapes))
        frames = [s for s in every if getattr(s, "has_text_frame", False)]
        shape = _pick(text, frames, frames)
        if shape is None:
            raise KeyError(
                f"no text on this page matches {text!r}."
                + _near_says([(text, frames)], every)
                + _elsewhere([(text, frames)], _bound_template())
                + " The page holds: "
                + "; ".join(f"[{index}] {_describe(s)}" for index, s in enumerate(every, start=1))
            )
        text = new
    if not getattr(shape, "has_text_frame", False):
        raise ValueError("that shape holds no text")
    frame = shape.text_frame
    _ensure_paragraph(frame)
    # `\x0b` counts as a break as much as `\n` does: it is what PowerPoint's format uses
    # for a soft one, so it is what `.text` hands back from a template's own placeholder
    # and what an author writes after reading one. Passed through, XML cannot carry it
    # and python-pptx spells it -- a delivered cover printed "EverMind AI_x000B_给 AI
    # 智能体" at title size.
    # A run sequence is one paragraph by construction: the breaks a string carries are
    # what splits it, and a sequence of runs states its pieces instead. A caller wanting
    # two emphasised paragraphs calls this twice, or passes a list of sequences.
    # Before the write, because the copy the template put here is the measurement's
    # other half and the next lines are what remove it. Judged at the end of the
    # program rather than now -- see `_say_what_did_not_fit`.
    _record_fit(shape, frame.text)
    lines = _paragraphs_of(text)
    paragraphs = frame.paragraphs
    for index, line in enumerate(lines):
        if index < len(paragraphs):
            _write(paragraphs[index], line)
        else:
            # A new paragraph inherits the last one's properties, which is the
            # template's list style rather than a default.
            added = copy.deepcopy(paragraphs[-1]._p)
            frame._txBody.append(added)
            _write(frame.paragraphs[-1], line)
    for extra in list(frame.paragraphs)[len(lines) :]:
        extra._p.getparent().remove(extra._p)


def _paragraphs_of(text):
    """`text` as the paragraphs to write: strings split on breaks, sequences kept.

    Three shapes arrive here and all three are what an author means by "these
    lines": a string with breaks in it; a list of strings, one per paragraph; a list
    of run sequences, one emphasised paragraph each. A live program wrote
    `[["选址评估"], ["六维模型"]]` -- two paragraphs, each a list holding one plain
    string -- and the old dispatch, which knew only strings and `Run` sequences, fell
    through to `str.replace` on a list and crashed the build. A sequence of plain
    strings inside a paragraph is that paragraph's text, joined.
    """
    if isinstance(text, str):
        return text.replace("\x0b", "\n").split("\n")
    if _emphasis(text) is not None:
        return [text]
    try:
        items = list(text)
    except TypeError:
        raise TypeError(
            f"replace_text takes a string, a list of strings (one per paragraph) or a list of Run sequences, "
            f"not {type(text).__name__}"
        ) from None
    lines = []
    for item in items:
        if isinstance(item, str):
            lines.extend(item.replace("\x0b", "\n").split("\n"))
        elif _emphasis(item) is not None:
            lines.append(item)
        elif isinstance(item, (list, tuple)) and all(isinstance(piece, str) for piece in item):
            lines.append("".join(item))
        else:
            raise TypeError(
                f"replace_text got {item!r} inside the list: each paragraph is a string, a list of strings, "
                f"or a sequence of Run"
            )
    return lines or [""]


def _emphasis(line):
    """`line` as the runs to set, or None when it is a plain string.

    A cloned page could not emphasise a word. `_write` puts the whole line into run 0
    and deletes the rest, so a paragraph coming out of `replace_text` holds exactly one
    run and one run carries one colour -- and twelve of one fifteen-page deck's pages
    were clones. The author had written `colour=ACCENT_INK` in five places and none of
    it reached the page.

    The pairs are `ppt_layout.Run`'s fields by name rather than by import: this module
    is projected as its own source beside the author's program and may not import a
    sibling projection, and duck-typing the four names is cheaper than a third spelling
    of the same tuple.
    """
    if isinstance(line, str):
        return None
    try:
        pieces = list(line)
    except TypeError:
        return None
    if not pieces or any(not hasattr(piece, "text") for piece in pieces):
        return None
    return pieces


def _restyle(run, piece, colour_of) -> None:
    """`piece`'s own size, weight and colour over whatever the template set.

    Only what the piece states. Everything else is the template's, which is the whole
    point: the page keeps its typography and gains one emphasised word.
    """
    from pptx.util import Pt

    if getattr(piece, "size", None) is not None:
        run.font.size = Pt(float(piece.size))
    if getattr(piece, "bold", None) is not None:
        run.font.bold = bool(piece.bold)
    stated = getattr(piece, "colour", None)
    if stated is not None:
        run.font.color.rgb = colour_of(stated)


def _rgb_of(value):
    """A colour as python-pptx wants it, from the spelling the reference uses."""
    from pptx.dml.color import RGBColor

    if isinstance(value, RGBColor):
        return value
    text = str(value).lstrip("#")
    if len(text) != 6:
        raise ValueError(f"a colour is #RRGGBB, not {value!r}")
    return RGBColor(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _ensure_paragraph(frame) -> None:
    """A frame with no `<a:p>` at all given the one the format says it must have.

    `<p:txBody>` is `bodyPr, lstStyle?, p+` -- a text body carries at least one
    paragraph -- but eight shapes across the bundled templates carry none:
    `black_circuit_tech_launch` page 25 and `green_aurora_tech_trends` page 20 each
    hold four rounded rectangles named `Text` whose whole body is an `<a:bodyPr/>` and
    an empty `<a:lstStyle/>`, byte for byte the same shape on both pages down to its
    creation id. They are the two pages' card panels: the dark plate a pill heading and
    a paragraph of body copy sit on top of. Writing into one went looking for the last
    paragraph to copy the template's list style from, found no last paragraph, and
    `replace_text` raised `IndexError` -- and a build is one program, so the raise took
    every page after it with it.

    Skipping the frame instead would be worse: an author asked for words and would get
    silence on a page that still looks finished. The paragraph is added bare, with no
    `pPr` and no run, because nothing here knows better than the file does and a run
    invented with properties would carry this function's typography instead of the
    template's. What a bare paragraph inherits is the file's own answer for a shape
    that states nothing: the presentation's `<p:defaultTextStyle>` first level,
    `sz="1800"` filled `tx1`, over the face and colour the shape's own
    `<p:style><a:fontRef idx="minor"><a:schemeClr val="lt1"/>` names. Not a layout or a
    master placeholder -- these are plain autoshapes inside a group and inherit from no
    placeholder at all. Read back off the render of both pages: 18.0pt, `#FFFFFF`, the
    Arial substitute, against the `#2F2F2F` plate, beside the template's own 14pt body
    copy. Which is one step larger than that body copy, and centred where it already
    sits: an author who writes into the plate rather than into the box on top of it
    gets words over words, and the render's `word_collision` check is what says so.
    Writing the frame's own (empty) copy back changes not one pixel of either page.
    """
    if not frame.paragraphs:
        frame._txBody.add_p()


def _write(paragraph, line) -> None:
    """One paragraph's words replaced, and nothing of the old line left behind.

    A template's soft breaks belong to its placeholder, not to what replaces it. The
    prompt on one cover ran over two lines -- run, `<a:br/>`, run -- and replacing it
    dropped the second run and kept the break, so the new title carried a trailing
    empty line and sat a line high inside a box that had grown one line taller than
    anything visible in it. Real line breaks in new copy arrive as separate lines and
    become separate paragraphs, so nothing here needs an `<a:br/>` to survive.
    """
    for brk in paragraph._p.findall(f"{{{_A}}}br"):
        paragraph._p.remove(brk)
    runs = paragraph.runs
    pieces = _emphasis(line)
    if not runs:
        if pieces is None:
            paragraph.text = line
            return
        paragraph.text = "".join(str(piece.text) for piece in pieces)
        runs = paragraph.runs
        if runs:
            _restyle(runs[0], pieces[0], _rgb_of)
        return
    if pieces is None:
        runs[0].text = line
        for extra in runs[1:]:
            extra._r.getparent().remove(extra._r)
        return
    # Run 0 is the template's carrier, so every piece is a copy of it with only what
    # the piece states overridden. Copied rather than added bare: a run python-pptx
    # adds has no properties at all, and the line would come back at body size in body
    # colour -- the page keeping its geometry and losing its typography, which is the
    # failure `replace_text` was written to avoid in the first place.
    carrier = runs[0]._r
    made = []
    for piece in pieces:
        fresh = copy.deepcopy(carrier)
        carrier.addprevious(fresh)
        made.append(fresh)
    for stale in list(paragraph.runs):
        if stale._r not in made:
            stale._r.getparent().remove(stale._r)
    for run, piece in zip(paragraph.runs, pieces):
        run.text = str(piece.text)
        _restyle(run, piece, _rgb_of)


# How much wider the width measurer reads a line than the render draws it. The
# measurer never reads a string narrower than either FreeType or the export dialect
# will draw it, which is the right bias for a check run over a finished file and
# makes every reading here an upper bound. Measured two ways: over the ten bundled
# templates' own renders, 182 single-line boxes with no autofit above them, the
# overshoot is 1.13 at the median and 1.27 at the 99th percentile; over nine
# unshrunk English lines off a delivered deck it runs 1.10 to 1.30. So a prediction
# that spoke inside this band would be reporting the measurer and not the page --
# `wrapped_labels` reached the same conclusion from the other side and left the
# render to decide which of its candidates had really broken. Before the deck is
# built there is no render, so the copy is wrapped at a column this much wider than
# the real one and only an overflow that survives that is said out loud.
MEASURED_WIDTH_OVERSHOOT = 1.35


def _inherited_frames(shape):
    """This shape, then the layout placeholder it inherits from, then the master's."""
    yield shape
    try:
        if not shape.is_placeholder:
            return
        index = shape.placeholder_format.idx
        slide = shape.part.slide
        for holder in (slide.slide_layout, slide.slide_layout.slide_master):
            for candidate in holder.placeholders:
                if candidate.placeholder_format.idx == index:
                    yield candidate
                    break
    except Exception:  # noqa: BLE001 -- a shape off a slide inherits nothing reachable
        return


def _anchor_of(shape) -> str:
    """Where the copy is anchored in its frame: the shape's answer, else inherited.

    The anchor is not on the shape that carries it. Every text frame on the closing
    page of `black_circuit_tech_launch` answers `vertical_anchor is None`;
    `anchor="b"` is written on the layout's title placeholder, and that one
    attribute is the difference between copy that grows down the page and copy that
    grows up off the top of it. Read from the shape alone, a four-line 72pt headline
    is reported as running low -- and the page that headline actually made had its
    first line cut off by the top edge of the slide.
    """
    for holder in _inherited_frames(shape):
        properties = holder.text_frame._txBody.find(f"{{{_A}}}bodyPr")
        if properties is not None and properties.get("anchor") is not None:
            return properties.get("anchor")
    return "t"


def _shrinks_to_fit(shape) -> bool:
    """Whether this frame will really shrink its type rather than spill.

    The shape's own `bodyPr` and not the layout's, because that is the difference
    the renderer makes. Both are measured on one file: the card labels of
    `black_circuit_tech_launch` carry `normAutofit` on the shapes themselves, and a
    live deck's copy in them came back at 17pt where the template set 20pt. Its
    closing-page headline inherits `normAutofit` from the layout and nothing else,
    and a four-line replacement was drawn at the full 72pt and clipped by the top
    edge of the slide. So an inherited autofit is not a shrink, and reading the
    inheritance chain here would have promised one on the page that lost a line.
    """
    properties = shape.text_frame._txBody.find(f"{{{_A}}}bodyPr")
    return properties is not None and properties.find(f"{{{_A}}}normAutofit") is not None


def _grows_itself(shape) -> bool:
    """Whether this frame resizes itself to its copy instead of keeping its height.

    The third outcome, and the one neither of the other two describes. `spAutoFit` is
    16% of the bundled templates' boxes and the majority behaviour on
    `gold_panel_year_end_summary` -- 109 of its 177, with no `normAutofit` anywhere in
    the file -- so a frame read as shrinking-or-spilling is read wrongly there six
    times out of ten.

    The shape's own `bodyPr`, for the same reason `_shrinks_to_fit` reads it there.
    """
    properties = shape.text_frame._txBody.find(f"{{{_A}}}bodyPr")
    return properties is not None and properties.find(f"{{{_A}}}spAutoFit") is not None


def _plain_lines(text) -> list[str]:
    """A string, a list of them, or run sequences, as the plain lines they will set."""
    lines = []
    for line in _paragraphs_of(text):
        pieces = _emphasis(line)
        lines.append("".join(str(piece.text) for piece in pieces) if pieces is not None else str(line))
    return lines


def _shortened(text: str, limit: int = 34) -> str:
    """One line of `text`, short enough to sit inside a sentence."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "..."


def _copy_fit(shape, old: str, new) -> dict | None:
    """What the box shows, what `new` needs in it, and how far past the box that is.

    None when there is nothing to say: no run states a size (the case `overset_copy`
    skips too, and for the same reason -- a size read off the master is not the size
    a run will paint), wrapping is off, the box has no room, or the copy fits.

    The prototype's own copy is the second half of the answer and the half a check
    run over the finished file cannot have. A box's height in lines is arithmetic on
    an assumed line-height factor; how many lines the template's designer put in it
    is a fact. Where the two disagree the fact wins, so a box the template filled
    past what the arithmetic allows still counts as holding what it was drawn
    holding -- and writing the template's own copy back into the template's own box
    can never report anything, whatever the arithmetic says.
    """
    from raven_ppt.services.assets.text_metrics import measurer
    from raven_ppt.services.measure.fit import LineOverflowError, capacity_lines, needed_height_px, wrap

    frame = shape.text_frame
    if frame.word_wrap is False:
        return None
    sizes = [
        run.font.size.pt
        for para in frame.paragraphs
        for run in para.runs
        if run.font.size is not None and run.text.strip()
    ]
    if not sizes:
        return None
    size_pt = max(sizes)
    width_in = ((shape.width or 0) - (frame.margin_left or 0) - (frame.margin_right or 0)) / EMU_PER_INCH
    height_in = ((shape.height or 0) - (frame.margin_top or 0) - (frame.margin_bottom or 0)) / EMU_PER_INCH
    if width_in <= 0 or height_in <= 0:
        return None
    words = "\n".join(_plain_lines(new))
    if not words.strip():
        return None
    bold = any(run.font.bold for para in frame.paragraphs for run in para.runs)
    font_px = int(round(size_pt * 96 / 72))
    gauge = measurer()

    def rows(text: str, room_in: float) -> int:
        try:
            return len(wrap(text.replace("\x0b", "\n"), room_in * 96, font_px, bold=bold, measurer=gauge))
        except LineOverflowError:
            return 1

    # Both sides wrapped at a column wider than the real one: the number reported is
    # then one the measurer's own overshoot cannot have invented, and the prototype's
    # own copy measured the same way makes writing it back into its own box silent by
    # construction rather than by a threshold that happens to hold.
    room_in = width_in * MEASURED_WIDTH_OVERSHOOT
    took = rows(old, room_in)
    shows = max(capacity_lines(height_in * 96, font_px), took)
    needs = rows(words, room_in)
    if needs <= shows:
        return None
    return {
        "size_pt": size_pt,
        "width_in": width_in,
        "height_in": height_in,
        "needs": needs,
        "shows": shows,
        "took": took,
        "needs_in": needed_height_px(needs, font_px) / 96,
    }


def _growth_note(shape, fit: dict) -> str:
    """Where the lines the box cannot show end up, on this page, in inches.

    Three outcomes, and the author can act on none of them without being told which
    one this box has. A frame that shrinks its text to fit keeps its geometry and
    loses the size the template set, so one box in a repeated row comes back smaller
    than its neighbours. Every other frame keeps its type, and then the anchor decides
    where the copy it cannot show goes: a top-anchored frame grows down, into whatever
    the template drew under it, and a bottom-anchored one grows *up*, at display size
    off the top of the canvas -- the case that destroys a page rather than spoiling it,
    and the one that reading the box's own geometry does not predict. The third
    outcome rides on those as a clause, because it changes what travels rather than
    where: a frame with `spAutoFit` takes its own fill and outline along.
    """
    if _shrinks_to_fit(shape):
        # How far it shrinks is the renderer's arithmetic and not worth predicting: a
        # first version put the scale at shows/needs and said "near 10pt" where the
        # render came back at 17pt. That it shrinks at all is the finding, because a
        # repeated row is read across and one card a step down is visible.
        return (
            "The frame shrinks its text to fit, so this box comes back under the "
            f"{fit['size_pt']:g}pt its neighbours keep."
        )
    _, top = page_position(shape)
    frame = shape.text_frame
    bottom = top + (shape.height or 0) / EMU_PER_INCH
    canvas_height = _canvas_of_shape(shape)[1]
    anchor = _anchor_of(shape)
    # The third outcome, and a clause rather than a branch of its own: rendered, a
    # `spAutoFit` frame grows in exactly the direction its anchor says, so the sentences
    # below still hold and this adds the part they cannot. Three boxes drawn 0.50in tall
    # at 3.00in and filled with copy needing 1.20in came back as fills of 3.00-4.20
    # anchored top, 2.30-3.50 anchored bottom, and centred on the box's own centre. So
    # the copy is not clipped by its box -- but a card's own background travels with it,
    # which is a different edit from shortening the copy, and the page edge is no kinder
    # to a frame that grew than to copy that spilled.
    grown = (
        " The frame grows itself rather than clipping, so its own fill and outline travel with the copy."
        if _grows_itself(shape)
        else ""
    )
    if anchor == "b":
        starts = bottom - (frame.margin_bottom or 0) / EMU_PER_INCH - fit["needs_in"]
        past = (
            f"{-starts:.2f}in above the top of the slide, and its first line is cut off"
            if starts < 0
            else f"{top - starts:.2f}in above the frame, over whatever is drawn there"
        )
        return f"Bottom-anchored: it grows upward, beginning {past}.{grown}"
    if anchor == "ctr":
        return f"Centre-anchored, so it grows {(fit['needs_in'] - fit['height_in']) / 2:.2f}in past each edge.{grown}"
    ends = top + (frame.margin_top or 0) / EMU_PER_INCH + fit["needs_in"]
    past = (
        f"{ends - canvas_height:.2f}in past the bottom edge of the slide"
        if ends > canvas_height
        else f"{ends - bottom:.2f}in below the frame, over whatever is drawn there"
    )
    return f"It grows downward: the copy ends {past}.{grown}"


def _canvas_of_shape(shape) -> tuple[float, float]:
    """The page's (width, height) in inches, off the presentation this shape sits on."""
    try:
        return _canvas_of(shape.part.slide)
    except Exception:  # noqa: BLE001 -- a shape off a slide answers the default canvas
        return (13.333, 7.5)


# Every box this program has written into, with the copy the template had in it.
# Kept until the program ends rather than judged as it is written, because a page is
# not finished when its words go in: a live cover wrote three lines of headline and
# then set the runs to 44pt, and measured at the 72pt in force during the write it
# reported a first line cut off by the top of a slide the render shows to be fine.
# The copy that ships is the copy at the end of the program, and so is the type size,
# and so is the frame -- an author may `place` it wider afterwards.
_WRITTEN: list = []


def _record_fit(shape, old: str) -> None:
    """Remember this box and the copy the template put in it, once."""
    for held, _ in _WRITTEN:
        if held._element is shape._element:
            # The first write is the one that had the template's own copy in front of
            # it; a second write over the author's own first draft measures nothing.
            return
    _WRITTEN.append((shape, old))


def _say_what_did_not_fit() -> None:
    """At the end of the program, the boxes whose copy the page cannot show.

    Registered with `atexit` because there is no other end: this module is projected
    beside the author's program as a file it imports, the program's last line is its
    own `prs.save`, and nothing here is called again afterwards.

    Drains what it reports, so calling it twice does not say everything twice.
    """
    written, _WRITTEN[:] = list(_WRITTEN), []
    for shape, old in written:
        try:
            if shape._element.getparent() is None:
                # The block that drew this page raised and the runner dropped its
                # slides, standing a placeholder in for them.
                continue
            _report_fit(shape, old, shape.text_frame.text)
        except Exception:  # noqa: BLE001 -- a measurement is not worth a build over
            continue


def _report_fit(shape, old: str, new) -> None:
    """Say what this box holds when the copy in it needs more than that.

    Said at all because here is the only place both halves are known: the box, and
    the copy the template had in it, which the first write removes. The checks that
    run over the built deck see the overflow and its consequences -- copy outside its
    box, words over words, a rule struck through, one card's type a step smaller than
    the four beside it -- but they see them after a whole deck has been written and
    rendered, and by then the author is answering fourteen pages at once.
    """
    try:
        fit = _copy_fit(shape, old, new)
    except Exception:  # noqa: BLE001 -- a measurement is not worth losing a page over
        return
    if fit is None:
        return
    # The page rather than the call site: an author writes its pages through helpers of
    # its own, and `stacklevel` then lands inside `card_page` for nine of the fourteen.
    # A live build's warnings all pointed at one line of one helper.
    page = _page_of(shape)
    where = f"page {page}: " if page else ""
    warnings.warn(
        f"{where}{_shortened(' '.join(_plain_lines(new)))!r} needs {fit['needs']} lines at "
        f"{fit['size_pt']:g}pt in this {fit['width_in']:.2f}in column and the box shows {fit['shows']} -- "
        f"the copy it replaces took {fit['took']}. "
        + _growth_note(shape, fit)
        + " Written as asked; fewer words, or place() the frame first.",
        stacklevel=3,
    )


def _page_of(shape) -> int | None:
    """This shape's page number, counting from 1, or None off a slide."""
    try:
        slide = shape.part.slide
        return list(slide.part.package.presentation_part.presentation.slides).index(slide) + 1
    except Exception:  # noqa: BLE001 -- a shape off a slide has no page to name
        return None


atexit.register(_say_what_did_not_fit)


def drop_shape(shape) -> None:
    """Remove an element the page does not need. The commonest edit after text.

    A shape on a layout is refused: it is the template's design, shared by every page
    on that layout, and a live cover deleted the whole of it to make room for a washed
    photograph. The picture in it is changed with `replace_picture(layout_pictures(slide)[0], ...)`.

    One shape. To make room for a chart or a panel of your own, `clear_region(slide,
    box)` takes every shape drawn in that box and says which ones those were -- the
    template's chart is never alone in the space it occupies.
    """
    if getattr(shape.part, "slide", None) is None:
        raise ValueError(
            f"{getattr(shape, 'name', shape)!r} is on a layout, not on the page: a layout's shapes are the "
            "template's design and every page on the layout shares them. Change the picture in it with "
            "`replace_picture(layout_pictures(slide)[0], image, 'cover')`; cover it on one page with a "
            "shape of the page's own; do not remove it"
        )
    shape._element.getparent().remove(shape._element)


class Cleared(tuple):
    """What `clear_region` took out, and what it decided to leave.

    A tuple of the descriptions it removed, so `len(...)` is the count and printing
    it reads as a list, with `left_standing` beside it for the shapes that touch the
    box and stayed. Both halves are the point: the sweep this replaces removed things
    silently, and the author found out from the render three builds later.
    """

    # No __slots__: a variable-length builtin subtype cannot have non-empty slots, and
    # the two extra fields have to live somewhere.
    def __new__(cls, removed, left_standing=(), region=(0.0, 0.0, 0.0, 0.0)):
        made = super().__new__(cls, tuple(removed))
        made.left_standing = tuple(left_standing)
        made.region = tuple(region)
        return made

    def __str__(self) -> str:
        left, top, right, bottom = self.region
        said = f"cleared ({left:g}, {top:g})-({right:g}, {bottom:g}): "
        said += f"removed {len(self)} -- {'; '.join(self)}" if self else "nothing was in it"
        if self.left_standing:
            said += f". Still over it: {'; '.join(self.left_standing)}"
        return said


def clear_region(container, box, *, share: float = 0.5, keep=()) -> Cleared:
    """Make room in `box` for something of your own, and say what that cost.

    `drop_shape` for every shape `shapes_in` finds there. Drawing into a cloned page
    means clearing a space in it first, and until this existed there was no supported
    way to say that -- so a program wrote its own sweep, missed the arrows, the number
    labels and every connector, and drew two charts on top of them.

    `keep` spares a shape by the words it shows, in `shape_saying`'s prefix reading, or
    by the `# [n]` ordinal a template reference prints: the heading and the source line
    of a page are usually inside the region an author wants cleared under them.

    What it leaves alone: the shape the box sits inside, because on a template that is
    the card the chart was drawn in and it carries the arrangement the page was cloned
    for; anything in a group the template flipped or rotated, which has no position to
    compare against; and anything a layout owns, which `drop_shape` refuses.

    Loud on both sides. The return value names every shape removed, and a shape that
    overlaps the box at all and was not removed is named in `left_standing` and in a
    warning -- because "it looked clear and it was not" is the failure this replaces,
    and the number that decides it (`share`, half by default) is one an author may
    have to lower once it has seen what stayed.
    """
    region = _as_region(box)
    spare_words = tuple(str(word) for word in keep if not isinstance(word, int))
    spare_numbers = {int(word) for word in keep if isinstance(word, int)}
    # One walk, and the ordinals come off it. Two walks cannot be joined up: both
    # `_all_shapes` and lxml hand out a fresh proxy for the same node every time, so
    # neither the shape nor its element is the same object twice and every line of
    # this report came out numbered [0]. The numbers are the page's as the author
    # read it -- a reference's `# [n]` -- and after the clear they have shifted, so
    # the durable handle in each line is the position, which `shape_near` takes.
    walked = list(enumerate(_all_shapes(container.shapes), start=1))
    removed: list[str] = []
    spared: list[str] = []
    said_already: set[int] = set()
    for ordinal, shape in walked:
        if not _inside(shape, region, share):
            continue
        said = _named(shape, ordinal)
        text = _copy_of(shape)
        if ordinal in spare_numbers or (spare_words and any(text.startswith(word) for word in spare_words)):
            spared.append(said + " -- kept, you named it")
            said_already.add(ordinal)
            continue
        try:
            drop_shape(shape)
        except ValueError as refused:
            spared.append(f"{said} -- {refused}")
            said_already.add(ordinal)
            continue
        removed.append(said)
    # Everything that still lies over the box after the clear, so a region that only
    # looks empty says so here rather than in a render three builds later.
    # Deduplicated on the ordinal rather than on the sentence: a shape `keep` spared
    # is named with a reason on the end, and matching those strings listed it twice.
    over = [
        _named(shape, ordinal)
        for ordinal, shape in walked
        if ordinal not in said_already and _still_on_the_page(shape) and _touches(shape, region)
    ]
    left = tuple(spared) + tuple(over)
    if left:
        warnings.warn(
            f"clear_region({', '.join(f'{value:g}' for value in region)}) left {len(left)} shape(s) over that "
            f"box: {'; '.join(left)}. Look at the render; drawing there puts your content on top of them. "
            f"Lower `share` to take in what only partly overlaps, or name one and call drop_shape yourself",
            stacklevel=2,
        )
    return Cleared(removed, left, region)


def _named(shape, ordinal: int) -> str:
    """One line of the clear's report: the ordinal, what the shape is, where it is.

    The position and the size are the drawn ones, not the declared ones, because a
    shape inside a group declares neither and this line is read against a render.
    Its own kind word rather than `_describe`'s: that one reports a drawing as the
    size of the whole group it belongs to, and two different sizes on one line about
    one shape is a line nobody can act on.
    """
    words = _copy_of(shape)
    if getattr(shape, "has_chart", False):
        kind = "a chart"
    elif shape._element.tag.endswith("}cxnSp"):
        kind = "a connector"
    elif getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE or _blip_fill(shape) is not None:
        kind = "a picture"
    elif words:
        kind = f"text {words[:26]!r}" if len(words) <= 26 else f"text {words[:25] + chr(8230)!r}"
    elif _is_drawing(shape):
        kind = "a drawing"
    else:
        kind = "a shape"
    try:
        drawn = page_box(shape)
        return f"[{ordinal}] {kind} at ({drawn.x0:.2f}, {drawn.y0:.2f}) {drawn.w:.2f}x{drawn.h:.2f}in"
    except ValueError:
        return f"[{ordinal}] {kind}, inside a flipped or rotated group"


def _copy_of(shape) -> str:
    """The words a shape shows, or "" -- `keep` matches on these."""
    if not getattr(shape, "has_text_frame", False):
        return ""
    return " ".join(shape.text_frame.text.split())


# Below this much overlap a shape is not what the author is asking about: a template's
# full-width background band reaches into every region on the page, and naming it in
# every clear would bury the shape that does matter. Both readings, because a corner
# poking into a big box and a small box sitting on a corner are both real.
WORTH_SAYING = 0.1


def _still_on_the_page(shape) -> bool:
    """Whether this shape survived the clear -- the walk it came from predates it."""
    return shape._element.getparent() is not None


def _touches(shape, region) -> bool:
    """Whether this shape overlaps `region` by enough to be worth naming."""
    try:
        drawn = page_box(shape)
    except ValueError:
        return False
    left, top, right, bottom = region
    area = max(0.0, right - left) * max(0.0, bottom - top)
    if _share_of_shape(drawn, region) >= WORTH_SAYING:
        return True
    return bool(area) and _overlap(drawn, region) / area >= WORTH_SAYING


# Why the template's own naming is read before geometry, measured on the sixteen
# templates that shipped when this was written: `title=` came back holding "Presenter
# name" on two covers and nothing at all on a third, and `subtitle=` raised KeyError on
# ten of the sixteen covers and on every one of the sixteen section dividers.
# Re-measured on the twelve that ship now, `title` resolves on all 197 example pages
# and `subtitle` on 180, the seventeen it does not being closing pages carrying a
# presenter row and six pages between.
# How much of a template is repeating units, measured across 119 real templates and
# their 1563 example pages: every single template ships pages built this way, 75% of
# all example pages have at least one repeating unit, 77% have groups at all, and the
# remaining 23% are flat.
def units(container):
    """The repeating units on a page: sibling groups built the same way.

    Every template ships pages built this way. A card row, an agenda list, a set of
    steps -- each is one small group repeated, `[number, heading]` or
    `[number, body, heading]`. The most common shapes of it are 3x2, 2x3, 4x2 and
    4x3, and one template's agenda page is 8x2.

    Returns a list of runs, each run being the sibling groups that share a signature,
    in page order. A page with nothing repeating returns [], and on one of those
    `texts` is the whole story.
    """
    from collections import Counter

    runs = []

    def signature(group):
        # What the group holds, not the order it holds it in. A template's fourth
        # row is drawn with the same four shapes as the three above it and saved with
        # the icon before the label instead of after -- ordered, that row is not a
        # sibling, its texts are emptied with everyone else's and its icon is left
        # standing beside nothing. Two of the user's reference pages are built that way.
        return tuple(sorted(str(shape.shape_type) for shape in group.shapes))

    def walk(shapes):
        groups = [shape for shape in shapes if shape.shape_type == MSO_SHAPE_TYPE.GROUP]
        counts = Counter(signature(group) for group in groups)
        for wanted, count in counts.items():
            if count >= 2:
                runs.append([group for group in groups if signature(group) == wanted])
        for group in groups:
            walk(list(group.shapes))

    walk(list(container.shapes))
    return runs


# The distribution behind "no re-flow", measured over the 1222 runs in 119 templates:
# 29% a single row, 17% a single column, 25% a regular grid, and 29% following no grid
# at all. Two thirds have even spacing along their main axis.
def arrangement(run):
    """How a run of units is laid out: ("row"|"column"|"grid"|"irregular", rows, cols).

    A run that follows no grid at all -- staggered, fanned, around a circle -- is
    about as common as a single row, which is why nothing here re-flows a page by
    itself. Deleting two of eight
    units leaves a hole, and closing it means deciding whether four survivors become
    a centred row, a 2x2, or stay where they are -- a design decision on a regular
    grid and a guess on an irregular one, where a re-flow would destroy the
    arrangement the template was drawn with. So this reports, `place` moves, and the
    author decides. `boxes(run)` gives the geometry to decide from.
    """
    spots = boxes(run)
    if not spots:
        return ("irregular", 0, 0)
    xs = sorted({round(box[0], 2) for box in spots})
    ys = sorted({round(box[1], 2) for box in spots})
    if len(ys) == 1:
        return ("row", 1, len(spots))
    if len(xs) == 1:
        return ("column", len(spots), 1)
    if len(xs) * len(ys) == len(spots):
        return ("grid", len(ys), len(xs))
    return ("irregular", 0, 0)


def boxes(run):
    """Each unit's (left, top, width, height) in inches, in page order.

    A size, not a `ppt_layout.Box`: the same word names both rectangles, and what comes
    back here is what `place` takes, so `boxes(run)[0][2]` is a width and not a far edge.
    """
    found = []
    for unit in run:
        try:
            found.append((unit.left / 914400, unit.top / 914400, unit.width / 914400, unit.height / 914400))
        except TypeError:  # a unit with no geometry of its own
            return []
    return found


def _as_size(box, taken_by: str) -> tuple[float, float, float, float]:
    """`box` as (left, top, width, height) in inches, whichever rectangle was handed over.

    One word, two rectangles: a box here is a size, because that is what every
    python-pptx call takes, and a `ppt_layout.Box` is two corners. A Box says which of
    the two it is, so it is converted; four bare numbers cannot, so they stay a size.

    Recognised by its corners rather than by its type. This module is copied whole into
    the author's build directory as `ppt_template.py`, where `raven` is not importable
    and `ppt_layout` is a separate module object anyway, so an `isinstance` against the
    class here would be False for the very Box the author passed.

    EMU are refused rather than converted, because there is no rectangle they could be a
    size of: `box.pptx()` and a shape's own `.left` / `.width` are python-pptx lengths,
    and 12.6in reaches this as 11521440.
    """
    if all(hasattr(box, corner) for corner in ("x0", "y0", "x1", "y1")):
        return (float(box.x0), float(box.y0), float(box.x1 - box.x0), float(box.y1 - box.y0))
    wanted = f"{taken_by} is (left, top, width, height) in inches or a ppt_layout Box, not {box!r}"
    try:
        numbers = tuple(box)
    except TypeError:
        numbers = ()
    if len(numbers) != 4:
        raise ValueError(wanted)
    if any(hasattr(number, "emu") for number in numbers):
        inches = ", ".join(f"{float(number) / 914400:g}" for number in numbers)
        raise ValueError(
            f"{taken_by} is in inches and these are EMU -- as inches they read ({inches}). "
            "`box.pptx()` hands back python-pptx lengths, and so do a shape's own .left and .width: "
            "pass the ppt_layout Box itself, or divide each number by 914400"
        )
    try:
        return tuple(float(number) for number in numbers)
    except (TypeError, ValueError):
        raise ValueError(wanted) from None


def place(unit, box):
    """Move a unit to `box` -- (left, top, width, height) in inches -- children and all.

    A group carries its own child coordinate space, so moving the group moves what is
    inside it and nothing has to be recomputed per child. Scaling is proportional for
    the same reason: set the group's extent and PowerPoint maps the children onto it.

    This is the primitive re-flowing needs. Four units left of eight on a 2x4 grid
    become a centred row with four calls, and it is four calls rather than a flag
    because which layout the four should take is the author's decision -- see
    `arrangement`.

    Works on any shape, not only a unit: a picture frame moved to make room for a
    caption, a title nudged off the artwork behind it. A group is the interesting case
    only because moving one moves everything inside it.

    A `ppt_layout.Box` is accepted and converted, because unpacked as a size it was a
    wrong answer nothing reported: `place(unit, Box.corners(0.72, 1.24, 12.6, 6.7))` drew
    a 12.6x6.7in frame running off a 13.33x7.5in page, where those corners name an
    11.88x5.46in one. Same call, same four numbers, no error either time.
    """
    from pptx.util import Inches

    left, top, width, height = _as_size(box, "a box for place")
    # A shape inside a group keeps its numbers in the group's child space, which is
    # the page's only when the group was never resized. Written straight in, a page
    # box lands wherever the group's mapping sends it: a live program found `place`
    # "equivalent to assigning .top directly", measured the offset itself and wrote
    # its own conversion helper -- three rounds, and every later move went through
    # it. The box an author gives is on the page, so it is converted here.
    left, top, width, height = _to_child_space(unit, left, top, width, height)
    unit.left, unit.top = Inches(left), Inches(top)
    unit.width, unit.height = Inches(width), Inches(height)
    return unit


def _to_child_space(shape, left: float, top: float, width: float, height: float):
    """Page inches to the coordinate space `shape`'s own numbers are read in.

    The inverse of the walk `page_position` makes: each enclosing group maps its
    child extent onto its own, outermost first, so going in means undoing them
    outermost first as well. A shape at the top level comes back unchanged.
    """
    for (offset, extent), (child_offset, child_extent) in _group_chain(shape):
        sx = child_extent[0] / extent[0] if extent[0] else 1.0
        sy = child_extent[1] / extent[1] if extent[1] else 1.0
        left = child_offset[0] + (left - offset[0]) * sx
        top = child_offset[1] + (top - offset[1]) * sy
        width, height = width * sx, height * sy
    return left, top, width, height


def fill(run, items):
    """Fill a run of repeating units with `items`, and delete the ones left over.

    This is the operation a template page is *for*. A page ships eight agenda slots
    and the deck has six sections: the six get filled and the seventh and eighth are
    removed, group and all, rather than left holding an empty circle: writing "" into
    a slot empties its text and leaves its numbered bubble sitting there.

    Each item is either a list, positional over the unit's text shapes with `None`
    meaning "leave this one alone", or a dict keyed by the text a shape currently
    holds. A list shorter than the unit leaves the rest alone, the same as `None`
    would -- a unit often holds a shape the author has no opinion about. `items` longer than the run raises: a page with four slots cannot show
    six points, and quietly dropping two of them is the failure this is here to stop.

    One value is not taken literally: **an empty string written over a unit's number
    restates the number for its new position** rather than emptying it. A template's
    agenda numbers its slots 01 to 08, and a deck with six sections writing "" into
    that shape would otherwise leave six blank circles, which looks worse than the
    template it came from. The zero padding is the template's own: 01 stays two digits,
    1 stays one.

    A shape no entry addresses is left as the template wrote it, as everywhere else on
    a cloned page: what stands there is the template's example copy, and
    `placeholder_copy` refuses to publish it.

    Returns the shapes it wrote.
    """
    written = []
    slots = len(run)
    if len(items) > len(run):
        # Grown rather than refused. Ten builds across the measured runs died on this
        # refusal, nine of them one or two items over; the authors then wrote their own
        # `clone_panel` -- deepcopy the element, hang it on the tree, set a box -- which is
        # `add_unit` without the re-flow. A run that follows no grid still refuses, with
        # the slot counts the template menu now prints as the way out.
        try:
            run = list(run) + add_unit(run, len(items) - len(run))
        except ValueError as refusal:
            # Re-raised rather than chained, because `add_unit` is answering a question
            # about the page and the author asked one about their content: two live runs
            # read "it cannot take 5" off a page whose slot count they had not counted,
            # and had no way to see which five things they had handed over.
            raise ValueError(f"{refusal}. {_entries_given(items)}") from None
    # In the order a reader meets them, not the order the file stores them. The author
    # counts items off the render -- top row first, left to right -- and the file's
    # order is whatever the designer drew last. Measured on one reference page: the
    # unit's number box is stored third of three, after the heading and the body, so
    # a positional item ["72%", "heading", "body"] put the body in the 60pt number box
    # and the number in the heading slot. The same applies to which unit is first.
    spots = boxes(run)
    kind = arrangement(run)
    run = _reading_order(run)
    for position, (unit, item) in enumerate(zip(run, items), start=1):
        frames = _reading_order([s for s in _all_shapes(unit.shapes) if getattr(s, "has_text_frame", False)])
        if not isinstance(item, dict):
            # A frame the prototype left empty is not a slot: it is an icon's box or a
            # spacer, invisible on the page and uncountable from it. Counting it put a
            # live deck's four card headings into icon containers -- the author read two
            # slots off a card that reads as two, the unit held four frames with the
            # empty ones first and third, so both values landed one shape early and the
            # heading placeholder was never reached. `texts` and the index form still
            # see every frame; only this positional walk skips them, because it is the
            # only caller that asks the author to count.
            spoken = [shape for shape in frames if (shape.text_frame.text or "").strip()]
            # Only while some frame still holds words. An empty frame reads as a spacer
            # because the ones beside it hold text, so on a run whose every frame is
            # blank the filter has nothing to tell them apart by and would raise
            # "0 text shape(s)" against the count the author can see on the render.
            if spoken:
                frames = spoken
        if isinstance(item, dict):
            for key, value in item.items():
                shape = _pick(key, frames, frames)
                if shape is None:
                    raise KeyError(
                        f"no text in this unit matches {key!r}."
                        + _near_says([(key, frames)])
                        + " It holds "
                        + ", ".join(repr(_head(f)) for f in frames)
                    )
                replace_text(shape, value)
                written.append(shape)
            continue
        if len(item) > len(frames):
            # An empty string is a value with nothing in it -- the placeholder an author
            # writes for a number tile it means to leave alone -- and a list that outruns
            # the unit only by those is the unit's own list. Measured: `["", title, sub]`
            # against a two-frame unit ended a build on the refusal below, over a value
            # that would have written nothing.
            spare = [value for value in item if value != ""]
            if len(spare) < len(item) and len(spare) <= len(frames):
                item = spare
        if len(item) > len(frames):
            # Neither joining the extras onto the last slot nor cloning a slot for them is
            # this function's call to make: the first sets body copy at heading size, the
            # second guesses where the new shape goes, and both were tried and made the
            # page worse. What each unit holds is stated here so the author can choose --
            # a prototype with more slots, one fewer point, or a shape of their own added
            # to the cloned page.
            raise ValueError(
                f"entry [{position}] of the {len(items)} you gave holds {len(item)} value(s), "
                f"{len(item) - len(frames)} more than the {len(frames)} text shape(s) a unit on this page holds. "
                f"The page repeats {slots} unit(s)"
                + (
                    f", and this unit's {len(frames)} shape(s) still say "
                    + ", ".join(repr(_head(shape)) for shape in frames)
                    + " -- the template's own words, not yours"
                    if frames
                    else ""
                )
                + f". {_entries_given(items)} Give one value per shape (None keeps one as it is), pick a "
                "prototype whose units hold more, or add your own shape to the cloned slide"
            )
        # A list as long as the unit addresses every shape, one to one. A shorter list
        # is read against the shapes that are not the unit's number: the number sits
        # first in reading order on most units, and `["甲"]` handed to `[01, label]`
        # otherwise writes 甲 over the 01 and empties the label -- the one outcome no
        # author means. So a number frame that meets a value which is not itself a
        # number is restated for its position and the value moves on to the next shape.
        # An author who wants their own numbering writes the full list, or the dict.
        values = list(item)
        addressed = len(values) == len(frames)
        cursor = 0
        for shape in frames:
            ordinal = _renumbered(shape.text_frame.text, position)
            value = values[cursor] if cursor < len(values) else None
            index = cursor
            if (
                not addressed
                and ordinal is not None
                and cursor < len(values)
                and value is not None
                and str(value).strip()
                and _renumbered(str(value), position) is None
            ):
                replace_text(shape, ordinal)
                written.append(shape)
                continue
            cursor += 1
            if value is None:
                # Nothing is written either way: an explicit None and a list that runs
                # out both mean "this shape is the template's". The one thing a unit
                # moved to a new position cannot keep is its old number, so a shape past
                # the end of a short list that holds one is restated -- a template's
                # agenda numbers 01 to 08 and a run cut to six has to read 01 to 06.
                if index >= len(values) and ordinal is not None:
                    replace_text(shape, ordinal)
                    written.append(shape)
                continue
            if not str(value).strip() and ordinal is not None:
                replace_text(shape, ordinal)
                written.append(shape)
                continue
            replace_text(shape, value)
            written.append(shape)
    for spare in run[len(items) :]:
        drop_shape(spare)
    if len(items) < len(run):
        _reflow(run[: len(items)], spots, kind)
    return written


def _reading_order(shapes):
    """`shapes` as a reader meets them: row by row from the top, left to right in a row.

    Two shapes share a row when their vertical extents overlap by more than half of
    the shorter one -- a number in a circle and the heading beside it are a row even
    though their tops differ by a tenth of an inch, and a heading over its body is two
    rows even though they nearly touch.
    """
    placed = [s for s in shapes if getattr(s, "top", None) is not None and getattr(s, "left", None) is not None]
    rest = [s for s in shapes if s not in placed]
    rows: list[list] = []
    for shape in sorted(placed, key=lambda s: (s.top, s.left)):
        top, bottom = shape.top, shape.top + (shape.height or 0)
        for row in rows:
            r_top = min(s.top for s in row)
            r_bottom = max(s.top + (s.height or 0) for s in row)
            overlap = min(bottom, r_bottom) - max(top, r_top)
            shorter = max(1, min(bottom - top, r_bottom - r_top))
            if overlap > shorter / 2:
                row.append(shape)
                break
        else:
            rows.append([shape])
    ordered = []
    for row in sorted(rows, key=lambda row: min(s.top for s in row)):
        ordered.extend(sorted(row, key=lambda s: s.left))
    return ordered + rest


# A unit narrower than this cannot carry a heading and a line of copy, so a row is not
# grown past it: measured on the bundled templates' card rows, the narrowest unit that
# holds copy is 1.42in wide.
UNIT_MIN_IN = 1.2
# And the shortest a column's unit may become: a badge beside two lines of copy is
# 0.75in tall on the templates' agenda pages, and the reference page this was measured
# on runs four such rows in 4.66in.
UNIT_MIN_TALL_IN = 0.6
# The gutter a grown row closes up to before its units start shrinking.
GAP_MIN_IN = 0.1
# What a grid keeps clear of the page's bottom edge when it gains a row.
CANVAS_MARGIN_IN = 0.35


def _reflow(survivors, spots, kind):
    """Lay the run out again, where the arrangement says how.

    `arrangement` reports and this used to stop there, leaving three cards of four
    left-aligned with a card-sized hole on the right -- which every author then had to
    close by hand with `place`, or shipped. On a row or a column there is one answer:
    the units share the run's original extent with equal gaps. Fewer units keep their
    size; more units first close the gutters to `GAP_MIN_IN` and then shrink, uniformly
    so a circle stays a circle, until they fit. On a grid they fill it row-major at the
    grid's own pitches, a new row below the last when they overflow it, and the last,
    partial row is centred. On an irregular run -- fanned, staggered, around a circle --
    nothing moves, because any move is a guess at the design.
    """
    from pptx.util import Emu, Inches

    shape, *_ = kind
    if shape == "irregular" or not spots or not survivors:
        return
    left0 = min(box[0] for box in spots)
    right0 = max(box[0] + box[2] for box in spots)
    top0 = min(box[1] for box in spots)
    bottom0 = max(box[1] + box[3] for box in spots)
    n = len(survivors)
    if shape in ("row", "column"):
        along = 0 if shape == "row" else 1
        extent = (right0 - left0) if along == 0 else (bottom0 - top0)
        sizes = [(unit.width / 914400, unit.height / 914400) for unit in survivors]
        total = sum(size[along] for size in sizes)
        gap = (extent - total) / (n - 1) if n > 1 else 0.0
        floor = min(_original_gap(spots, along), GAP_MIN_IN) if n > 1 else 0.0
        if n > 1 and gap < floor:
            scale = (extent - floor * (n - 1)) / total
            for unit in survivors:
                unit.width = Emu(int(unit.width * scale))
                unit.height = Emu(int(unit.height * scale))
            sizes = [(unit.width / 914400, unit.height / 914400) for unit in survivors]
            total = sum(size[along] for size in sizes)
            gap = (extent - total) / (n - 1)
        start = (
            (left0 if along == 0 else top0)
            if n > 1
            else ((left0 + right0 - total) / 2 if along == 0 else (top0 + bottom0 - total) / 2)
        )
        cursor = start
        for unit, size in zip(survivors, sizes):
            if along == 0:
                unit.left = Inches(cursor)
            else:
                unit.top = Inches(cursor)
            cursor += size[along] + gap
        return
    xs = sorted({round(box[0], 2) for box in spots})
    ys = sorted({round(box[1], 2) for box in spots})
    cols = len(xs)
    pitch_x = (xs[1] - xs[0]) if cols > 1 else 0.0
    pitch_y = (ys[1] - ys[0]) if len(ys) > 1 else (spots[0][3] + GAP_MIN_IN)
    for index, unit in enumerate(survivors):
        row, col = divmod(index, cols)
        in_last_row = row == (n - 1) // cols
        short_by = cols - (n - row * cols) if in_last_row else 0
        unit.left = Inches(xs[col] + short_by * pitch_x / 2)
        unit.top = Inches(ys[row] if row < len(ys) else ys[-1] + pitch_y * (row - len(ys) + 1))


def _original_gap(spots, along: int) -> float:
    """The gutter the template drew between neighbours along one axis, or 0."""
    edges = sorted((box[along], box[along] + box[along + 2]) for box in spots)
    gaps = [nxt[0] - prev[1] for prev, nxt in zip(edges, edges[1:])]
    gaps = sorted(gap for gap in gaps if gap > 0)
    return gaps[len(gaps) // 2] if gaps else 0.0


def _slide_of(shape):
    return shape.part.slide


def _canvas_in(shape) -> tuple[float, float]:
    """The page's (width, height) in inches, off the presentation the shape belongs to."""
    try:
        presentation = shape.part.package.presentation_part.presentation
        return (presentation.slide_width / 914400, presentation.slide_height / 914400)
    except Exception:  # noqa: BLE001 -- a shape outside a package answers the default canvas
        return (13.333, 7.5)


def _every_shape(shapes):
    """Every shape on the page including the groups themselves, outermost first."""
    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _every_shape(shape.shapes)


def _fresh_ids(element, slide) -> None:
    """Give every shape in a copied element an id the page does not hold yet.

    A copy carries the original's `cNvPr id`; two shapes sharing one is a file
    PowerPoint offers to repair.
    """
    taken = [int(node.get("id")) for node in slide._element.iter(f"{{{_P}}}cNvPr") if str(node.get("id", "")).isdigit()]
    next_id = max(taken, default=1) + 1
    for node in element.iter(f"{{{_P}}}cNvPr"):
        node.set("id", str(next_id))
        next_id += 1


def _shape_for(slide, element):
    for shape in _every_shape(slide.shapes):
        if shape._element is element:
            return shape
    raise LookupError("the copied shape is not on the page it was added to")


def _run_holding(slide, unit):
    for run in units(slide):
        if any(other._element is unit._element for other in run):
            return run
    return None


def add_unit(target, count: int = 1):
    """Grow a page's repeating run by `count` units, copied from its last one.

    `target` is a run from `units(slide)`, or the slide itself for its longest run. The
    copies are the last unit again -- its shapes, its words, its icon -- inserted after
    it in the same container, so `fill` and `replace_text` treat them as slots like
    any other. The run is then laid out again (`_reflow`): a row closes its gutters and
    then shrinks its units uniformly to fit the width it had, a grid gains a row at its
    own pitch, and a run that follows no grid refuses, because where a seventh pill on
    an S-curve goes is a design decision.

    Refused when the grown units would fall under {UNIT_MIN_IN}in on the axis they share,
    or a grid's new row would run off the page: the way out is a prototype with more
    slots -- `ppt_template` prints each page's -- or a second page.

    Returns the new units, in the order they were added.
    """
    run = list(target) if isinstance(target, (list, tuple)) else max(units(target), key=len, default=[])
    if not run:
        raise ValueError("nothing repeats on this page, so there is no unit to add another of")
    if count < 1:
        return []
    run = _reading_order(run)
    spots = boxes(run)
    kind = arrangement(run)
    shape, _rows, _cols = kind
    n = len(run) + count
    if shape == "irregular":
        placed = ", ".join(f"({b[0]:.2f}, {b[1]:.2f}, {b[2]:.2f}, {b[3]:.2f})" for b in spots)
        raise ValueError(
            f"this page repeats {len(run)} units along no row, column or grid, so it cannot take {n}: where the "
            f"next one goes is the design's to say. Say it: `clone_shape(run[-1], (left, top, width, height))` "
            f"copies the last unit to a box you choose -- the existing ones sit at {placed} in inches -- and "
            f"`fill(run + [copy], items)` then writes it like any other slot. Otherwise pick a prototype with "
            f"{n} slots (ppt_template prints each page's slot count) or split the content over two pages"
        )
    last = run[-1]
    if shape in ("row", "column"):
        along = 0 if shape == "row" else 1
        extent = (
            (max(b[0] + b[2] for b in spots) - min(b[0] for b in spots))
            if along == 0
            else (max(b[1] + b[3] for b in spots) - min(b[1] for b in spots))
        )
        size = (last.width if along == 0 else last.height) / 914400
        total = sum((u.width if along == 0 else u.height) / 914400 for u in run) + count * size
        floor = min(_original_gap(spots, along), GAP_MIN_IN)
        scale = min(1.0, (extent - floor * (n - 1)) / total) if total else 1.0
        least = UNIT_MIN_IN if along == 0 else UNIT_MIN_TALL_IN
        # Only a shrink is refused: a template whose units are already under the floor
        # drew them that way, and growing it without shrinking changes nothing about them.
        if scale < 1.0 and size * scale < least:
            raise ValueError(
                f"{n} units across this run's {extent:.2f}in would be {size * scale:.2f}in each, under the "
                f"{least}in a unit needs to carry copy. Pick a prototype with {n} slots -- ppt_template "
                f"prints each page's -- or split the content over two pages"
            )
    else:
        xs = sorted({round(b[0], 2) for b in spots})
        ys = sorted({round(b[1], 2) for b in spots})
        rows_needed = -(-n // len(xs))
        pitch_y = (ys[1] - ys[0]) if len(ys) > 1 else (spots[0][3] + GAP_MIN_IN)
        bottom = ys[0] + (rows_needed - 1) * pitch_y + spots[0][3]
        if bottom > _canvas_in(last)[1] - CANVAS_MARGIN_IN:
            raise ValueError(
                f"{n} units on this {len(ys)}x{len(xs)} grid need {rows_needed} rows, and the last would end "
                f"{bottom:.2f}in down a {_canvas_in(last)[1]:.2f}in page. Pick a prototype with {n} slots -- "
                f"ppt_template prints each page's -- or split the content over two pages"
            )
    slide = _slide_of(last)
    anchor = last._element
    copies = []
    for _ in range(count):
        element = copy.deepcopy(last._element)
        _fresh_ids(element, slide)
        anchor.addnext(element)
        anchor = element
        copies.append(element)
    added = [_shape_for(slide, element) for element in copies]
    _reflow(run + added, spots, kind)
    return added


def remove_unit(unit) -> None:
    """Take one unit out of its run and close the gap it leaves.

    `drop_shape` removes and leaves the hole; this is the call for a slot the content
    does not fill on a page written line by line with `replace_text`. The survivors are
    laid out again the way `add_unit` and `fill` lay theirs out; a unit that repeats
    along no grid is removed and nothing else moves.
    """
    slide = _slide_of(unit)
    run = _run_holding(slide, unit)
    if run is None:
        drop_shape(unit)
        return
    run = _reading_order(run)
    spots = boxes(run)
    kind = arrangement(run)
    drop_shape(unit)
    _reflow([other for other in run if other._element is not unit._element], spots, kind)


def clone_shape(shape, box=None):
    """A copy of `shape` on the same page, at `box` when one is given.

    What five measured builds wrote for themselves as `clone_panel`: deepcopy the
    element, hang it on the tree, set a box -- and two of the five hung it on the page
    rather than in the shape's own group, because `place` did not then convert a page
    box into a group's space. The copy sits beside the original in the same container,
    with ids the page does not already hold, and `box` is (left, top, width, height) in
    inches on the page whichever group it lands in. Returns the new shape, ready for
    `replace_text` and `replace_picture`.
    """
    slide = _slide_of(shape)
    element = copy.deepcopy(shape._element)
    _fresh_ids(element, slide)
    shape._element.addnext(element)
    new = _shape_for(slide, element)
    if box is not None:
        place(new, box)
    return new


# A unit's number: one or two digits and nothing else. Narrow on purpose -- "3.1" is
# a section reference and "2023" is a year, and restating either as a position would
# be wrong.
_UNIT_NUMBER = re.compile(r"^(\d{1,2})$")


def _renumbered(current: str, position: int) -> str | None:
    """This shape's number restated for `position`, or None if it holds no number."""
    match = _UNIT_NUMBER.match((current or "").strip())
    if match is None:
        return None
    return f"{position:0{len(match.group(1))}d}"


# The three ways a layout can say "this box is the page's title".
_TITLE_SLOTS = (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE, PP_PLACEHOLDER.VERTICAL_TITLE)


# How close under the title a line has to start, and how nearly aligned with it, to be
# the second half of one heading block: 8% of the page's height and 1% of its width.
# Both numbers come off one measurement of the bundled templates, and both are
# load-bearing. A section divider's line sits between 0% and 4.5% under its title and at
# exactly the title's left edge. A closing page carries the same kind of placeholder
# holding "Presenter name", and the nearest of those is either 13.7% of the page below
# the title or 4.8% of its width off the left edge -- so loosening either number writes
# the author's subtitle onto the presenter's name.
_BLOCK_GAP = 0.08


_BLOCK_LEFT = 0.01


def _slot(shape):
    """The role the layout gave this shape, or None for a box the author drew.

    Read as an enum member rather than by matching its name, because the names overlap
    where it matters: `"TITLE" in str(type)` is true of a SUBTITLE placeholder, and a
    cover whose subtitle sits above its title would then answer `title=` with it.
    """
    if not getattr(shape, "is_placeholder", False):
        return None
    return getattr(getattr(shape, "placeholder_format", None), "type", None)


# Why the template's own naming is read before geometry, measured on the sixteen
# templates that shipped when this was written: `title=` came back holding "Presenter
# name" on two covers and nothing at all on a third, and `subtitle=` raised KeyError on
# ten of the sixteen covers and on every one of the sixteen section dividers.
# Re-measured on the twelve that ship now, `title` resolves on all 197 example pages
# and `subtitle` on 180, the seventeen it does not being closing pages carrying a
# presenter row and six pages between.
def _heading_rows(slide, presentation):
    """The page's title and subtitle shapes, as a reader would point at them.

    What the template named, wherever it put it, before anything is inferred from
    where it sits. Inferring first is what the top-third rule did, and it fails on
    exactly the pages a deck opens and divides with: a cover's title is centred in the
    page, anywhere from 1.24in to 4.76in on a 7.5in canvas, so its top third holds the
    presenter and date lines instead -- `title=` comes back holding "Presenter name" --
    and a section divider's one line under the title sits at 3.09in, out of the band
    altogether.

    Geometry still decides the rest, and there it is unchanged: a page that names a title
    placeholder and nothing else takes its subtitle from the topmost line in the top
    third, as before. Two fallbacks sit under that band,
    for the pages whose heading is not at the top of the page at all: the line that
    forms one block with the title, and then `_largest_row`.
    """
    height = presentation.slide_height or 0
    width = presentation.slide_width or 0
    band = height * 0.35
    rows = sorted(
        (
            shape
            for shape in _all_shapes(slide.shapes)
            if getattr(shape, "has_text_frame", False) and shape.top is not None and shape.text_frame.text.strip()
        ),
        key=lambda shape: (shape.top, -(shape.width or 0)),
    )
    title = next((shape for shape in rows if _slot(shape) in _TITLE_SLOTS), None)
    if title is None:
        title = next((shape for shape in rows if shape.top <= band), None)
    if title is None:
        return {}

    under = [shape for shape in rows if shape._element is not title._element and shape.top > title.top]
    column = [shape for shape in under if abs((shape.left or 0) - (title.left or 0)) <= width * _BLOCK_LEFT]
    # Anywhere on the page rather than under the title: a kicker set above the title is
    # still the row the template called its subtitle. The element guard is what keeps
    # that from answering both roles with one shape on a page whose title was inferred.
    subtitle = next(
        (shape for shape in rows if shape._element is not title._element and _slot(shape) == PP_PLACEHOLDER.SUBTITLE),
        None,
    )
    if subtitle is None:
        subtitle = next((shape for shape in under if shape.top <= band), None)
    if subtitle is None:
        floor = title.top + (title.height or 0)
        subtitle = next((shape for shape in column if shape.top - floor <= height * _BLOCK_GAP), None)
    if subtitle is None:
        subtitle = _largest_row(column)
    found = {"title": title}
    if subtitle is not None:
        found["subtitle"] = subtitle
    return found


def _largest_row(rows):
    """The one line set larger than every other line in the title's column.

    The last thing tried, and narrow on purpose. A page whose subtitle sits halfway
    down -- under a photograph, over a row of cards -- is out of reach of both the band
    and the heading block, and the only thing left that says "heading" is the type size.
    Most template copy declares no size at all, inheriting one from the layout, so this
    answers on the few pages that do state it, and a tie is a refusal: an agenda page's
    eight numbered slots are all set at one size, and any of them would put the author's
    subtitle inside slot 01.
    """
    sized = []
    for shape in rows:
        largest = None
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                size = run.font.size or paragraph.font.size
                if size is not None and (largest is None or size > largest):
                    largest = size
        if largest is not None:
            sized.append((largest, shape))
    if not sized:
        return None
    biggest = max(size for size, _ in sized)
    if sum(1 for size, _ in sized if size == biggest) > 1:
        return None
    return next(shape for size, shape in sized if size == biggest)


def _all_shapes(shapes):
    """Every shape on the page, including the ones inside groups.

    A template's content is mostly inside groups: a card page has two shapes at the
    top level and fourteen with text one group down -- the four cards, their numbers,
    their headings and their body copy. `slide.shapes` does not descend, so without
    this `replace_text` cannot see thirteen of the fourteen placeholders and every one
    of an author's calls raises, against a page whose copy says exactly the strings it
    is keyed on.
    """
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _all_shapes(shape.shapes)
        else:
            yield shape


# How alike the key and a shape's copy have to be before the failure offers one as the
# other's near miss, and the shortest key it will offer one for. Both measured over the
# 865 distinct strings the ten bundled templates hold, each corrupted three ways -- one
# glyph substituted, one dropped, and a quarter of them rewritten. At 0.55 the two
# classes that model the live evidence are named correctly 1292/1292 and 1184/1184,
# the rewritten class 1134/1292, and 421 strings real runs wrote that appear in no
# template draw no hint at all. Below four characters the measure stops working rather
# than degrading: a three-character key would be offered a hint 121 times over the same
# corpus and name the wrong shape in 10 of them, because a page's 35%/38%/68%/95% row
# scores 0.5 against every member of itself -- and a hint that points at the wrong shape
# costs more than the silence it replaces. Not an edit distance: one glyph is a whole
# morpheme in CJK and a typo in English, so the threshold has to be a ratio of the
# length rather than a count of edits.
NEAR_MISS = 0.55
NEAR_MISS_CHARS = 4


# How far from the point asked for a shape may sit and still be offered as the nearest
# thing to it; how nearly a shape has to share one of the two coordinates before the
# failure calls it the same row or column; and how far out it may be on the other axis.
# Measured over the 3362 placed shapes of the ten bundled templates, asking for points
# 0.3in to 3.0in off each of them: past 1.0in the shape a point was offset from is never
# the nearest one any more, so `nearest` stops meaning anything and saying it would
# point across the page -- and holding it to 1.0in is what makes a hint impossible for a
# point sitting in empty space, where 0/103189 grid points more than 1.5in from every
# shape draw one. The axis pair is the other half: 0.10in covers an author who copied
# the two-decimal position the reference prints (both live failures were 0.00in and
# 0.06in off on x), and 2.0in of slack on the other axis names the shape in 66% of
# single-axis misses while 1 grid point in 10000 more than 2in from anything gets a
# clause at all.
NEAR_POINT = 1.0
NEAR_AXIS = 0.10
NEAR_AXIS_OUT = 2.0


def _nearby(left, top, tol, considered):
    """The `nearest is` clause a failed position lookup opens with.

    Up to two clauses, because they are two different answers and only sometimes the
    same shape. The nearest shape is what an author who mistyped a coordinate wants. A
    shape sitting on one of the two coordinates asked for while being out on the other
    is a row or column counted one off, which names the mistake rather than just the
    shape -- and that is what both live failures were. Only one of the two was also the
    nearest: asked for (7.39, 2.3), the nearest shape is a drawing 0.72in away and the
    one the author meant is the text box 0.87in away, on exactly the x it asked for.

    `considered` holds the shapes the lookup actually compared -- numbered as the
    inventory numbers them, `with_text` already applied, and the ones with no position
    already counted out -- so the clause can never offer a shape the lookup would not
    have accepted.
    """
    if not considered:
        return ""
    window = min(NEAR_AXIS, tol)
    nearest = min((math.hypot(at[0] - left, at[1] - top), index, shape) for index, shape, at in considered)
    if nearest[0] > NEAR_POINT:
        nearest = None
    rows = []
    for index, shape, at in considered:
        if abs(at[0] - left) <= window and abs(at[1] - top) > tol:
            rows.append((abs(at[1] - top), index, shape, "x", "row"))
        elif abs(at[1] - top) <= window and abs(at[0] - left) > tol:
            rows.append((abs(at[0] - left), index, shape, "y", "column"))
    row = min(rows) if rows else None
    if row is not None and row[0] > NEAR_AXIS_OUT:
        row = None
    if nearest is not None and row is not None and nearest[1] == row[1]:
        return (
            f" Nearest is {_listed(nearest[1], nearest[2])}, {nearest[0]:.2f}in away, on the {row[3]} you"
            f" asked for -- a {row[4]} counted one off."
        )
    said = []
    if nearest is not None:
        said.append(f" Nearest is {_listed(nearest[1], nearest[2])}, {nearest[0]:.2f}in away.")
    if row is not None:
        said.append(
            f" {_listed(row[1], row[2])} is on the {row[3]} you asked for and {row[0]:.2f}in out"
            f" -- a {row[4]} counted one off."
        )
    return "".join(said)


def _near_miss(wanted, pool, *, head=False):
    """The shape whose copy `wanted` was most likely a slip of, or None.

    `head=True` for `shape_saying`, which matches a prefix: the key is compared against
    each candidate's opening of the same length, since scoring a six-character prefix
    against a whole paragraph would find nothing.

    Where two shapes score the same, the one closest in length wins and the earlier on
    the page breaks what is left, so one page always produces one answer.
    """
    wanted = " ".join(str(wanted).split())
    if len(wanted) < NEAR_MISS_CHARS:
        return None
    best = None
    for shape in pool:
        if not getattr(shape, "has_text_frame", False):
            continue
        text = " ".join(shape.text_frame.text.split())
        # A template page is mostly drawings and empty boxes -- 13 of the 27 shapes on
        # the page that raised this four times running -- and an empty string scores
        # against anything short.
        if not text:
            continue
        score = SequenceMatcher(None, wanted, text[: len(wanted)] if head else text).ratio()
        if score < NEAR_MISS:
            continue
        rank = (score, -abs(len(text) - len(wanted)))
        if best is None or rank > best[0]:
            best = (rank, shape)
    return None if best is None else best[1]


def _near_says(asked, every=None, *, head=False):
    """The `did you mean` clause a failed lookup opens with, or an empty string.

    Before the inventory rather than after it, because the inventory is what the author
    reads past: four consecutive builds of one live deck died naming '数字健康崛起'
    against a page whose shape 25 held '数字健康兴起' -- one glyph apart, same meaning --
    with the answer sitting at position 25 of 27 in a message the author re-read and
    re-submitted each time. Each attempt was a different program, so the author was
    editing between them and never that line: nothing in the message pointed at it.

    `every` is the page's shapes in the numbering the failure prints, so the clause can
    name the shape the same way the inventory below it does; without it the clause names
    the copy alone, which is how `fill` and `shape_saying` address a shape anyway.
    """
    said = []
    for key, pool in asked:
        found = _near_miss(key, pool, head=head)
        if found is None:
            continue
        number = next((index for index, one in enumerate(every or (), start=1) if one is found), None)
        where = f"[{number}] " if number else ""
        said.append(f" Did you mean {where}{_head(found, 60)!r}? (you asked for {' '.join(str(key).split())!r}).")
        if len(said) == 3:
            break
    return "".join(said)


def _elsewhere(asked, prototype):
    """The template page a key names word for word, when it is not the page being adapted.

    The other half of the same live run: two builds died naming
    '新技术、新产品及新服务在行业中的应用', which is shape 3 of the template's page 18
    while the call was adapting page 16. Nothing on page 16 is near it -- the closest
    scores 0.20 -- so the near-miss clause correctly says nothing, and the author is
    left with an inventory that cannot explain a string it never held.

    Word for word only. The page number is either right or there is no clause.
    """
    said = []
    pages = here = None
    for key, pool in asked:
        wanted = str(key).strip()
        if not wanted or _near_miss(key, pool) is not None:
            continue
        if pages is None:
            pages, here = _template_pages(prototype)
        for number, slide in enumerate(pages, start=1):
            if number == here:
                continue
            if not any(
                getattr(shape, "has_text_frame", False) and wanted in shape.text_frame.text
                for shape in _all_shapes(slide.shapes)
            ):
                continue
            said.append(
                f" {wanted!r} is on page {number} of this template"
                + (f", not this page {here}." if here else ", not this one.")
            )
            break
        if len(said) == 2:
            break
    return "".join(said)


def _bound_template():
    """The template this build was bound to, or None, read where the runner puts it.

    `_elsewhere` walks the pages of the file a clone came out of. The only raiser that
    held that file was `adapt`, which the copy route replaced: `replace_text` is handed
    a page already cloned into the deck being built, and that page's package is the
    deck. The runner sets `PPT_TEMPLATE_SOURCE` to the original in the environment of
    the very process the program runs in -- the only place the shapes python-pptx cannot
    redraw can be reached at all -- and this module already reads that environment for
    `bundled()`. Never raises: it only decorates a failure that is being raised anyway,
    so a template that cannot be opened is one clause fewer, not a second error.
    """
    import os

    source = os.environ.get("PPT_TEMPLATE_SOURCE", "")
    if not source:
        return None
    try:
        from pptx import Presentation

        return Presentation(source)
    except Exception:
        return None


def _template_pages(prototype):
    """The pages of the file `prototype` came from, and which of them it is.

    An empty pair for anything that does not answer as a slide of a presentation, since
    this only ever decorates a failure that is being raised either way.
    """
    try:
        pages = list(prototype.part.package.presentation_part.presentation.slides)
    except AttributeError:
        return [], None
    return pages, next((number for number, page in enumerate(pages, start=1) if page is prototype), None)


def _describe(shape, limit=26):
    """A shape as the refusal names it: its place, its kind, and its words."""
    size = f"{(shape.width or 0) / 914400:.1f}x{(shape.height or 0) / 914400:.1f}in"
    if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE or _blip_fill(shape) is not None:
        kind = f"picture {size}"
    elif _is_drawing(shape):
        whole = _outermost(shape)
        if whole is not shape:
            size = f"{(whole.width or 0) / 914400:.1f}x{(whole.height or 0) / 914400:.1f}in"
        kind = f"drawing {size} (a picture may take its place)"
    else:
        kind = "shape"
    text = _head(shape, limit) if getattr(shape, "has_text_frame", False) else ""
    return f"{kind} {text!r}" if text else kind


def _head(shape, limit=30):
    text = " ".join(shape.text_frame.text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _given(value, limit=24):
    """One value a caller passed, as a refusal should show it back."""
    if value is None:
        return "None"
    text = " ".join(str(value).split())
    return repr(text if len(text) <= limit else text[: limit - 1] + "…")


# Enough entries to recognise the list by, and a bound so a forty-item run does not
# bury the sentence that says what to do.
_ENTRIES_SHOWN = 12


def _entries_given(items):
    """The entries handed to `fill`, counted and quoted back to the caller.

    The refusal above this used to quote `frames` -- the prototype's own shapes -- on
    both sides of "N values were given", so an author who passed `['a', 'b', 'c']` read
    the template's example Chinese back at them and could not tell which entry the count
    was about, nor that the quoted words were not their own. Three live builds died on
    that message and the author's next call changed the prototype rather than the list.
    """
    shown = []
    for position, item in enumerate(items[:_ENTRIES_SHOWN], start=1):
        if isinstance(item, dict):
            body = ", ".join(f"{_given(key)}: {_given(value)}" for key, value in item.items())
            shown.append(f"[{position}] a dict of {len(item)}: {body}")
        else:
            body = ", ".join(_given(value) for value in item)
            shown.append(f"[{position}] {len(item)} value(s): {body}")
    if len(items) > _ENTRIES_SHOWN:
        shown.append(f"and {len(items) - _ENTRIES_SHOWN} more")
    return f"The {len(items)} entries you gave: " + "; ".join(shown) + "."


def _pick(key, by_index, by_text):
    """A shape named by its 1-based place on the page, or by the text it holds now.

    One numbering for every argument, and it is the page's own: shape N is the Nth
    shape `decompile` prints, groups opened, whether or not it holds text. The
    alternative -- counting text frames for `texts`, pictures for `pictures` -- gives
    three numberings for one page, so a fifteenth read off the reference lands on
    a page whose text frames stop at fourteen.

    Where several shapes hold the key, the one whose whole copy *is* the key takes it and
    position decides the rest. The walk reaches a group's members before the top-level
    shape drawn over them, so taking the first match handed `自动化与人工智能` to an arrow
    label reading `自动化与人工智能助力产业升级` and left the page's title -- that string
    and nothing else, last in the z-order because it sits on top -- saying what the
    template shipped. A shape the key names completely is not the near miss.
    """
    if isinstance(key, int):
        return by_index[key - 1] if 1 <= key <= len(by_index) else None
    wanted = str(key).strip()
    partial = None
    for shape in by_text:
        if not (getattr(shape, "has_text_frame", False) and wanted in shape.text_frame.text):
            continue
        if shape.text_frame.text.strip() == wanted:
            return shape
        if partial is None:
            partial = shape
    return partial


def helper_source() -> str:
    """This module's own text, to write beside a build script that needs it.

    The author's program runs in a subprocess that has python-pptx and nothing of
    this package, so these operations reach it as a file it imports rather than as
    a call it makes. Its own source rather than a second copy kept in a string:
    two spellings of `clone_page` would drift, and the one that drifts is the one
    no test runs.
    """
    return Path(__file__).read_text(encoding="utf-8")
