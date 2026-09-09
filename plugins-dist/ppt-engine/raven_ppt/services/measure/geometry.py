"""Declared geometry: what the .pptx file itself says is where.

Two ground truths measure a built page and this is the one that comes off the
file. It is exact for everything the renderer will not move -- a picture, a
filled panel, a hairline rule, a table -- and wrong for text, which the
renderer reflows inside its frame: a word's declared position is where its box
was put, not where the word landed. So text positions come from
`measure.words`, read off the render, and what lives here is the shape walking
and the unit arithmetic every measurement needs.

In one copy, which it was not. The predecessor carried `_iter_shapes` and
`_walk_shapes` -- the same six lines -- in two modules, `_is_panel` and
`_is_filled` likewise, and inlined the same "has this shape any text" test at
five call sites. Nothing had gone wrong with that yet; it is the shape of thing
that goes wrong quietly, when one copy is taught about grouped shapes.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EMU_PER_INCH = 914400
EMU_PER_POINT = 12700

# MSO_SHAPE_TYPE.PICTURE, spelled out rather than imported: the enum costs a
# python-pptx import in modules that otherwise only compare an integer.
PICTURE = 13
# MSO_FILL.BACKGROUND -- "filled with the slide background", i.e. not filled.
_FILL_BACKGROUND = 5


@dataclass(frozen=True)
class Rect:
    """An axis-aligned box, in whatever unit the caller is working in.

    Both unit systems appear below: points for anything compared against
    rendered words (the PDF and the .pptx agree on the point), EMU for anything
    compared against the canvas, where the .pptx's own integers are exact.
    """

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(self.width, 0.0) * max(self.height, 0.0)

    def overlap(self, other: Rect) -> float:
        """Shared area, zero when they only touch or miss."""
        return max(min(self.x1, other.x1) - max(self.x0, other.x0), 0.0) * max(
            min(self.y1, other.y1) - max(self.y0, other.y0), 0.0
        )


# The last few decks opened, by path, mtime and size. A measurement pass opens the
# same file from forty-odd call sites, and parsing a 25MB deck costs 0.13s each time:
# 10.5s of a 45s pass on one measured build. Nothing under `measure` writes to what
# it opened, so one parse per file version serves them all; a rebuilt deck has a new
# mtime and misses.
_OPENED: dict[tuple[str, int, int], Any] = {}
_OPENED_MAX = 4


def open_deck(pptx_path: Path) -> Any:
    """The built deck, as python-pptx sees it. Read-only: callers must not save it."""
    from pptx import Presentation

    path = Path(pptx_path)
    try:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return Presentation(str(path))
    deck = _OPENED.get(key)
    if deck is None:
        deck = Presentation(str(path))
        while len(_OPENED) >= _OPENED_MAX:
            _OPENED.pop(next(iter(_OPENED)))
        _OPENED[key] = deck
    return deck


def iter_shapes(shapes: Any) -> Iterator[Any]:
    """Every shape on a page, groups walked into.

    A grouped shape is where decoration hides: a band welded to a card reads as
    one shape from the top level and two from inside the group.
    """
    for shape in shapes:
        yield shape
        nested = getattr(shape, "shapes", None)
        if nested is not None:
            yield from iter_shapes(nested)


_DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_PRESENTATION_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def page_box(shape: Any) -> Rect | None:
    """Where this shape actually sits on the page, in inches.

    `shape.left` is not that for anything inside a group. A group defines its own
    coordinate space -- `a:chOff`/`a:chExt` -- and its children are positioned in it,
    so python-pptx hands back the child's number in the group's space and nothing
    converts it. Measured on a real template's agenda page: six section numbers, laid
    out two columns by three rows, all reported at the same (7.67, 4.27) and 4.46in
    wide, which is neither where nor what any of them is. Every check that reads a
    shape's rectangle was wrong about every grouped shape, and `covered_shape` said
    five of the six numbers were completely hidden under the others.

    The transform is the standard one, applied outward through each group above it:
    the child offset is subtracted, the ratio of the group's extent to its child
    extent scales, and the group's own offset is added. Returns None when the shape
    has no geometry at all (a placeholder inheriting its position from the layout).
    """
    if shape.left is None or shape.top is None or shape.width is None or shape.height is None:
        return None
    x, y = float(shape.left), float(shape.top)
    width, height = float(shape.width), float(shape.height)
    element = shape._element.getparent()  # noqa: SLF001 -- the group is not on the shape API
    while element is not None and element.tag == f"{_PRESENTATION_NS}grpSp":
        frame = element.find(f"{_PRESENTATION_NS}grpSpPr/{_DRAWING_NS}xfrm")
        if frame is None:
            break
        offset = frame.find(f"{_DRAWING_NS}off")
        extent = frame.find(f"{_DRAWING_NS}ext")
        child_offset = frame.find(f"{_DRAWING_NS}chOff")
        child_extent = frame.find(f"{_DRAWING_NS}chExt")
        if offset is None or extent is None or child_offset is None or child_extent is None:
            break
        span_x = float(child_extent.get("cx") or 0) or 1.0
        span_y = float(child_extent.get("cy") or 0) or 1.0
        scale_x = float(extent.get("cx") or 0) / span_x
        scale_y = float(extent.get("cy") or 0) / span_y
        x = float(offset.get("x") or 0) + (x - float(child_offset.get("x") or 0)) * scale_x
        y = float(offset.get("y") or 0) + (y - float(child_offset.get("y") or 0)) * scale_y
        width *= scale_x
        height *= scale_y
        element = element.getparent()
    return Rect(x / EMU_PER_INCH, y / EMU_PER_INCH, (x + width) / EMU_PER_INCH, (y + height) / EMU_PER_INCH)


def ink_box(shape: Any) -> Rect | None:
    """`page_box` with the shape's own rotation applied, which is where the ink lands.

    A rotated shape is painted about its centre, so its declared rectangle is not the
    one the render fills. Measured on a bundled template: a heading inside three nested
    groups carries `rot="5400000"` and declares 0.57 x 2.34in, and the renderer sets a
    2.34 x 0.57in line. Reading the ground at the declared box crops a strip straight
    across the words instead of along them -- the gold pill under the type came to 17%
    of that strip and the white page under it 18%, so the mode was white, and the
    contrast reading called seven of that template's own pages unreadable at 1.0:1 with
    nothing wrong on any of them.

    The axis-aligned bounding box of the rotated rectangle, so it is never narrower than
    the ink: a 2-degree lift on a picture reaches past its own edges too, which is the
    other half of what this is for.
    """
    where = page_box(shape)
    if where is None:
        return None
    angle = math.radians(float(getattr(shape, "rotation", 0.0) or 0.0))
    if not angle:
        return where
    across = abs(where.width * math.cos(angle)) + abs(where.height * math.sin(angle))
    down = abs(where.width * math.sin(angle)) + abs(where.height * math.cos(angle))
    middle_x, middle_y = (where.x0 + where.x1) / 2, (where.y0 + where.y1) / 2
    return Rect(middle_x - across / 2, middle_y - down / 2, middle_x + across / 2, middle_y + down / 2)


def has_text(shape: Any) -> bool:
    """Whether a shape carries copy, rather than merely being able to."""
    return bool(getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip())


def iter_text_frames(slide: Any) -> Iterator[Any]:
    """Text frames on a page, table cells included.

    Tables carry much of a results deck's copy and are set smaller than its
    prose, so a census that skips them misses where the type is smallest.
    """
    for shape in iter_shapes(slide.shapes):
        if getattr(shape, "has_text_frame", False):
            yield shape.text_frame
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                for cell in row.cells:
                    yield cell.text_frame


def cell_boxes(slide: Any) -> Iterator[tuple[Any, Rect]]:
    """Every table cell holding copy, with where it sits on the page in points.

    A table cell is not a shape: python-pptx puts the whole table in one
    GraphicFrame, so every check that walks `iter_shapes` and asks for a
    rectangle is blind to the copy inside a table -- which on a results deck is
    most of the numbers.

    The boundaries come out as fractions of the frame's own rectangle rather
    than as a running sum of the declared widths, so a table inside a scaled
    group lands where the group puts it, the way `page_box` handles a shape.
    The two agree to the EMU on a table `ppt_layout.table` drew, because it
    sizes the frame from the columns and rows it then declares.

    Only what the file can be trusted on: a spanned cell yields once, from its
    origin, over the span it really covers.
    """
    for shape in iter_shapes(slide.shapes):
        if not getattr(shape, "has_table", False):
            continue
        table = shape.table
        frame = shape_rect_pt(shape)
        widths = [int(column.width or 0) for column in table.columns]
        heights = [int(row.height or 0) for row in table.rows]
        if frame.area <= 0 or sum(widths) <= 0 or sum(heights) <= 0:
            continue
        xs = _fractions(frame.x0, frame.width, widths)
        ys = _fractions(frame.y0, frame.height, heights)
        for down, row in enumerate(table.rows):
            for across in range(len(widths)):
                cell = table.cell(down, across)
                if getattr(cell, "is_spanned", False) or not cell.text_frame.text.strip():
                    continue
                last_across = min(across + int(getattr(cell, "span_width", 1) or 1), len(widths))
                last_down = min(down + int(getattr(cell, "span_height", 1) or 1), len(heights))
                yield cell, Rect(xs[across], ys[down], xs[last_across], ys[last_down])


def _fractions(start: float, span: float, sizes: list[int]) -> list[float]:
    """Cumulative boundaries across `span`, one per size plus the closing edge."""
    total = float(sum(sizes))
    edges, run = [start], 0
    for size in sizes:
        run += size
        edges.append(start + span * run / total)
    return edges


def page_paragraphs(slide: Any) -> list[str]:
    """Every piece of copy on a page, one string per paragraph or table cell.

    Split this finely on purpose: the citation gate reads a window around each
    reference, and a whole page joined into one string puts a citation's
    neighbours several columns away from it.
    """
    found: list[str] = []
    for shape in iter_shapes(slide.shapes):
        if getattr(shape, "has_text_frame", False):
            found.extend(paragraph.text for paragraph in shape.text_frame.paragraphs)
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                found.extend(cell.text for cell in row.cells)
    return [text.strip() for text in found if text.strip()]


def slide_count(pptx_path: Path) -> int:
    """How many pages the finished file has.

    Off the file rather than off the build's own report, because that is the
    number an audience will page through -- and because a check has to work on a
    deck that arrived without a build record.
    """
    from pptx import Presentation

    return len(Presentation(str(pptx_path)).slides)


def deck_text(pptx_path: Path) -> list[tuple[int, str]]:
    """Every piece of copy in a built deck, with the page it sits on.

    Read off the finished file rather than off the submission, because a build
    program composes text the submission never named: a value formatted into a
    table cell, a label built from two fields. What lands on the page is what
    has to be true.
    """
    return [(number, text) for number, slide in pages(pptx_path) for text in page_paragraphs(slide)]


def pages(pptx_path: Path) -> list[tuple[int, Any]]:
    """(page number, slide) for a built deck, numbered from 1."""
    return list(enumerate(open_deck(pptx_path).slides, start=1))


_DRAWINGML = "http://schemas.openxmlformats.org/drawingml/2006/main"
_RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PRESENTATIONML = "http://schemas.openxmlformats.org/presentationml/2006/main"


def picture_blob(shape: Any) -> bytes | None:
    """The image this shape shows, whether it *is* a picture or is filled with one.

    A template's photograph is as often a rounded rectangle with a `blipFill` as it is a
    picture frame -- that is how a designer gets a soft corner on a photo. python-pptx
    calls the first a PICTURE and the second a FREEFORM, `shape.image` raises on the
    second, and every check that asked that question missed it: a delivered deck kept the
    template's own stock photograph of a meeting table on its contents page, 27% of the
    canvas, named `PictureMisc1`, and nothing reported it.
    """
    try:
        return shape.image.blob
    except Exception:  # noqa: BLE001 -- not a picture frame; it may still be filled with one
        pass
    element = getattr(shape, "_element", None)
    if element is None:
        return None
    # This shape's own fill, not a descendant's: `.//` finds the picture inside a group
    # and reports the group as showing it too, which double-counts every grouped photo.
    blip = element.find(f"{{{_PRESENTATIONML}}}spPr/{{{_DRAWINGML}}}blipFill/{{{_DRAWINGML}}}blip")
    if blip is None:
        return None
    embed = blip.get(f"{{{_RELATIONSHIPS}}}embed")
    if not embed:
        return None
    try:
        return shape.part.related_part(embed).blob
    except Exception:  # noqa: BLE001 -- a relationship that does not resolve is no image
        return None


def shows_picture(shape: Any) -> bool:
    """Whether a reader sees an image here, however the file spells it."""
    return picture_blob(shape) is not None


# A picture at most this long on a side is a mark beside one unit of the page rather than
# a picture of something. Measured over the 62 picture slots of the eight bundled
# templates: the marks that stand one per unit -- the three seals on red page 4 (1.7in),
# the badges on beige 21 (1.4-1.5in), mint 13 (1.3in) and red 13 (1.4in) -- all sit at
# or under 1.7in, and the smallest picture that shows something of its own, a card
# cartoon on teal 7, starts at 2.0in (two of that page's four cartoons are smaller and
# take the label too, which costs nothing: they are already one per card). The generated
# `ppt_icons.swap_icon` refuses anything larger under the same number, so a slot named an
# icon is one that call accepts.
ICON_MAX_IN = 1.8


def is_icon_sized(shape: Any) -> bool:
    """Whether this picture is a mark rather than a picture: no longer than ICON_MAX_IN a side."""
    box = page_box(shape)
    if box is None:
        return False
    return max(box.width, box.height) <= ICON_MAX_IN


def picture_opacity(shape: Any) -> float:
    """How much of what is under a picture it keeps out, 0.0 to 1.0.

    PowerPoint's Picture Transparency is an `a:alphaModFix` on the blip, and a picture
    at 40 percent does not hide the rule beneath it. The fill answer next to this one
    reads `a:alpha` and finds none on a blip, so a translucent photograph came back
    fully opaque -- which is the reading `is_panel` refused to make for a fill and this
    made for an image.
    """
    blip = _blip(shape)
    if blip is None:
        return 1.0
    alphas = [_alpha_value(node) for node in blip.iter() if node.tag.endswith("}alphaModFix")]
    return min(alphas) if alphas else 1.0


def _blip(shape: Any) -> Any:
    """The `a:blip` this shape shows an image through, or None."""
    try:
        return shape._element.find(f".//{_DRAWING_NS}blip")
    except (AttributeError, TypeError):
        return None


# Where a fill stops being a cover. Rendered at 80dpi with 20pt text under a
# mid-blue band: at 100% the words are gone, at 90% they are a ghost, at 80% they
# are legible but poorly, and from 70% down they read plainly. Above this line a
# fill hides what is under it; below it, the layer is the point.
COVERING_OPACITY = 0.8


def _fill_alphas(shape: Any) -> list[float]:
    """Every `a:alpha` this shape's fill states, as fractions.

    Empty means the fill states none, which is opaque. python-pptx has no alpha of
    its own, so this reads the drawing. `fore_color` raises on a gradient, hence
    the second look at `a:gradFill`.
    """
    try:
        fill = shape.fill.fore_color._xFill
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        fill = None
    if fill is None:
        fill = _gradient_fill(shape)
    if fill is None:
        return []
    found = [_alpha_value(alpha) for alpha in fill.iter() if alpha.tag.endswith("}alpha")]
    return [one for one in found if one is not None]


def fill_opacity(shape: Any) -> float:
    """How much of what is under this shape its fill keeps out, 0.0 to 1.0.

    A gradient is read through its stops, and the reason is a false refusal: the
    scrim that makes type legible over a photograph is a `gradFill` from opaque to
    transparent, and `fore_color` raises on one -- so the old reading returned 1.0
    and the occlusion check called four such pages a picture 100% hidden under its
    own scrim. What a gradient keeps out is not one number; the least it keeps out
    anywhere is, because a reader looking at the picture is looking through the
    stop that hides least.
    """
    alphas = _fill_alphas(shape)
    return min(alphas) if alphas else 1.0


def fill_showing(shape: Any) -> float:
    """How much of this shape's fill a reader can see anywhere on it, 0.0 to 1.0.

    The other side of `fill_opacity`, and a different question: whether the shape is
    painted at all, rather than whether it hides what is beneath. A gradient answers
    it at the stop that shows most, because a card tinted 25% at its top and 0% at
    its bottom is a card a reader sees.
    """
    alphas = _fill_alphas(shape)
    return max(alphas) if alphas else 1.0


def _gradient_fill(shape: Any) -> Any | None:
    """This shape's `a:gradFill` element, or None when it has no gradient."""
    try:
        properties = shape.fill._xPr
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        return None
    if properties is None:
        return None
    return properties.find(f"{_DRAWING_NS}gradFill")


