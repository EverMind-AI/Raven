"""The assets, projected into modules an author's build script can import.

On the script route the author writes a python-pptx program, so there is no
schema to hang a theme field, an icon field or a shape field on. The assets reach
it as files instead: ``ppt_theme.py`` beside ``themes.json``, ``ppt_icons.py``
beside ``icons.json`` and ``icon_keywords.json``, ``ppt_shapes.py`` beside
``shapes.json``, dropped into the build directory where the script runs. The
script does ``from ppt_theme import THEMES, rgb``, ``from ppt_icons import
add_icon`` and ``from ppt_shapes import chevron_row``.

The keywords are a second file rather than a field inside ``icons.json`` because
the two are read by different code at different times: the geometry is walked
once per drawn icon and the keywords only when a name misses. Folding them
together would make every draw parse a hundred kilobytes of tags.

This module produces the *content* only. Creating the build directory and
writing the files there belongs to the script backend, which owns that
directory's layout and lifecycle; assets are stateless and touch no filesystem
of their own. ``script_helper_files()`` returns exactly what to write and under
what name, because the filenames are part of the contract -- the script imports
them by name -- and so they are not the backend's to choose.

The generated modules import nothing from raven. That is deliberate: the build
directory has to be readable and runnable as a plain python-pptx project, the
author reads these files to learn what it may draw with, and a script that
reached back into raven internals would break the moment those moved. They do
import each other -- ``ppt_shapes`` takes ``Box`` from ``ppt_layout`` and ``rgb``
from ``ppt_theme`` -- because they land in one directory and a second copy of a
colour converter is a second thing to keep right.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from raven.ppt.services.assets import icons, shapes
from raven.ppt.services.assets.themes import THEME_GUIDE, THEMES

THEME_MODULE_FILENAME = "ppt_theme.py"
THEME_DATA_FILENAME = "themes.json"
ICON_MODULE_FILENAME = "ppt_icons.py"
ICON_DATA_FILENAME = "icons.json"
ICON_KEYWORD_FILENAME = "icon_keywords.json"
SHAPE_MODULE_FILENAME = "ppt_shapes.py"
SHAPE_DATA_FILENAME = "shapes.json"

# What a script draws with. The on-dark tokens and the engine's decoration
# dimensions stay behind: pages are white, and the decoration is the author's to
# design rather than a token to look up. No size or ladder is exported, because
# there is none to export -- a theme carries no type scale.
_EXPORTED_THEME_FIELDS = (
    "background",
    "surface",
    "foreground",
    "muted",
    "accent",
    "accent_soft",
    "grid",
    "chart_series",
    "font_family",
)

_THEME_MODULE = '''"""The deck's palette and faces, as plain data.

    from ppt_theme import THEMES, rgb
    T = THEMES[next(iter(THEMES))]      # one entry, and it is the template's
    INK, ACCENT, FONT = rgb(T["foreground"]), rgb(T["accent"]), T["font_family"]
    HAN = T["cjk_font_family"]          # the CJK companion for this theme

A deck is built inside a template, and the build writes that template's palette in
here as the single entry -- named after the template's own file. So take it by
iteration; a theme id typed into `THEMES[...]` is a KeyError.

Every colour is a #RRGGBB string. `chart_series` is the ordered list a chart paints
its series from, six of them off a template that declares six accents and whatever
the deck stated where it named its own.

`font_family` is the Latin face and `cjk_font_family` is its CJK companion. A
deck in Chinese needs both named on the same run -- `ppt_layout.write` does it for
you -- or the Han characters are set in whatever the viewer falls back to, which
on this renderer has no Han glyphs at all.
"""

import json
from pathlib import Path

from pptx.dml.color import RGBColor

THEMES = json.loads((Path(__file__).parent / "themes.json").read_text(encoding="utf-8"))
THEME_NAMES = sorted(THEMES)


def rgb(value):
    """'#RRGGBB' -> RGBColor, so a theme colour drops straight into python-pptx."""
    if isinstance(value, RGBColor):
        return value
    text = str(value).lstrip("#")
    return RGBColor(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
'''

_ICON_MODULE = '''"""Draw packaged Tabler Outline icons onto a python-pptx slide.

    from ppt_icons import add_icon, the_ink_an_icon_covers, ICON_NAMES

    ink = the_ink_an_icon_covers("check", 0.4)   # -> Box, inches: where it will land
    drawn = add_icon(slide, "check", Inches(1), Inches(2 - ink.y1), Inches(0.4), ACCENT)
    drawn.box                                    # -> Box, inches: where it did land

    find_icons("deadline")                       # -> ['calendar_due', ...]

Icons are strokes on a 24x24 grid, drawn as freeform shapes so they stay vector
and editable after export. `colour` takes either a '#RRGGBB' string or an
RGBColor, so a theme colour goes in as it comes out of ppt_theme.

The ink is not the square you give it, and how much of it varies enormously:
`target` covers 75% x 75% of the box, `check` 62% x 42%, `minus` 58% x 0% -- a rule
through the middle with no height at all. So ask `the_ink_an_icon_covers` before
drawing and read `add_icon(...).box` after, and an icon sits on a baseline or flush
with a card's padding without a render to nudge it against.

`add_icon`'s `left`, `top` and `size` are EMU -- `Inches(0.4)`, not `0.4`. It is
the one call in this directory that is not in inches, and a bare `0.4` means four
ten-millionths of an inch, which would draw a shape that rounds to nothing at the
corner of the page; a size that small is refused. Every box either function
*answers* with is in inches, like every other box here.

