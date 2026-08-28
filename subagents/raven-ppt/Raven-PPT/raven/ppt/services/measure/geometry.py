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


def open_deck(pptx_path: Path) -> Any:
    """The built deck, as python-pptx sees it."""
    from pptx import Presentation

    return Presentation(str(pptx_path))


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


# Where a fill stops being a cover. Rendered at 80dpi with 20pt text under a
# mid-blue band: at 100% the words are gone, at 90% they are a ghost, at 80% they
# are legible but poorly, and from 70% down they read plainly. Above this line a
# fill hides what is under it; below it, the layer is the point.
COVERING_OPACITY = 0.8


def fill_opacity(shape: Any) -> float:
    """How much of what is under this shape its fill keeps out, 0.0 to 1.0.

    python-pptx has no alpha of its own, so this reads the `a:alpha` the drawing
    writes; a solid fill states none and is opaque.

    A gradient is read through its stops, and the reason is a false refusal: the
    scrim that makes type legible over a photograph is a `gradFill` from opaque to
    transparent, and `fore_color` raises on one -- so the old reading returned 1.0
    and the occlusion check called four such pages a picture 100% hidden under its
    own scrim. What a gradient keeps out is not one number; the least it keeps out
    anywhere is, because a reader looking at the picture is looking through the
    stop that hides least.
    """
    try:
        fill = shape.fill.fore_color._xFill
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        fill = None
    if fill is None:
        fill = _gradient_fill(shape)
    if fill is None:
        return 1.0
    alphas = [_alpha_value(alpha) for alpha in fill.iter() if alpha.tag.endswith("}alpha")]
    alphas = [one for one in alphas if one is not None]
    return min(alphas) if alphas else 1.0


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


def is_panel(shape: Any) -> bool:
    """A filled shape -- a card, a band, a rule -- rather than a bare frame.

    python-pptx raises several different ways when a shape has no fill to speak
    of (a picture, a connector, a graphic frame), and every one of them means
    the same thing here.

    A fill you can see through is not one of these. The occlusion check asks what
    is hidden, and a translucent band over a chart is a layer the author drew on
    purpose -- refusing it would refuse the technique the charts reference now
    teaches.
    """
    try:
        filled = shape.fill.type is not None and shape.fill.type != _FILL_BACKGROUND
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        return False
    return filled and fill_opacity(shape) >= COVERING_OPACITY


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