def _alpha_value(alpha: Any) -> float | None:
    try:
        return max(0.0, min(1.0, int(alpha.get("val", "100000")) / 100000))
    except (TypeError, ValueError):
        return None


# The theme slots that name a page's own ground rather than a colour on it.
_GROUND_SCHEMES = frozenset({"bg1", "bg2", "lt1", "lt2"})


def _ground_only_fill(shape: Any) -> bool:
    """Whether every colour in this shape's fill is one of the page's ground slots.

    A shape filled with nothing but `bg1` is painted the colour of the page it sits
    on, so at less than full opacity it puts nothing on the page. Five cards on one
    bundled template's page 6 and five on its page 14 are drawn that way -- `bg1` at
    50% to 70% -- and every pixel inside them renders 255,255,255 against a
    252,254,254 ground. Read as containers they were reported as cards using a third
    of their height, about cards a reader cannot see.

    A `p:style` fill states no colour here and is not one of these: it resolves to a
    theme fill, which is an accent rather than the ground.

    The reverse case is a dark page, where a half-transparent white is the scrim the
    design is made of. No bundled template has one -- all eight are light -- so this
    reads the shape alone; a dark template arriving here would want the page's own
    ground read first.
    """
    properties = None
    try:
        properties = shape.fill._xPr
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        return False
    if properties is None:
        return False
    named: set[str] = set()
    for tag in ("solidFill", "gradFill"):
        block = properties.find(f"{_DRAWING_NS}{tag}")
        if block is None:
            continue
        if block.find(f"{_DRAWING_NS}srgbClr") is not None:
            return False
        for colour in block.iter(f"{_DRAWING_NS}schemeClr"):
            named.add(colour.get("val") or "")
    return bool(named) and named <= _GROUND_SCHEMES