`find_icons` searches the names and each icon's keywords, so a word that is in no
filename still finds something.
"""

import difflib
import json
from pathlib import Path

from pptx.dml.color import RGBColor
from pptx.util import Emu

# The grid, beside this module in the build directory. `Box` and `_ink_box` are
# borrowed rather than copied for the reason ppt_charts borrows `_em_width`: an
# icon's ink is a rectangle on the page, the same kind of rectangle every other
# helper here hands back, and a second rectangle type in one directory is one the
# author has to keep converting between.
from ppt_layout import Box, _ink_box

_DATA = json.loads((Path(__file__).parent / "icons.json").read_text(encoding="utf-8"))
_KEYWORDS = {
    name: frozenset(words.split())
    for name, words in json.loads((Path(__file__).parent / "icon_keywords.json").read_text(encoding="utf-8")).items()
}
ICON_NAMES = sorted(_DATA)
_GRID = 24.0
_CURVE_STEPS = 8
# How small a run has to be, on the 24 grid, to be one of upstream's dots rather
# than a stroke. Nothing in the packaged set lands between the two: every dot is
# a stub spanning at most 0.016 and the shortest real stroke -- a ray on `sun`,
# the accent on `language` -- spans 0.58, so the line can sit anywhere in that
# gap and a stroke can never be mistaken for a dot.
_DOT_SPAN = 0.1
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _cubic(p0, p1, p2, p3, steps=_CURVE_STEPS):
    points = []
    for step in range(1, steps + 1):
        t = step / steps
        u = 1 - t
        points.append(
            (
                u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
                u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
            )
        )
    return points


def _polylines(name):
    """Flatten one icon's paths into polylines on the 24x24 grid."""
    lines = []
    for kind, commands in _DATA[name]:
        if kind != "path":
            continue
        current = None
        run = []
        for op, args in commands:
            if op == "M":
                if len(run) > 1:
                    lines.append(run)
                current = (args[0], args[1])
                run = [current]
            elif op == "L":
                current = (args[0], args[1])
                run.append(current)
            elif op == "C" and current is not None:
                p1, p2, p3 = (args[0], args[1]), (args[2], args[3]), (args[4], args[5])
                run.extend(_cubic(current, p1, p2, p3))
                current = p3
            elif op == "Z" and run:
                run.append(run[0])
        if len(run) > 1:
            lines.append(run)
    return lines


def _is_dot(polyline):
    """Whether this run is one of upstream's dots rather than a stroke.

    Tabler draws a dot -- the one under an exclamation mark, the one on an `i`,
    the beads of `wifi` and `braille` -- as a hundredth-of-a-unit segment that
    `stroke-linecap="round"` swells into a disc the width of the pen. A freeform
    strokes with flat caps, so the same segment paints a smudge a ten-thousandth
    of an inch long: 409 of them across 141 icons, and `warning` lost the dot
    under its bar.
    """
    xs = [x for x, _ in polyline]
    ys = [y for _, y in polyline]
    return max(xs) - min(xs) < _DOT_SPAN and max(ys) - min(ys) < _DOT_SPAN


def _dot_square(points, side):
    """The `side`-wide square, in EMU, that a round cap would have left here.

    Sized to the pen rather than to the icon, because that is what a round cap is
    -- the pen set down once. A dot scaled off `size` instead would swell into a
    blot beside strokes that keep their `width_pt` however large the icon gets.
    """
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    x0 = int((min(xs) + max(xs)) / 2) - side // 2
    y0 = int((min(ys) + max(ys)) / 2) - side // 2
    return [(x0, y0), (x0 + side, y0), (x0 + side, y0 + side), (x0, y0 + side)]


def _contained(key):
    """Names that are most of `key`, or of which `key` is most."""
    found = []
    for candidate in ICON_NAMES:
        if key not in candidate and candidate not in key:
            continue
        overlap = min(len(key), len(candidate)) / max(len(key), len(candidate))
        if overlap >= 0.6:
            found.append((-overlap, len(candidate), candidate))
    return [candidate for _, _, candidate in sorted(found)]


def find_icons(term):
    """The names nearest to `term`, best guess first. Empty if nothing is close.

    `term` does not have to be a name. Each icon carries the upstream tags and
    category as keywords, so "deadline" finds `calendar_due` and "risk" finds
    `warning` -- neither of which shares a letter with what was asked for.
    """
    key = str(term).strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")
    if not key:
        return []
    tokens = [part for part in key.split("_") if part]
    ranked = []

    def add(candidate):
        if candidate not in ranked:
            ranked.append(candidate)

    def by_hits(scored):
        return [item for _, _, item in sorted((-hits, len(item), item) for hits, item in scored if hits)]

    for candidate in by_hits([(sum(t in c.split("_") for t in tokens), c) for c in ICON_NAMES]):
        add(candidate)
    for candidate in _contained(key):
        add(candidate)
    for candidate in by_hits([(sum(t in _KEYWORDS[c] for t in tokens), c) for c in ICON_NAMES]):
        add(candidate)
    for candidate in difflib.get_close_matches(key, ICON_NAMES, n=8, cutoff=0.62):
        add(candidate)
    for token in tokens:
        for candidate in difflib.get_close_matches(token, ICON_NAMES, n=3, cutoff=0.7):
            add(candidate)
    return ranked[:8]


