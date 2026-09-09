"""The page grid, projected as a module an author's build script imports.

Three live runs wrote every rectangle by eye. What they produced was measurable:
labels needing 4.2in in a 3.0in box so they wrapped mid-phrase, copy painted over
copy, and body type pushed to 12.5pt to make invented boxes fit. And what a reader
saw was a page with no visible structure -- gaps of six different sizes, rows that
did not line up, nothing to separate one group from the next -- because nothing
shared a grid.

So the grid is code. An author asks a page for its regions and asks a region to
divide itself; the gutters are one number, the safe margin is one number, and every
box a program draws is as wide as the column it sits in. Zones become visible
because a region can paint itself in the theme's own surface tint rather than being
implied by whitespace the author eyeballed.

The type ramp is here for the same reason: a size chosen per page drifts, and the
floor the measurements enforce (14pt body) belongs next to the sizes rather than in
a document the author may not re-read.

`raven_ppt.services.assets.script_helpers` writes this as `ppt_layout.py` beside
the script. Coordinates are inches, because that is what python-pptx's `Inches`
wants and what the author reads in the reference.
"""

from __future__ import annotations

from raven_ppt.services.assets.fonts import FACE_WIDTH

LAYOUT_MODULE_FILENAME = "ppt_layout.py"

# The line in the module body that `layout_module_source` fills in from
# `FACE_WIDTH`. The table is measured beside the substitutions it was measured
# against and travels from there, rather than being typed out twice.
_FACE_WIDTH_MARKER = "_FACE_WIDTH = {}"

