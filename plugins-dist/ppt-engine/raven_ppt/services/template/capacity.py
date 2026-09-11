"""How much copy a template's box holds, said while the author is choosing the page.

`replace_text` keeps the prototype's geometry, and `compose._copy_fit` says so at the
end of the program -- after the copy has been written. This is the same measurement
one step earlier, on the template's own boxes, so that the number reaches the author
while it is deciding what to write rather than after it has written it.

The mismatch it exists for is measurable in the templates themselves. All ten bundled
templates are Chinese-authored and ship CJK example copy (`green_aurora_tech_trends`
4,438 characters, `black_circuit_tech_launch` 6,091), so their boxes are drawn for
full-width glyphs at roughly two Latin characters each. Of 351 heading-class boxes
(18-27pt) carrying real copy, 219 cannot hold 40 Latin characters even at the generous
bound. An ordinary English headline does not fit a Chinese template's heading box, and
before this the author was never told: the whole template reply measured 45,851
characters and contained the substring `chars` zero times.

Four rules the measurement imposes, each of them a thing this module says out loud:

* **A range, never a point.** The width measurer reads 1.10 to 1.30 wider than the
  render draws (`compose.MEASURED_WIDTH_OVERSHOOT` records both measurements), so a
  single number would be the measurer talking. The lower bound is the promise and the
  upper is where it stops being one.
* **The direction.** Resolved over the ten templates' 1,116 speakable boxes, the
  anchors are top 65%, centre 25%, bottom 10%: a quarter of all overflow grows past
  *both* edges and a tenth grows *up*, off the top of the slide. Ninety boxes are both
  bottom-anchored and too tight for 40 characters, so the dangerous direction and the
  tight geometry coincide rather than cancelling out.
* **Three outcomes.** Shrink (the shape's own `normAutofit`) 731, spill 201, and the
  frame growing itself (`spAutoFit`) 184 -- which is the majority behaviour on
  `gold_panel_year_end_summary`, 109 of its 177 boxes, with no `normAutofit` at all.
* **Only the boxes it can speak for.** A size read off a run is a fact; a box that
  states none anywhere is not measured and gets no band. Where the inheritance chain
  answers, the band says the size is inherited, because a template's placeholder that
  declares nothing renders at a size its layout chose and `house_style` has measured
  that disagreement at 24pt declared against 28pt drawn.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from raven_ppt.services.assets.text_metrics import measurer
from raven_ppt.services.measure.fit import (
    RENDER_DRIFT_HEADROOM,
    LineOverflowError,
    capacity_lines,
    wrap,
)
from raven_ppt.services.measure.geometry import EMU_PER_INCH
from raven_ppt.services.template.compose import (
    MEASURED_WIDTH_OVERSHOOT,
    _anchor_of,
    _grows_itself,
    _inherited_frames,
    _shrinks_to_fit,
)

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"

# What the copy is wrapped against, per rule 1: the real column is the promise and the
# column the measurer's own overshoot could have invented is where the promise ends.
BOUNDS = (1.0, MEASURED_WIDTH_OVERSHOOT)

# Under this many characters of the template's own copy a box is a mark -- a step
# number, a bullet, a decorative bracket -- and not a copy slot. The two boxes in the
# whole bundled set whose own copy runs past the generous bound are `gold_panel` page
# 2's angle brackets, 0.031in wide with one glyph in each, and calling the tightest box
# on that page "holds 0" would name a mark as the page's copy limit.
COPY_CHARS = 4

# How close two boxes have to be to be read as one row of cards: the same width, the
# same height, to within this. Measured this way the ten templates carry 199 level
# rows, and 120 of them have every member shrinking -- which is the mechanism behind
# one card's type coming back a step under its neighbours', rather than a shrinking box
# sitting beside a non-shrinking one (0 of the 199 rows mix the two).
ROW_MATCH_IN = 0.15

# The measurer's advance per character, which is what a capacity count needs and no
# API returns. Read off a probe rather than hard-coded: the measurer is either FreeType
# over the deck's own faces or the export dialect's estimator, and a constant fitted to
# one of them is wrong under the other. Scale-free, so one reading serves every size.
_LATIN_PROBE = "Operating margin improved across every regional business unit while capital expenditure stayed flat"
_HAN_PROBE = "本季度营业收入同比增长百分之十二净利润率提升至百分之十八主要来自海外市场的稳定扩张"
_PROBE_PX = 96

# Where the ink sits, and so which way the copy that does not fit travels. Top is left
# unsaid -- it is 65% of the boxes and the direction a reader already assumes -- and
# centre carries its name only, with `LEGEND` saying what a centred box does. Bottom
# spells the direction out on every line that has one: it is 10% of the boxes, three of
# the ten templates anchor none there, and 90 boxes are both anchored there and too
# tight for 40 characters, which is a page destroyed rather than spoiled.
_DIRECTION = {"ctr": ", centred", "b": ", bottom-anchored (grows up)"}

# Why a box on the page carries no band, singular and plural. Counted and said rather
# than passed over: a page whose band covers four of its nine boxes is a different
# offer from one whose band covers all four of its four.
_NO_WRAP = ("does not wrap", "do not wrap")
_NO_SIZE = ("states no size", "state no size")

_SHRINKS = "shrinks"
_FRAME_GROWS = "frame grows"
_SPILLS = "spills"

# The vocabulary, said once per reply beside the lines that use it. Without it the
# band is six numbers and three verbs an author has to guess the meaning of, and the
# guess that costs a page is reading `holds` as "will be made to fit".
LEGEND = (
    "Each page's `holds A-B en / C-D zh` is how much copy its tightest text box takes at the size the "
    "template set there, counted in Latin characters and in CJK glyphs: A and C fit, B and D are the most "
    "the measurement can defend, and past B or D the copy has to go somewhere. Where it goes is the box's "
    "own: unmarked boxes are anchored top and grow downward, `centred` grows past both edges, and "
    "`bottom-anchored` grows upward off the top of the slide. What the box then does is the last word -- "
    "`shrinks` sets the copy under the size its neighbours keep, `frame grows` makes the box itself taller "
    "over whatever is under it, `spills` leaves the copy outside its box. A template page whose numbers are "
    "smaller than the copy you have for it is a page to adapt with fewer words, or one to draw instead."
)


@dataclass(frozen=True)
class Held:
    """One text box, and how much copy it holds at the size the template set in it."""

    size_pt: float
    inherited: bool
    """Whether the size came from the layout or master rather than from a run."""
    lines: int
    latin: tuple[int, int]
    han: tuple[int, int]
    anchor: str
    behaviour: str
    width_in: float
    height_in: float
    own_chars: int

    @property
    def is_copy(self) -> bool:
        """Whether the template put copy in this box rather than a mark in it."""
        return self.own_chars >= COPY_CHARS

    def said(self, count: int = 1) -> str:
        """The band, as it reads inside a page's line."""
        size = f"{self.size_pt:g}pt{' inherited' if self.inherited else ''}"
        row = f"{count} alike at {size} hold" if count > 1 else f"{size} holds"
        return (
            f"{row} {self.latin[0]}-{self.latin[1]} en / {self.han[0]}-{self.han[1]} zh"
            f"{_DIRECTION.get(self.anchor, '')}, {self.behaviour}"
        )