def is_filled(shape: Any) -> bool:
    """A shape with paint on it -- a card, a band, a tint -- rather than a bare frame.

    python-pptx raises several different ways when a shape has no fill to speak
    of (a picture, a connector, a graphic frame), and every one of them means
    the same thing here.

    How see-through the paint is does not enter into it, which is what separates this
    from `is_panel`. The way a current template draws a card is a fill at 5% to 25%
    alpha, sometimes a `gradFill` running to nothing at one edge: across the twelve
    bundled templates 159 shapes are painted that way and 131 of them are card-sized,
    including every card on four of the templates' content pages. Rendered, they are
    plainly cards -- so the readings that ask "is this a card" (`cards`, the container
    and row readings, `page_signature`, the evidence census) ask this, and only the
    occlusion readings ask `is_panel`.
    """
    try:
        kind = shape.fill.type
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        return False
    # `BACKGROUND` is an author saying "no fill" and is answered here: every shape
    # python-pptx draws also carries a `p:style`, so falling through to that would make
    # a deliberately bare frame a panel.
    if kind == _FILL_BACKGROUND:
        return False
    filled = kind is not None
    if not filled:
        # Nothing in the shape's own `spPr` does not mean nothing is painted:
        # PowerPoint's default shape leaves the fill to a `p:style` whose `a:fillRef`
        # names one of the theme's fill styles, and the renderer resolves it. Two of the
        # four cards on a bundled template's page are drawn that way, and reading only
        # `spPr` called them bare frames -- so every check built on this saw two cards
        # where a reader sees four, and a page of cards was reported as copy with no
        # edges around it.
        filled = _styled_fill(shape)
    if not filled:
        return False
    showing = fill_showing(shape)
    # A fill transparent at every stop is a hit region or a leftover, not a shape a
    # reader meets: sixteen of them sit across the bundled templates.
    if showing <= 0.0:
        return False
    return showing >= 1.0 or not _ground_only_fill(shape)