_LAYOUT_MODULE = '''"""The page grid: regions, divisions, the type ramp, and what each thing measures.

    from ppt_layout import page, GUTTER, BODY_PT, plane, write, text_size

    T = THEMES[next(iter(THEMES))]        # or by name -- ppt_theme says which
    FONT, HAN = T["font_family"], T["cjk_font_family"]
    frame = page()                        # kicker / title / body / footer
    left, right = frame.body.split_left(0.58)
    for card in right.rows(3):
        plane(slide, card, T)             # the zone, visible
        line = write(slide, card.inset(0.22), "…", size=BODY_PT, colour=INK, font=FONT, cjk_font=HAN)
        # line.box is what the copy covered -- ask text_size for it before drawing.

Every box is inches: `box.pptx()` unpacks straight into python-pptx.
Never write a coordinate by eye -- ask a region to divide itself, and the gutters,
the alignment and the safe margin come out right by construction.

Ask before you draw, and read back after. `text_size`, `points_size`, `table_size`,
`picture_size`, `lines_needed`, `formula_type_size` and `fits` answer what a thing
will take while there is still time to place it, and
`the_largest_step_this_copy_takes` answers the other direction -- how big the copy
in a box is allowed to be, so a label in a 1.25in shape is not set at the smallest
step on the ramp; and every helper that draws hands back the box it really covered,
so the next thing down the page is `written.box.y1 + GUTTER` and never a number
tried against a render:

    down = stack(frame.body)
    title = write(slide, down.take(0.4), "结论", size=LEAD_PT, colour=INK, font=FONT)
    down.skip(GUTTER)
    grid = table_size(rows, T, box=down.rest())
    tbl = table(slide, down.take(grid.h), rows, T)
    write(slide, Box(tbl.box.x0, tbl.box.y1 + GUTTER, tbl.box.x1, ...), "来源：Table 2", ...)

The other end of that arithmetic: a run measured this way is as tall as its content,
which is rarely as tall as the body. Ask the page to hold it --
`page().holding(grid.h, GUTTER, group.h)` -- and the leftover becomes air above and
below the page's content instead of a band of white along its foot.
"""

import math
import re
from collections import namedtuple

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Inches, Pt

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"

CANVAS_W, CANVAS_H = 13.333, 7.5

# The safe area. Type outside it reads as falling off the page, and a projector
# crops less predictably than a screen.
MARGIN = 0.72
# A plane's corner, in inches rather than as a share of its short side, so a tall panel
# and a one-line strip on the same page come out with the same curve.
PLANE_RADIUS_IN = 0.10
# One gap, everywhere. Six different gaps on one page is what "no structure" looks
# like; a single number is what makes a three-card row read as three cards.
GUTTER = 0.28
# Space inside a plane before its copy starts.
PAD = 0.22

# The ramp. Sizes drift when they are chosen per page, so they are chosen once.
# BODY_PT sits above the 14pt floor the measurements enforce, with room to drop one
# step and still clear it.
KICKER_PT = 12
TITLE_PT = 30
LEAD_PT = 20
BODY_PT = 16
LABEL_PT = 14
BODY_FLOOR_PT = 14
NUMBER_PT = 40

# The gap between the heading's ground and the first thing on the page. Touching
# reads as welded -- the same thing that makes an accent strip on a card's edge look
# generated -- so the band stops short of the body.
#
# This used to be one number for two distances that pull against each other, because
# the band's bottom was computed off the body: the gap it left over the body was the
# same hundredths it took away from the air under the title inside it. Both readings
# were measured and they disagree. 0.07in of gap was enough while the first thing on
# the page was type, which carries its own air in the line box, and not enough once it
# was another painted surface -- a row of cards taken off `body.y0` came back as a
# tinted band and four tinted cards separated by six pixels of white, one shape
# somebody forgot to finish. 0.18 fixed that and overran the other way: `crowded_panel`
# reported the title's ink 0.051in off the band's bottom edge, four pages out of four
# and every round of every one, where the other panels on those pages carried 0.19 to
# 0.50in. Swept to 0.10 the title had its air and the gap over the body was two and a
# half millimetres, which is the reading that came back from the render as the band and
# the body being stuck together.
#
# So the band's bottom is now the title's business and the gap over the body is the
# page's. The air under the title is what the sweep found; the gap is a gutter,
# because a band is an element on this canvas and so is a card, and the distance
# between two elements here has a name already.
_TITLE_AIR = 0.20

# The air over the kicker. Not MARGIN: the side margin keeps copy off the edge of a
# projected page, and the heading band bleeds to that edge anyway, so spending the
# full 0.72in there bought nothing and cost the body. Measured against the sixteen
# templates that ship with this: every one of them starts its title row at 0.14in
# and ends it at 1.12in, and a heading built from MARGIN pushed the body to 1.94in
# -- a quarter of the canvas for a kicker and one line.
#
# 0.34in was the first cut at that and it still read as a band with a gap over it.
# The templates put the title box itself at 0.14; this leaves 0.20 because what sits
# at the top here is the kicker, which the templates do not have, and a kicker whose
# ink starts 0.02in from the canvas edge reads as a printing error rather than as a
# section tag.
_HEAD_TOP = 0.20
_KICKER_H = 0.25
_TITLE_H = 0.56
_FOOTER_H = 0.30


# A box is not a number, and the two of its numbers a caller might have meant are `.h`
# and `.w`. Said here once because six operators say it.
_NOT_A_NUMBER = (
    "a box is not a number: {other} {op} Box(...) has no meaning. A measuring call hands back the "
    "box the thing needs, so add its `.h` for a height or its `.w` for a width -- "
    "`text_size(copy, width).h`, not `text_size(copy, width)`"
)


# Said once, because the whole point is that the two calls are indistinguishable and the
# message has to show the difference rather than assert it.
_AT_TAKES_ITS_SIZE_BY_NAME = (
    "Box.at takes its size by name: Box.at(x, y, w=..., h=...). Given {given} positionally it "
    "cannot tell a size from a corner{spelled} Say which: Box.corners(x0, y0, x1, y1) for two corners, "
    "Box.at(x, y, w=, h=) for a corner and a size."
)


def _two_readings(given):
    """The two rectangles four numbers could be, so the caller can see which it wanted."""
    if len(given) != 4:
        return ""
    x, y, third, fourth = given
    try:
        corners = f"({x:g}, {y:g}) to ({third:g}, {fourth:g})"
        size = f"({x:g}, {y:g}) to ({x + third:g}, {y + fourth:g})"
    except (TypeError, ValueError):
        return ""
    return f" -- as corners they are the box {corners}, as a size the box {size}."


class Box(namedtuple("Box", "x0 y0 x1 y1")):
    """A rectangle in inches, given as two corners, which knows how to divide itself.

    Two corners is what makes `columns()` and `rows()` read as division rather than
    arithmetic. It is also the opposite of every python-pptx call in the same script
    -- add_textbox, add_shape and add_picture all take left, top, width, height --
    and a script holding both conventions is a real trap. Use `Box.at` when a size
    is what you have.
    """

    __slots__ = ()

    @classmethod
    def corners(cls, x0, y0, x1, y1):
        """A box from its two corners, saying so.

        `Box.at` is keyword-only because four positional numbers cannot say whether
        the last two are a far corner or a size. This is the other half of that: an
        author writing corners says `corners`, and a script then carries no call
        whose meaning has to be inferred from the numbers. The bare constructor
        stays -- the library is full of it and every one of those is unambiguous in
        context -- but the vocabulary an author is handed has two named calls and no
        unnamed one. A size written into the bare constructor -- `Box(0.72, 1.24,
        12.6, 0.57)` -- reads as corners that end above where they begin.
        """
        return cls(x0, y0, x1, y1)

    @classmethod
    def at(cls, x, y, *size, w=None, h=None):
        """A box from its top-left corner and a size, which it takes by name.

        Two constructors of four numbers each, and positionally they are the same call:
        `Box(a, b, c, d)` is two corners and `Box.at(a, b, w=c, h=d)` is a corner and a size,
        and nothing in either spelling says which was meant, and a box that is wrong but
        on the page looks like a box: corners passed to `at` put shapes off the canvas,
        and a size passed to the bare constructor turns a 3.75in box into a 12.4in one.

        So the size is keyword-only. It costs a `w=` and an `h=`, it reads as what it is,
        and the mistake it prevents is the kind no render tells you about.
        """
        if size or w is None or h is None:
            given = (x, y, *size) if size else (x, y)
            raise TypeError(_AT_TAKES_ITS_SIZE_BY_NAME.format(given=given, spelled=_two_readings(given)))
        return cls(x, y, x + float(w), y + float(h))

    def __add__(self, other):
        """Arithmetic on a box is arithmetic on one of its numbers, and it says which.

        A measuring call hands back a box, so
        `chev_h + gap + rule_h + points_size(items, down.w)` adds the measurement where
        the height was wanted. The answer is one attribute away; the bare box raises
        `unsupported operand type(s) for +: 'float' and 'Box'`, which names neither the
        box nor the attribute.

        Between two boxes it was worse than an error: a box is a `namedtuple`, so
        `first + second` concatenated them into a tuple of eight numbers and carried on.
        """
        raise TypeError(_NOT_A_NUMBER.format(op="+", other=type(other).__name__))

    __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = __truediv__ = __add__

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def h(self):
        return self.y1 - self.y0

    # `Box.at` is spelled (x, y, w=, h=) and `w`/`h` read back under those names, so
    # a program that passes one box's geometry into another reaches for `.x` and `.y`
    # too. Two of the four names read back and two raised AttributeError, which is
    # the worst arrangement: a build that got three quarters of the line right still
    # crashed. Measured on a live run.
    @property
    def x(self):
        return self.x0

    @property
    def y(self):
        return self.y0

    def pptx(self):
        """(left, top, width, height) as python-pptx lengths."""
        return Inches(self.x0), Inches(self.y0), Inches(self.w), Inches(self.h)

    def inset(self, dx=PAD, dy=None):
        dy = dx if dy is None else dy
        return Box(self.x0 + dx, self.y0 + dy, self.x1 - dx, self.y1 - dy)

    def columns(self, n, gutter=GUTTER, weights=None):
        """`n` side-by-side boxes filling this one, separated by `gutter`."""
        return self._divide(n, gutter, weights, vertical=False)

    def rows(self, n, gutter=GUTTER, weights=None):
        """`n` stacked boxes filling this one, separated by `gutter`."""
        return self._divide(n, gutter, weights, vertical=True)

    def grid(self, cols, rows, gutter=GUTTER):
        """Row-major cells, so `grid(3, 2)[4]` is the second row's middle cell."""
        return [cell for band in self.rows(rows, gutter) for cell in band.columns(cols, gutter)]

    def split_left(self, fraction, gutter=GUTTER):
        """This box as (left, right), the left one taking `fraction` of the width."""
        left, right = self.columns(2, gutter, weights=_a_share("split_left", fraction))
        return left, right

    def split_top(self, fraction, gutter=GUTTER):
        """This box as (top, bottom), the top one taking `fraction` of the height."""
        top, bottom = self.rows(2, gutter, weights=_a_share("split_top", fraction))
        return top, bottom

    def _divide(self, n, gutter, weights, vertical):
        if n < 1:
            raise ValueError("a region divides into at least one part")
        span = (self.h if vertical else self.w) - gutter * (n - 1)
        if span <= 0:
            raise ValueError(f"{n} parts and {gutter}in gutters do not fit in {self.w:.2f}x{self.h:.2f}in")
        shares = list(weights or [1] * n)
        if len(shares) != n:
            raise ValueError(f"{len(shares)} weights for {n} parts")
        total = float(sum(shares))
        parts, offset = [], self.y0 if vertical else self.x0
        for share in shares:
            size = span * share / total
            if vertical:
                parts.append(Box(self.x0, offset, self.x1, offset + size))
            else:
                parts.append(Box(offset, self.y0, offset + size, self.y1))
            offset += size + gutter
        return parts


class Frame(namedtuple("Frame", "kicker title body footer")):
    """The regions of a page: the kicker and title rows, the body, the footer strip.

    `page()` builds the ordinary one, and a frame you build yourself is what a left
    rail or a full-bleed opener is made of -- every helper that takes a frame takes
    yours without knowing the difference.

    `body` is the room the page may use, which is more than most pages use. `laying`
    and `holding` are the two things to do with the difference, and one of them has to
    be done: a body used down to 60% of its height and left empty under that is the
    commonest defect a delivered deck has.
    """

    __slots__ = ()

    def laying(self, *heights, gutter=GUTTER):
        """A cursor down the body with the leftover already spent between the bands.

        `stack(frame.body).spread(*heights)` in one call, because the version that took
        two was not written: the room a page is given is not the room it holds, and every
        cursor that walks the body top-down leaves the difference at the foot unless it
        is told otherwise. Eleven of eighteen pages of one delivered run ended their
        content between 60% and 70% down; the call that fixes it existed the whole time.

        Pass the bands and take them back in the same order, and **no `skip` between
        them** -- the gaps are what the leftover became, so skipping one overruns the
        body by exactly what was skipped:

            down = frame.laying(figure.h, rows.h)
            band = down.take(figure.h)
            grid = down.take(rows.h)

        For components. Running copy spread down a region reads worse than copy left at
        the top of one -- for a page whose body genuinely holds less than it was given,
        `holding` cuts the region instead of stretching the gaps.
        """
        return stack(self.body, gutter).spread(*heights)

    def holding(self, *heights):
        """This frame with its body cut to the run these heights add up to.

        A run measured at 4.1in taken off the top of a 5.5in body leaves the last 1.4in
        of the page empty, and the page then reads as one that stopped early. It is the
        commonest thing wrong with a delivered deck: eleven of eighteen pages of one run
        ended their content between 60% and 70% down, and the render measured 1.5in of
        trailing white on the worst of them. Nothing on those pages had asked for that
        height -- it was the room that was left.

        The leftover goes outside the run rather than between its bands. Between them is
        `stack(...).spread(...)`, which is what a lane of cards wants and what a page of
        groups does not: a whole page's leftover put into one gap is reported as a blank
        field in the middle instead of one at the foot.

        And not half above, half below, because a reader sees white and not regions. The
        white over the body is the `GUTTER` the page leaves under the heading, the white
        under it is `MARGIN`, so an even split still leaves 0.44in more at the foot than
        at the head -- and on the page this was written for, that difference decided
        whether the render reported a trailing field. The two are made equal instead,
        measured from the heading's edge and to the page's. A run with less slack than
        that difference sits on the safe margin rather than being pushed through it.

        What is under the body is what decides that, so a page that reserved the footer
        strip is the other case: the white under its body ends at the footer's hairline
        one `GUTTER` below it, which is the same `GUTTER` that is over it, and an even
        split already reads even. Correcting anyway lifted the run by the whole
        `MARGIN - GUTTER` -- measured on two reference pages, 0.44in more air above the
        run than below, which reads as content pushed down onto the foot.

        Because it moves the region and not a cursor, it answers for the page rather
        than for one lane: `columns`, `split_left` and a stack down each lane all come
        out of the same corrected band, so two lanes still end level. Where the lanes
        differ, the run to pass is the taller one.

        Not for a run something else is already spreading into the body: a
        `card_group(down=True)` handed the whole body puts the leftover between its own
        cards, so cutting the body to the sum of their heights first leaves them
        touching. Ask this where the page takes measured bands off a cursor, which is
        where the leftover has nowhere to go.

        Bands and gaps are the same thing here, as they are for `short_by` and `spread`:
        pass them in the order they occur, and a single list works too.

            frame = page().holding(grid.h, GUTTER, group.h)
            heading(slide, frame, T, title, kicker=section)
            down = stack(frame.body)

        A run with no slack is left as it is, and so is one that overruns: growing the
        body past the safe margin would put type where a projector crops, and an overrun
        is `take`'s to refuse, naming both numbers. Only the body moves -- the kicker,
        the title and the footer stay where the page put them, so this can be asked
        before or after `heading`.
        """
        if len(heights) == 1 and not isinstance(heights[0], (int, float)):
            heights = tuple(heights[0])
        if not heights:
            raise ValueError(
                "holding() takes the heights of the run the body has to hold, and was given none. "
                "Measure them first -- text_size(...).h, table_size(...).h, card_size(...).h, "
                "picture_size(...).h -- and pass the gaps between them as well"
            )
        slack = self.body.h - sum(float(height) for height in heights)
        if slack <= 0:
            return self
        # A body that stops a GUTTER short of the footer box is a page that reserved the
        # strip, and what is under it is that GUTTER rather than the page's margin.
        below = GUTTER if self.body.y1 <= self.footer.y0 - GUTTER / 2 else MARGIN
        above = min(slack, (slack + below - GUTTER) / 2)
        return self._replace(
            body=Box(self.body.x0, self.body.y0 + above, self.body.x1, self.body.y1 - (slack - above))
        )


# Why the box travels with the shape: over one run of 105 steps, thirteen moved a
# table's columns and five guessed the y of a band -- arithmetic these helpers had
# already done and did not say.
class Drawn(namedtuple("Drawn", "shape box")):
    """What a helper drew, and the box it actually covers.

    `shape` is the python-pptx object on its own: a text frame for the copy
    helpers, python-pptx's table for `table`, the shape for the rest. An attribute
    this tuple does not carry is looked for on the shape and then on the box, so
    `write(...).paragraphs`, `table(...).columns`, `heading(...).x1` and
    `overlaps([title, figure])` all read straight through.

    `box` is what was drawn and not what was asked for: a rule sits below the box it
    underlines, a picture keeps its own aspect inside the region it was given, and
    copy that does not fit comes back as a box taller than the one it was handed
    rather than as a claim that it fit.
    """

    __slots__ = ()

    def __getattr__(self, name):
        try:
            return getattr(self.shape, name)
        except AttributeError:
            return getattr(self.box, name)


class Cards(list):
    """The cards one `card_group` drew, with `box` for the region they cover together.

    A list and not a `Drawn`, for `Marks`'s reason below: a group is several cards and
    `first, second, third = card_group(...)` is how they are read. The box is measured
    off what was placed, so it is the run's own extent -- where the page carries on --
    and not the region the group was handed, which a group deliberately does not fill.
    """

    __slots__ = ("box",)

    def __init__(self, cards, box):
        super().__init__(cards)
        self.box = box


class Marks(list):
    """The shapes one `mark` drew, with `box` for the ink they cover.

    A list rather than a `Drawn`, because a mark is several shapes and `track, bar =
    mark(...)` is how they are read -- a two-field tuple would unpack into something
    else and say nothing about it. The box is measured off what was placed, which for
    a rating is the row of steps and not the cell it was centred in: a column is wide
    enough for a scale or it is not, and that is the number that says so.
    """

    __slots__ = ("box",)

    def __init__(self, shapes, box):
        super().__init__(shapes)
        self.box = box


def _ink_box(shapes):
    """The rectangle a set of placed shapes covers, read back off them in inches."""
    unit = Inches(1)
    return Box(
        min(shape.left for shape in shapes) / unit,
        min(shape.top for shape in shapes) / unit,
        max(shape.left + shape.width for shape in shapes) / unit,
        max(shape.top + shape.height for shape in shapes) / unit,
    )


# ------------------------------------------------------------- what it will take

# A renderer gives a line the face's own ascent, descent and gap before the
# paragraph's spacing multiplies it. Measured on this one: six 16pt lines set at
# spacing 1.15 came out on a 22.0pt pitch, and at spacing 1.0 on a 19.15pt one --
# 1.197 ems both times, in the Latin faces and in the CJK one. Reserving the type
# size alone is a fifth short, which is one line in five landing outside its box.
_LINE_BOX = 1.2
# What a *table cell* holding Han has to be given instead, which is a different
# question from the pitch above and has a different answer.
#
# A row height is a floor the renderer may raise, and it raised every one: swept over
# 32 renders (12, 14, 16 and 20pt against paddings of 0, 0.03, 0.06 and 0.12in, Han
# and Latin), the height a cell came back at less its padding was 1.400 to 1.425 em
# for Han at every size, against 1.155 to 1.173 for Latin. Charging 1.2 left each Han
# row 0.037in short at 12pt through 0.061in at 20pt, and a short row is not a cosmetic
# matter: the arithmetic the module hands back is then wrong about where the table
# ends. Measured on a nine-row Han table, `table_size(...).h` came back 0.44in short
# of the render, so a source note placed at the answer's own `box.y1` set its glyphs
# 0.08in inside the last row, and `rule(slide, drawn.box, T)` -- the module's own
# prescription for underlining a table -- landed 0.35in inside it, across a hairline.
# Anything drawn from `plan.heights` drifts by the same arithmetic, accumulating down
# the table: a four-row Han table put its last row's progress bar 52 percent above
# its own boundary rule, in the row above.
#
# Zero padding is exempt in the measurement -- the `body_h` floor already clears the
# renderer there -- but it is not exempted here: the floor is a maximum of the two,
# so a table that did not need this does not pay for it.
_CELL_LINE_BOX_HAN = 1.47
# What `write` and `points` spend on their own margins before a character is set.
_FRAME_SIDE = 0.04
_FRAME_ENDS = 0.02


def _line_h(size, spacing=1.0):
    """One line at `size`, set with `spacing`, in inches."""
    return size * _LINE_BOX * spacing / 72.0


class Run(namedtuple("Run", "text size bold colour")):
    """One stretch of a line with its own size, weight or colour.

    A paragraph handed to `write` may be a sequence of these instead of a string, and
    what the sequence buys is the one thing a whole-box style cannot say: the number in
    the accent and its unit in body ink, on one line. `no_anchor` asks for exactly that
    -- "set the number or the claim that carries it two steps up the size ladder" -- and
    until now the only way to comply was a second text box beside the first, or a
    run-level writer of the author's own. Two live authors wrote one; one of them used
    it for four lines of this shape:

        write(slide, box, [[Run("\u8425\u6536 "), Run("1.4 \u4ebf\u5143", size=16, bold=True, colour=ACCENT_INK)]], size=14)

    Every field but the text is optional and falls back to the `write` call's own, so a
    line that needs one emphasised word states one field on one run.
    """

    __slots__ = ()

    def __new__(cls, text, size=None, bold=None, colour=None):
        return super().__new__(cls, str(text), size, bold, colour)


def _paragraphs(text):
    """`text` as the list of paragraphs the copy helpers treat it as.

    A newline inside a string is one of them. `write` turns it into an `<a:br/>` and the
    renderer sets a second line, and this used to hand the measurers one paragraph -- so
    a band measured with `text_size` came back one line short and whatever sat under it
    was painted over. Two of the picture passages write a title that way and only got
    away with it by hard-coding the band's height.
    """
    # A Run is a tuple, so without these two both would be taken apart field by
    # field and set as four paragraphs of its repr: one handed in as the whole copy,
    # and one standing alone where a paragraph was expected.
    items = [text] if isinstance(text, Run) else (list(text) if isinstance(text, (list, tuple)) else [text])
    lines = []
    for item in items:
        if isinstance(item, Run):
            lines.append([item])
            continue
        if _is_runs(item):
            lines.append([one if isinstance(one, Run) else Run(one) for one in item])
            continue
        lines.extend(str(item).splitlines() or [""])
    return lines


def _is_runs(item) -> bool:
    """Whether this paragraph is a sequence of runs rather than a string."""
    return isinstance(item, (list, tuple)) and not isinstance(item, Run)


def _flat(line) -> str:
    """A paragraph's text, whichever way it was written, for measuring and for width."""
    return "".join(one.text for one in line) if _is_runs(line) else str(line)


def _largest(line, size):
    """The size a paragraph's line box is set by: its tallest run."""
    return max([one.size or size for one in line], default=size) if _is_runs(line) else size


# Where a Latin word may be broken without a hyphen being added, because the
# renderer adds none: after these, never inside a word.
_BREAK_AFTER = "-\u2010\u2013\u2014/"


def _tokens(line):
    """The pieces a line may break between: Latin words whole, Han characters apart.

    Anything from U+2010 up stands alone -- Han, the CJK punctuation, and the weak
    characters that sit between the two scripts -- and everything below it gathers
    into words, which is what stops `Forward-Looking` coming back as `Forward-Loo`
    and `king`.
    """
    pieces, word = [], ""
    for character in line:
        if ord(character) >= _WEAK_FROM or character.isspace():
            if word:
                pieces.append(word)
                word = ""
            pieces.append(character)
            continue
        word += character
        if character in _BREAK_AFTER:
            pieces.append(word)
            word = ""
    if word:
        pieces.append(word)
    return pieces


# Greedy rather than `ceil(the whole string / room)`, which cannot see the ragged end
# every greedy line leaves. Measured against the render over 2996 strings from eight
# delivered decks -- their paragraphs, their outlines and their source material -- ceil
# answered 84.0% of the wrapping ones and this answers 91.7%, and where ceil was out by
# two lines or more on four of them this is out by two on none.
def _wrapped(line, room, size, face, bold):
    """The lines this paragraph greedily breaks onto in `room` inches."""

    def fits(candidate):
        return _em_width(candidate, size, face) * (1.08 if bold else 1.0) <= room + 1e-9

    lines, current = [], ""
    for token in _tokens(line):
        if token.isspace() and not current:
            continue
        if fits(current + token):
            current += token
            continue
        if current:
            lines.append(current)
        current = "" if token.isspace() else token
        if fits(current):
            continue
        # A token wider than the box: the renderer breaks it rather than overflow, so
        # the count has to break it too. Reached with the token already in `current`,
        # because a word too wide to start a line used to fall past both branches and
        # be dropped -- and copy measured without one of its words answers that a size
        # fits which does not.
        #
        # `max(1, ...)`, because a box narrower than one character makes
        # `len(current) - 1` zero: the cut took nothing off the front, `current` came
        # back the length it went in at, and the loop never ended -- a hang, in a
        # generated build script, until the run's own timeout killed it. One character
        # is the smallest line there is, so it takes a line of its own and overflows
        # it. Overflow is measurable and gets reported off the render; dropping the
        # character instead would answer that copy fits in a column it cannot be set
        # in at all.
        while current and not fits(current):
            cut = max(1, len(current) - 1)
            while cut > 1 and not fits(current[:cut]):
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    if current:
        lines.append(current)
    return lines or [""]


def _copy_extent(text, width, size, face, bold, spacing=1.0):
    """(lines, widest line in inches, height in inches) for copy in a box `width` across.

    The height is summed line by line rather than read as the line count times the
    tallest line in the block: one 26pt heading over four lines of body copy is one
    tall line and four short ones, and five tall ones comes back nearly an inch over.
    """
    room = width - 2 * _FRAME_SIDE
    if room <= 0:
        raise ValueError(f"{width:.2f}in is no width to set copy in")
    lines, widest, tall = 0, 0.0, 0.0
    for line in _paragraphs(text):
        # A line of mixed runs sets at the largest of them, both for how much room it
        # takes across and for how tall its line box is -- a renderer lays the line out
        # to its tallest run, so measuring the whole string at the call's own size reads
        # a 26pt number as body copy and comes back short.
        at = _largest(line, size)
        broken = _wrapped(_flat(line), room, at, face, bold)
        lines += len(broken)
        tall += len(broken) * _line_h(at, spacing)
        for one in broken:
            # Bold sets wider than the estimate for the same string, by the same 1.08
            # a sized column allows for its header.
            widest = max(widest, _em_width(one, at, face) * (1.08 if bold else 1.0))
    return lines, widest, tall


def lines_needed(text, width, *, size=BODY_PT, font=None, bold=False):
    """How many lines this copy wraps onto in a box `width` inches across.

    Where every "does it fit" argument starts, and the one thing `write` will not
    say: it turns autofit off on purpose, so copy that does not fit runs out of its
    box rather than dropping under the type floor -- and the only place that showed
    was the render.
    """
    return _copy_extent(text, width, size, font, bold)[0]


def text_size(text, width, *, size=BODY_PT, font=None, bold=False, spacing=1.15):
    """The box this copy really needs at this width, before anything is drawn.

    Takes what `write` takes and hands back a box at the origin: `.h` is the height
    to ask a `stack` for, and `.w` is what the longest line actually sets, never more
    than the width given -- a four-word label in a 6in column measures the four
    words, which is what something placed beside it has to keep clear of.

    The estimate is `_em_width`'s: a class average per character with the theme's own
    face scaled in. It decides a layout, and the render-side measurements still catch
    what it misses.
    """
    _lines, widest, tall = _copy_extent(text, width, size, font, bold, spacing)
    return Box.at(0.0, 0.0, w=min(width, widest + 2 * _FRAME_SIDE), h=tall + 2 * _FRAME_ENDS)


def points_size(items, width, *, size=BODY_PT, font=None, spacing=1.25):
    """The box a bulleted list needs, which is not the box its copy needs.

    An item's mark hangs in a margin one and a half ems wide, so every line of every
    item is set in less room than the box is; and what separates two items is a
    paragraph setting rather than a blank line. Both are `points`'s own arithmetic,
    and a list stacked against `text_size` comes up short by both of them.
    """
    hang = size * 1.5 / 72.0
    _lines, widest, tall = _copy_extent(items, width - hang, size, font, False, spacing)
    gaps = max(0, len(_paragraphs(items)) - 1) * size * 0.45 / 72.0
    return Box.at(
        0.0,
        0.0,
        w=min(width, widest + hang + 2 * _FRAME_SIDE),
        h=tall + gaps + 2 * _FRAME_ENDS,
    )


def fits(what, box, *, size=BODY_PT, font=None, bold=False, spacing=1.15):
    """Whether it goes in that box. Yes or no, without drawing it.

    `what` is copy -- a string or a list of paragraphs, measured as `write` would set
    it -- or a box any of the size helpers handed back, so `fits(table_size(rows, T),
    band)` and `fits("...", band, size=LABEL_PT)` are one question asked of two
    different things.
    """
    needed = what if isinstance(what, Box) else text_size(what, box.w, size=size, font=font, bold=bold, spacing=spacing)
    return needed.w <= box.w + 1e-9 and needed.h <= box.h + 1e-9


# The ramp, largest first, walked upwards -- and stopping at the floor, because
# nothing here may answer with a size the measurements refuse.
#
# It answers a step and never a number between two, and that is the decision this
# constant carries. Both were built and rendered over forty cases: short CJK
# labels, mixed lines, sentences, card bodies, chevron steps, table cells, in the
# six faces a theme may name. Measured off `pdftotext -bbox`, the ramp left a
# median 11.6pt of clearance to the nearest edge of the box it was asked about and
# put ink past an edge in 2 of 40; the same search over every integer left 5.7pt
# and put ink past an edge in 7 of 40. The estimate underneath is `_em_width`'s,
# and `_FACE_WIDTH` records that it is 5 to 22 percent out per face -- so an answer
# that spends the last point of room is an answer that fits here and breaks on the
# viewer's machine. The ramp's coarseness *is* that reserve.
#
# And the coarseness is what keeps the answer still. Over a box growing a
# hundredth of an inch at a time from 0.60 to 6.00in, the ramp changed 0 to 3
# times and every change was one step up; every integer changed 13 to 16 times.
# On a one-character edit, 1 of 1296 ramp answers moved two steps and 163 of 1296
# integer answers moved two points or more.
_RAMP = (NUMBER_PT, TITLE_PT, LEAD_PT, BODY_PT, LABEL_PT)

# What the page gave up to fit. Written to rather than returned, because the caller
# that asks for a size is not always the one that would report the answer, and a
# concession nobody wrote down is the one thing the invariants say must not happen:
# `the_largest_step_this_copy_takes` used to walk the whole ramp and hand back the
# floor with nothing to say it had, so a page squeezed to the floor read exactly like
# a page that fit. `what_this_page_gave_up()` empties it, which is how a build reads
# one page's concessions without inheriting the last one's.
_GAVE_UP = []


def _gave_up(what, asked, got, detail=""):
    _GAVE_UP.append({"what": what, "asked": asked, "got": got, "detail": detail})


def what_this_page_gave_up():
    """Every concession made since this was last called, and clears the record.

    A concession is a size stepped down, a line broken where the copy did not ask
    for a break, or a figure shrunk to fit. Ask for it right after drawing a page and
    put it in the page's own note: the render shows the result and not the fact that
    something was surrendered to get there.

        for gone in what_this_page_gave_up():
            print(f"page {n}: {gone['what']} {gone['asked']} -> {gone['got']}")
    """
    given, _GAVE_UP[:] = list(_GAVE_UP), []
    return given


# What guessing a size costs, and what `wrap` is worth: ten live pages picked their own
# sizes -- thirteen distinct values from 10 to 32pt, not one of them off the ramp -- and
# over forty measured cases the wrapped and unwrapped answers differed in eleven.
def the_largest_step_this_copy_takes(text, box, *, font=None, bold=False, spacing=1.15, wrap=False, largest=TITLE_PT):
    """The biggest step of the ramp this copy still fits that box at.

    The other direction from `fits`, which answers yes or no at a size already chosen.
    Guessing instead of asking puts sizes off the ramp and type under the shape that
    holds it: a chevron label set at `LABEL_PT` in a shape 1.25in tall is a fifth the
    height of the thing it names, and the render is the only place that shows.

    So ask, and hand the answer to `write`:

        size = the_largest_step_this_copy_takes(label, step.box, font=F)

    Everything in a row shares one size or the row reads as five unrelated words,
    so ask for each and take the smallest -- the same `min` that levels a row of
    cards:

        size = min(the_largest_step_this_copy_takes(label, one.box, font=F)
                   for one, label in zip(steps, labels))

    `wrap` is the difference between a label and a paragraph, and it is the whole
    reason this takes an argument beyond the box: by default the copy stays on the
    lines you gave it -- one string is one line, a list of three is three -- so a
    four-character label in a wide shape is not answered by breaking it in half.
    Pass `wrap=True` for copy that is meant to reflow, a card's body or a band of
    prose, and the answer is the largest step whose wrapped block still fits. The two
    answers differ by as much as two steps, so the flag is not a detail.

    `align` and `anchor` are not arguments because the answer does not depend on
    them: a block of copy is the same size centred as it is top-left, and where it
    lands inside the box is `write`'s own answer to give back. What does depend on
    them is the air around it -- ask about the box the copy may cover, not the panel
    it sits on, and `box.inset(PAD)` is that box.

    `largest` is the step to stop at, and it is the author's to name because it is a
    decision about what the copy is rather than about whether it fits. The ramp is a
    set of roles -- kicker, label, body, lead, title, figure -- and this call answers
    the largest of them the copy still fits at, no further up than the one you name.
    A ceiling inferred from the copy is not offered: read off a character count, it
    calls `internationalization` a sentence and a fourteen-character CJK line with no
    spaces a label, so it caps a label and lets a line of prose come back at the size
    of the page's own title. A count of characters cannot tell those apart, and
    guessing wrong is worse than asking. Name `LEAD_PT` for a line that leads a band,
    `BODY_PT` for copy, `NUMBER_PT` when the copy really is the figure.
    `BODY_FLOOR_PT` means either that the floor is what fits or that nothing does --
    `fits(text, box, size=BODY_FLOOR_PT)` tells those two apart, and a False there
    wants a bigger box or shorter copy, not another build.
    """
    paragraphs = _paragraphs(text)
    if any(_is_runs(line) for line in paragraphs):
        raise ValueError(
            "this answers what one size a block of copy takes, and a run that states its own size is "
            "not part of that question. Ask about the copy as plain text, then set the emphasised run "
            "from the answer: size = the_largest_step_this_copy_takes(text, box); write(slide, box, "
            "[[Run(head, bold=True), Run(rest)]], size=size)"
        )
    if largest not in _RAMP:
        raise ValueError(
            f"{largest} is not a step of the ramp. Say which step to stop at -- LABEL_PT, BODY_PT, LEAD_PT, "
            "TITLE_PT or NUMBER_PT -- and never a size of your own: the point of asking is that the answer "
            "is one of those."
        )
    for size in _RAMP:
        if size <= largest and _copy_takes(paragraphs, box, size, font, bold, spacing, wrap):
            return size
    _gave_up("type size", largest, BODY_FLOOR_PT, _first_words(paragraphs))
    return BODY_FLOOR_PT


def _copy_takes(paragraphs, box, size, font, bold, spacing, wrap):
    """Whether `paragraphs` sit inside `box` at `size`, on the lines they were given."""
    lines, _widest, _tall = _copy_extent(paragraphs, box.w, size, font, bold)
    if not wrap and lines > len(paragraphs):
        return False
    if wrap:
        # A line break cannot fall inside a Latin word, so wrapping does not save one
        # that is wider than the box -- the renderer breaks it mid-word instead, and
        # the estimate that counted it as two lines would call that a fit.
        run = max((_unbroken(_flat(one)) for one in paragraphs), key=len, default="")
        if run and _em_width(run, size, font) * (1.08 if bold else 1.0) > box.w - 2 * _FRAME_SIDE + 1e-9:
            return False
    return lines * _line_h(size, spacing) + 2 * _FRAME_ENDS <= box.h + 1e-9


def _copy_ink(box, needed, align, anchor):
    """Where in `box` a `needed`-sized block of copy lands, given its alignment.

    A text box is as wide and as tall as the region it was handed; the copy in it is
    only as big as the copy. The difference is what the next thing on the page has to
    clear, and reporting the region instead is how a page ends up with three inches
    of white under one line of type.
    """
    width, height = min(box.w, needed.w), needed.h
    if align == PP_ALIGN.CENTER:
        x0 = box.x0 + (box.w - width) / 2
    elif align == PP_ALIGN.RIGHT:
        x0 = box.x1 - width
    else:
        x0 = box.x0
    if height >= box.h or anchor == MSO_ANCHOR.TOP:
        y0 = box.y0
    elif anchor == MSO_ANCHOR.BOTTOM:
        y0 = box.y1 - height
    else:
        y0 = box.y0 + (box.h - height) / 2
    return Box.at(x0, y0, w=width, h=height)


# Why `footer` defaults off: twenty-nine live pages out of twenty-nine wrote `page()`
# and none of them drew anything in the footer, so every one spent `_FOOTER_H + GUTTER`
# on a strip that stayed empty.
def page(kicker=True, footer=False):
    """The regions of an ordinary page, inside the safe area.

    `kicker` is the small section label over the title; `footer` is the line a
    source citation goes on. Both take their space out of the body when asked for,
    so a page that skips them gets the room rather than leaving a hole.

    The footer is off by default: reserving it spends `_FOOTER_H + GUTTER` on a strip
    at the bottom, which is 0.58in of a 7.5in page and 12% of the body. Ask for it on
    the pages that cite.

    The body runs from under the heading to the safe margin, which is the room the page
    may use and not the room it uses. A page holding less than that says so:
    `page().holding(*heights)` cuts the body to the measured run and splits the leftover
    above and below it, instead of leaving all of it at the foot of the page.
    """
    top = _HEAD_TOP
    kicker_box = Box(MARGIN, top, CANVAS_W - MARGIN, top + _KICKER_H)
    if kicker:
        top += _KICKER_H + 0.04
    title_box = Box(MARGIN, top, CANVAS_W - MARGIN, top + _TITLE_H)
    top += _TITLE_H + _TITLE_AIR + GUTTER
    bottom = CANVAS_H - MARGIN
    footer_box = Box(MARGIN, bottom - _FOOTER_H, CANVAS_W - MARGIN, bottom)
    if footer:
        bottom -= _FOOTER_H + GUTTER
    return Frame(kicker_box, title_box, Box(MARGIN, top, CANVAS_W - MARGIN, bottom), footer_box)


# How far `_em_width` can run under the render. Measured at the size a footer note is
# set at, over seven real notes: the worst came out 3.8% wider than the estimate, and a
# Latin-only line 12% narrower -- so the risk is all on the mixed CJK side and this is
# five percent rather than the two an earlier sample of longer strings suggested.
# Charged against a lane wherever the answer is a refusal, so the one-line answer is not
# the one the render disagrees with. What it costs is a note between 9.80in and 10.09in
# being asked to shorten when it would just have fitted; what it buys is that a note
# which renders onto a second line is not passed as one, and the second line of a footer
# is drawn into the page's own margin.
_ESTIMATE_SLACK = 1.05


def footer(slide, box, theme, *, note=None, number=True, colour=None, font=None, cjk_font=None):
    """The page's own bottom edge: a hairline, an optional note, and the page number.

    `page(footer=True)` has always handed back a footer region and nothing drew in it,
    so twenty-nine live pages out of twenty-nine reserved the strip and left it empty.
    What that costs is not the number: a body ending two thirds down an otherwise blank
    page reads as unfinished, and a rule across the foot is what tells a reader the
    page ends there because it was meant to.

    The number is a real `slidenum` field rather than a digit, so it stays right when a
    page is inserted ahead of it. Every bundled template defines a page-number
    placeholder on its master and none of them puts one on a page: python-pptx does not
    clone a footer placeholder onto a slide, so a deck built by cloning came out with
    no page numbers at all.

    `colour` is for a template that paints its own band across the foot -- the note and
    the number have to read on that band, and the theme's `muted` is chosen against the
    page's ground.

    `note` is one line. The strip is the page's bottom edge and there is nothing under
    it but the margin, so a note that wraps is a note drawn outside the page rather than
    a taller footer -- and with autofit off that is what it did, silently. So the length
    is measured here and a second line comes back as this call refusing, with the width
    it had and the width it needed.
    """
    ink = _paint(theme, colour, "muted")
    line = Box.at(box.x0, box.y0, w=box.w, h=0.008)
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *line.pptx())
    bar.fill.solid()
    bar.fill.fore_color.rgb = _rgb(_paint(theme, None, "grid"))
    bar.line.fill.background()
    # Directly under the hairline and the rest of the strip is the note's: started
    # 0.10in down, its 0.20in box was 0.07in shorter than the single 12pt line it holds,
    # and the last 0.04in of every note on every page set below the strip.
    top = box.y0 + line.h
    room = max(0.2, box.h - line.h)
    lane = box.w - 1.6
    if note:
        # `_em_width` runs within a percent of the render, measured over five strings at
        # two sizes, over-estimating Latin and under-estimating CJK. The lane is charged
        # that percent, so a note that only just fits is not reported as fitting.
        # The width the test above actually allows, so the refusal can name it. Printing
        # the bare lane instead told an author a 10.21in note did not fit 10.29in: true,
        # because the lane is charged the estimate's slack and the frame's own margins,
        # and unusable, because the two numbers printed say it fits. That author cut the
        # note to just under the width it was shown and the next build failed the same
        # way -- one iteration spent on a message that reported a different pair of
        # numbers than the decision used.
        allowed = lane / _ESTIMATE_SLACK - 2 * _FRAME_SIDE
        width = _em_width(note, KICKER_PT, font)
        if lines_needed(note, lane / _ESTIMATE_SLACK, size=KICKER_PT, font=font) > 1:
            over = max(1, round(len(note) * (width - allowed) / width)) if width > 0 else 1
            raise ValueError(
                f"a footer note is one line and this one wraps: {width:.2f}in of copy where the strip "
                f"allows {allowed:.2f}in beside the page number -- about {over} characters too many. "
                f"(The strip is {lane:.2f}in wide; the rest is the frame's margins and the margin the "
                f"width estimate is charged against the render.) Under the strip is the page's own "
                f"margin, so a second line is drawn off the page. Shorten it: the source stays and the "
                f"least of what follows it goes."
            )
        write(slide, Box.at(box.x0, top, w=lane, h=room), note,
              size=KICKER_PT, colour=ink, font=font, cjk_font=cjk_font, align="left")
    if number:
        holder = write(slide, Box.at(box.x1 - 1.2, top, w=1.2, h=room), "00",
                       size=KICKER_PT, colour=ink, font=font, cjk_font=cjk_font, align="right")
        _as_slide_number(holder)
    return Drawn(_bare(bar), line)


def _as_slide_number(drawn):
    """Turn a written run into the field PowerPoint renumbers.

    The run's own properties move onto the field rather than being dropped with it, or
    the number comes back at the theme's default size in the theme's default colour --
    the one thing an author cannot see from the file they just wrote.
    """
    # `write` hands back a Drawn whose first field is the *text frame*, not the shape,
    # so both have to be accepted here or the field is never written and the number
    # stays the literal that was measured with.
    target = getattr(drawn, "shape", drawn)
    frame = getattr(target, "text_frame", target)
    if not getattr(frame, "paragraphs", None):
        return drawn
    paragraph = frame.paragraphs[0]._p
    run = paragraph.find(f"{{{_A}}}r")
    if run is None:
        return drawn
    field = paragraph.makeelement(
        f"{{{_A}}}fld", {"id": "{2B7A9F1C-4E3D-4A5B-9C8D-1F2E3A4B5C6D}", "type": "slidenum"}
    )
    properties = run.find(f"{{{_A}}}rPr")
    if properties is not None:
        # Moved, not copied: the run this came off is replaced on the next line.
        field.append(properties)
    text = field.makeelement(f"{{{_A}}}t", {})
    text.text = "1"
    field.append(text)
    paragraph.replace(run, field)
    return drawn


def rule(slide, box, theme, thickness=0.03, colour=None):
    """A hairline under a box: a chart's baseline, a table's header, a real divider.

    In the accent unless told otherwise, and `theme["grid"]` is the quiet one to ask
    for where the line is structure rather than emphasis. Not for under a title --
    `heading` drew one there once and stopped, because the same mark repeated under
    every title in a deck is decoration by the time a reader reaches page three.

    The box it hands back is the rule, not the box the rule was asked for: a rule
    starts 0.06in *below* what it underlines and is at most 1.05in long, so an author
    who read its own argument back got neither where the mark is nor how far down the
    page it reaches.
    """
    drawn = Box.at(box.x0, box.y1 + 0.06, w=min(1.05, box.w), h=thickness)
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *drawn.pptx())
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(_paint(theme, colour, "accent"))
    shape.line.fill.background()
    return Drawn(_bare(shape), drawn)


def plane(slide, box, theme, tint="surface", radius=True, opacity=1.0):
    """Paint a region, so the page's divisions are visible rather than implied.

    `tint` names a theme colour -- "surface" for a grouped zone, "accent_soft" for
    the one zone that carries the page's answer. No outline: an edge and a tint
    together read as a form to fill in.

    `opacity` below 1 makes it a veil rather than a ground, which is the one way to
    set copy over a photograph and have it stay readable: the picture shows through
    and the type has something even to sit on. Three of the twelve pages of a
    reference deck are a full-bleed photograph with a title on it, and without this
    an author has two choices, both bad -- white type straight onto the picture, which
    `unreadable` reports where the picture happens to be pale, or an opaque panel,
    which is the photograph thrown away. A dark veil at 0.35 to 0.55 over a photograph
    is the usual setting.

    Rounded by default, and `radius=False` for a band that runs to the trim, where a
    corner radius on an edge the page cuts off reads as a mistake. The radius is
    `PLANE_RADIUS_IN` of an inch and not a share of the shape: the adjustment python-pptx
    takes is a fraction of the short side, so one number gave a 4.8in panel a visible
    curve and a 0.6in strip a 2.6pt one that reads as square. Every plane on a page
    should carry the same corner.
    """
    if not 0.0 < opacity <= 1.0:
        raise ValueError(f"opacity is a share of 1, so {opacity!r} is not one: 0.35 to 0.55 veils a photograph")
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE, *box.pptx()
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(_paint_of(theme, tint))
    if opacity < 1.0:
        _veil(shape, opacity)
    shape.line.fill.background()
    _bare(shape)
    if radius:
        short = min(box.w, box.h)
        shape.adjustments[0] = min(PLANE_RADIUS_IN / short, 0.5) if short > 0 else 0.06
    return Drawn(shape, box)


def _veil(shape, opacity):
    """Set the fill's alpha, which python-pptx has no property for."""
    from pptx.oxml.ns import qn

    fill = shape.fill._xPr.find(qn("a:solidFill"))
    colour = fill.find(qn("a:srgbClr"))
    for stale in colour.findall(qn("a:alpha")):
        colour.remove(stale)
    alpha = colour.makeelement(qn("a:alpha"), {"val": str(int(round(opacity * 100000)))})
    colour.append(alpha)


def _with_faces(theme, font, cjk_font):
    """`theme`, with the faces a caller named in place of the ones it carries.

    The face comes from the page and not from the theme -- `house_style` measures what
    a template's own pages are set in, and a template's theme can say 微软雅黑 where
    every page is Arial. Measure and draw in one face or the other: a card measured
    with `card_size(font=FACE)` and drawn in the theme's face is two faces.

    Every helper reads its faces off the theme it is handed, so handing it a theme is
    the whole of it. Unnamed faces leave the theme alone rather than copying it.
    """
    if font is None and cjk_font is None:
        return theme
    faces = dict(theme)
    if font is not None:
        faces["font_family"] = font
    if cjk_font is not None:
        faces["cjk_font_family"] = cjk_font
    return faces


def _a_share(where, fraction):
    """`(fraction, 1 - fraction)`, or a refusal saying what the number is.

    A share of the box, not a measurement of it: `body.split_top(1.22)` beside
    `split_left(0.70)` in the same program reads the first as inches, and 1.22 makes
    the second weight -0.22, which is a box with negative height. Unrefused, that
    surfaces calls later as "2 parts and 0.28in gutters do not fit in 8.13x-1.09in",
    naming neither `split_top` nor 1.22.

    Refused here because a share outside 0 to 1 cannot mean anything else, so there is
    no reading of it to preserve and no page this costs.
    """
    try:
        share = float(fraction)
    except (TypeError, ValueError):
        raise ValueError(f"{where} takes a share of the box, not {fraction!r}") from None
    if not 0.0 < share < 1.0:
        raise ValueError(
            f"{where}({fraction}) -- the number is the share of the box the first part takes, "
            f"between 0 and 1, not a measurement in inches. For a part {fraction}in "
            f"{'tall' if where == 'split_top' else 'wide'}, `stack(box).take({fraction})` takes it "
            f"and leaves the rest"
        )
    return (share, 1.0 - share)


def _paint_of(theme, tint):
    """The colour a tint names, or the tint itself where it is already one.

    Through `_paint`, so that a misspelt role is a sentence rather than a hex-parser
    error naming neither the argument nor the mistake: unrouted, `plane(tint="accnet")`
    reaches the parser as its last two characters. Same for the faces -- the theme
    carries two typefaces and a list of series, and `tint="font_family"` is not a
    colour.
    """
    return _paint(theme, tint, "surface")


def _ink_on(plane_colour, *inks):
    """Whichever of these inks can be read on that plane.

    `tint` opens the plane to the rest of the palette -- `_PAINTS` offers the accent,
    and a template's own pages put their blocks on the accent and not on a tint of the
    ink -- and on an accent plane `foreground` and `muted` are the plane's colour and a
    grey a shade off it: on #1A3397, `muted` measures 1.9:1 where the ground reads
    11:1.

    Nothing to clear, so no bar to clear it: between inks the page already holds, the
    one furthest from the plane is the one a reader can see. On a near-white plane the
    answer is the ink it always was -- black on #F0F0F0 is 18.9:1 against white's
    1.13:1 -- so a page that names no tint draws exactly as it did.
    """
    return max(inks, key=lambda ink: _contrast(ink, plane_colour))


def _shows_on(plane_colour, accent, otherwise):
    """The accent, unless the plane it would be drawn on is that same accent.

    Not `_ink_on`, and not a bar either. Asked to pick the ink furthest from a plane,
    `_ink_on` is right, because between two inks the page already holds there is nothing
    to decide. An icon is not that question: the accent is a design decision and the
    other candidate is a fallback, so a contest between them has one outcome --
    `_ink_on(plane, accent, title_ink)` kept the accent 0 times out of 16 bundled
    templates and 0 times in a 20,000-pair sweep, because `title_ink` is already the
    better-contrasting of black and white and luminance is monotone. Every card icon in
    every deck came out black.

    A contrast floor was the first repair and it was worse than nothing: at 3.0 it still
    took the accent away from 6 of the 16, including every template whose accent is a
    pale green or lime, which are exactly the decks whose icons were the point. The
    accent had been drawn unconditionally for the life of this helper and no page had
    complained. So the only case left is the one the contest was reaching for -- an
    accent icon on an accent plane, where the icon is not dim but absent.
    """
    return otherwise if str(accent).upper() == str(plane_colour).upper() else accent


def _contrast(one, two):
    """WCAG contrast between two colours, the lighter over the darker."""
    first, second = _luminance(one), _luminance(two)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def _luminance(colour):
    """Relative luminance of #RRGGBB, or of the same six digits without the hash."""
    text = str(colour)
    body = text[1:] if text.startswith("#") else text
    channels = []
    for pair in (body[0:2], body[2:4], body[4:6]):
        value = int(pair, 16) / 255
        channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def write(
    slide,
    box,
    text,
    *,
    size=BODY_PT,
    colour="#000000",
    font=None,
    cjk_font=None,
    bold=False,
    align="left",
    anchor="top",
    spacing=1.15,
):
    """Copy in a box, wrapping inside it and never shrinking to fit.

    The box is the box: autofit is off, so a page that does not fit comes back as a
    measurement instead of as type quietly dropping below the floor.

    `font` is the Latin face and `cjk_font` its CJK companion -- pass both from the
    theme (`T["font_family"]`, `T["cjk_font_family"]`) and one line of mixed text
    comes out right: "TarViS" in the Latin face, 目标查询 in the CJK one. Naming only
    the Latin face leaves every Han character to the viewer's fallback.

    The box it hands back is the copy's, not the region's: as wide as the longest
    line really sets and as tall as the lines really take, placed by `align` and
    `anchor`. So the next thing down the page is `written.box.y1` and not a number
    the author picked; and copy that overruns says so, by coming back taller than the
    box it was given. `text_size` is the same measurement asked before drawing.
    """
    horizontal = _named(_ALIGNS, align, "align")
    vertical = _named(_ANCHORS, anchor, "anchor")
    left, top, width, height = box.pptx()
    frame = slide.shapes.add_textbox(left, top, width, height).text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.margin_left = frame.margin_right = Inches(_FRAME_SIDE)
    frame.margin_top = frame.margin_bottom = Inches(_FRAME_ENDS)
    frame.vertical_anchor = vertical
    lines = _paragraphs(text)
    for index, line in enumerate(lines):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.alignment = horizontal
        para.line_spacing = spacing
        pieces = line if _is_runs(line) else [Run(line)]
        for piece in pieces:
            run = para.add_run()
            run.text = piece.text
            run.font.size = Pt(piece.size if piece.size is not None else size)
            run.font.bold = bold if piece.bold is None else piece.bold
            run.font.color.rgb = _rgb(colour if piece.colour is None else piece.colour)
            if font:
                run.font.name = font
            if cjk_font:
                _east_asian(run, cjk_font)
    needed = text_size(lines, box.w, size=size, font=font, bold=bold, spacing=spacing)
    return Drawn(frame, _copy_ink(box, needed, horizontal, vertical))


def _east_asian(run, name):
    """Set the run's east-asian face, which python-pptx has no property for.

    `run.font.name` writes `a:latin` only, and a renderer picks the face for a Han
    character from `a:ea`. Without this the two are decided by different rules and a
    mixed line comes out in two unrelated designs.
    """
    properties = run.font._rPr  # noqa: SLF001 -- the only way to reach a:ea
    for existing in properties.findall(f"{{{_A}}}ea"):
        properties.remove(existing)
    element = properties.makeelement(f"{{{_A}}}ea", {"typeface": name})
    latin = properties.find(f"{{{_A}}}latin")
    if latin is not None:
        latin.addnext(element)
    else:
        properties.append(element)


def table(slide, box, rows, theme, *, weights=None, size=LABEL_PT, numeric_from=None,
          style="header_tint", emphasize_rows=(), emphasize_columns=(), group_rows=None,
          indent_rows=(), total_rows=(), marks=None, header_size=None, align=None,
          rule_pt=None, grid_pt=None, row_height=None, header_height=None,
          padding=None, fill=True, column_rules=True, banding=False, fills=None):
    """A shortcut for the ordinary table. Every part of it is yours to overrule.

    python-pptx hands you the Office default, and the default is why tables come out
    looking cheap: a white hairline around every cell, banding on, and a header style
    that fights whatever palette the deck is in. On a dark page the grid is the loudest
    thing on the slide.

    So the defaults here draw the other kind: quiet lines at one weight, no banding,
    no filled column -- and the header on the theme's own `accent_soft`, because a
    header row separated from its data by a line alone reads as a fourth data row. The
    tint was a keyword away and a delivered deck's table was called ugly for not
    turning it: a default is what ships. The rule between the columns was off here too, on the
    reading that alignment already told one column from the next -- and that reading
    expired when every non-numeric column started being centred, because a centred
    cell has no visible edge to be centred against. `column_rules=False` takes it back
    off for a table read across one row at a time.

    **What every style does draw is a frame, a rule at every row boundary and a rule
    between the columns** -- the four outer edges in the theme's `muted` tone and the
    interior lines in the quieter `grid` one, all of them at `grid_pt`. That is a grid
    and it is not the Office grid: one weight and two quiet tones, with no banding
    under it and no blue header over it, where `add_table` gives every one of 25 cells
    a white hairline on a banded, gallery-styled table whatever palette the deck is in.
    Without the frame there are three lines that each end in mid-air -- a rule under
    the header, a hairline under the last row, and no side of any kind -- which reads
    as unfinished rather than restrained. Without the row rules a four-column
    comparison whose cells wrap onto two lines says nothing about where one row ends
    and the next begins, and the reader counts baselines to work out which cell belongs
    to which label. Without the column rules, on the rows a delivered deck shipped with
    every interior vertical written as noFill, a five-column table of figures read
    "2012" and "8" as one cell and a three-column table of phrases read as one run-on
    line. All of them are at `grid_pt` rather than `rule_pt`, so the
    accent rule under the header stays the heaviest line and the table is still read
    from it. A default is what ships, so the default has to be presentable with no
    keyword turned.

    **Those are defaults and not a house rule.** `column_rules=False` takes the line
    between the columns off, `fills` paints any cell, row or column in a colour you name,
    `banding` tints alternate rows, `align` sets each column's alignment by name,
    `header_size` sets the header's type independently of the body's, `rule_pt` and
    `grid_pt` set the two line weights in points, and `row_height`, `header_height`
    and `padding` set the rows' own measurements. A page whose table wants its columns
    left open, or its third column painted, should have it. And when
    what a page needs is not a variation on this table at all -- merged cells, a
    grouped header spanning three columns, an icon inside a cell, a sparkline down a
    column -- draw it out of `Box`, `columns`, `stack`, `lines_needed`, `write` and
    `plane` instead. `deck/build/references/tables.md` has that worked through; nothing here is
    the only way to put a grid on a page.

    `rows` is a list of lists of strings, the first being the header. **Alignment is
    read off the cells**: a column every one of whose entries is a figure is
    right-aligned, because a column of numbers that is not right-aligned cannot be
    compared down its length, and a column of prose is left-aligned. Right-aligning
    everything but the first column on the assumption that a table is labels and
    figures leaves a four-column comparison of sentences ragged down three left edges.
    `numeric_from` takes an index for a table the measurement reads differently, and
    `align` names every column outright. `style` is optional and applies
    to this table only: `header_tint` is the default and tints the header row,
    `minimal` takes that tint back off for a table that has no header to speak of, and
    `compact` reduces row padding for a dense lookup.
    `row_rules` is the fourth, and it is now the same table as `minimal`: the row
    boundaries it used to switch on are what every style draws, and the name is still
    accepted so that a script already passing it keeps building. `emphasize_rows` tints
    selected body-row indices when they carry the page's point.

    **Columns are sized from what they hold**, so a 22-character benchmark name gets
    the room it needs and a 4-character metric does not. Equal columns leave you
    shortening headers and guessing at `weights` to buy room the measurement already
    finds. `weights` is still there for a table that wants a column wider than its
    content.

    **A table spans the box it is given**, in those proportions. So the box is where
    you say how much room this table gets: hand it `frame.body` and it runs margin to
    margin like the heading over it, hand it one column of a `split_left` and it stops
    there. A two-column table of five figures does not want the whole page -- give it
    a column and put something beside it.

    Beyond the grid, five things a table has to be able to say. `emphasize_rows` and
    `emphasize_columns` tint the row or the column carrying the page's point in the
    same soft accent, so a table can name the one option under comparison as readily
    as the one criterion. `group_rows` is `{row: "name"}`: that row becomes one band
    across the table, in the theme's surface, carrying the group's name -- its own
    cells stay empty and never set a column's width. `indent_rows` steps a detail
    row's label in by one PAD and sets it in the muted tone, and `total_rows` sets a
    row bold under a rule in that same muted tone -- darker than the hairline every
    boundary already carries, which is what says the numbers above it were added up
    rather than merely listed.

    `fills` is `{(row, column): colour}`, and either coordinate may be None for "all
    of them": `{(None, 3): "accent_soft"}` paints column 3, `{(2, None): "surface"}`
    paints row 2, `{(2, 3): "#FFEECC"}` paints one cell. The colour is any role the
    theme carries or a literal, so a deck that stated `ours` once in its palette can
    fill a column with it. That is the difference from `emphasize_columns`, which
    picks the colour for you: use the emphasis where the page is saying "this is the
    one", and `fills` where the page is saying which colour.

    `marks` is `{(row, column): "kind[:value][:colour]"}`, drawn over the cell by
    `mark`: `harvey:3.5`, `status_dot:accent`, `delta`, `progress:76%`, `check`,
    `cross`, `partial`. A mark that needs a number and is not given one reads the
    cell's own string, so `{(3, 4): "progress"}` on a cell holding "76%" says it once.
    Row 0 is the header and takes none of these.

    **A row is as tall as the lines its cells really wrap onto**, counted at the
    widths the columns really get. A declared row height is only a floor: the renderer
    grows every wrapped row and each boundary below drifts down, so a rule drawn as a
    free rectangle at the boundary the arithmetic named comes down through a row's
    copy. The rules are the cells' own borders, so they move with the row whatever the
    renderer does with it, and the heights are measured so that it has less to do.

    **And the rows spread into the box** rather than leaving its bottom third white.
    `fill=False` keeps the table at the size its content asks for.

    Returns the table and the box it fills, which is as wide as its content wants, as
    tall as `box` where there is slack to spread into, and **taller than `box` when
    the rows need more than it gives** -- a row is never squeezed under the line in
    it, so the height is the rows' and not the box's. So the next thing on the page
    goes at `written.box.y1`, which is where this table really ends, rather than at
    the bottom of the box handed in. `table_size` is the same arithmetic, asked before
    the table is drawn: with the same `box` and the same keywords it answers this same
    height, taller box included.
    """
    plan = _table_geometry(
        rows,
        theme,
        weights=weights,
        size=size,
        style=style,
        group_rows=group_rows,
        marks=marks,
        room=box.w,
        header_size=header_size,
        indent_rows=indent_rows,
        row_height=row_height,
        header_height=header_height,
        padding=padding,
        fill=fill,
        room_h=box.h,
    )
    widths, heights, width = plan.widths, plan.heights, plan.width
    groups, cell_marks = plan.groups, plan.marks
    # The plan's rows and not the caller's: a mark that could be said in its cell was
    # put into the copy there, and the widths and heights were measured off that.
    rows = plan.rows
    head_pt = size if header_size is None else header_size
    ends = _CELL_ENDS if padding is None else float(padding)
    grid_w = GRID_RULE_PT if grid_pt is None else float(grid_pt)
    frame_ink = theme.get("muted", theme["foreground"])
    emphasize_rows = {_body_row(index, len(rows), "emphasize_rows") for index in emphasize_rows}
    columns = len(rows[0])
    emphasize_columns = {_column_index(index, columns) for index in emphasize_columns}
    indented = {_body_row(index, len(rows), "indent_rows") for index in indent_rows}
    totals = {_body_row(index, len(rows), "total_rows") for index in total_rows}
    aligns = _cell_aligns(align, columns, numeric_from, rows, cell_marks)
    painted = _cell_fills(fills, len(rows), columns, theme)
    banded = set(range(2, len(rows), 2)) if banding else set()
    edges = _rule_edges(
        len(rows), totals, theme,
        HEADER_RULE_PT if rule_pt is None else float(rule_pt),
        grid_w,
    )
    shape = slide.shapes.add_table(
        len(rows), columns, Inches(box.x0), Inches(box.y0), Inches(width), Inches(plan.height)
    )
    tbl = shape.table
    _drop_gallery_style(tbl)
    tbl.first_row = True
    tbl.horz_banding = False
    tbl.vert_banding = False

    for index, column_width in enumerate(widths):
        tbl.columns[index].width = Inches(column_width)

    for index, row in enumerate(tbl.rows):
        row.height = Inches(heights[index])

    # Merged before a word is written: merging carries the spanned cells' text into
    # the origin, so a band assembled afterwards would hold every placeholder in the
    # row. An unmerged band would also wrap its name inside column one the moment
    # the name is long.
    if columns > 1:
        for index in groups:
            tbl.cell(index, 0).merge(tbl.cell(index, columns - 1))

    for r, line in enumerate(rows):
        for c, value in enumerate(line):
            cell = tbl.cell(r, c)
            cell.fill.background()
            if r in groups:
                cell.fill.solid()
                cell.fill.fore_color.rgb = _rgb(theme["surface"])
            elif (r, c) in painted:
                cell.fill.solid()
                cell.fill.fore_color.rgb = _rgb(painted[(r, c)])
            elif r == 0 and style == "header_tint":
                cell.fill.solid()
                cell.fill.fore_color.rgb = _rgb(theme["accent_soft"])
            elif r in emphasize_rows or c in emphasize_columns:
                # Under a tinted header the emphasised row is a second tint, not the
                # header's: a live scoring table set its header and its winning row in
                # the same accent_soft, and the winner read as a second header.
                cell.fill.solid()
                cell.fill.fore_color.rgb = _rgb(
                    _emphasis_tint(theme) if style == "header_tint" else theme["accent_soft"]
                )
            elif r in banded:
                cell.fill.solid()
                cell.fill.fore_color.rgb = _rgb(theme["surface"])
            lines = dict(edges.get(r, {}))
            # The two upright sides of the frame. `_rule_edges` holds the two flat
            # ones, which are a row's business; a side belongs to the first and last
            # column and there is nothing else in this loop's shape to hang it on.
            # The interior rules first and the frame over them, because a cell on
            # either end owns one of each and the frame is the one that has to win.
            # A group row is skipped: it is a single merged cell with no interior
            # boundary to draw, and a rule inside it would cross its name.
            if column_rules and columns > 1 and r not in groups:
                if c:
                    lines["lnL"] = (theme["grid"], grid_w)
                if c < columns - 1:
                    lines["lnR"] = (theme["grid"], grid_w)
            if c == 0:
                lines["lnL"] = (frame_ink, grid_w)
            # A group row's right side belongs to the cell that owns the span: a
            # border written on a spanned cell is not drawn, so the band would break
            # the frame open on that side.
            if c == columns - 1 or (r in groups and c == 0):
                lines["lnR"] = (frame_ink, grid_w)
            _strip_borders(cell, lines)
            cell.margin_left = cell.margin_right = Inches(_CELL_SIDE)
            # One step in, and the step is the page's own padding rather than a number
            # invented for this table, so a detail row lines up with everything else.
            if c == 0 and r in indented:
                cell.margin_left = Inches(_CELL_SIDE + PAD)
            cell.margin_top = cell.margin_bottom = Inches(ends)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            if r in groups and c:
                continue
            frame = cell.text_frame
            frame.word_wrap = True
            para = frame.paragraphs[0]
            marked = None if r in groups else plan.glyphs.get((r, c))
            steps = ()
            if marked is None:
                para.text = groups[r] if r in groups else str(value)
            else:
                steps = _write_marks(para, marked)
            # The header takes its column's alignment, not its own: a left-aligned
            # "差值" over a right-aligned column of numbers sits over nothing.
            # A group's name is a heading over the rows under it, so it stays left.
            if r in groups:
                para.alignment = PP_ALIGN.LEFT
            else:
                para.alignment = aligns[c]
            for run in para.runs:
                run.font.size = Pt(head_pt if r == 0 else size)
                run.font.bold = r == 0 or r in groups or r in totals
                # A detail row's label in the muted tone is the other half of the
                # indent: the hierarchy reads down the column without a single rule.
                quiet = c == 0 and r in indented
                run.font.color.rgb = _rgb(theme.get("muted", theme["foreground"]) if quiet else theme["foreground"])
                if theme.get("font_family"):
                    run.font.name = theme["font_family"]
                if theme.get("cjk_font_family"):
                    _east_asian(run, theme["cjk_font_family"])
            # After the loop above, which paints every run one colour: a scale reads
            # only because the steps that are not filled are visibly not filled.
            for run, filled in steps:
                run.font.color.rgb = _rgb(
                    _paint(theme, marked[1], "accent") if filled else theme.get("muted", theme["foreground"])
                )
                run.font.size = Pt(_mark_pt(head_pt if r == 0 else size))
                run.font.name = _MARK_FACE

    for (r, c), (kind, value, paint) in sorted(cell_marks.items()):
        # The cell says it itself now, and drawing it again would put two of them on
        # the page -- one of which drifts.
        if (r, c) in plan.glyphs:
            continue
        text = "" if (r in groups and c) else str(rows[r][c])
        cell = _cell_box(box, widths, heights, r, c)
        reading = text if value is None and kind in _NUMERIC_MARKS else value
        room = _mark_room(
            cell, text, size, aligns[c], theme.get("font_family"), _mark_scale(size, ends),
            lane=None if kind in _FILLING_MARKS else _mark_width(kind, reading, _mark_scale(size, ends)),
        )
        mark(slide, room, theme, kind, reading, colour=paint)
    return Drawn(tbl, Box.at(box.x0, box.y0, w=width, h=plan.height))


# What a cell spends on its own margins, a side and an end.
_CELL_SIDE = 0.10
_CELL_ENDS = 0.03

# The two weights a table draws at, in points, which is the unit a line weight is
# asked for in: the accent rule under the header, and everything quiet -- the frame,
# the row hairlines, a total's rule, a column rule. The quiet weight is what has to
# clear the render: at 1pt it is 1.5px at 110dpi, and in the `grid` tone that came
# back as no line at all, so a four-column table's rows ran together. 2.5pt holds a
# line at every dpi a render is taken at. The header rule stays the heavier of the
# two -- `_rule_edges` reads the table from it -- so it moves with the quiet one.
HEADER_RULE_PT = 3.5
GRID_RULE_PT = 2.5

# How much air a row may take to fill a box it does not fill on its own, in lines. Two
# and a half: rendered at every row count over a 4.67in body, five rows and up fill it
# exactly, four stop 0.53in short at 1.04in a row and still read as rows, and past that
# the leftover white is the honest sign that a three-row table does not want a whole
# page. One line's worth -- what this was -- left a five-row table 1.50in short of the
# body every other element on the page ran to; uncapped, three rows came out 1.56in each
# and read as three bands with a word in each.
_ROW_FILL_MAX = 2.5

# The arithmetic `table` draws from, so that `table_size` cannot answer for a
# different table than the one that gets drawn.
_Plan = namedtuple("_Plan", "widths heights width height groups marks rows glyphs")

# The marks a cell can carry as its own copy, and the characters they are said in.
#
# A glyph is set by the renderer inside the cell, so it moves when the row moves --
# and a row moves whenever the renderer wants more height than the file asked for.
# Drawn as shapes over the table they stayed on the file's grid instead: measured on
# a live five-row table, the renderer grew each body row 0.05in and the fourth row's
# dots came out 0.19in above their own cell, half a row, sitting on the rule over
# them. The drift accumulates down the table, so the last row is always the worst.
# `progress` is not here because a bar is a length and not a character, and `delta`
# because its triangle is one fact and the cell's number the other.
_HARVEY_FULL = "\u25cf"
_HARVEY_HALF = "\u25d0"
_HARVEY_EMPTY = "\u25cb"
# Between two steps, so a row of them reads as separate marks rather than a bar.
_MARK_GAP = "\u2009"
# One face for every glyph a mark is written as. Left to the copy's face, a full
# circle came from Arial and the half circle beside it from whatever face had one,
# so a scale's steps were two sizes; and Arial's is small against 12pt copy. Segoe
# UI Symbol carries all five glyphs at one design, and a renderer without it
# substitutes one face for the run rather than one per glyph.
_MARK_FACE = "Segoe UI Symbol"
_MARK_GLYPH_SCALE = 1.3


def _mark_pt(size):
    """The size a mark's glyphs are set at, off the copy they sit beside."""
    return round(size * _MARK_GLYPH_SCALE, 1)


def _emphasis_tint(theme):
    """A tint a step deeper than the header's tile, for the row a tinted-header table emphasises."""
    soft, accent = _rgb(theme["accent_soft"]), _rgb(theme["accent"])
    mixed = tuple(round(a + (b - a) * 0.35) for a, b in zip(soft, accent, strict=True))
    return "#{:02X}{:02X}{:02X}".format(*mixed)


def _mark_glyphs(kind, value):
    """The mark as (character, reads as filled) pairs, or None where it needs geometry."""
    if kind == "harvey":
        level, steps = _rating(value)
        said = []
        for step in range(steps):
            share = min(1.0, max(0.0, level - step))
            glyph = _HARVEY_FULL if share >= 0.75 else _HARVEY_HALF if share >= 0.25 else _HARVEY_EMPTY
            said.append((glyph, share >= 0.25))
        return said
    if kind == "status_dot":
        return [(_HARVEY_FULL, True)]
    if kind == "check":
        return [("\u2713", True)]
    if kind == "cross":
        return [("\u2717", False)]
    if kind == "partial":
        return [(_HARVEY_HALF, True)]
    return None


_CELL_ALIGNS = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}


# What a cell holds when it holds a figure: a number, optionally signed, optionally
# with a currency mark, thousands separators and one unit after it. Deliberately
# narrow -- "4.1万+ Stars" and "Free / $19 / $249" are prose that begins with a
# digit, and reading either as a figure is what put a whole column of sentences
# hard against its right edge.
# Character classes rather than the backslash shorthands: this module's source is
# carried inside a string, where one backslash reaches the file as none.
_FIGURE_RE = re.compile(r"^[+-]?[$¥€£]?[0-9][0-9,.]*[ ]?(?:%|pp|bp|x|ms|s|min|h|k|K|M|B|GB|MB|TB)?$")

# Cells that say "no value here". They neither make a column numeric nor stop it
# from being one, because a dash in a column of figures is still a column of figures.
_NO_FIGURE = {"", "-", "--", "\u2014", "\u2013", "n/a", "N/A", "na", "/", "?"}


def _column_is_numeric(rows, index):
    """Whether this column's body cells are all figures.

    All of them, not most: one sentence in a column of numbers is the case where
    right-aligning costs more than it buys, and a column that is genuinely mixed
    reads better left.
    """
    if not rows or len(rows) < 2:
        return False
    seen = False
    for row in rows[1:]:
        if index >= len(row):
            continue
        cell = str(row[index]).strip()
        if cell in _NO_FIGURE:
            continue
        if not _FIGURE_RE.match(cell):
            return False
        seen = True
    return seen


def _cell_aligns(align, columns, numeric_from, rows=None, cell_marks=None):
    """One alignment per column: the names the caller gave, or the cells' own shape.

    `numeric_from` says "figures from here rightwards", which is the whole answer for
    a table of labels and figures and no answer at all for one whose single left-aligned
    column sits between two others. `align=("left", "center", "right")` is
    how to say that, and a name that is not an alignment comes back as a sentence
    rather than as a page that looks nearly right.
    """
    # Figures right, everything else centred. A column of numbers that is not right
    # aligned cannot be compared down its length, which is the one alignment with a
    # reason behind it rather than a look. The rest used to go left and came out ragged
    # down every column on a deck whose tables were mostly short labels and phrases --
    # and a cell long enough for centring to hurt is a cell that wanted a card, which is
    # a different fix than an alignment.
    # A cell holding a mark beside its string is two things sharing a cell, and the mark
    # takes whichever side the string does not. Centred, the string sits in the middle
    # and there is no side left -- the marks landed on top of the words on the first
    # table drawn after centring became the default.
    marked = {column for _, column in (cell_marks or ())}
    if align is None and numeric_from is None:
        return [
            PP_ALIGN.RIGHT
            if _column_is_numeric(rows, index)
            else PP_ALIGN.LEFT
            if index in marked
            else PP_ALIGN.CENTER
            for index in range(columns)
        ]
    if align is None:
        return [
            PP_ALIGN.RIGHT if index >= numeric_from else PP_ALIGN.LEFT if index in marked else PP_ALIGN.CENTER
            for index in range(columns)
        ]
    named = list(align)
    if len(named) != columns:
        raise ValueError(f"align names {len(named)} columns, this table has {columns}")
    return [_named(_CELL_ALIGNS, one, "align") for one in named]


def _cell_fills(fills, count, columns, theme):
    """`{(row, column): colour}` for every cell a `fills` key names.

    A key's row or column may be None for "all of them", so one entry paints a cell,
    a whole row or a whole column -- which is the case a page actually has, and the
    reason this is not keyed cell-by-cell like `marks`. `emphasize_columns` says
    "this is the one that matters" and picks the colour; this says which colour, for
    a page that has a palette of its own to spend.
    """
    resolved = {}
    for where, colour in dict(fills or {}).items():
        try:
            row, column = where
        except (TypeError, ValueError):
            raise ValueError(f"a fill is keyed by (row, column), not {where!r}") from None
        rows_named = range(count) if row is None else [_named_index(row, count, "fill", "row")]
        columns_named = range(columns) if column is None else [_named_index(column, columns, "fill", "column")]
        painted = _paint_of(theme, colour)
        for one in rows_named:
            for other in columns_named:
                resolved[(one, other)] = painted
    return resolved


def _named_index(value, count, what, axis):
    """One index into a table's rows or columns, or a sentence saying it is not one."""
    try:
        index = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"a {what}'s {axis} is a number or None, not {value!r}") from None
    if not 0 <= index < count:
        raise ValueError(f"a {what} names {axis} {index}; this table has {count}")
    return index


def _rule_edges(count, totals, theme, rule_pt, grid_pt):
    """Which row carries a line on which edge, in what colour and at what weight.

    The same lines whatever `style` is: it took a `style` argument only so that
    `row_rules` could switch the row boundaries on, and they are what every style
    draws now.

    A table's rules are the cells' own borders here, not rectangles laid across the
    grid at the boundaries the arithmetic named. The arithmetic is an estimate: a
    renderer that breaks one cell's copy onto a line this module did not predict grows
    that row, every boundary under it moves, and a rectangle -- which cannot move --
    comes down through a row's copy. That is the `rule_strike` reading, and a border
    cannot produce it, because the border *is* the boundary wherever the boundary
    ends up. The trade is that the rules no longer exist as shapes, so nothing
    measures them off the file any more; there is nothing left for that measurement
    to find.
    """
    accent, quiet = theme["accent"], theme["grid"]
    firm = theme.get("muted", theme["foreground"])
    edges = {}

    def line(row, edge, colour, points):
        if 0 <= row < count:
            edges.setdefault(row, {})[edge] = (colour, points)

    # Both sides of a boundary, because a neighbour's explicit noFill on the shared
    # edge is a renderer's licence to paint the line away.
    def boundary(above, colour, points):
        line(above, "lnB", colour, points)
        line(above + 1, "lnT", colour, points)

    # The frame's flat sides. A table with a rule under its header, nothing between
    # its rows and one hairline under the last of them has three lines that all stop
    # in mid-air, and what a reader takes from that is a page that was not finished.
    # The frame closes it, in the firm tone rather than the quiet one: the frame is
    # the table's outer edge against the page, and the grid tone -- picked to be
    # nearly invisible so that interior lines do not compete with the copy -- leaves
    # that edge indistinguishable from no edge at all on a projector.
    line(0, "lnT", firm, grid_pt)
    boundary(0, accent, rule_pt)
    # And the row boundaries, for the same reason and in the same tone. A four-column
    # comparison whose cells wrap onto two lines has nothing left saying where one row
    # ends: the reader counts baselines to find out which cell belongs to which label,
    # and gets it wrong. `grid_pt` and not `rule_pt`, so the header's accent rule is
    # still the heavier of the two and still the line the table is read from. The last
    # boundary is the frame's own bottom edge and is already drawn below -- a second
    # line on it would be the same line twice.
    for row in range(1, count - 1):
        boundary(row, quiet, grid_pt)
    line(count - 1, "lnB", firm, grid_pt)
    # A subtotal sits under a line. Bold alone reads as emphasis; the line is what
    # says the numbers above it were added up rather than merely listed. In the firmer
    # tone, because the quiet one is now on every boundary: a grid-toned hairline over
    # a total is the line the row above it already had, so drawing it says nothing and
    # the loop could not change a pixel. Same weight, darker tone -- three readings off
    # two weights, and the accent stays the header's alone. Row 1 needs none: the
    # accent rule under the header is already there and is the louder.
    for index in sorted(totals):
        if index > 1:
            boundary(index - 1, firm, grid_pt)
    return edges


def _table_geometry(rows, theme, *, weights, size, style, group_rows, marks, room,
                    header_size=None, indent_rows=(), row_height=None, header_height=None,
                    padding=None, fill=True, room_h=None):
    """The column widths, the row heights and the table's own extent.

    `room` is the width available -- a box's -- or None for "as wide as the content
    wants", and `room_h` is the height available, which a table shorter than its box
    spreads into. Both `table` and `table_size` come through here on purpose: a size
    that is not the drawn size is worse than no size at all, and two copies of this
    arithmetic is exactly how the two would come apart.
    """
    if style not in ("minimal", "header_tint", "row_rules", "compact"):
        raise ValueError(f"unknown table style {style!r}")
    columns = len(rows[0])
    # A group row carries its name in `groups`, merged across the table, so what its
    # own cells hold is nothing and must not be measured as if it were a column.
    groups = {_body_row(index, len(rows), "group_rows"): str(name) for index, name in dict(group_rows or {}).items()}
    cell_marks = {
        _cell_key(where, rows, columns): _mark_spec(spec, theme) for where, spec in dict(marks or {}).items()
    }
    # Said in the cell where it can be said there -- but charged exactly as before.
    # `_mark_floors` reads `cell_marks`, so dropping a mark from it drops the lane its
    # column was being given: a ten-step scale then took the room out of the label
    # column beside it, which came back 0.53in against a 0.55in floor. So the glyphs
    # are for the filling and the drawing, and the arithmetic above them does not know
    # they exist.
    filled_rows, glyphs = _marks_in_cells(rows, cell_marks)
    head_pt = size if header_size is None else float(header_size)
    ends = _CELL_ENDS if padding is None else float(padding)
    # A row is never shorter than the line in it. Measured on the render: a compact
    # 14pt row asks for 19pt and LibreOffice gave it 20 to 21, so four rows drifted a
    # tenth of an inch past where the table said they ended -- and the hairline placed
    # from those heights came down through the last row's figures. The floor is the
    # line box plus the cell's own margins, which is what the renderer is doing.
    han = _has_han(rows)
    floor = _cell_floor(size, ends, han)
    head_h = max((size + 12) / 72, floor)
    body_h = max((size + 5 if style == "compact" else size + 10) / 72, floor)
    # A header set larger than the body it heads needs the room its own line takes.
    head_h = max(head_h, (head_pt + 12) / 72, _cell_floor(head_pt, ends, han))
    face = theme.get("font_family") if theme else None
    indented = {_body_row(index, len(rows), "indent_rows") for index in indent_rows}

    content = _content_weights(rows, size, skip=set(groups), face=face)
    head_floors = _header_floors(rows[0], head_pt, face)

    def spread_of(shares):
        # A box is what the author gave this table, so the table spans it -- the
        # predecessor let an unweighted table stay at its content width, and five real
        # tables measured 43% to 92% of the box they were handed, a page-wide table
        # stopping 6.8in short of the margin every other element on the page ran to.
        # The air an unweighted table used to leave behind reads worse than the air
        # inside it: rendered both ways, the short table left the page looking
        # unfinished and the spread one still reads, because every style draws the row
        # boundaries that carry the eye across. A table that should not span the page
        # is a table given a narrower box.
        total = float(sum(shares))
        if room is None:
            # Proportions carry no width of their own, so with no box to fill they are
            # apportioned over the width the content asks for -- the same width this
            # table is given when nobody weights it. Summing them as inches instead
            # would make `weights=[0.34, 0.16, 0.28, 0.22]` a table one inch wide.
            return float(sum(content)) if weights else total
        return room

    def laid():
        # What each column holds before a mark is charged to it. The marks are then
        # allowed the room left over rather than a share of the whole, which is what
        # keeps an unmarked column from paying for a neighbour's rating.
        base_shares = list(weights) if weights else content
        divided_over = spread_of(base_shares)
        # The marks are charged against what the strings ask for and not against the
        # width the table is spread to: a spread table's columns already sum to the box,
        # so slack measured there is nought and every mark was left the room it had
        # without one -- none. Given weights keep dividing the whole width, which is the
        # author's division and not a mark's to take from.
        asked_over = divided_over if weights else float(sum(content))
        base = _column_widths(base_shares, asked_over, head_floors)
        # The marks are charged against the width the table is going to have, which is
        # the width `base` was just divided over. Only one table has none: unweighted
        # and unboxed, whose width is the sum of shares that the mark floors themselves
        # raise, so nothing caps what they may ask for. Given weights are apportioned
        # over a width even with no box, and `inf` there funded the full floors out of
        # a fixed width -- five columns over 4.52in put the label column at 0.22in,
        # 0.02in of it left after the cell's margins, which is narrower than any
        # character and is the width `_wrapped` could not break a token into.
        reserve = room if room is not None else (divided_over if weights else float("inf"))
        floors = _mark_floors(cell_marks, rows, columns, size, _mark_scale(size, ends), reserve, base)
        shares = list(weights) if weights else _content_weights(rows, size, skip=set(groups), minimums=floors, face=face)
        # Given weights get the same treatment as a starved heading: a column too narrow
        # for the mark in it is raised and the shortfall comes off the columns with slack.
        raised = [max(pair) for pair in zip(head_floors, floors)]
        width = spread_of(shares)
        # A mark's width is inches; a share is a proportion. Folded into the shares, the
        # inches were scaled along with them the moment the table spanned a box: a
        # five-step rating needing 0.99in came out 2.98in of a page-wide table, and the
        # label column it qualifies fell from 2.84in to 1.86in for it. So the marks are
        # set aside first and the strings divide what is left -- the same order
        # `_mark_floors` charges them in, now held to through the division as well.
        held = [max(0.0, floor - holds) for floor, holds in zip(floors, base)]
        if any(held) and sum(held) < width:
            over = _column_widths(
                base_shares,
                width - sum(held),
                [max(0.0, floor - hold) for floor, hold in zip(raised, held)],
            )
            sized = [part + hold for part, hold in zip(over, held)]
        else:
            sized = _column_widths(shares, width, raised)
        tall = _row_heights(
            rows, sized, groups, indented, size, head_pt, head_h, body_h, ends, face, han,
            marked={r for r, _ in glyphs},
        )
        if header_height is not None:
            tall[0] = max(tall[0], float(header_height))
        if row_height is not None:
            tall[1:] = [max(value, float(row_height)) for value in tall[1:]]
        if fill and room_h:
            tall = _filled_heights(tall, room_h, size)
        return sized, width, tall

    widths, width, heights = laid()
    return _Plan(tuple(widths), tuple(heights), width, sum(heights), groups, cell_marks, filled_rows, glyphs)


def _write_marks(para, marked):
    """The cell's copy and its mark's steps, as runs, and which of them are filled.

    One run per step, because a step that is not filled is set in the quiet tone and a
    single run cannot hold two colours. The gap between them rides on the step before
    it, so a scale is as many runs as it has steps.
    """
    pairs, _paint, before = marked
    steps = []
    if before:
        first = para.add_run()
        first.text = f"{before}{_MARK_GAP}"
    for index, (glyph, filled) in enumerate(pairs):
        run = para.add_run()
        run.text = glyph if index == len(pairs) - 1 else f"{glyph}{_MARK_GAP}"
        steps.append((run, filled))
    return steps


def _marks_in_cells(rows, cell_marks):
    """(rows with the sayable marks in their cells, what to colour in them).

    A mark that can be a character is written into its cell's copy, so the renderer
    sets it inside the cell and it moves when the row moves. The cell keeps whatever
    it already said -- a rating column shows its figure and its dots -- so the glyphs
    are appended rather than substituted.
    """
    said = {}
    for where, (kind, value, paint) in cell_marks.items():
        pairs = _mark_glyphs(kind, value)
        if pairs is not None:
            said[where] = (pairs, paint, "")
    if not said:
        return rows, {}
    grown = [list(line) for line in rows]
    for where in list(said):
        r, c = where
        if not (0 <= r < len(grown) and 0 <= c < len(grown[r])):
            del said[where]
            continue
        pairs, paint, _ = said[where]
        before = str(grown[r][c])
        said[where] = (pairs, paint, before if before.strip() else "")
        marks_text = _MARK_GAP.join(glyph for glyph, _filled in pairs)
        grown[r][c] = f"{before}{_MARK_GAP}{marks_text}" if before.strip() else marks_text
    return [tuple(line) for line in grown], said


def _has_han(rows):
    """Whether any cell of this table holds a Han character.

    Per table and not per cell: one row given more height than its neighbours is a
    table with a limp, and a table with Han anywhere is set in a face whose line box
    is the taller one throughout.
    """
    return any(_han(character) for line in rows for value in line for character in str(value))


def _line_box(size, han):
    """One line of a table cell, in inches -- the renderer's own floor, not the pitch."""
    return size * (_CELL_LINE_BOX_HAN if han else _LINE_BOX) / 72.0


def _cell_floor(size, ends, han):
    """The shortest a cell holding one line of `size` may be drawn."""
    return _line_box(size, han) + 2 * ends


def _row_heights(rows, widths, groups, indented, size, head_pt, head_h, body_h, ends, face, han=False, marked=()):
    """Every row as tall as the lines its own cells wrap onto.

    A height off the type alone is one line's worth whatever the cell holds, and a
    declared height is a floor rather than a measurement: the renderer grows each
    wrapped row, so a divider drawn at the boundary the arithmetic named comes down
    through the copy of a row further along.

    So the count is asked here, at the widths the columns are actually getting, with
    the same `_wrapped` every other fit answer on this page is made from. It is an
    estimate and not a font metric, which is why the rules moved to the cells' own
    borders as well: this makes the renderer's job smaller, and the borders make what
    is left of it harmless.
    """
    heights = []
    for index, line in enumerate(rows):
        bold = index == 0 or index in groups
        point = head_pt if index == 0 else size
        if index in groups:
            counted = len(_wrapped(groups[index], sum(widths) - 2 * _CELL_SIDE, point, face, bold))
        else:
            counted = 1
            for column, value in enumerate(line[: len(widths)]):
                # A detail row's label is stepped in by a PAD, so it wraps in that
                # much less room than the column it sits in.
                side = 2 * _CELL_SIDE + (PAD if column == 0 and index in indented else 0.0)
                if widths[column] - side <= 0:
                    continue
                counted = max(counted, len(_wrapped(str(value), widths[column] - side, point, face, bold)))
        line = counted * _line_box(point, han)
        # A row that says a mark in a cell holds a line of the mark's size as well; the
        # glyphs are Latin, so it is the Latin box at that size and not the Han one.
        if index in marked:
            line = max(line, _line_box(_mark_pt(point), False))
        heights.append(max(head_h if index == 0 else body_h, line + 2 * ends))
    return heights


def _filled_heights(heights, room, size):
    """The rows spread into the box, so a table does not sit in the top of one.

    A content-sized table leaves whatever its box has over as a single band of white
    under the last row, which reads as a page that ran out rather than as a table
    that ended. The slack goes onto the rows in
    equal parts, equal parts being the one distribution that leaves the table's own
    rhythm alone, and it is capped -- a table with little to say should look like a
    small table and not like a page of bands.
    """
    slack = room - sum(heights)
    if slack <= 0:
        return list(heights)
    share = min(slack / len(heights), _ROW_FILL_MAX * _line_h(size))
    return [height + share for height in heights]


# The frame costs no height, checked rather than assumed: the same table rendered with
# the frame and without it put every glyph, the accent rule and the last row's hairline
# on the identical pixel at 144dpi, with the frame's own strips straddling the table's
# outer edges.
def table_size(rows, theme, *, weights=None, size=LABEL_PT, style="header_tint", numeric_from=None,
               group_rows=None, marks=None, header_size=None, indent_rows=(), row_height=None,
               header_height=None, padding=None, fill=True, box=None):
    """How much of the page this table will take, before a cell of it is drawn.

    The geometry is decided here -- the header row is (size + 12)/72 tall, a body row
    (size + 10)/72 and a `compact` one (size + 5)/72, each held to the line box it has
    to hold, and the columns are sized from what they hold -- so where a table ends is
    an answer and not a build: `stack.take(table_size(rows, T, box=band).h)`.

    Takes what `table` takes, so the two calls sit side by side and cannot describe
    different tables -- `numeric_from` decides alignment rather than width and is
    accepted only so that the pair stay copy-for-copy identical, and `emphasize_*`,
    `total_rows`, `align`, `banding` and `column_rules` change the ink and not the
    geometry, so they are not here at all. `indent_rows` is, because a stepped-in
    label wraps in a PAD less room than its column and so can cost a line.

    **The frame is ink and not room.** A border is drawn on the boundary and not beside
    it, so it takes nothing from the row it edges: `grid_pt` may be raised without this
    answer going stale, and the frame's weight is not added to the height -- adding it
    would put the rows somewhere they are not.

    With `box`, the answer is placed at that box's top-left corner and the box is the
    room the table spreads into -- which is what `fill` does, and the height comes
    back as the box's own where there was slack to take. `fill=False` answers for the
    table its content asks for.

    **So do not ask this in order to decide how much of a region to give the table.**
    From five rows up the answer *is* `box.h`, and asking it about the whole body and
    then putting a band above the table is circular: the height came back for a region
    the table is not going to be drawn in, and `take` refuses it at the band where the
    room runs out. A live build failed four times over exactly this -- a 4.22in answer
    measured against a 4.67in body, drawn under a 0.62in band. Either hand `table` the
    band and let it fill it, which needs no measurement at all, or pass `box=` **the
    band the table will be drawn in**. `fill=False` answers what the content alone asks
    for, and that answer is a measurement and not a height to draw at: a table drawn at
    it sits in a band it does not fill. Measured on a live page, one five-row table came
    back 4.69in with `fill` and 1.77in without -- 62 percent shorter, in the 5.10in band
    it had been handed. So when something goes under the table, take that something off
    the region first and pass `box=` what is left, `fill` still on; asking without `fill`
    answers whether the content fits at all, which is a different question from where the
    table ends. Without a box it is a size at the origin and the
    table is as wide as its content wants -- and given `weights` are apportioned over
    that width, because proportions carry no width of their own. That answer is the
    one to build on: every column of a weighted table drawn in a box at least this
    wide gets at least the room measured here, so no row of it wraps onto a line this
    height did not count. A box narrower than that width is a table drawn taller than
    the height answered, and the columns squeezed under their own content are what
    `wide_table` reads off the built file.
    """
    plan = _table_geometry(
        rows,
        theme,
        weights=weights,
        size=size,
        style=style,
        group_rows=group_rows,
        marks=marks,
        room=None if box is None else box.w,
        header_size=header_size,
        indent_rows=indent_rows,
        row_height=row_height,
        header_height=header_height,
        padding=padding,
        fill=fill,
        room_h=None if box is None else box.h,
    )
    origin = box or Box(0.0, 0.0, 0.0, 0.0)
    return Box.at(origin.x0, origin.y0, w=plan.width, h=plan.height)


# No column takes more than this share of the table, however long one cell is: a
# single 60-character note in an otherwise numeric table would otherwise squeeze
# every metric into nothing.
_WIDEST_COLUMN = 0.5
# And none narrower than this, so a column of "-" still has a header over it.
_NARROWEST_IN = 0.55


def _content_weights(rows, size, skip=(), minimums=(), face=None):
    """Column weights from the widest thing in each column.

    Measured rather than declared: `_em_width` is the same estimate the formula
    setter uses, which is a class average per character and not a font metric. It
    does not need to be exact -- it decides proportions, and the render-side checks
    catch a column that still does not fit.
    """
    columns = len(rows[0])
    widest = [0.0] * columns
    for index, line in enumerate(rows):
        if index in skip:
            continue
        for column, value in enumerate(line[:columns]):
            # The header is set bold, which is wider than the estimate for its text.
            needed = _em_width(str(value), size, face) * (1.08 if index == 0 else 1.0)
            widest[column] = max(widest[column], needed)
    # The cell's own margins are 0.10 a side; the rest is the air that keeps a column
    # from reading as a box with a word wedged into it.
    padded = [value + 0.30 for value in widest]
    # The ceiling is against one runaway cell, not against a table that genuinely has
    # a wide column: twice the median leaves a two-column table alone and still stops
    # a 60-character note from squeezing seven metrics into nothing.
    middle = sorted(padded)[len(padded) // 2]
    ceiling = max(sum(padded) * _WIDEST_COLUMN, middle * 2)
    sized = [min(max(value, _NARROWEST_IN), ceiling) for value in padded]
    # `minimums` is what the marks need and the ceiling does not apply to it: the
    # ceiling is against one runaway string, and a rating scale is not a string.
    return [max(pair) for pair in zip(sized, list(minimums) or [0.0] * columns)]


def _unbroken(text):
    """The longest run in `text` a line break cannot fall inside.

    Latin words and figures are unbreakable; CJK is not -- a Chinese header wraps
    between any two characters, so it never sets a floor of its own.
    """
    longest = run = ""
    for character in str(text):
        if character.isascii() and (character.isalnum() or character in "-.&/+%"):
            run += character
            if len(run) > len(longest):
                longest = run
        else:
            run = ""
    return longest


def _header_floors(header, size, face=None):
    """What each column needs so its header is not broken mid-word.

    Bold, hence the same 1.08 the sized weights use, plus the cell's own margins.
    """
    return [_em_width(_unbroken(value), size, face) * 1.08 + 0.30 for value in header]


def _column_widths(shares, table_width, floors):
    """`shares` as proportions of `table_width`, except no header word is split.

    Weights that fit the numbers and starve the headings come out reading "VIPSe / g",
    "DAVI / S", "BURS / T". Weights say which
    column deserves the room; they cannot know what the words in row one measure,
    so a column too narrow for its own header is raised and the shortfall comes off
    whichever columns still have slack, in their own proportions.
    """
    total = float(sum(shares)) or 1.0
    widths = [table_width * share / total for share in shares]
    short = [max(0.0, floor - width) for floor, width in zip(floors, widths)]
    if not any(short):
        return widths
    owed = sum(short)
    slack = [max(0.0, width - floor) for floor, width in zip(floors, widths)]
    spare = sum(slack)
    if spare < owed:
        # Not enough room anywhere: hold the proportions between the floors and let
        # the render-side measurements report what still does not fit.
        scale = table_width / (sum(floors) or 1.0)
        return [floor * scale for floor in floors]
    return [width + rise - give * owed / spare for width, rise, give in zip(widths, short, slack)]


# The marks a cell can carry. Named rather than typed: a tick or an arrow written into
# a cell is a glyph from whatever face the renderer falls back to, and on this one a
# CJK face has no dingbats at all -- the delivered page shows an empty box.
_MARK_KINDS = ("harvey", "status_dot", "delta", "progress", "check", "cross", "partial")

# The three that read a number, and so can take it from the cell's own string rather
# than have the author write "76%" twice and keep the two in step by hand.
_NUMERIC_MARKS = ("harvey", "delta", "progress")

# And the three that read nothing at all. A tick is a tick, so a value handed to one
# of them was meant for something else and dropping it loses the only intent in the
# string: `check:ACCENT` is the accent misspelt, and it came out as the default ink.
_STATED_MARKS = ("check", "cross", "partial")

# The paints a mark may be named. A closed list, because a theme also holds a font
# family and a series of chart colours, and `check:font_family` has to be refused here
# rather than reaching _rgb as a typeface.
#
# `accent_ink` is in it because the reference tells an author to write with it -- "a
# number or heading in the accent's colour takes accent_ink" -- and every way of
# saying so raised instead: it was derived, documented and refused, so the one
# instruction about it was the one that could not be followed.
_PAINTS = (
    "foreground",
    "muted",
    "accent",
    "accent_soft",
    "accent_ink",
    "surface",
    "background",
    "grid",
)


def mark(slide, box, theme, kind, value=None, *, colour=None):
    """A rating, a state, a direction or a share, drawn over a cell as shapes.

    What a table cannot say in a string. "3.5 / 5" down a column of criteria is a
    number the reader compares by reading it; five dots with three and a half filled
    is a length, and lengths compare at a glance. Same for a share (a bar), a
    direction (an arrow) and a supported / not / partly triple (a tick, a cross, a
    half-filled dot).

    Drawn as shapes because python-pptx puts nothing but text in a table cell and
    cannot set a cell's edges either -- which is why the row rules are shapes too;
    `_hairline` is the same construction. Shapes added after the table sit over it.

    `kind` is one of harvey, status_dot, delta, progress, check, cross, partial.
    `value` is what that kind reads, and the two readings are not interchangeable: a
    *rating* for harvey ("3.5", "3.5/4" for a four-step scale, or "75%" of whichever
    scale), a signed number for delta, and a *share* for progress ("76%", "0.76", "76"
    all mean three quarters). check, cross, partial and status_dot read nothing.
    `colour` names one of the theme's paints or gives #RRGGBB.

    Nothing is red and nothing is green: a theme carries neither, and up is not good
    in every column -- an arrow on "open exceptions" means the opposite of one on
    "availability". A mark states the fact in the page's own ink, and the accent stays
    the author's to spend on the row that carries the claim.

    Every dimension comes out of `box`. There is no size to pass, because a mark that
    could be sized apart from its cell is a mark that stops fitting the cell.

    Comes back as the list of shapes it always was, with `.box` for the ink they
    actually cover -- a rating's row of steps rather than the cell it was centred in,
    which is what says whether the column carrying it is wide enough to read.
    """
    if kind not in _MARK_KINDS:
        raise ValueError(f"unknown mark {kind!r}; one of {', '.join(_MARK_KINDS)}")
    if kind in _STATED_MARKS and value not in (None, ""):
        raise ValueError(
            f"a {kind} mark reads no value, so {value!r} says nothing. Its colour goes in "
            f"colour=, as one of {', '.join(_PAINTS)} or #RRGGBB"
        )
    if box.w <= 0.02 or box.h <= 0.02:
        raise ValueError(f"a {kind} mark has no room: {box.w:.2f}x{box.h:.2f}in")
    if kind == "harvey":
        shapes = _harvey(slide, box, theme, value, colour)
    elif kind == "status_dot":
        shapes = [_disc(slide, _square(box, 0.40), _paint(theme, colour or value, "accent"))]
    elif kind == "delta":
        shapes = _delta(slide, box, theme, value, colour)
    elif kind == "progress":
        shapes = _progress(slide, box, theme, value, colour)
    elif kind == "check":
        shapes = _check(slide, _square(box, 0.52), _paint(theme, colour, "accent"))
    elif kind == "cross":
        shapes = _cross(slide, _square(box, 0.44), _paint(theme, colour, "muted"))
    else:
        shapes = _partial(slide, _square(box, 0.46), theme, colour)
    return Marks(shapes, _ink_box(shapes))


def _harvey(slide, box, theme, value, colour):
    """The scale, repeated, with the rating filled in from the left.

    The empty steps are drawn as well, and they are the half that carries the
    meaning: three filled dots say "three" only when the reader can see there were
    five to fill. A rating between two steps fills half of the one it lands in.
    """
    level, steps = _rating(value)
    diameter = min(box.h * 0.52, box.w / (steps * 1.32))
    if diameter <= 0.02:
        raise ValueError(f"a {steps}-step scale does not fit in {box.w:.2f}in")
    pitch = diameter * 1.32
    left = box.x0 + (box.w - (pitch * (steps - 1) + diameter)) / 2
    top = box.y0 + (box.h - diameter) / 2
    filled = _paint(theme, colour, "accent")
    outline = theme.get("muted", theme["foreground"])
    shapes = []
    for step in range(steps):
        step_box = Box.at(left + pitch * step, top, w=diameter, h=diameter)
        share = min(1.0, max(0.0, level - step))
        if share >= 0.75:
            shapes.append(_disc(slide, step_box, filled))
            continue
        shapes.append(_ring(slide, step_box, outline))
        if share >= 0.25:
            shapes.append(_half_disc(slide, step_box, filled))
    return shapes


def _delta(slide, box, theme, value, colour):
    """The direction, as a triangle. The number stays the cell's own string.

    One fact per shape: the arrow says which way and the cell says how far, so a
    column of arrows scans down the page and the figures stay right-aligned under
    their heading rather than being redrawn as pictures of themselves.
    """
    reading = _signed(value)
    ink = _paint(theme, colour, "foreground")
    square = _square(box, 0.40)
    if reading == 0:
        # No change is a reading of its own, and either arrow would state the opposite.
        return [_pill(slide, Box.at(square.x0, square.y0 + square.h * 0.42, w=square.w, h=square.h * 0.16), ink)]
    shape = slide.shapes.add_shape(MSO_SHAPE.ISOSCELES_TRIANGLE, *square.pptx())
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(ink)
    shape.line.fill.background()
    _bare(shape)
    if reading < 0:
        shape.rotation = 180.0
    return [shape]


def _progress(slide, box, theme, value, colour):
    """A share as a length against the whole, which is what makes a column of them
    comparable: 76% and 51% are two numbers to read and two lengths to see."""
    share = _share(value)
    height = min(box.h * 0.34, box.w * 0.12)
    top = box.y0 + (box.h - height) / 2
    shapes = [_pill(slide, Box.at(box.x0, top, w=box.w, h=height), theme["grid"])]
    if share > 0:
        # Never shorter than it is tall: a 2% share drawn to scale is a sliver the
        # renderer rounds away, and a bar that is not there reads as no data.
        filled = max(box.w * share, height)
        shapes.append(_pill(slide, Box.at(box.x0, top, w=filled, h=height), _paint(theme, colour, "accent")))
    return shapes


def _cross(slide, box, colour):
    return [
        _stroke(slide, [(box.x0, box.y0), (box.x1, box.y1)], colour, box.w * 0.19),
        _stroke(slide, [(box.x1, box.y0), (box.x0, box.y1)], colour, box.w * 0.19),
    ]


def _partial(slide, box, theme, colour):
    """Neither yes nor no: the step's own outline with half of it filled."""
    return [
        _ring(slide, box, theme.get("muted", theme["foreground"])),
        _half_disc(slide, box, _paint(theme, colour, "accent")),
    ]


def _check(slide, box, colour):
    """A tick, weighted so the long arm is the one that reads at a cell's size."""
    side = box.w
    points = [
        (box.x0 + side * 0.06, box.y0 + side * 0.55),
        (box.x0 + side * 0.38, box.y0 + side * 0.88),
        (box.x0 + side * 0.96, box.y0 + side * 0.14),
    ]
    return [_stroke(slide, points, colour, side * 0.17)]


def _square(box, share):
    """A centred square, `share` of the box's height and never wider than the box."""
    side = min(box.h * share, box.w)
    return Box.at(box.x0 + (box.w - side) / 2, box.y0 + (box.h - side) / 2, w=side, h=side)


def _disc(slide, box, colour):
    shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, *box.pptx())
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(colour)
    shape.line.fill.background()
    _bare(shape)
    return shape


def _ring(slide, box, colour):
    shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, *box.pptx())
    shape.fill.background()
    shape.line.color.rgb = _rgb(colour)
    shape.line.width = Inches(max(0.010, box.h * 0.09))
    _bare(shape)
    return shape


def _pill(slide, box, colour):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, *box.pptx())
    shape.adjustments[0] = 0.5
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(colour)
    shape.line.fill.background()
    _bare(shape)
    return shape


def _half_disc(slide, box, colour):
    """Half a step filled, for a rating between two and for `partial`.

    A polygon rather than the preset pie: the pie's adjustments are angles in
    sixty-thousandths of a degree, which is a unit to get wrong once and never see --
    at this size twelve segments are a circle to the eye and to the printer.
    """
    radius = min(box.w, box.h) / 2
    middle_x, middle_y = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    steps = 12
    points = [
        (
            middle_x - radius * math.sin(math.pi * step / steps),
            middle_y - radius * math.cos(math.pi * step / steps),
        )
        for step in range(steps + 1)
    ]
    shape = _freeform(slide, points, close=True)
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(colour)
    shape.line.fill.background()
    _bare(shape)
    return shape


def _stroke(slide, points, colour, width):
    """A polyline, unfilled -- the construction the icons use, at a cell's scale."""
    shape = _freeform(slide, points, close=False)
    shape.fill.background()
    shape.line.color.rgb = _rgb(colour)
    shape.line.width = Inches(width)
    _bare(shape)
    return shape


def _freeform(slide, points, *, close):
    builder = slide.shapes.build_freeform(Inches(points[0][0]), Inches(points[0][1]))
    builder.add_line_segments([(Inches(x), Inches(y)) for x, y in points[1:]], close=close)
    return builder.convert_to_shape()


def _cell_box(box, widths, heights, row, column):
    """Where one cell sits, in inches: the table's own geometry, read back out."""
    x0 = box.x0 + sum(widths[:column])
    y0 = box.y0 + sum(heights[:row])
    return Box(x0, y0, x0 + widths[column], y0 + heights[row])


def _mark_room(cell, text, size, aligned, face=None, height=None, lane=None):
    """The part of a cell its own string does not need.

    A mark beside a string is the common case -- a bar beside its percentage, a dot
    beside its word -- and a cell cannot flow one around the other. So the string
    keeps the side its alignment puts it on and the mark takes what is left, measured
    with `_em_width` rather than split down the middle and hoped over. A string is
    never given more than three fifths, so a mark always has room to be a mark.

    `aligned` is the cell's own paragraph alignment, and all three answers differ: the
    string keeps the side its alignment puts it on and the mark takes what is left, which
    for a centred string is the wider of the two ends rather than either one.

    `height` caps how tall the mark may be drawn and centres it in the row, which is
    the same number the column was sized from -- every mark takes its dimensions out
    of the box handed to it, so a cap that applied only to the sizing would leave the
    drawn mark and the width bought for it describing different marks. `lane` is the
    same for the width: a glyph centres in whatever it is handed, so the whole leftover
    put the arrow in the middle of the empty half and left it an inch and a half from
    the figure it reads. `_mark_width` already knows what each kind needs, and the lane
    hugs the side the string is on, so the two sit together however wide the column is.
    """
    inner = Box(cell.x0 + _CELL_SIDE, cell.y0, cell.x1 - _CELL_SIDE, cell.y1)
    if height is not None and 0.0 < height < inner.h:
        middle = (inner.y0 + inner.y1) / 2
        inner = Box(inner.x0, middle - height / 2, inner.x1, middle + height / 2)
    if not text.strip():
        return inner
    needed = min(inner.w * 0.6, _em_width(text, size, face) + _CELL_SIDE)
    if aligned == PP_ALIGN.CENTER:
        # A centred string sits in the middle, so neither end of the cell is the leftover:
        # both are, and taking the left one at the string's width put a rating straight
        # over the figure it rates. Rendered on a delivered page, the dots ran through
        # "3.4". The wider side, and the string keeps the middle.
        middle = (inner.x0 + inner.x1) / 2
        left, right = Box(inner.x0, inner.y0, middle - needed / 2, inner.y1), Box(
            middle + needed / 2, inner.y0, inner.x1, inner.y1
        )
        room = left if left.w >= right.w else right
        if lane and lane < room.w:
            return (
                Box(room.x1 - lane, room.y0, room.x1, room.y1)
                if room is left
                else Box(room.x0, room.y0, room.x0 + lane, room.y1)
            )
        return room
    if aligned == PP_ALIGN.RIGHT:
        room = Box(inner.x0, inner.y0, inner.x1 - needed, inner.y1)
        return Box(room.x1 - lane, room.y0, room.x1, room.y1) if lane and lane < room.w else room
    room = Box(inner.x0 + needed, inner.y0, inner.x1, inner.y1)
    return Box(room.x0, room.y0, room.x0 + lane, room.y1) if lane and lane < room.w else room


def _mark_scale(size, ends):
    """The height a mark is drawn at, which is the line box of the type beside it.

    Not the row's own height. A row grows to fill a band it is spread into
    (`_ROW_FILL_MAX`) and grows again when a neighbouring cell wraps, and a mark
    sized off that is a mark whose column is wider in a tall band than in a short
    one -- the same five rows asked 1.72in for their rating column in a 1.6in band
    and 2.52in in a 3.6in one, and the dots came out half an inch across beside
    14pt copy. What a mark has to be read against is the type it sits with, and
    that is one line however much air the row has.
    """
    return _line_h(size) + 2 * ends


def _mark_floors(cell_marks, rows, columns, size, mark_height, table_width, base):
    """What each marked column needs, on top of what it already holds.

    Content-driven columns measure strings, and a marked cell usually holds none --
    so the column carrying the marks is exactly the one that collapses: a five-step
    rating in a 0.53in column comes out 0.08in across, which is a texture and not a
    reading. What a mark needs is a measurement of its own, and it goes in beside the
    strings'.

    `base` is what every column holds with no mark charged to it, so the mark is only
    ever allowed the room over that. The predecessor reserved `_NARROWEST_IN` per
    unmarked column instead and let the marks divide the rest, which took the room out
    of the labels: two rating columns in a 6.0in table came to 2.17in each and left a
    label column that measured 1.15in with 0.51in -- under the floor every other path
    holds, because `_column_widths` scales the floors in proportion once they do not
    fit and nothing below that is a floor any more. With given weights the reserve is
    the whole width those weights divide -- a box's, or the content width an unboxed
    answer is for -- so a mark takes nothing at all from a division the author declared
    and is drawn inside its own column's share.
    """
    extra = [0.0] * columns
    for (row, column), (kind, value, _) in cell_marks.items():
        reading = str(rows[row][column]) if value is None and kind in _NUMERIC_MARKS else value
        extra[column] = max(extra[column], _mark_width(kind, reading, mark_height))
    asked = sum(extra)
    if not asked:
        return extra
    slack = table_width - sum(base)
    if slack < asked:
        share = max(0.0, slack) / asked
        extra = [value * share for value in extra]
    return [holds + more if more else 0.0 for holds, more in zip(base, extra)]


def _mark_width(kind, value, row_height):
    """How wide a mark has to be drawn, as a multiple of the row it sits in.

    The rating's figure is the one `_harvey` sizes its steps by (0.52 of the row
    across, 1.32 of that between centres), so the column it asks for and the scale it
    draws cannot drift apart.
    """
    if kind == "harvey":
        return _rating(value)[1] * row_height * 0.52 * 1.32
    if kind == "progress":
        return row_height * 3.4
    return row_height * 0.62


# A bar is a length against the whole, so it takes the width beside its figure rather
# than a lane of its own: two shares only compare when the two tracks are the same
# track. Every other mark is a glyph, and a glyph centres in whatever it is handed --
# the leftover of a 2.5in column put the arrow an inch and a half from its number.
_FILLING_MARKS = ("progress",)


def _mark_spec(spec, theme=None):
    """"harvey:3.5", "progress:76%:accent", "check" -> (kind, value, colour).

    One string per cell, because a page of marks is written as data and a dict of
    dicts is a shape an author gets wrong once per table. A field that reads as a
    colour -- a colour this theme carries by name, or #RRGGBB -- is the colour wherever
    it sits, so `status_dot:accent` and `harvey:4:accent` both say what they look like
    they say.

    The theme is what decides, not a list: `mark(colour="ours")` takes a role the deck
    stated in its palette, and so does the same word inside a spec -- `harvey:4:ours`
    reads `ours` as the colour and not as the rating. A cell's mark and a mark drawn
    beside it read one vocabulary.
    """
    parts = [part.strip() for part in str(spec).split(":")]
    kind, fields = parts[0], [part for part in parts[1:] if part]
    if kind not in _MARK_KINDS:
        raise ValueError(f"unknown mark {kind!r} in {spec!r}; one of {', '.join(_MARK_KINDS)}")
    if len(fields) > 2:
        raise ValueError(f'a mark is "kind[:value][:colour]", not {spec!r}')
    named = theme or {}
    value = colour = None
    for field in fields:
        if _is_hex(str(named.get(field, ""))) or field in _PAINTS or _is_hex(field):
            colour = field
        else:
            value = field
    if value is not None and kind in _STATED_MARKS:
        raise ValueError(
            f"a {kind} mark reads no value, so {value!r} in {spec!r} says nothing. A colour is "
            f"one of {', '.join(_PAINTS)} or #RRGGBB, and both are case-sensitive"
        )
    return kind, value, colour


def _paint(theme, value, fallback):
    """A mark's colour: one of the theme's paints by name, #RRGGBB, or the fallback."""
    if value is None or value == "":
        return theme.get(fallback, theme["foreground"])
    if isinstance(value, RGBColor):
        return value
    text = str(value)
    # Any colour the theme carries, not only the eight this list was written around: a
    # deck states the pair its comparison is drawn in once, as `ours` and `theirs`, and
    # a mark in that pair's colour is the same request as a mark in the accent. Only
    # where the theme's value is itself a colour: it also carries two faces and a list
    # of series, and `colour="font_family"` reached _rgb as a typeface once already.
    # _PAINTS stays as what the message names, because a misspelling still reads as one.
    if _is_hex(str(theme.get(text, ""))):
        return str(theme[text])
    if text in _PAINTS:
        return theme.get(text, theme["foreground"])
    if _is_hex(text):
        return text
    raise ValueError(f"{value!r} is not a colour: one of {', '.join(_PAINTS)}, or #RRGGBB")


def _is_hex(text):
    """#RRGGBB, and only with the hash, because six digits are also a number.

    Without the hash, `progress:123456` reads as (progress, None, "123456") -- the
    reading taken for a colour, and the bar drawn from the cell's own string instead.
    """
    body = str(text)
    if len(body) != 7 or not body.startswith("#"):
        return False
    return all(character in "0123456789abcdefABCDEF" for character in body[1:])


def _rating(value):
    """"3.5", "3.5/4" or "75%" -> (level, steps). Five steps unless the scale says otherwise.

    A share is the other spelling of the same idea and it is the one a caller reaches
    for: `progress` beside it reads "76%", 0.76 and 76 all as three quarters, so a
    harvey handed 0.75 means three quarters of the scale -- not three quarters of one
    step out of five, drawn without complaint as a row of five with the first one
    part-filled. A bare fraction is refused rather than
    guessed at, with both spellings named, because "0.75 of five" is a reading nobody
    writes and "0.75/5" says it for anyone who does.
    """
    text = str("" if value is None else value).strip().replace(",", "")
    level, scaled, scale = text.partition("/")
    try:
        steps = int(float(scale)) if scale.strip() else 5
    except ValueError:
        raise ValueError(f"a rating scale is a number of steps, not {scale.strip()!r} in {value!r}") from None
    if not 1 <= steps <= 10:
        raise ValueError(f"a rating scale runs 1 to 10 steps, not {steps}")
    share = level.strip().endswith("%")
    try:
        reading = float(level.strip().rstrip("%").strip())
    except ValueError:
        raise ValueError(f"a harvey mark needs a rating, not {value!r}") from None
    if share:
        reading = reading / 100.0 * steps
    elif not scaled and 0 < reading < 1:
        raise ValueError(
            f"a rating of {reading:g} is {reading:g} of a {steps}-step scale, which is a mark almost "
            f'nobody means. For that share of the whole write "{reading * 100:g}%" or {reading * steps:g}; '
            f'for a rating of {reading:g} out of {steps} write "{reading:g}/{steps}"'
        )
    if not 0 <= reading <= steps:
        raise ValueError(f"a rating of {reading:g} is outside a {steps}-step scale")
    return reading, steps


def _signed(value):
    """The reading in `value`, sign kept: "+15%" -> 15.0, "-2.4pp" -> -2.4, "0" -> 0."""
    digits = ""
    for character in str("" if value is None else value).strip().replace(",", ""):
        if character.isdigit() or character == "." or (character in "+-" and not digits):
            digits += character
        elif digits:
            break
    try:
        return float(digits)
    except ValueError:
        raise ValueError(f"a delta mark needs a signed number, not {value!r}") from None


def _share(value):
    """"76%" -> 0.76, "0.76" -> 0.76, "76" -> 0.76, and anything over the whole is 1."""
    text = str("" if value is None else value).strip().replace(",", "")
    try:
        reading = float(text.rstrip("%").strip())
    except ValueError:
        raise ValueError(f"a progress mark needs a share, not {value!r}") from None
    if text.endswith("%") or reading > 1:
        reading /= 100.0
    return min(1.0, max(0.0, reading))


def _body_row(index, count, what):
    """A row index naming a body row. Row 0 is the header and is none of these."""
    number = int(index)
    if not 1 <= number < count:
        raise ValueError(f"{what} names row {number}; the body rows are 1 to {count - 1} (row 0 is the header)")
    return number


def _column_index(index, columns):
    number = int(index)
    if not 0 <= number < columns:
        raise ValueError(f"emphasize_columns names column {number}; this table has {columns}")
    return number


def _cell_key(where, rows, columns):
    """(row, column) naming a cell that is actually there.

    The row's own length is checked as well as the header's. Ragged rows draw --
    a short row's missing cells come out empty -- but a mark on one of those
    missing cells reads `rows[row][column]`, which unchecked is a bare IndexError,
    the one misuse in this module that would not name itself.
    """
    count = len(rows)
    try:
        row, column = (int(part) for part in where)
    except (TypeError, ValueError):
        raise ValueError(f"a mark is keyed by (row, column), not {where!r}") from None
    if not 1 <= row < count or not 0 <= column < columns:
        raise ValueError(
            f"a mark at {(row, column)} is outside the body of a {count}-row, "
            f"{columns}-column table (row 0 is the header)"
        )
    if column >= len(rows[row]):
        raise ValueError(
            f"a mark at {(row, column)} has no cell: row {row} holds {len(rows[row])} of the "
            f"table's {columns} columns. Give the row that cell, or move the mark"
        )
    return row, column


# What a renderer shrinks a raised or lowered run to, measured off LibreOffice:
# 13pt came back as 7.5pt. Used for the width estimate, since the size written into
# the file is the line's own.
_SUB_RENDERED = 0.58

# The order a:tcPr's children have to be written in (ECMA-376, CT_TableCellProperties).
# The four edges come first and in this order, then the fill, and an XML file that
# writes them in any other order is not the format -- see _strip_borders.
_EDGES = ("lnL", "lnR", "lnT", "lnB")

# The table style python-pptx stamps on every table it creates: "Medium Style 2 -
# Accent 1", a blue banded thing from the Office gallery. Recognised by value so a
# table cloned out of a template keeps the style its designer chose.
_PPTX_DEFAULT_STYLE = "{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"


def _line_emu(points):
    """A line weight in EMU, which is the unit a:ln writes its `w` in."""
    return int(round(points * 12700))


def _strip_borders(cell, lines=None):
    """Set the cell's four edges: off, or carrying a rule the table wants there.

    python-pptx has no border API, so the four edge elements are written into the
    cell's properties as explicit noFill: absent would mean "inherit", and what it
    would inherit is the gallery style below. `lines` is `{edge: (colour, points)}`
    for the edges that carry a rule instead -- which is how a table draws its rules,
    because a border sits on the boundary wherever the renderer puts the boundary,
    and a rectangle laid at the boundary the arithmetic predicted does not.

    The order matters and getting it wrong is invisible in this renderer: `insert(0)`
    per edge leaves `lnB, lnT, lnR, lnL` -- exactly reversed. LibreOffice does not
    check the sequence, so every render and every measurement says the tables are
    clean; PowerPoint does check it, and drops the cell properties it cannot parse, so
    the reader sees the blue gallery table underneath. Nothing in a pipeline that
    judges decks by rendering them can catch that, so the order is asserted in a test.
    """
    properties = cell._tc.get_or_add_tcPr()  # noqa: SLF001 -- no API for cell borders
    for index, edge in enumerate(_EDGES):
        for existing in properties.findall(f"{{{_A}}}{edge}"):
            properties.remove(existing)
        drawn = (lines or {}).get(edge)
        if drawn is None:
            element = properties.makeelement(f"{{{_A}}}{edge}", {})
            element.append(element.makeelement(f"{{{_A}}}noFill", {}))
        else:
            colour, points = drawn
            element = properties.makeelement(f"{{{_A}}}{edge}", {"w": str(_line_emu(points)), "cap": "flat"})
            painted = element.makeelement(f"{{{_A}}}solidFill", {})
            painted.append(painted.makeelement(f"{{{_A}}}srgbClr", {"val": str(_rgb(colour))}))
            element.append(painted)
        properties.insert(index, element)


def _drop_gallery_style(tbl):
    """Take off the Office gallery style, keeping a template's own.

    Every cell here is styled explicitly, so the style underneath should be
    unreachable -- and it is not: a table style also carries the header's font, its
    borders and its band fills, and PowerPoint applies each of those wherever the
    cell itself is silent. Leaving it on means the deck's tables look one way here
    and another way in the room.
    """
    for element in tbl._tbl.iter(f"{{{_A}}}tableStyleId"):  # noqa: SLF001 -- no API for the table style
        if (element.text or "").strip().upper() == _PPTX_DEFAULT_STYLE:
            element.getparent().remove(element)
            break


# What a stack answers off its region rather than off its cursor. The width is the
# same for every band it will ever hand out; the height is the region's, and `left` is
# the cursor's.
_REGION_OF_A_STACK = frozenset({"x0", "y0", "x1", "y1", "w", "h"})


class Stack:
    """A cursor down a region: `take` hands back the next band, `rest` the remainder.

    What it replaces is arithmetic on y: a band written as
    `Box(BODY.x0, 5.34, BODY.x1, 6.72)` is a guess at where the last thing on the page
    ended, and the render is the only place it is checked.

    Bands are adjacent: `take(a)` then `take(b)` spends exactly `a + b`, so a page
    whose heights come from `table_size` and `the_smallest_box_a_chart_needs` can be
    budgeted against `box.h` and the sum will be right. `skip(h)` is the gap, and it
    is the only gap -- a gutter added here on the caller's behalf is height the
    caller cannot see and did not ask for, and a page that writes its own `skip` as
    well pays both. Pass `gutter=` to have one added after every band.
    """

    __slots__ = ("box", "gutter", "spent_by_rest", "spread_short", "spread_to_fit", "taken", "y")

    def __iter__(self):
        raise TypeError(
            "a Stack is a cursor down a region, not the bands it will hand out -- it does not know how "
            "many there are until you ask. For a band at a measured height call take(h) once per band; "
            "for a fixed number of equal bands ask the region instead: region.rows(n) hands back a list "
            "you can zip against your content"
        )

    def __init__(self, box, gutter=0.0):
        self.box = box
        self.gutter = gutter
        self.y = box.y0
        self.spent_by_rest = False
        self.spread_to_fit = False
        self.spread_short = False
        self.taken = 0

    def __getattr__(self, name):
        """The region's own geometry, because a question about the region is not a typo.

        Measure the copy, then take exactly that height:
        `text_size(caption, inner.w, size=15).h`. Answering that with `AttributeError:
        'Stack' object has no attribute 'w'` would punish the page for the one call that
        could have answered it. Every band a stack hands out is the
        full width of its region, so `w`, `x0` and `x1` are the same answer however far
        the cursor has gone; `h`, `y0` and `y1` are the region's, and `left` and `room`
        are the cursor's. Dividing is not delegated: `down.rows(3)` would carve up the
        whole region including the part already spent, so it says to ask `room` instead.
        """
        if name in _REGION_OF_A_STACK:
            return getattr(self.box, name)
        if not name.startswith("_") and hasattr(Box, name):
            raise AttributeError(
                f"a stack does not divide: {name!r} would carve up the whole region and the "
                f"cursor has already spent part of it. Ask what is left -- room.{name}"
            )
        raise AttributeError(f"a stack has no {name!r}; it has take, skip, rest, left, room")

    @property
    def left(self):
        """How much height is still unspoken for."""
        return max(0.0, self.box.y1 - self.y)

    @property
    def room(self):
        """What is still unspoken for, as a box, without taking it.

        `rest()` answers the same question and spends the region answering it, so a
        program that wanted to ask "does the figure fit in what is left" had to take
        the remainder first and then live with whatever it had taken. This is the
        question on its own: `fits(picture_size(fig, down.room), down.room)`.
        """
        return Box(self.box.x0, min(self.y, self.box.y1), self.box.x1, self.box.y1)

    def short_by(self, *heights):
        """How many inches these bands would run over, or 0.0 if they fit.

        The whole plan against the region, before the first band is drawn. Measuring
        every band correctly and then taking them one at a time discovers the sum at the
        last `take`, with everything above it already on the slide: the refusal names
        both numbers, but by then the only lever left is the band that happened to be
        last.

        Bands and gaps are the same thing here: they are inches down the region, so
        pass them in the order they occur and it does not matter which is which. A
        single list works too, for a plan built up in a loop.
        """
        if len(heights) == 1 and not isinstance(heights[0], (int, float)):
            heights = tuple(heights[0])
        return max(0.0, sum(float(height) for height in heights) - self.left)

    def slack(self, *heights):
        """How many inches this region has left over once these bands are in it.

        `short_by` the other way round, off the same arithmetic, because both
        questions come up before the first band is drawn and only one of them had an
        answer. This is the one that says whether the run should be hung from the top
        of the region or centred in it.
        """
        if len(heights) == 1 and not isinstance(heights[0], (int, float)):
            heights = tuple(heights[0])
        return max(0.0, self.left - sum(float(height) for height in heights))

    def centre(self, *heights):
        """Put half the leftover above the run, and hand back this same cursor.

        A cursor runs from the top of its region, so a column of copy shorter than
        the region it was given hangs from the top with every inch of slack under it.
        Beside a figure that fills its own column, that reads as the page slipping
        upward -- measured on a delivered page whose five points ended 62% down while
        the figure beside them ran to 88%.

        Centring it is `skip((region - run) / 2)` before the first `take`, which is
        four lines of arithmetic the author has to get right and the same arithmetic
        `short_by` already does. Pass the heights you measured for `short_by`; a run
        that does not fit is left where it is, because there is nothing to centre and
        `take` will say so at the band that overruns.

        Chains, so the measure-first shape stays one statement:

            down = stack(box).centre(*heights)
        """
        self.skip(self.slack(*heights) / 2)
        return self

    def spread(self, *heights):
        """Spend the leftover as the air between these bands, and hand back this cursor.

        `centre` puts the slack above and below the run; this puts it between the
        bands, which is what a lane of cards or a table over a chart wants -- the run
        then ends on the region's own bottom edge, so two columns given the same
        region end level and the page has no band of white under it.

        For components and not for running copy. The gap between two cards is air;
        the gap between two paragraphs is a break in a thought, and copy spread down
        a region reads worse than copy left at the top of it. A copy column far
        shorter than its region is a page bigger than what it holds, which no cursor
        can fix.

        The commonest defect on a delivered deck is the other thing: eleven of
        eighteen pages in one run ended their content between 60% and 70% down and
        left the rest empty, and four more were two columns that stopped at different
        heights. Both are this call not being made.

        A run with no slack still gets `GUTTER`, which is the floor and not the
        share. This used to be the other way -- no slack meant the gap the cursor
        already carried, on the reasoning that the region cannot pay for more -- and
        a delivered page shows what that buys: three tinted panels down a column at
        1.24-3.16, 3.16-4.78 and 4.78-6.70in, 0.000in apart twice, reading as one
        block somebody forgot to finish rather than as three cards. The program that
        drew it was `stack(right).spread(*need)` with `need` already shortened to fit
        the region exactly, so the share was zero. Welding components is not a
        cheaper page than a shorter one, so the bands are what gives: with no room
        for the floor the run overruns and `take` says by how much. `card_group` has
        had this floor from the start, by passing `gutter=GUTTER` into the same call.

        One band has no between, so it centres instead. So it needs no guard around
        it -- `stack(box).spread(*heights)` is right whether or not there is slack,
        and a caller that adds its own `skip` overruns by exactly what it skipped.

            down = stack(box).spread(*heights)
            for card, tall in zip(cards, heights):
                card_at(slide, down.take(tall), *card)
        """
        if len(heights) == 1 and not isinstance(heights[0], (int, float)):
            heights = tuple(heights[0])
        if len(heights) < 2:
            return self.centre(*heights)
        share = self.slack(*heights) / (len(heights) - 1)
        if share > max(self.gutter, GUTTER):
            self.gutter = share
            self.spread_to_fit = True
        elif self.gutter < GUTTER:
            self.gutter = GUTTER
            self.spread_short = True
        return self

    def take(self, height):
        """The next `height` inches, full width."""
        if height <= 0:
            raise ValueError("a band takes some height")
        if height > self.left + 1e-9:
            # Said, not refused. The refusal this used to be was the commonest way a build
            # ended -- 65 of 228 measured crashes -- and one refusal hid every page after
            # it: a live run rebuilt six times against the same sentence and shipped
            # nothing. The band is drawn at the height asked and runs past the region's
            # bottom; the review reports what it covers on that page, the other pages
            # come out, and the author reads the same arithmetic here as a warning.
            import warnings

            warnings.warn(
                "Drawn past the region: "
                + f"{height:.2f}in was asked for and {self.left:.2f}in is left in this region"
                # The band that refuses is never the band that overspent, so the refusal
                # says what the region has already paid out. A run reading only the last
                # number went back and shortened the last card twice, on a page whose
                # first three were the ones that did not fit.
                + (
                    f", after {self.taken} band(s) spent {self.y - self.box.y0:.2f}in of its "
                    f"{self.box.h:.2f}in. "
                    if self.taken
                    else ". "
                )
                + (
                    "rest() already spent it: it hands back the remainder and takes it. `room` is that "
                    "same box without taking it, and `left` is its height. "
                    if self.spent_by_rest
                    else ""
                )
                # The gap is invisible in the caller's arithmetic -- the heights add up and the
                # region still runs out -- so a cursor that is spending one says so here. `spread`
                # sizes its gap to leave the run ending exactly on the bottom edge, which means any
                # skip added on top of it overruns by exactly what was skipped.
                + (
                    f"spread() already sized this region's gap at {self.gutter:.2f}in so the run ends "
                    f"exactly on the bottom edge -- so a skip() or a gutter of your own on top of it "
                    f"overruns by exactly what you added. Take the bands and add nothing between them. "
                    if self.spread_to_fit
                    # The floor, not the share: spread had nothing to share and still keeps the
                    # deck's gap, so the bands are the only thing left to shorten. Said here
                    # because the run that met it measured every band correctly and the height it
                    # had not counted was the air between them.
                    else f"spread() had no slack to share, so this region keeps the deck's own "
                    f"{self.gutter:.2f}in between components -- any closer and n cards read as one "
                    f"unfinished shape. The bands are what has to give: count the n-1 gaps into "
                    f"short_by(*heights, *gaps) and shorten the run by what it reports. "
                    if self.spread_short
                    else f"This cursor leaves a {self.gutter:.2f}in gap after every band, so a run of n "
                    f"bands spends n gaps as well as their heights; slack() and short_by() answer "
                    f"about the heights alone. "
                    if self.gutter
                    else ""
                )
                # The commonest way a first band eats the whole region: something was
                # asked how tall it would be *in this region* and answered with the
                # region, because it fills what it is given. Two live builds looped on
                # it, each having measured a table against the whole body and then wanted
                # a legend under it.
                + (
                    f"And the {self.box.h:.2f}in this region has is what the first band spent, to the "
                    f"inch. A table or a card group asked how tall it will be in a box answers with "
                    f"the box, because it spreads into what it is given -- so measuring against the "
                    f"whole region and then wanting a band under it cannot come out. Take the band "
                    f"you want under it off the region first and measure against that -- which is "
                    f"the box the table is going to be drawn in. Not fill=False: that answers what the "
                    f"content alone asks for, and a table drawn at that height stands in a band it does "
                    f"not fill, measured once at 1.77in of a 5.10in band. "
                    if self.taken == 1 and abs(self.y - self.gutter - self.box.y0 - self.box.h) <= 0.2
                    else ""
                )
                # What to do about *this* build, which everything above is silent on: the
                # advice before this sentence is all about the run before it was drawn, and
                # a program meeting it has already drawn four bands. A live run retried the
                # identical code twice against an identical refusal, so the shortfall and
                # the two ways out are named here in the numbers of the band that failed.
                + f"This band is {height - self.left:.2f}in over what is left. Either fit it into the "
                f"{self.left:.2f}in there is -- fits(what, down.room) answers that without drawing -- or "
                f"shorten an earlier band and take this one again. "
                + "Measure every band first and ask short_by(*heights) before drawing any of them -- "
                "asked at the last band, the only band left to shorten is the last one. "
                + f"This band was drawn anyway, {height - self.left:.2f}in past the region's bottom, so the page "
                "builds and the review reports what it covers; shorten the run and build again",
                stacklevel=2,
            )
        band = Box(self.box.x0, self.y, self.box.x1, self.y + height)
        self.y = band.y1 + self.gutter
        self.taken += 1
        return band

    def rest(self):
        """Everything still unspoken for, as one band -- and spends it.

        `room` is the same box without spending it, and the two answers are one word
        apart: `room = down.rest()` -- the variable named after the query it meant --
        leaves the next `take` with 0.00in. Only one of them is a question, so `take`
        says which when it runs out this way.
        """
        if self.left <= 0:
            raise ValueError(
                "nothing is left in this region"
                + (". rest() already spent it; `room` is the same box without taking it" if self.spent_by_rest else "")
            )
        band = Box(self.box.x0, self.y, self.box.x1, self.box.y1)
        self.y = self.box.y1
        self.spent_by_rest = True
        return band

    def skip(self, height):
        """Leave `height` inches empty and carry on."""
        self.y += height
        return self


def stack(box, gutter=0.0):
    """A cursor down `box`, so a page of stacked bands needs no arithmetic on y.

    Bands come out adjacent; `skip(h)` puts a gap between them. Pass `gutter=` to have
    every `take` leave one.

    Adjacent is the default because most runs down a region are copy, a figure and its
    caption, or a table and the key under it -- type carries its own air in the line box
    and a gap added on the caller's behalf is height it cannot see. A run of *painted*
    bands is the other case and has its own call: `spread(*heights)` for a column of
    cards or panels, which never leaves less than `GUTTER` between them, and
    `card_group(..., down=True)` when the bands are cards.
    """
    return Stack(box, gutter)


def picture_fit(
    slide, image, box, theme, *, caption=None, size=LABEL_PT, align="center", font=None, cjk_font=None
):
    """A picture scaled to fit `box` whole, centred in it, with its caption under it.

    `add_picture` with one dimension scales the other, and which of the two to give
    depends on the image -- so a program that wants "fill this region, keep the
    aspect, do not crop" has to place it, measure, and place it again, reaching into
    `slide.shapes._spTree` to do it.

    Returns the picture and the box it and its caption really cover, which is smaller
    than `box` in one direction whenever the aspects differ -- and that difference is
    where a page's white space comes from. `picture_size` is the same measurement
    asked first, so the band can be cut to the figure instead of the figure being
    centred in a band cut by eye.
    """
    theme = _with_faces(theme, font, cjk_font)
    room = Box(box.x0, box.y0, box.x1, max(box.y0, box.y1 - _caption_strip(caption, size)))
    unit = Inches(1)
    shape = slide.shapes.add_picture(str(image), Inches(room.x0), Inches(room.y0), width=Inches(room.w))
    if shape.height / unit > room.h:
        shape._element.getparent().remove(shape._element)  # noqa: SLF001 -- no API for removing a shape
        shape = slide.shapes.add_picture(str(image), Inches(room.x0), Inches(room.y0), height=Inches(room.h))
    shape.left = Inches(room.x0 + (room.w - shape.width / unit) / 2)
    shape.top = Inches(room.y0 + (room.h - shape.height / unit) / 2)
    covered = _ink_box([shape])
    if caption:
        said = write(
            slide,
            Box(box.x0, room.y1 + 0.06, box.x1, box.y1),
            caption,
            size=size,
            colour=theme.get("muted", theme["foreground"]),
            align=align,
            font=theme.get("font_family"),
            cjk_font=theme.get("cjk_font_family"),
        )
        covered = Box(
            min(covered.x0, said.box.x0),
            covered.y0,
            max(covered.x1, said.box.x1),
            max(covered.y1, said.box.y1),
        )
    return Drawn(shape, covered)


def _caption_strip(caption, size):
    """The height `picture_fit` takes out of a box for the caption under the figure."""
    if not caption:
        return 0.0
    return (size + 9) / 72 * (1 + str(caption).count(chr(10))) + 0.08


def _image_ratio(image):
    """The image's own width to height, which is what `add_picture` scales by."""
    from pptx.parts.image import Image

    width, height = Image.from_file(str(image)).size
    if not width or not height:
        raise ValueError(f"{image} carries no size to scale a picture by")
    return width / height


def picture_size(image, box, *, caption=None, size=LABEL_PT):
    """How much room a picture and its caption need inside `box`, before placing it.

    A figure keeps its own aspect, so one of the two dimensions runs out first and
    the other comes back short -- and `picture_fit` centres what is left, which on a
    band cut by eye is a strip of white over the figure and another under it. Ask for
    the size first, hand that height to `stack.take`, and the centring has nothing
    left to centre in.

    Takes what `picture_fit` takes and reads the image's own pixels, so the two agree
    to the rounding. The box is anchored at `box`'s top-left corner: it is a size and
    not a placement.
    """
    strip = _caption_strip(caption, size)
    ratio = _image_ratio(image)
    width, height = box.w, box.w / ratio
    room = max(0.0, box.h - strip)
    if height > room:
        width, height = room * ratio, room
    return Box.at(box.x0, box.y0, w=width, h=height + strip)

def points(slide, box, theme, items, *, size=BODY_PT, numbered=False, mark="\u2022", colour=None,
           font=None, cjk_font=None,
           mark_colour=None, spacing=1.25):
    """A list of labels, each with a mark and a hanging indent.

    Written as a real bullet (`a:buChar` or `a:buAutoNum` with marL/indent) rather
    than by prefixing the string, so the wrap is the renderer's problem and the list
    stays a list when someone edits the deck. The hanging indent is what keeps the
    second line of an item aligned with its first rather than with the mark, and
    `numbered=True` writes `a:buAutoNum` in place of the bullet character.

    Every item is one paragraph, and what separates two of them is a paragraph
    setting rather than a blank line. Two claims stacked in one frame read as one
    thing whatever mark sits in front of them, and the build reports a box holding
    two or more of them as `listed_claims`.

    Returns the frame and the box the list really fills -- the hanging indent and the
    space between items included, both of which are this helper's own arithmetic and
    neither of which an author could see. `points_size` asks the same before drawing.
    """
    left, top, width, height = box.pptx()
    frame = slide.shapes.add_textbox(left, top, width, height).text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.margin_left = frame.margin_right = Inches(_FRAME_SIDE)
    frame.margin_top = frame.margin_bottom = Inches(_FRAME_ENDS)
    lines = _paragraphs(items)
    for index, line in enumerate(lines):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        pieces = line if _is_runs(line) else [Run(line)]
        for piece in pieces:
            para.add_run().text = piece.text
        para.line_spacing = spacing
        if index:
            para.space_before = Pt(size * 0.45)
        _bullet(para, size, numbered=numbered, mark=mark, colour=_paint(theme, mark_colour, "accent"))
        for run, piece in zip(para.runs, pieces):
            run.font.size = Pt(piece.size if piece.size is not None else size)
            if piece.bold is not None:
                run.font.bold = piece.bold
            ink = piece.colour if piece.colour is not None else _paint(theme, colour, "foreground")
            run.font.color.rgb = _rgb(ink)
            face = font or theme.get("font_family")
            han = cjk_font or theme.get("cjk_font_family")
            if face:
                run.font.name = face
            if han:
                _east_asian(run, han)
    needed = points_size(lines, box.w, size=size, font=font or theme.get("font_family"), spacing=spacing)
    return Drawn(frame, _copy_ink(box, needed, PP_ALIGN.LEFT, MSO_ANCHOR.TOP))


def _bullet(para, size, *, numbered=False, mark="\u2022", colour="#000000"):
    """The paragraph's mark and hanging indent, which python-pptx has no API for.

    a:pPr fixes the order of its children (ECMA-376): the bullet colour, then its
    size, then its font, then the bullet itself. Written in that order because
    PowerPoint drops what it cannot parse -- the same trap the table borders fell
    into.
    """
    properties = para._pPr if para._pPr is not None else para._p.get_or_add_pPr()  # noqa: SLF001
    hang = int(size * 1.5 / 72 * 914400)
    properties.set("marL", str(hang))
    properties.set("indent", str(-hang))
    for tag in ("buClrTx", "buClr", "buSzTx", "buSzPct", "buSzPts", "buFontTx", "buFont", "buNone",
                "buAutoNum", "buChar"):
        for existing in properties.findall(f"{{{_A}}}{tag}"):
            properties.remove(existing)
    fill = properties.makeelement(f"{{{_A}}}buClr", {})
    solid = fill.makeelement(f"{{{_A}}}solidFill", {})
    value = solid.makeelement(f"{{{_A}}}srgbClr", {"val": str(_rgb(colour))})
    solid.append(value)
    fill.append(solid)
    properties.append(fill)
    font = properties.makeelement(f"{{{_A}}}buFont", {"typeface": "Arial"})
    properties.append(font)
    if numbered:
        properties.append(properties.makeelement(f"{{{_A}}}buAutoNum", {"type": "arabicPeriod"}))
    else:
        properties.append(properties.makeelement(f"{{{_A}}}buChar", {"char": mark}))

# What a mismatched `anchor` costs: against a template anchoring its title placeholder
# to the bottom of a 0.98in row, a middle-anchored composed title landed 0.42in high --
# ink at y=36px against y=99px at 150 DPI, on the same 55px glyph.
def heading(
    slide,
    frame,
    theme,
    title,
    kicker=None,
    *,
    tint="surface",
    bleed=True,
    size=TITLE_PT,
    anchor="middle",
    font=None,
    cjk_font=None,
):
    """The title row with a ground under it, which is what gives a page a top edge.

    A title floating on the same white as the body leaves the page without one, and
    a deck of those reads as a document someone is reading out. A quiet band behind
    the title row gives every page the same anchor -- and because the band carries
    the title, it is grouping rather than decoration: the band gate reports a filled
    bar only when nothing sits on it.

    This is one way to give a page its top edge, not the only one. What a deck may
    not do is give each page a different one: the header is the element every page
    shares, so whatever form it takes, it takes the same form throughout.

    No rule under the title: an accent hairline repeated under every title is the most
    repeated mark in the deck and it carries nothing, which is the definition of the
    decoration this house style spends its warnings on. `rule` is still here for the
    dividers that do carry something -- a chart's baseline, a table's header.

    `bleed` runs the band the full width of the canvas, which reads as design; off,
    it stops just outside the safe area, which reads as a box. Returns the ground and
    its box, so a page that wants something at its right edge knows where the row
    ends -- `heading(...).x1` and `heading(...).box` are the same corner.

    `anchor` is where the title sits inside its row, and it is a parameter because a
    template decides it: anchored to the middle inside a template whose own title
    placeholder anchors to the bottom of its row, every composed title sits above every
    cloned one. The house style reports the template's answer as `title_row_as_code`;
    pass it here.
    """
    theme = _with_faces(theme, font, cjk_font)
    top = 0.0 if bleed else max(0.0, frame.kicker.y0 - 0.14)
    # Under the title it carries, not up from the body below it. Computed off the body
    # it once reached 0.04in past `body.y0` and put the corner of every card in the top
    # row inside the heading's ground; computed off the title, the air the title needs
    # and the gap the page needs stop being the same hundredths spent twice.
    bottom = frame.title.y1 + _TITLE_AIR
    x0, x1 = (0.0, CANVAS_W) if bleed else (MARGIN - 0.26, CANVAS_W - MARGIN + 0.26)
    band = Box(x0, top, x1, bottom)
    ground = plane(slide, band, theme, tint=tint)
    painted = _paint_of(theme, tint)
    title_ink = _ink_on(painted, theme["foreground"], theme["background"])
    face, han = theme.get("font_family"), theme.get("cjk_font_family")
    if kicker:
        write(
            slide,
            frame.kicker,
            kicker,
            size=KICKER_PT,
            colour=_ink_on(painted, theme.get("muted", theme["foreground"]), theme["background"]),
            font=face,
            cjk_font=han,
        )
    write(
        slide,
        frame.title,
        title,
        size=size,
        bold=True,
        colour=title_ink,
        font=face,
        cjk_font=han,
        anchor=anchor,
    )
    return Drawn(ground.shape, band)

def formula(slide, box, text, theme, *, size=BODY_PT, align="left", anchor="top", font=None, cjk_font=None):
    """An expression, set as one unbreakable line with real subscripts.

    A formula written with `write` is prose to a text box: it wraps wherever the box
    runs out, and where it runs out is the middle of a symbol. In a 3.9in column,
    "分类 logits = (Q'inst, concat(Q'sem, Q'bg))" breaks after "Q" with "'bg))" alone
    on the next line, and every subscript in it comes out flat -- F4 for F-sub-4,
    Qinst for Q-sub-inst -- so the one thing the notation was carrying is gone.

    So: wrapping off, because a line that overflows is a measurement the gates
    report and a line broken mid-symbol is a page nobody can read. The size steps
    down from `size` until the line fits the box, and if the floor is reached first
    the expression is split at its own top-level separators (semicolons) rather than
    anywhere. `_x` and `_{xyz}` subscript, `^x` and `^{xyz}` superscript, a lone
    latin letter is italic the way a variable is, and a word of two or more letters
    is upright the way `concat` and `softmax` are.

    The box it hands back is the expression's own ink at whatever size it settled on,
    and `formula_type_size` says that size before anything is drawn: a formula three
    steps under the body copy around it reads as a mistake.
    """
    theme = _with_faces(theme, font, cjk_font)
    face = theme.get("font_family")
    if _is_tex(text):
        return _formula_picture(slide, box, text, theme, size, align, anchor)
    lines = _paragraphs(text)
    if any(_is_runs(line) for line in lines):
        raise ValueError(
            "a formula sets every run at one size on purpose -- the renderer shrinks a raised or "
            "lowered run on its own, and a second size here multiplies the two. Write the expression "
            "as a string and mark its parts with _x and ^x; `write` is the helper that takes runs"
        )
    chosen, lines = _formula_size(lines, box.w, size, face)
    left, top, width, height = box.pptx()
    frame = slide.shapes.add_textbox(left, top, width, height).text_frame
    frame.word_wrap = False
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.margin_left = frame.margin_right = Inches(_FRAME_SIDE)
    frame.margin_top = frame.margin_bottom = Inches(_FRAME_ENDS)
    horizontal = _named(_ALIGNS, align, "align")
    vertical = _named(_ANCHORS, anchor, "anchor")
    frame.vertical_anchor = vertical
    for index, line in enumerate(lines):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.alignment = horizontal
        para.line_spacing = 1.25
        for body, level in _pieces(str(line)):
            for token, italic in _words(body):
                run = para.add_run()
                run.text = token
                # One size for every run, including the sub- and superscripts: the
                # renderer shrinks a raised or lowered run on its own, and shrinking
                # it here as well multiplies the two. Measured on a rendered deck: an
                # 18pt formula asked for 13pt subscripts and got 7.5pt, four tenths
                # of the line it sat on and unreadable at projector distance.
                run.font.size = Pt(chosen)
                run.font.italic = italic
                run.font.color.rgb = _rgb(theme["foreground"])
                if theme.get("font_family"):
                    run.font.name = theme["font_family"]
                if theme.get("cjk_font_family"):
                    _east_asian(run, theme["cjk_font_family"])
                if level:
                    _baseline(run, level)
    widest = max(_formula_width(line, chosen, face) for line in lines)
    needed = Box.at(
        0.0,
        0.0,
        w=min(box.w, widest + 2 * _FRAME_SIDE),
        h=len(lines) * _line_h(chosen, 1.25) + 2 * _FRAME_ENDS,
    )
    return Drawn(frame, _copy_ink(box, needed, horizontal, vertical))


def formula_type_size(text, width, *, size=BODY_PT, font=None):
    """The size `formula` will really set this expression at, before it sets it.

    It steps down from `size` until the line fits and then, at the floor, splits the
    expression at its own separators -- and where it stopped was visible only in the
    render. Ask first and the answer is a number: one that comes back at
    `BODY_FLOOR_PT` wants a wider column or fewer symbols, not another build. A TeX
    expression answers with the size its picture is scaled to in that width.
    """
    if _is_tex(text):
        width_in, _height_in = _tex_extent(text, size)
        room = max(0.5, width - 0.12)
        return round(size * min(1.0, room / width_in), 1) if width_in > 0 else size
    return _formula_size(_paragraphs(text), width, size, font)[0]


# Why an expression may be a picture at all, when every other helper here writes text
# python-pptx can measure: a text box has one baseline per line. A fraction, a root, a
# sum with its limits -- anything that stacks -- has no spelling in it, and a delivered
# page set the attention formula as `softmax(QK^T / sqrt(d_k)) V`, a slash for the bar
# and a stray radical sign, then broke it at the equals sign. Two-dimensional notation
# is set by a typesetter and placed as a picture, in the deck's ink at the deck's size.
_TEX_MARKS = ("\\\\frac", "\\\\sqrt", "\\\\sum", "\\\\prod", "\\\\int", "\\\\left", "\\\\right", "\\\\mathrm", "\\\\mathbf",
              "\\\\hat", "\\\\bar", "\\\\vec", "\\\\cdot", "\\\\times", "\\\\infty", "\\\\partial", "\\\\nabla",
              "\\\\alpha", "\\\\beta", "\\\\gamma", "\\\\delta", "\\\\epsilon", "\\\\theta", "\\\\lambda", "\\\\mu", "\\\\pi",
              "\\\\sigma", "\\\\tau", "\\\\phi", "\\\\omega", "\\\\Delta", "\\\\Sigma", "\\\\Omega", "\\\\log", "\\\\exp",
              "\\\\min", "\\\\max", "\\\\arg", "\\\\text", "\\\\operatorname", "\\\\le", "\\\\ge", "\\\\ne", "\\\\approx",
              "\\\\to", "\\\\rightarrow", "\\\\ldots", "\\\\dots")
_TEX_DPI = 300
_TEX_CACHE: dict = {}


def _is_tex(text):
    """Whether the expression is written in TeX: wrapped in `$`, or carrying a TeX command."""
    body = str(text or "").strip()
    if len(body) >= 2 and body.startswith("$") and body.endswith("$"):
        return True
    return any(mark in body for mark in _TEX_MARKS)


def _tex_body(text):
    return str(text).strip().strip("$").strip()


def _render_tex(tex, size, colour):
    """A transparent PNG of the expression, in the deck's ink, at `size` points.

    Rendered by matplotlib's own typesetter (mathtext, no TeX installation), which
    knows fractions, roots, sums, limits, Greek and the operators a slide's formula
    uses; a construct it does not know raises, and the message names it.
    """
    key = (tex, size, colour)
    if key in _TEX_CACHE:
        return _TEX_CACHE[key]
    import io

    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(0.1, 0.1))
    FigureCanvasAgg(figure)
    figure.text(0, 0, f"${tex}$", fontsize=size, color=colour)
    buffer = io.BytesIO()
    try:
        figure.savefig(buffer, dpi=_TEX_DPI, transparent=True, bbox_inches="tight", pad_inches=0.02, format="png")
    except ValueError as exc:
        raise ValueError(
            f"the expression {tex!r} did not typeset: {exc}. Write it in the TeX mathtext knows -- "
            "\\\\frac{}{}, \\\\sqrt{}, \\\\sum_{}^{}, \\\\mathrm{} for a word set upright, _{} and ^{} -- "
            "and keep the prose around it outside the expression"
        ) from exc
    _TEX_CACHE[key] = buffer.getvalue()
    return _TEX_CACHE[key]


def _tex_extent(text, size):
    """(width, height) in inches the expression takes at `size` points."""
    import io

    from PIL import Image

    png = _render_tex(_tex_body(text), size, "#000000")
    with Image.open(io.BytesIO(png)) as image:
        return image.width / _TEX_DPI, image.height / _TEX_DPI


def _formula_picture(slide, box, text, theme, size, align, anchor):
    tex = _tex_body(text)
    if any("\u4e00" <= character <= "\u9fff" or "\u3000" <= character <= "\u30ff" for character in tex):
        raise ValueError(
            "a TeX expression cannot carry CJK text -- the typesetter has no glyphs for it. Keep the words "
            "outside: `write` the sentence, and give `formula` the expression alone"
        )
    colour = theme.get("foreground") or "#000000"
    png = _render_tex(tex, size, str(colour))
    width_in, height_in = _tex_extent(tex, size)
    room_w, room_h = max(0.5, box.w - 2 * _FRAME_SIDE), max(0.2, box.h - 2 * _FRAME_ENDS)
    scale = min(1.0, room_w / width_in, room_h / height_in) if width_in > 0 and height_in > 0 else 1.0
    if scale < 1.0:
        _gave_up("formula size", size, round(size * scale, 1), "the expression was scaled to fit its box")
    needed = Box.at(0.0, 0.0, w=width_in * scale, h=height_in * scale)
    horizontal = _named(_ALIGNS, align, "align")
    vertical = _named(_ANCHORS, anchor, "anchor")
    placed = _copy_ink(box, needed, horizontal, vertical)
    import io

    shape = slide.shapes.add_picture(io.BytesIO(png), Inches(placed.x0), Inches(placed.y0), width=Inches(placed.w))
    shape.name = "formula"
    return Drawn(shape, placed)


# Why the icon is drawn here rather than described: three live decks drew the surface
# and the copy by hand and not one of them put an icon on a card, so 180 icons shipped
# unused while the cards' titles sat in a column of identical bold lines.
def card(
    slide,
    box,
    theme,
    *,
    icon=None,
    title="",
    body=(),
    tint="surface",
    size=BODY_PT,
    title_size=LEAD_PT,
    font=None,
    cjk_font=None,
):
    """A titled card: the surface, an icon, the title beside it, the copy under it.

    One card. `card_group` draws a row of them across a region or a column down it,
    passing each item straight into this call, so a group needs no helper of its own --
    and a helper of your own that reads the items itself is how a delivered deck drew
    seven groups with the icon missing from every card.

    The copy is body copy, so it is set at the body size (`BODY_PT`) and its title one
    step above it at `LEAD_PT`. Copy at `LABEL_PT` is copy sitting exactly on
    `BODY_FLOOR_PT`, with no room to step down and nothing between it and the smallest
    type the deck is allowed, and one ramp step between a title and its copy is the
    scale this house style's own §3 calls no hierarchy at all.

    `icon` is a name from `ICON_NAMES` in ppt_icons -- `find_icons("compare")` finds
    one, and an argument easier to write with one than without it has somewhere to put
    it. Everything else is the geometry, which is this function's business: the
    icon's square, the gap after it, the title's line, and the copy filling what is
    left inside the padding.

    The box it hands back is the card, because the card is exactly the region it was
    given -- the surface is what a reader sees, so there is nothing else it could
    honestly be. `card_body_box` is the other half, where the copy goes, so
    `fits(body, card_body_box(box, icon=..., title=...), size=size)` answers "did it
    go in" without a render.
    """
    theme = _with_faces(theme, font, cjk_font)
    surface = plane(slide, box, theme, tint=tint, radius=True)
    # Named for what they write, not `ink`: `ink` is the icon's own ink extents a few
    # lines down, and a title that took this one came out drawn with a Box.
    painted = _paint_of(theme, tint)
    title_ink = _ink_on(painted, theme["foreground"], theme["background"])
    copy_ink = _ink_on(painted, theme.get("muted", theme["foreground"]), theme["background"])
    glyph_ink = _shows_on(painted, theme["accent"], title_ink)
    x = box.x0 + PAD
    top = box.y0 + PAD
    head = _card_head(box, icon, title, title_size)
    if icon:
        side = _ICON_SIDE
        try:
            from ppt_icons import add_icon, the_ink_an_icon_covers
        except ImportError:
            add_icon = the_ink_an_icon_covers = None
        if add_icon is not None:
            # Centre the ink, not the square it was asked for. The two are not the same
            # square: measured over the whole set, a glyph's strokes fill anywhere from
            # nothing of its box's height (`minus`, a horizontal rule) to five sixths of
            # it, so centring the box put the ink of one icon a fifth of an inch off the
            # ink of the next in the same row of cards.
            ink = the_ink_an_icon_covers(icon, side) if the_ink_an_icon_covers else None
            offset = (head - (ink.h if ink else side)) / 2 - (ink.y0 if ink else 0.0)
            add_icon(slide, icon, Inches(x), Inches(top + offset), Inches(side), glyph_ink)
            x += side + _ICON_GAP
    if title:
        write(
            slide,
            Box(x, top, box.x1 - PAD, top + head),
            title,
            size=title_size,
            bold=True,
            colour=title_ink,
            font=theme.get("font_family"),
            cjk_font=theme.get("cjk_font_family"),
            anchor="middle",
        )
    if body:
        write(
            slide,
            card_body_box(box, icon=icon, title=title, title_size=title_size),
            body,
            size=size,
            colour=copy_ink,
            font=theme.get("font_family"),
            cjk_font=theme.get("cjk_font_family"),
        )
    return Drawn(surface.shape, box)


# A card's title line, its icon's square and the gap after the icon.
_CARD_HEAD = 0.36
_ICON_SIDE = 0.30
_ICON_GAP = 0.14
# Between the title's line and the copy under it.
_CARD_AIR = 0.10


def _card_head(box, icon, title, title_size):
    """How tall a card's title line is: one line of `title_size`, or the title's own.

    It was a flat 0.36in, which is a line of 16pt and no more -- so a card given
    `title_size=LEAD_PT` set a 20pt title in a box 0.36in tall, and a title long
    enough to wrap set its second line over the copy. Measuring it here means the
    card grows its head rather than printing two things in one place, and means
    `card_body_box` can say where the copy starts without knowing how it got there.
    """
    if not title:
        return _CARD_HEAD
    left = PAD + (_ICON_SIDE + _ICON_GAP if icon else 0.0)
    lines = lines_needed(title, max(0.2, box.w - PAD - left), size=title_size, bold=True)
    return max(_CARD_HEAD, lines * _line_h(title_size, 1.15) + 2 * _FRAME_ENDS)


def card_body_box(box, *, icon=None, title="", title_size=LEAD_PT):
    """Where a card's copy actually goes, so you can ask whether it will fit.

    `card` handed back the box it was given, which told an author nothing it did not
    already know -- and the one thing it needed to know, whether the copy cleared the
    title and the padding, was in the render. The icon is here because it decides how
    much of the title's line is left and therefore whether the title wraps, which is
    what pushes the copy down.

    An icon clears the copy even with no title beside it: `card` draws it in the head
    whether or not a title shares the line, and this said the copy started at the
    padding, which put the first line of it under the icon.
    """
    top = box.y0 + PAD
    if title or icon:
        top += _card_head(box, icon, title, title_size) + _CARD_AIR
    return Box(box.x0 + PAD, top, box.x1 - PAD, box.y1 - PAD)


def card_size(width, *, icon=None, title="", body=(), size=BODY_PT, title_size=LEAD_PT, font=None):
    """How tall a card has to be for what goes in it, before anything is drawn.

    The half of the card that could not be asked about. `card` fills the box it is
    given, so a row of four handed `frame.body.columns(4)` is four cards as tall as the
    body -- and two lines of copy in a 5.4in card is nine tenths void. `card_body_box`
    says where the copy goes inside a height already chosen; this says what the height
    should be.

    A box at the origin, like `text_size`: `.h` is what to ask a `stack` for, and the
    tallest of a row's cards is what the row needs -- `max(card_size(w, **c).h for c in
    cards)`, so they come out level and none of them is padded out to the page.
    `card_group` is that composition already written and is what a plain row or column
    of cards wants; this is for the groups that are not one -- a staggered column, a
    grid cell, a panel placed over a figure -- and for asking before drawing anything.
    """
    if width <= 2 * PAD:
        raise ValueError(f"{width:.2f}in is no width for a card; its padding alone is {2 * PAD:.2f}in")
    needed = 2 * PAD
    if title or icon:
        needed += _card_head(Box.at(0.0, 0.0, w=width, h=0.0), icon, title, title_size) + _CARD_AIR
    if body:
        needed += text_size(body, width - 2 * PAD, size=size, font=font).h
    return Box.at(0.0, 0.0, w=width, h=needed)


def _card_height(width, item):
    """`card_size` asked about an item, whose keys are `card`'s and not only its own.

    `tint` and `cjk_font` change nothing about how tall a card is, so `card_size` does
    not take them. Which keys it does take is read off the function rather than listed
    here, because a list of a card's fields kept beside the card is exactly what the
    helper this replaces got wrong.
    """
    asked = {name: value for name, value in item.items() if name in card_size.__kwdefaults__}
    return card_size(width, **asked).h


def card_group(slide, box, theme, items, *, down=False, gutter=GUTTER):
    """A row of cards across `box`, or a column of them down it: `card` once per item.

    The composition between the two halves that were already here. `card` draws one and
    `card_size` says how tall one has to be; dividing the region, levelling the cards and
    drawing n of them was left to the page, and a delivered deck wrote its own helper for
    it -- `(region.h - gutter * (n - 1)) / n`, which is `Box.rows`, over a plane with a
    title and a body under it, which is `card`. That helper read `title`, `body` and
    `tint` out of each item and nothing else, so the `icon` all seven of its groups
    carried was dropped seven times, with nothing raised and nothing to see.

    So an item is `card`'s own keyword arguments and `card(**item)` is what reads them:
    every argument `card` takes travels, and a key it does not take is refused here
    before the first card is drawn rather than quietly doing nothing.

        card_group(slide, frame.body, T, [
            {"icon": "link", "title": "One claim", "body": "what it rests on"},
            {"icon": "stack_2", "title": "Another", "body": "and its evidence"},
        ])

    Across is the default, a row across a region being the commoner shape; `down=True` is
    the column. Both spend the region rather than fill it: a row is levelled to the
    tallest height `card_size` asks for and centred in the region, a column keeps each
    card's own height and the leftover becomes the air between them, so the group ends on
    the region's own edge and none of its cards is padded out to the page. A group that
    does not fit is refused by `take`, which names both numbers.

    Hands back each card's `Drawn` in order, carrying `.box` for the run as a whole, so
    `group.box.y1 + GUTTER` is where the page carries on and `overlaps(group)` reads
    straight through.
    """
    items = [dict(item) for item in items]
    if not items:
        raise ValueError("a card group draws at least one card, and was given no items")
    unknown = sorted({key for item in items for key in item} - set(card.__kwdefaults__))
    if unknown:
        raise ValueError(
            f"a card group's items are card()'s own arguments: {unknown} is not among "
            f"{sorted(card.__kwdefaults__)}. A key this call does not pass on is a field the cards "
            f"come out without, which is what reading the items by hand cost seven groups of one deck"
        )
    if down:
        heights = [_card_height(box.w, item) for item in items]
        cursor = stack(box, gutter=gutter).spread(*heights)
        boxes = [cursor.take(height) for height in heights]
    else:
        columns = box.columns(len(items), gutter)
        tall = max(_card_height(column.w, item) for column, item in zip(columns, items))
        band = stack(box).spread(tall).take(tall)
        boxes = [Box(column.x0, band.y0, column.x1, band.y1) for column in columns]
    drawn = [card(slide, one, theme, **item) for one, item in zip(boxes, items)]
    return Cards(drawn, _ink_box([one.shape for one in drawn]))


# Character widths in ems, by class. Not a font metric: a formula is one line and
# the only question is whether it fits, so the class average decides the size and
# the render-side measurement catches the rest.
#
# The class average is the *shape* of a face, not its scale, and the six faces a
# theme may name do not share a scale -- so `_FACE_WIDTH` carries the scale and
# `_em_width` takes the face. Without that this table answered the same for all
# six, and the two whose names have no metric-compatible stand-in on this
# renderer set well over it: "12M" at 14pt estimates 0.342in and sets 0.446in in
# the face `Cambria` resolves to, which is 0.026in more than the box it is given
# holds, so the render folded it to "12" over "M".
_EMS = {"cjk": 1.02, "cap": 0.64, "low": 0.52, "digit": 0.56, "space": 0.28, "thin": 0.32, "other": 0.66}
_THIN = ".,;:!|'()[]{}ilt"

# How much wider than `_EMS` each face a theme may name actually sets, on the
# face this renderer resolves the name to. Written in when this module is, from
# the asset service's font table, which is where the measurement and the
# name-to-face substitution it was made against are recorded together.
_FACE_WIDTH = {}
# What a call that names no face gets. The widest of them, because this number
# reserves a box: guessing narrow is a label broken across two lines and nothing
# to see it -- the render has no overlap to report, the label is simply in
# pieces -- and guessing wide is a tenth of an inch of air.
_WIDEST_FACE = max(_FACE_WIDTH.values(), default=1.0)


# What a Han character advances, off the theme's CJK companion face. Exactly one em,
# which is how a full-width ideograph is drawn rather than how one renderer draws it,
# and `_EMS`'s 1.02 was a Latin table's guess at it.
#
# This renderer also opens a gap at every seam between a Han character and a Latin
# one -- 0.2400 em, measured over 1, 2, 4 and 8 alternations, linear in the count --
# and 58% of the strings in eight delivered decks carry at least one. It is not
# modelled here. The term is this renderer's, not the format's, and against the
# render it bought nothing anything downstream can see: `overset_copy` carries a
# line of slack, and with or without the term the count is out by two lines or more
# on 5 of 2996 strings against 6, under-estimating on none either way. What it was
# worth was 34 fewer one-line under-estimates in the author's own arithmetic, and
# that is not worth a constant read off one machine's LibreOffice.
_HAN_EM = 1.0

# Characters that take their width from what they sit beside. A curly quote in a
# Chinese sentence sets full-width and the same quote in an English one sets a third
# of that -- measured 1.000 em against 0.333 -- so a table that answers "Han or not"
# for U+201C answers wrongly half the time. Below U+2E80 and above Latin-1: the
# dashes, the quotes, the arrows, the maths signs and the circled numerals.
_WEAK_FROM = 0x2010
_WEAK_TO = 0x2E7F


def _han(character):
    """Whether this character is strongly Han-width, weak scripts excluded."""
    return ord(character) > 0x2E7F


def _scripts(text):
    """One 'cjk'/'latin' per character, weak ones taking the side they sit on.

    Backwards first because a closing quote belongs to the sentence it closes, then
    forwards for a string that opens with one, then Latin for a string that is only
    weak characters.
    """
    kinds = ["cjk" if _han(c) else ("weak" if _WEAK_FROM <= ord(c) <= _WEAK_TO else "latin") for c in text]
    for index, kind in enumerate(kinds):
        if kind != "weak":
            continue
        near = next((k for k in reversed(kinds[:index]) if k != "weak"), None)
        if near is None:
            near = next((k for k in kinds[index + 1 :] if k != "weak"), "latin")
        kinds[index] = near
    return kinds


def _em_width(text, size, face=None):
    """What `text` set at `size` in `face` is worth in inches.

    `face` is the theme's `font_family`; None means "any of the six", which
    reserves for the widest of them.
    """
    latin = 0.0
    han = 0.0
    kinds = _scripts(text)
    for character, kind in zip(text, kinds):
        if kind == "cjk":
            han += _HAN_EM
        elif character.isspace():
            latin += _EMS["space"]
        elif character.isdigit():
            latin += _EMS["digit"]
        elif character in _THIN:
            latin += _EMS["thin"]
        elif character.isupper():
            latin += _EMS["cap"]
        elif character.islower():
            latin += _EMS["low"]
        else:
            latin += _EMS["other"]
    # `_FACE_WIDTH` is how much wider a Latin face sets than `_EMS` says, so it
    # belongs to the Latin part alone. A Han character comes off the CJK companion
    # face and advances one em exactly, and multiplying it by the Latin face's
    # correction made every Chinese line read 8% wide. That error used to cancel the
    # one this function no longer has, which is why removing only the other one made
    # the prediction worse than it started.
    return (latin * _FACE_WIDTH.get(face, _WIDEST_FACE) + han) * size / 72.0


def _formula_width(line, size, face=None):
    total = 0.0
    for body, level in _pieces(str(line)):
        total += _em_width(body, size if level == 0 else size * _SUB_RENDERED, face)
    return total


def _formula_size(lines, width, size, face=None):
    """The largest size in the ramp that fits, and the lines to set at it."""
    room = max(0.5, width - 0.12)
    chosen = size
    while chosen > BODY_FLOOR_PT and max(_formula_width(line, chosen, face) for line in lines) > room:
        chosen -= 1
    if max(_formula_width(line, chosen, face) for line in lines) <= room:
        return chosen, lines
    # Still over at the floor: break at the expression's own separators, which is
    # the one place a break does not land inside a symbol.
    split = []
    for line in lines:
        split.extend(_clauses(str(line)))
    if len(split) > len(lines):
        _gave_up("formula lines", len(lines), len(split), "broken at its own separators")
        return _formula_size(split, width, size, face)
    return chosen, lines


def _first_words(paragraphs, most=28):
    """Enough of the copy to recognise which block gave way."""
    for paragraph in paragraphs:
        words = _flat(paragraph).strip()
        if words:
            return words[:most]
    return ""


def _clauses(text):
    parts = []
    current = ""
    for character in text:
        current += character
        if character in "；;":
            parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return parts or [text]


def _pieces(text):
    """(text, level) runs, where level is 0 for the baseline, -1 sub, +1 super."""
    out = []
    plain = []
    index = 0
    while index < len(text):
        character = text[index]
        if character in "_^" and index + 1 < len(text):
            level = -1 if character == "_" else 1
            index += 1
            if text[index] == "{":
                end = text.find("}", index)
                end = len(text) if end < 0 else end
                token = text[index + 1 : end]
                index = end + 1
            else:
                token = ""
                while index < len(text) and (text[index].isalnum() or text[index] == "'"):
                    token += text[index]
                    index += 1
            if plain:
                out.append(("".join(plain), 0))
                plain = []
            if token:
                out.append((token, level))
            continue
        plain.append(character)
        index += 1
    if plain:
        out.append(("".join(plain), 0))
    return out or [("", 0)]


def _words(text):
    """(token, italic) runs: a lone latin letter is a variable, a word is a name."""
    out = []
    current = ""
    for character in text:
        if character.isascii() and character.isalpha():
            current += character
            continue
        if current:
            out.append((current, len(current) == 1))
            current = ""
        out.append((character, False))
    if current:
        out.append((current, len(current) == 1))
    merged = []
    for token, italic in out:
        if merged and merged[-1][1] == italic:
            merged[-1] = (merged[-1][0] + token, italic)
        else:
            merged.append((token, italic))
    return merged or [(text, False)]


def _baseline(run, level):
    """Raise or lower the run, which python-pptx has no property for."""
    run.font._rPr.set("baseline", "30000" if level > 0 else "-25000")  # noqa: SLF001

def overlaps(boxes, tolerance=0.01):
    """Which pairs of these boxes overlap -- an assertion an author can make cheaply.

    Returns a list of (i, j). Regions that came out of the same division never
    overlap; this is for pages that mix a division with a box placed by hand.
    """
    hits = []
    for i, first in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            second = boxes[j]
            if (
                first.x0 < second.x1 - tolerance
                and second.x0 < first.x1 - tolerance
                and first.y0 < second.y1 - tolerance
                and second.y0 < first.y1 - tolerance
            ):
                hits.append((i, j))
    return hits


_ALIGNS = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}
_ANCHORS = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}


def _named(table, value, what):
    """A name from this table, or the python-pptx enum a caller passed instead.

    `write(..., align="right")` is what these helpers take and `align=PP_ALIGN.RIGHT` is
    what somebody writing python-pptx reaches for. One design pass wrote the second,
    the lookup raised `KeyError: <PP_PARAGRAPH_ALIGNMENT.RIGHT: 3>`, the round was
    reverted and the deck lost the whole pass over a spelling.
    """
    if isinstance(value, str):
        try:
            return table[value.lower()]
        except KeyError:
            raise ValueError(f"{what} is one of {', '.join(table)}, not {value!r}") from None
    if value in set(table.values()):
        return value
    raise ValueError(f"{what} is one of {', '.join(table)}, not {value!r}")


def _bare(shape):
    """A shape with no shadow, which `shadow.inherit = False` alone does not give.

    That call writes an empty `a:effectLst`, which is supposed to read as "no
    effects". It does not, because `add_shape` also stamps a `p:style` whose
    `a:effectRef idx="2"` points at the theme's second effect -- a drop shadow --
    and a renderer resolves the reference separately from the empty list.
    Left alone, every plane, every rule, every table hairline and every mark comes out
    with a grey shadow down its right side. Nothing on these pages is meant to float,
    and a deck of rectangles that all do is the single loudest sign that nobody looked
    at the render.

    The style also carries a line, a fill and a font off the theme's accent, and
    every caller here sets all three explicitly, so nothing goes with it.
    """
    shape.shadow.inherit = False
    element = shape._element  # noqa: SLF001 -- python-pptx has no API for p:style
    for style in element.findall(f"{{{_P}}}style"):
        element.remove(style)
    return shape


def _rgb(value):
    if isinstance(value, RGBColor):
        return value
    text = str(value).lstrip("#")
    return RGBColor(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
'''


def layout_module_source() -> str:
    """Source of the `ppt_layout` module a build script imports.

    The face-width table is written in here rather than typed into the module
    body: it was measured against the name-to-face substitutions recorded in
    `assets.fonts` and belongs beside them, and six numbers kept in two places
    are six numbers that drift.
    """
    table = "".join(f"    {name!r}: {factor},\n" for name, factor in FACE_WIDTH.items())
    return _LAYOUT_MODULE.replace(_FACE_WIDTH_MARKER, "_FACE_WIDTH = {\n" + table + "}")