def held(shape, size_pt: float | None = None) -> Held | None:
    """What this box holds, or None when there is nothing this can honestly say.

    None for a box with wrapping off (its copy runs on one line as far as it likes, so
    a character count per line is not the thing that limits it), for a box with no room,
    and -- per rule 4 -- for a box no run and no ancestor states a size for. A band
    computed off a size nobody declared is a band that is wrong, and a wrong band is
    worse than none.

    `size_pt` overrides the file's answer, for the caller that has a render: the
    renderer has resolved every inheritance and every autofit, so where `house_style`
    has read a size off the page that is the size to measure against.
    """
    frame = getattr(shape, "text_frame", None)
    if frame is None or frame.word_wrap is False:
        return None
    text = frame.text.strip()
    inherited = False
    if size_pt is None:
        size_pt = _stated_size(frame)
        if size_pt is None:
            size_pt, inherited = _inherited_size(shape), True
    if not size_pt:
        return None
    width_in = ((shape.width or 0) - (frame.margin_left or 0) - (frame.margin_right or 0)) / EMU_PER_INCH
    height_in = ((shape.height or 0) - (frame.margin_top or 0) - (frame.margin_bottom or 0)) / EMU_PER_INCH
    if width_in <= 0 or height_in <= 0:
        return None
    bold = any(run.font.bold for para in frame.paragraphs for run in para.runs)
    font_px = int(round(size_pt * 96 / 72))
    # The same line count `_copy_fit` judges against, and for the same reason: the box's
    # height in lines is arithmetic on an assumed line-height factor, how many lines the
    # designer put in it is a fact, and where the two disagree the fact wins. Taking the
    # arithmetic alone here would promise less than the template's own page shows.
    lines = max(capacity_lines(height_in * 96, font_px), _rows(text, width_in, font_px, bold))
    return Held(
        size_pt=size_pt,
        inherited=inherited,
        lines=lines,
        latin=_band(width_in, font_px, lines, bold, han=False),
        han=_band(width_in, font_px, lines, bold, han=True),
        anchor=_anchor_of(shape),
        behaviour=_behaviour(shape),
        width_in=width_in,
        height_in=height_in,
        own_chars=len(text),
    )