def is_connector(shape: Any) -> bool:
    """Whether this shape is a line between two points rather than a form.

    A template's column divider is a `p:cxnSp`. It has no fill to speak of and no
    text, so every census built on fills or copy missed it: one bundled template
    separates five columns of copy with five of these and nothing else, and a census
    of what divides its pages read that page as undivided.
    """
    element = getattr(shape, "_element", None)
    return element is not None and str(element.tag).endswith("}cxnSp")


def has_outline(shape: Any) -> bool:
    """Whether this shape is drawn with a visible stroke.

    The other way a template makes an edge without a fill: the numbered rings down one
    bundled page are outline-only circles. An unstated line is not counted -- it may
    resolve to a theme stroke, and reading it as drawn would find an edge on every
    text box.
    """
    try:
        kind = shape.line.fill.type
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        return False
    return kind is not None and kind != _FILL_BACKGROUND


def is_panel(shape: Any) -> bool:
    """A filled shape whose paint hides what is under it.

    A fill you can see through is not one of these. The occlusion check asks what
    is hidden, and a translucent band over a chart is a layer the author drew on
    purpose -- refusing it would refuse the technique the charts reference now
    teaches. Asking "is this a card" wants `is_filled` instead.
    """
    return is_filled(shape) and fill_opacity(shape) >= COVERING_OPACITY