def _resolve(name):
    """Accept the upstream spelling, and name the near misses when there is none.

    Tabler publishes these as `map-pin`; they are stored here as `map_pin`, so an
    author writing what it knows would otherwise be refused over a separator. And
    a name outside the set entirely -- `throughput`, `stakeholder` -- is worth
    answering with what is close, because the alternative is a round spent
    guessing. `find_icons` runs the same search without raising.
    """
    key = str(name).strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")
    if key in _DATA:
        return key
    near = find_icons(key)
    if near:
        hint = "; closest: " + ", ".join(near)
    else:
        hint = f"; nothing close -- all {len(ICON_NAMES)} names are in ICON_NAMES"
    raise LookupError(f"unknown icon {name!r}{hint}")


def _line_color(colour):
    """A '#RRGGBB' string or an RGBColor, because ppt_theme hands out strings."""
    if isinstance(colour, RGBColor):
        return colour
    text = str(colour).lstrip("#")
    return RGBColor(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


_EMU_IN = 914400
# A side under a hundredth of an inch is not a small icon: it is inches handed to
# `add_icon`, which takes EMU. Measured -- `add_icon(s, "target", 1.0, 2.0, 0.4,
# INK)`, written the way the rest of this directory reads, put a shape 0.000000in
# wide one EMU from the corner and raised nothing, so the page came back with the
# icon simply absent. `left` and `top` get no such guard: 0 is a legal corner, so
# there is no line to draw between inches and EMU there.
_TOO_SMALL_FOR_EMU = _EMU_IN // 100
# And a side of more than this many inches is not a box on a 13.3in page; it is EMU
# handed to the call that takes inches -- the same mistake, mirrored.
_TOO_BIG_FOR_INCHES = 200


class Icon(list):
    """The shapes one `add_icon` drew, with `box` for the ink they cover.

    A list, because `for shape in add_icon(...)` and `add_icon(...)[0]` are how the
    return has always been read and both still read that way. `box` is the one thing
    the call could not say: it is measured off the shapes that landed, so it answers
    in the inches every other box here is in -- not in the EMU that went in.
    """

    __slots__ = ("box",)

    def __init__(self, shapes, box):
        super().__init__(shapes)
        self.box = box


# Why this has to be asked rather than assumed: 151 of the packaged icons are half
# their box or less tall.
def the_ink_an_icon_covers(name, size=1.0):
    """Where this icon's strokes will land in a square of side `size`, before drawing.

    A `Box` at the origin, in inches: add the corner you are about to draw at. Note
    the unit -- `add_icon` takes EMU for `left`, `top` and `size` and this takes
    inches, because every other box in this directory is inches and the answer has to
    add to one. With no size at all the four numbers read as fractions of whatever
    box you hand the icon.

    Same square, wildly different ink. As a share of that square: `target`, `clock`
    and `circle` cover 75% x 75%, `chart_bar` 75% x 67%, `check` 62% x 42%, and
    `minus` 58% x 0% -- a rule through the middle with no height. So "icon at the top
    of the card, title beside it" puts the mark three different distances from the
    title depending on which name was written.

    The box is the strokes' centrelines, which is what a freeform's own extent is:
    the pen straddles the path, so `width_pt` paints half its width outside every
    edge and moves no centre. Sitting `minus` on a baseline means sitting its
    zero-high box on it, which is where a reader sees the rule.
    """
    side = float(size)
    if not side > 0:
        raise ValueError(f"size={size!r}: an icon's square has a positive side")
    if side > _TOO_BIG_FOR_INCHES:
        raise ValueError(
            f"size={size!r} is inches here and not EMU, so write {side / _EMU_IN:g} rather than Inches(...). "
            f"add_icon is the one call in this directory that takes EMU."
        )
    runs = _polylines(_resolve(name))
    points = [point for run in runs for point in run]
    if not points:
        raise LookupError(f"icon {name!r} carries no strokes to measure")
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return Box(
        min(xs) / _GRID * side,
        min(ys) / _GRID * side,
        max(xs) / _GRID * side,
        max(ys) / _GRID * side,
    )


def add_icon(slide, name, left, top, size, colour, width_pt=1.75):
    """Draw `name` in a square of side `size` with its top-left at (left, top).

    `left`, `top` and `size` are EMU: `Inches(0.4)`, not `0.4`. A bare `0.4` is four
    ten-millionths of an inch and would draw a shape that rounds to nothing, so it is
    refused.

    Hands back the shapes it drew -- a list -- carrying `.box` for
    the ink they cover, in inches. The ink is not the square: `target` fills
    75% x 75% of it, `check` 62% x 42%, `minus` 58% x 0%, so `.box` is the only
    honest answer to "where did the mark end up", and `the_ink_an_icon_covers` is
    that answer before anything is drawn.
    """
    side = float(size)
    if side < _TOO_SMALL_FOR_EMU:
        raise ValueError(
            f"size={size!r} is {side / _EMU_IN:.8f}in: add_icon's left, top and size are EMU, so write "
            f"Inches({size!r}). Every box it answers with, and the_ink_an_icon_covers takes, is inches."
        )
    name = _resolve(name)
    colour = _line_color(colour)
    scale = side / _GRID
    stroke = int(width_pt * 12700)
    shapes = []
    for polyline in _polylines(name):
        points = [(left + x * scale, top + y * scale) for x, y in polyline]
        # A dot has no length for a stroke to run along, so it is painted rather
        # than drawn: a filled square as wide as the pen, which is the footprint
        # the round cap upstream counts on would have left, and which therefore
        # reads at the weight of the strokes it sits among.
        dot = _is_dot(polyline)
        if dot:
            points = _dot_square(points, stroke)
        (x0, y0), rest = points[0], points[1:]
        if not rest:
            continue
        builder = slide.shapes.build_freeform(Emu(int(x0)), Emu(int(y0)))
        builder.add_line_segments([(Emu(int(x)), Emu(int(y))) for x, y in rest], close=dot)
        shape = builder.convert_to_shape()
        if dot:
            shape.fill.solid()
            shape.fill.fore_color.rgb = colour
            shape.line.fill.background()
        else:
            shape.fill.background()
            shape.line.color.rgb = colour
            shape.line.width = Emu(stroke)
        # An icon is strokes, and strokes do not float. `convert_to_shape` stamps a
        # `p:style` whose `a:effectRef` points at the theme's drop shadow, which a
        # renderer resolves whatever the empty `a:effectLst` says -- so every stroke
        # of every icon came out with a grey double under it.
        for style in shape._element.findall(f"{{{_P}}}style"):
            shape._element.remove(style)
        shapes.append(shape)
    if not shapes:
        raise LookupError(f"icon {name!r} carries no strokes to draw")
    return Icon(shapes, _ink_box(shapes))
'''


_SHAPE_MODULE = '''"""Office preset shapes, positioned by the engine rather than by eye.

    from ppt_shapes import chevron_row, connect, preset, timeline
    from ppt_layout import LABEL_PT, write

    for step, label in zip(chevron_row(slide, band, T, 5), ("采集", "清洗", "标注", "训练", "评测")):
        write(slide, step.box, label, size=LABEL_PT, colour=T["background"], font=F, cjk_font=HAN,
              align="center", anchor="middle")

Office's preset geometries, by their DrawingML names -- `chevron`, `rightArrow`,
`flowChartDecision`. `PRESET_NAMES` is the 109 of Office's 177 that a business page can
use; the other 68 were drawn at their default adjustments and reviewed out, and `preset`
refuses one by name with the reason and what to reach for instead rather than drawing it.
`preset_intent(name)` says what a shape is for, and `find_presets(term)` answers a near
miss, marking a name that is out. They stay presets in the export: editable, theme-aware,
and carrying their own text rectangle, which is what drawing the same outline as a
freeform would throw away.

Boxes are inches, exactly as in `ppt_layout`, and every helper here hands back the box
its copy goes in -- so a five-step process is five words and no coordinates. Nothing
here takes a type size: the copy is written with `ppt_layout.write`, so the deck's one
ramp still decides it.
"""

import difflib
import json
import re
from collections import namedtuple
from pathlib import Path

from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.util import Inches, Pt

from ppt_layout import Box
from ppt_theme import rgb

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DATA = json.loads((Path(__file__).parent / "shapes.json").read_text(encoding="utf-8"))
_ALL_NAMES = sorted(_DATA)
# All 177 were rendered at their default adjustments and looked at, and 68 came back
# unusable on a business page: the action buttons bake in a grey glyph, the pseudo-3D
# shapes a grey second face, the leader callouts throw the leader outside the box, the
# single-sided braces come out as a slab with a spike, and the bursts and the clip art
# are decoration. The data still carries all 177 -- it is the vocabulary that shrank, not
# the catalogue -- and both `find_presets` and `preset` answer a dropped name with the
# reason, because a name refused with no reason reads as a misspelling and the next three
# tries go on respelling it.
_OUT_OF_VOCABULARY = {
    name: reason
    for names, reason in (
        (
            (
                "actionButtonBackPrevious", "actionButtonBeginning", "actionButtonBlank",
                "actionButtonDocument", "actionButtonEnd", "actionButtonForwardNext",
                "actionButtonHelp", "actionButtonHome", "actionButtonInformation",
                "actionButtonMovie", "actionButtonReturn", "actionButtonSound",
            ),
            "its grey glyph is baked into the geometry and no theme colour reaches it -- draw a rect or "
            "roundRect and write the label",
        ),
        (
            (
                "bevel", "can", "cube", "curvedDownArrow", "curvedLeftArrow", "curvedRightArrow",
                "curvedUpArrow", "ellipseRibbon", "ellipseRibbon2", "funnel", "horizontalScroll",
                "ribbon", "ribbon2", "verticalScroll",
            ),
            "it renders a second face in a hardcoded grey the theme cannot reach -- use rect or roundRect",
        ),
        (
            (
                "irregularSeal1", "irregularSeal2", "star10", "star12", "star16", "star24", "star32",
                "star4", "star5", "star6", "star7", "star8",
            ),
            "a starburst is sale signage and a business page has nothing for one to say, so there is no "
            "replacement to name",
        ),
        (
            # `gear6` and `gear9` are out twice over: at their default adjustments the teeth
            # come out uneven as well.
            (
                "cloud", "gear6", "gear9", "heart", "lightningBolt", "moon", "noSmoking", "smileyFace",
                "sun", "swooshArrow", "teardrop",
            ),
            "it is clip art, which a theme colour does not make part of the deck -- use rect or roundRect",
        ),
        (
            (
                "accentBorderCallout1", "accentBorderCallout2", "accentBorderCallout3", "accentCallout1",
                "accentCallout2", "accentCallout3", "borderCallout1", "borderCallout2", "borderCallout3",
            ),
            "its default adjustment throws the leader line outside the box, so it renders broken -- use "
            "wedgeRectCallout, wedgeRoundRectCallout or wedgeEllipseCallout",
        ),
        (
            ("leftBrace", "leftBracket", "rightBrace", "rightBracket"),
            "at its default adjustment it renders as a filled slab with a spike -- use bracePair or "
            "bracketPair, which render as the brace they name",
        ),
        (
            ("cornerTabs", "plaqueTabs", "squareTabs"),
            "it renders as disconnected corner fragments rather than as one shape, and nothing here "
            "replaces it",
        ),
        (
            ("chartPlus", "chartStar", "chartX"),
            "it is a crosshair meant to overlay a plot, and drawn on its own it reads as a crossed-out box",
        ),
    )
    for name in names
}
PRESET_NAMES = [name for name in _ALL_NAMES if name not in _OUT_OF_VOCABULARY]
_SHAPE_OF = {member.xml_value: member for member in MSO_SHAPE.__members__.values()}
_CONNECTORS = {"straight": MSO_CONNECTOR.STRAIGHT, "elbow": MSO_CONNECTOR.ELBOW, "curved": MSO_CONNECTOR.CURVE}

# A step's copy sits in the shape's own text rectangle, which for a chevron is inset
# past both points -- that inset is why the words do not sit on the arrow.
Step = namedtuple("Step", "shape box")
# `box` is the region under the spine and `above` the one over it, so a stop can carry
# a label and a date without either being placed by hand.
Stop = namedtuple("Stop", "mark box above")
Track = namedtuple("Track", "spine stops")


class Row(list):
    """The steps one `chevron_row` drew.

    A list, because `for step in chevron_row(...)` and `zip(chevron_row(...), labels)`
    are how the return reads. `steps` is that same list under the name its sibling
    `timeline` gives the stops, so one call does not have to be remembered differently
    from the other.
    """

    __slots__ = ()

    @property
    def steps(self):
        return self


def _how_many(n, what, thing):
    """The count, whether `n` is the count or the things to be counted.

    `chevron_row(slide, box, T, labels)` is what a caller holding the labels writes,
    and `len` is the number it meant. The copy is still written by the caller, into
    the box each step hands back -- so the deck's one ramp decides its size, and a
    row that was handed its labels does not end up with two of each.
    """
    if hasattr(n, "__len__") and not isinstance(n, str):
        count = len(n)
    else:
        try:
            count = int(n)
        except (TypeError, ValueError):
            raise TypeError(
                f"a {what} takes the number of {thing}s, or a list of the {thing}s themselves, "
                f"not {n!r}"
            ) from None
    if count < 1:
        raise ValueError(f"a {what} has at least one {thing}, not {count}")
    return count

# Thin enough to read as a rule rather than as a bar of colour.
SPINE = 0.045

# The line between two steps of a chevron row, in points. A row drawn in one solid tint
# with no outline renders as one long arrow carrying five words: a scanline across a
# five-step row at 110 dpi found each notch as a single pixel of antialiasing, 32 levels
# off the fill, between 245-pixel runs of that same fill. At this width in the ground
# colour the notch comes out white and the row reads as five steps, and the same line
# around the outside of the row lands on the page's own ground, where there is nothing for
# it to show against.
SEAM_PT = 0.75


def find_presets(term):
    """The preset names nearest to `term`, best guess first. Empty if nothing is close.

    Ranked by how much of a name a shared word accounts for, because twenty-nine of
    these carry the word "arrow": answering `arrowRight` alphabetically gets you
    eight names that are none of them `rightArrow`.

    A name this vocabulary dropped is still answered, marked with the reason it is out.
    Leaving it out of the search instead makes a near miss on a name Office really has
    come back empty, which reads as a spelling problem rather than as a decision.
    """
    return [_marked(name) for name in _ranked(term)]


def _marked(name):
    """One search result, carrying why it is out of the vocabulary when it is."""
    reason = _OUT_OF_VOCABULARY.get(name)
    return name if reason is None else f"{name} (not in this vocabulary: {reason})"


def _ranked(term):
    """The names nearest to `term`, best guess first, whatever their standing here."""
    key = _key(term)
    if not key:
        return []
    words = _words(term)
    ranked = []

    def add(candidate):
        if candidate not in ranked:
            ranked.append(candidate)

    scored = []
    for candidate in _ALL_NAMES:
        parts = _words(candidate)
        if parts & words:
            scored.append((-len(parts & words), len(parts ^ words), candidate))
    for _, _, candidate in sorted(scored):
        add(candidate)
    for candidate in _ALL_NAMES:
        folded = _key(candidate)
        if key in folded or folded in key:
            add(candidate)
    folded = {_key(name): name for name in _ALL_NAMES}
    for candidate in difflib.get_close_matches(key, list(folded), n=8, cutoff=0.62):
        add(folded[candidate])
    return ranked[:8]


def preset_intent(name):
    """The one line that says what a preset is for."""
    return _DATA[_resolve(name)]["for"]


def preset_adjustments(name):
    """The names of the knobs this preset has, in the order `adj` takes them."""
    return tuple(_DATA[_resolve(name)]["adj"])


def preset(slide, box, theme, name, *, adj=None, tint="accent", outline=None, width_pt=1.5):
    """One preset shape filling `box`.

    `adj` is a number, or one per knob in the order `preset_adjustments(name)` gives
    them. Most are a fraction of the shape -- the same number python-pptx's own
    `shape.adjustments` takes -- `preset(..., "roundRect", adj=0.06)` is a small
    radius, `adj=0.5` a stadium. On the seven presets built out of a sector (`arc`,
    `blockArc`, `chord`, `pie`, and the three circular arrows) the knobs that place
    the ends of the sweep are **degrees**, clockwise from three o'clock: a
    three-quarter pie is `adj=(0, 270)`, and 0.75 there asks for three quarters of
    one degree and draws a hairline without complaint. Four more, the `adj2` on each
    circular arrow and on `mathNotEqual`, are angles this module will not scale for
    you and it raises naming the one you passed. `tint` and `outline` name a theme
    colour or give a '#RRGGBB'; either may be None for no fill or no line.

    Copy goes in with `ppt_layout.write` into `box`, or into the narrower box
    `chevron_row` hands back for a shape with points in the way.
    """
    name = _resolve(name)
    shape = slide.shapes.add_shape(_SHAPE_OF[name], *box.pptx())
    _paint(shape, theme, tint, outline, width_pt)
    if adj is not None:
        _adjust(shape, name, adj)
    return shape


def chevron_row(slide, box, theme, n, *, adj=0.5, tint="accent", flat_start=True, outline=None):
    """`n` chevrons interlocking across `box`, and the text box inside each.

    `n` is the number of steps or the steps themselves -- pass the labels and `len` is
    the count. The labels are not written for you: each `Step` carries the box its copy
    goes in, and `ppt_layout.write` puts it there at a size from the deck's ramp.

    The arithmetic is the whole point of the helper. A chevron's notch is
    `min(width, height) * adj` deep, so the next one has to start exactly that far
    short of where this one ends -- then the point lands in the notch and the row
    reads as one sequence. Off by a tenth of an inch and it reads as five shapes
    that nearly touch, which is what a program computing it between renders produces.

    `flat_start` gives the first step a flat left edge, which is what a sequence that
    starts here looks like; pass False when the row continues something.

    Every step carries a hairline outline in the theme's ground colour, because a row in
    one solid `tint` with no outline is one long arrow with five words on it -- the notch
    between two steps of the same fill is a colour boundary with no colour change at it.
    An `outline` of your own still wins; see SEAM_PT for what the render measured.

    Returns a list of `Step(shape, box)`. Write into `step.box`.
    """
    n = _how_many(n, "chevron row", "step")
    share = min(max(float(adj), 0.0), 1.0)
    height = box.h
    # Solve the row: n shapes of one width, each overlapping the last by its notch.
    width = (box.w + (n - 1) * height * share) / n
    if width < height:
        # A tall step notches on its width instead, so the solve changes with it.
        width = box.w / (n - (n - 1) * share)
    overlap = min(width, height) * share
    seam = outline is None
    steps = []
    for index in range(n):
        x0 = box.x0 + index * (width - overlap)
        first = index == 0 and flat_start
        shape = preset(
            slide,
            Box(x0, box.y0, x0 + width, box.y1),
            theme,
            "homePlate" if first else "chevron",
            adj=share,
            tint=tint,
            outline=theme["background"] if seam else outline,
            width_pt=SEAM_PT if seam else 1.5,
        )
        if first:
            inner = Box(x0, box.y0, x0 + width - overlap / 2, box.y1)
        elif width > 2 * overlap:
            inner = Box(x0 + overlap, box.y0, x0 + width - overlap, box.y1)
        else:
            # Squatter than two notches: the preset gives up on the inset and so does this.
            inner = Box(x0, box.y0, x0 + width, box.y1)
        steps.append(Step(shape, inner))
    return Row(steps)


def timeline(slide, box, theme, n, *, tint="accent", arrow=True, colour=None):
    """A spine across `box` with `n` stops on it, and a label region at each.

    The spine runs along the middle of `box` and the stops are centred in `n` equal
    cells, so nothing lands on a page edge and no two labels share a column. `n` is the
    number of stops or the stops themselves -- pass the milestones and `len` is the
    count. Returns `Track(spine, stops)`; each stop has `box` under the spine and
    `above` over it, which is where a label and its date go with `ppt_layout.write`.
    """
    n = _how_many(n, "timeline", "stop")
    ink = rgb(colour or (theme[tint] if tint in theme else tint))
    middle = (box.y0 + box.y1) / 2
    dot = min(0.18, box.h / 3)
    head = dot * 1.15 if arrow else 0.0
    spine = preset(
        slide,
        Box(box.x0, middle - SPINE / 2, box.x1 - head, middle + SPINE / 2),
        theme,
        "rect",
        tint=ink,
    )
    if arrow:
        tip = Box(box.x1 - head, middle - head / 2, box.x1, middle + head / 2)
        point = preset(slide, tip, theme, "triangle", tint=ink)
        point.rotation = 90  # the preset points up; a timeline points the way it reads
    stops = []
    cell = box.w / n
    for index in range(n):
        centre = box.x0 + (index + 0.5) * cell
        at = Box(centre - dot / 2, middle - dot / 2, centre + dot / 2, middle + dot / 2)
        mark = preset(slide, at, theme, "ellipse", tint=ink)
        stops.append(
            Stop(
                mark,
                Box(centre - cell / 2, middle + dot, centre + cell / 2, box.y1),
                Box(centre - cell / 2, box.y0, centre + cell / 2, middle - dot),
            )
        )
    return Track(spine, stops)


def connect(slide, start, end, theme, *, kind="straight", arrow=True, colour=None, width_pt=1.5):
    """A connector from the edge of one box to the edge of another.

    The edges are chosen from where the two boxes sit -- side by side connects right
    to left, stacked connects bottom to top -- so a flow drawn from a division of the
    page never needs a coordinate. Either end may also be an `(x, y)` point, for the
    arrow that goes to a place rather than to a region. `kind` is "straight", "elbow"
    or "curved".
    """
    if kind not in _CONNECTORS:
        raise ValueError(f"kind is one of {', '.join(_CONNECTORS)}, not {kind!r}")
    begin, finish = _edges(start, end)
    line = slide.shapes.add_connector(
        _CONNECTORS[kind], Inches(begin[0]), Inches(begin[1]), Inches(finish[0]), Inches(finish[1])
    )
    # A name the theme carries, or #RRGGBB -- the same reading `tint` takes, so an
    # arrow in the pair a deck compares with is asked for the way its planes are.
    line.line.color.rgb = rgb(theme.get(colour, colour) if isinstance(colour, str) else colour or theme["muted"])
    line.line.width = Pt(width_pt)
    if arrow:
        # python-pptx has no arrowhead property; a:tailEnd is the last child of a:ln
        # before a:extLst, and the fill the line already carries is the first.
        element = line.line._get_or_add_ln()
        element.append(element.makeelement(f"{{{_A}}}tailEnd", {"type": "triangle", "w": "med", "len": "med"}))
    line.shadow.inherit = False
    for style in line._element.findall(f"{{{_P}}}style"):
        line._element.remove(style)
    return line


def _as_box(where):
    """A box, whether it arrived as a box or as the point somebody wanted to connect.

    `connect(slide, (x0 + w, cy), (ax0, cy - off), T)` is the obvious thing to write
    when the two ends are places rather than regions, and unaccepted it refuses with
    `AttributeError: 'tuple' object has no attribute 'x0'`, which names neither what
    was passed nor what was wanted. A point is a box with no size, and
    `_edges` already leaves from a box's edge, which for a point is the point.
    """
    if hasattr(where, "x0"):
        return where
    try:
        x, y = where
    except (TypeError, ValueError):
        raise TypeError(f"connect takes a Box or an (x, y) point, not {where!r}") from None
    return Box(float(x), float(y), float(x), float(y))


def _edges(start, end):
    """Which side of each box the connector leaves from and arrives at."""
    start, end = _as_box(start), _as_box(end)
    sx, sy = (start.x0 + start.x1) / 2, (start.y0 + start.y1) / 2
    ex, ey = (end.x0 + end.x1) / 2, (end.y0 + end.y1) / 2
    if abs(ex - sx) >= abs(ey - sy):
        rightwards = ex >= sx
        return (start.x1 if rightwards else start.x0, sy), (end.x0 if rightwards else end.x1, ey)
    downwards = ey >= sy
    return (sx, start.y1 if downwards else start.y0), (ex, end.y0 if downwards else end.y1)


def _paint(shape, theme, tint, outline, width_pt):
    if tint is None:
        shape.fill.background()
    else:
        shape.fill.solid()
        shape.fill.fore_color.rgb = rgb(theme[tint] if tint in theme else tint)
    if outline is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = rgb(theme[outline] if outline in theme else outline)
        shape.line.width = Pt(width_pt)
    shape.shadow.inherit = False
    # `shadow.inherit = False` writes an empty `a:effectLst` and that is not enough:
    # `add_shape` also stamps a `p:style` whose `a:effectRef idx="2"` names the theme's
    # drop shadow, which a renderer resolves on its own. Five interlocking chevrons
    # came out with a grey diagonal at every joint, which reads as a seam.
    for style in shape._element.findall(f"{{{_P}}}style"):
        shape._element.remove(style)
    return shape


def _adjust(shape, name, adj):
    """Write the preset's own adjustment names, which python-pptx's table gets wrong.

    Its `shape.adjustments` is positional over a hard-coded list that has no entry at
    all for `foldedCorner` and four for `upDownArrow`'s two. The names here come from
    the standard, so every preset takes the knobs it says it takes.

    Two units share the field. Most adjustments are hundred-thousandths of the shape's
    own width or height, so `0.5` is half of it. Fourteen of them, over `pie`, `arc`,
    `chord`, `blockArc` and the three circular arrows, are angles instead -- pass those
    in degrees and they are written in the sixtieth-thousandths the standard wants. A
    single scale sent `pie`'s three-quarter turn in as 1.25 degrees and drew a hairline
    without saying anything.

    Four more are angles by size and carry no mark saying so, so this refuses them
    rather than guessing: a named error beats a shape drawn to the wrong number.
    """
    entry = _DATA[name]
    names = entry["adj"]
    degrees = set(entry.get("deg") or ())
    opaque = set(entry.get("opaque") or ())
    values = (adj,) if isinstance(adj, (int, float)) else tuple(adj)
    if len(values) > len(names):
        raise ValueError(f"{name} takes {len(names)} adjustment(s) ({', '.join(names) or 'none'}), given {len(values)}")
    written = []
    for key, value in zip(names, values):
        if key in opaque:
            raise ValueError(
                f"{name}'s {key} is an angle this module cannot scale for you. Leave it at its default, "
                f"or reach for a shape whose knobs are proportions"
            )
        scale = 60000 if key in degrees else 100000
        written.append((key, int(round(float(value) * scale))))
    geometry = shape._element.spPr.prstGeom  # noqa: SLF001 -- the only way to a:avLst
    geometry.rewrite_guides(written)


def _key(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _words(text):
    """A name's words, however it was spelled: camelCase, snake_case or shouted."""
    return set(re.findall(r"[a-z]+|[0-9]+", re.sub(r"([a-z])([A-Z])", r"\\1 \\2", str(text)).lower()))


def _resolve(name):
    """Accept any spelling of a preset name, and name the near misses when there is none.

    `rightArrow`, `right_arrow` and `RIGHT ARROW` are one shape, and refusing two of
    them punishes an author for a separator it had no way to know about.

    A name Office has and this vocabulary dropped is refused with the reason it is out.
    Drawing it anyway puts the thing that was reviewed out on the page; answering "unknown
    preset shape" for a name the author read in PowerPoint sends them hunting a typo that
    is not there.
    """
    folded = {_key(candidate): candidate for candidate in _ALL_NAMES}
    found = folded.get(_key(name))
    if found is not None and found not in _OUT_OF_VOCABULARY:
        return found
    if found is not None:
        raise LookupError(f"{found} exists in Office but is not in this vocabulary: {_OUT_OF_VOCABULARY[found]}")
    near = _ranked(name)
    hint = "; closest: " + ", ".join(near) if near else f"; nothing close -- all {len(PRESET_NAMES)} are in PRESET_NAMES"
    raise LookupError(f"unknown preset shape {name!r}{hint}")
'''


def theme_catalog() -> dict[str, dict]:
    """The reviewed themes reduced to what a script draws with.

    The ten presets are a tested set: each accent clears contrast on its own
    ground, each pairs with a face the renderer does not substitute at a
    different width, and each has a six-colour series for data. A script that
    invents RGB values from scratch throws all of that away and usually lands on
    something worse, so the presets travel into the build directory as data.
    """
    catalog: dict[str, dict] = {}
    for theme_id, theme in THEMES.items():
        present = {field.name for field in dataclasses.fields(theme)}
        entry: dict[str, object] = {}
        for name in _EXPORTED_THEME_FIELDS:
            if name not in present:
                continue
            value = getattr(theme, name)
            entry[name] = list(value) if isinstance(value, tuple) else value
        # A property rather than a field, and the one the author actually needs for
        # a deck in Chinese: `font_family` alone leaves Han to the viewer's fallback.
        entry["cjk_font_family"] = theme.cjk_family
        catalog[theme_id] = entry
    return catalog


def theme_catalog_json() -> str:
    return json.dumps(theme_catalog(), indent=1)


def theme_module_source() -> str:
    """Source of the `ppt_theme` module a build script imports."""
    return _THEME_MODULE


def icon_catalog_json() -> str:
    return icons.catalog_json()


def icon_keyword_json() -> str:
    """The searchable words for every icon, as the build directory reads them."""
    return json.dumps(dict(icons.keyword_catalog()), separators=(",", ":"))


def icon_module_source() -> str:
    """Source of the `ppt_icons` module a build script imports."""
    return _ICON_MODULE


def shape_catalog_json() -> str:
    return shapes.catalog_json()


def shape_module_source() -> str:
    """Source of the `ppt_shapes` module a build script imports."""
    return _SHAPE_MODULE


REFERENCE_DIRNAME = "references"

_SKILL_REFERENCES = (
    Path(__file__).resolve().parents[3] / "memory_engine" / "skills" / "ppt-script-authoring" / REFERENCE_DIRNAME
)


def reference_files() -> dict[str, str]:
    """`references/<name>.md` -> text, the skill's detail documents.

    The skill body reaches the author as context and its reference documents do
    not: the pool reads SKILL.md and nothing beside it, and the workspace fence
    then refuses the path SKILL.md links to, which is where those documents
    actually live. So a fenced run had the 23 chart signatures, the table
    vocabulary and the 505 icon names worth knowing named at it and unreadable --
    one live run said so in as many words, then drew its tables with the bare
    default and placed no icon at all. Written beside the modules they document,
    under `references/` in the build directory, which is the tail of the
    `deck/build/references/<name>.md` path SKILL.md links by.

    Empty when the directory is absent, which is a checkout without the skill
    rather than an error worth a build.
    """
    if not _SKILL_REFERENCES.is_dir():
        return {}
    return {
        f"{REFERENCE_DIRNAME}/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted(_SKILL_REFERENCES.glob("*.md"))
    }


def script_helper_files() -> dict[str, str]:
    """Filename -> text, the complete set to write into a build directory."""
    from raven.ppt.services.assets.charts import CHART_MODULE_FILENAME, chart_module_source
    from raven.ppt.services.assets.layout import LAYOUT_MODULE_FILENAME, layout_module_source

    return {
        THEME_MODULE_FILENAME: theme_module_source(),
        THEME_DATA_FILENAME: theme_catalog_json(),
        ICON_MODULE_FILENAME: icon_module_source(),
        ICON_DATA_FILENAME: icon_catalog_json(),
        ICON_KEYWORD_FILENAME: icon_keyword_json(),
        LAYOUT_MODULE_FILENAME: layout_module_source(),
        CHART_MODULE_FILENAME: chart_module_source(),
        SHAPE_MODULE_FILENAME: shape_module_source(),
        SHAPE_DATA_FILENAME: shape_catalog_json(),
    }


def theme_summary() -> str:
    """One line per theme, for a caller that has to describe them in a prompt."""
    return "\n".join(f"- {theme_id}: {THEME_GUIDE[theme_id]}" for theme_id in sorted(theme_catalog()))