def page_band(shapes) -> str:
    """The capacity band for one template page, or empty when nothing can be said.

    Two boxes, because they are the two an author gets wrong in different ways: the
    tightest copy box on the page, which is what breaks first, and the largest row of
    matched boxes, which is where one card coming back a size under its neighbours is
    visible. Where the tightest box is itself in that row, one clause says both.

    The boxes it cannot speak for are counted rather than passed over in silence: a
    page whose band covers four of its nine boxes is a different offer from one whose
    band covers all four of its four.
    """
    boxes: list[tuple[Held, object]] = []
    mute: Counter[str] = Counter()
    for shape in shapes:
        frame = getattr(shape, "text_frame", None)
        if frame is None or not frame.text.strip():
            continue
        if frame.word_wrap is False:
            mute[_NO_WRAP] += 1
            continue
        found = held(shape)
        if found is None:
            mute[_NO_SIZE] += 1
        else:
            boxes.append((found, shape))
    unmeasured = ", ".join(f"{count} {why[count > 1]}" for why, count in sorted(mute.items()))
    copy = [(found, shape) for found, shape in boxes if found.is_copy]
    if not copy:
        # Said rather than left blank, because a page with nine text boxes and no band
        # reads as a page with room. `mint_memphis_thesis_defense` page 2 is the case:
        # four numbers and a title with wrapping off, four bullets whose size is stated
        # nowhere in the file, and nothing this can honestly put a number on.
        return f"copy capacity unmeasured: {unmeasured}" if unmeasured else ""
    tightest = min(copy, key=lambda pair: (pair[0].latin[1], pair[0].latin[0], -pair[0].size_pt))
    said = [f"tightest {tightest[0].said()}"]
    row = _row(copy, tightest[0])
    if row is not None:
        member, count = row
        if member == tightest[0]:
            said = [f"tightest of {member.said(count)}"]
        else:
            said.append(member.said(count))
    if unmeasured:
        said.append(unmeasured)
    return "; ".join(said)


def row_band(shape, size_pt: float | None = None) -> str:
    """The band for one measured row of the house style, as `Row.line()` appends it.

    Shorter than a page's by one word: a house-style row is a box the author will draw
    with `ppt_layout.write`, which sets `auto_size` to none, so the template's own
    autofit is not what will happen to it and only the geometry and the anchor carry
    over. What overflows a written box always spills.
    """
    found = held(shape, size_pt)
    if found is None:
        return ""
    return f"holds {found.latin[0]}-{found.latin[1]} en / {found.han[0]}-{found.han[1]} zh"