# The rectangle family: a card, a band, a strip, corner treatments aside.
_RECTANGLES = frozenset(
    {
        "rect",
        "roundRect",
        "round1Rect",
        "round2DiagRect",
        "round2SameRect",
        "snip1Rect",
        "snip2DiagRect",
        "snip2SameRect",
        "snipRoundRect",
    }
)


def is_rectangular(shape: Any) -> bool:
    """Whether the shape's own outline is a rectangle, so "how full is it" applies.

    Anything else a template draws -- an ellipse, a donut, a triangle, a callout with a
    tail, a freeform mask -- covers a fraction of its own bounding box, so a check that
    reads copy against that box reports it as a container nobody filled however it is
    filled. Measured across the twelve bundled templates and two built decks: 117 of
    the 328 shapes the two fill checks treated as panels are not rectangles (85
    freeform masks, 13 ellipses, 4 parallelograms, 4 teardrops, 4 tailed callouts, 3
    donuts, a block arc, a triangle, a diamond, a home plate). A 4.05in hub disc with
    one line of label on it came back as "14% of its height", which is a description of
    a hub and not a defect.

    A shape that states no geometry at all is a rectangle -- a picture frame, a
    placeholder. A `custGeom` is not: its path is arbitrary.
    """
    element = getattr(shape, "_element", None)
    if element is None:
        return True
    properties = element.find(f"{_PRESENTATION_NS}spPr")
    if properties is None:
        return element.find(f".//{_DRAWING_NS}custGeom") is None
    preset = properties.find(f"{_DRAWING_NS}prstGeom")
    if preset is not None:
        return preset.get("prst") in _RECTANGLES
    return properties.find(f"{_DRAWING_NS}custGeom") is None


def _styled_fill(shape: Any) -> bool:
    """Whether this shape takes a fill from the theme through its own `p:style`.

    `a:fillRef idx="0"` is the one that means no fill; every other index points into
    the theme's `fillStyleLst`.
    """
    element = getattr(shape, "_element", None)
    if element is None:
        return False
    style = element.find(f"{_PRESENTATION_NS}style")
    if style is None:
        return False
    reference = style.find(f"{_DRAWING_NS}fillRef")
    if reference is None:
        return False
    try:
        return int(reference.get("idx", "0")) > 0
    except (TypeError, ValueError):
        return False


def shape_rect_emu(shape: Any) -> Rect:
    """A shape's box on the page, in EMU. Absent geometry reads as zero.

    On the page rather than as declared: for anything inside a group the declared
    numbers are in the group's own coordinate space -- see `page_box`.
    """
    box = page_box(shape)
    if box is None:
        return Rect(0, 0, 0, 0)
    return Rect(
        int(box.x0 * EMU_PER_INCH),
        int(box.y0 * EMU_PER_INCH),
        int(box.x1 * EMU_PER_INCH),
        int(box.y1 * EMU_PER_INCH),
    )


def shape_rect_pt(shape: Any) -> Rect:
    """A shape's declared box in points, the unit rendered words come in."""
    box = shape_rect_emu(shape)
    return Rect(box.x0 / EMU_PER_POINT, box.y0 / EMU_PER_POINT, box.x1 / EMU_PER_POINT, box.y1 / EMU_PER_POINT)


def text_boxes_emu(slide: Any) -> list[Rect]:
    """Where a page's copy is declared to sit, in EMU."""
    return [shape_rect_emu(shape) for shape in iter_shapes(slide.shapes) if has_text(shape)]