def _band(width_in: float, font_px: int, lines: int, bold: bool, han: bool) -> tuple[int, int]:
    """How many characters of one script the box takes, at both bounds."""
    room = width_in * 96 / RENDER_DRIFT_HEADROOM
    return tuple(  # type: ignore[return-value]
        max(0, int(room * bound // (_em(bold, han) * font_px)) * lines) for bound in BOUNDS
    )


def _em(bold: bool, han: bool) -> float:
    """The measurer's advance per character of this script, in ems.

    CJK is probed at regular weight whatever the run's weight is. A full-width ideograph
    advances one em at any weight, which is what the FreeType side answers -- it has one
    CJK face and no bold companion for it -- while the estimator beside it applies a 5%
    Latin bold factor to every character it is handed, and `measurer()` returns the
    wider of the two. Taking that 5% would contradict the designer on the box this
    whole measurement started from: `green_aurora_tech_trends` page 20 fits 7 glyphs of
    bold 20pt copy in a 2.165in box, and the emboldened reading promises 6.
    """
    probe = _HAN_PROBE if han else _LATIN_PROBE
    return measurer().width(probe, _PROBE_PX, bold and not han) / _PROBE_PX / len(probe)


def _rows(text: str, width_in: float, font_px: int, bold: bool) -> int:
    """How many lines the template's own copy takes, at the generous bound."""
    if not text:
        return 0
    try:
        return len(
            wrap(
                text.replace("\x0b", "\n"),
                width_in * MEASURED_WIDTH_OVERSHOOT * 96,
                font_px,
                bold=bold,
                measurer=measurer(),
            )
        )
    except LineOverflowError:
        return 1


def _row(copy: list[tuple[Held, object]], tightest: Held) -> tuple[Held, int] | None:
    """The largest row of matched boxes on the page, and its tightest member.

    Matched on width, height and size rather than on position: a page's cards are laid
    out in a line most of the time and in a grid the rest of it, and a 2x3 grid of
    identical cards is one repeated unit to an author filling it with `items=`.
    """
    groups: dict[tuple[float, float, float], list[Held]] = {}
    for found, _shape in copy:
        key = (
            round(found.width_in / ROW_MATCH_IN),
            round(found.height_in / ROW_MATCH_IN),
            found.size_pt,
        )
        groups.setdefault(key, []).append(found)
    ranked = [members for members in groups.values() if len(members) > 1]
    if not ranked:
        return None
    # The row the tightest box is in, when it is in one, so the page says two things
    # rather than the same thing twice.
    for members in ranked:
        if tightest in members:
            return (tightest, len(members))
    largest = max(ranked, key=len)
    return (min(largest, key=lambda found: found.latin[1]), len(largest))


def _behaviour(shape) -> str:
    return _SHRINKS if _shrinks_to_fit(shape) else (_FRAME_GROWS if _grows_itself(shape) else _SPILLS)


def _stated_size(frame) -> float | None:
    sizes = [
        run.font.size.pt
        for para in frame.paragraphs
        for run in para.runs
        if run.font.size is not None and run.text.strip()
    ]
    return max(sizes) if sizes else None


def _inherited_size(shape) -> float | None:
    """The size the layout or the master states for this box, when no run does.

    Worth resolving because 815 of the ten templates' 2,084 copy-carrying boxes state
    no size on any run, and the chain answers for 372 of them -- boxes the reactive
    half stays silent about and an author still has to write into. Reported as
    inherited so the number carries its provenance: `house_style` has measured a
    template whose title placeholder declares 24pt and whose pages render at 28.
    """
    for holder in _inherited_frames(shape):
        try:
            body = holder.text_frame._txBody  # noqa: SLF001 -- no API reaches defRPr
        except AttributeError:
            continue
        for properties in body.iter(f"{{{_A}}}defRPr"):
            if properties.get("sz"):
                return int(properties.get("sz")) / 100.0
    return _master_style_size(shape)


def _master_style_size(shape) -> float | None:
    """The master's text style for this placeholder's kind, the last stop in the chain."""
    try:
        master = shape.part.slide.slide_layout.slide_master
        kind = str(shape.placeholder_format.type) if shape.is_placeholder else ""
    except (AttributeError, ValueError):
        return None
    styles = master._element.find(f"{{{_A}}}txStyles")  # noqa: SLF001 -- python-pptx models no text styles
    if styles is None:
        return None
    block = styles.find(f"{{{_A}}}" + ("titleStyle" if "TITLE" in kind else "bodyStyle"))
    if block is None:
        return None
    for properties in block.iter(f"{{{_A}}}defRPr"):
        if properties.get("sz"):
            return int(properties.get("sz")) / 100.0
    return None
