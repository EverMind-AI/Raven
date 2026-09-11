"""A template page, written back out as the python-pptx that would draw it.

This route's author writes a program. So the honest way to hand it a template is
not a set of slots to fill -- that bounds the deck at what the template's own pages
happened to hold, and a template drawn with six cards cannot then be used for eight
points. It is to show the author the code: here is exactly how this design places a
title, what its accent colour is, how wide its cards are and how far apart, in the
form you write in. Then the deck's pages are written, not filled, and adjusting a
six-card grid to eight is a loop bound rather than a mismatch.

What comes out is deliberately flat and literal. Groups are opened, inherited
properties are resolved to the values they resolve to, and every number is written
in inches rather than EMU -- because it is read by someone deciding what to keep,
not executed as-is. It is a reference, and a reference that hides its numbers
behind indirection is worth less than the render beside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

EMU_PER_INCH = 914400
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

# Which of the master's three text styles a placeholder takes its type from.
_FAMILY = {
    "TITLE": "title",
    "CENTER_TITLE": "title",
    "SUBTITLE": "body",
    "BODY": "body",
    "OBJECT": "body",
}

# What a template holds that python-pptx has no way to write. Measured over 65 of
# the user's templates: 19% of their shapes are drawn as custom geometry and 6%
# carry a gradient, so a reference that stayed silent about them would describe a
# page the author cannot actually reproduce -- and the author would find that out
# only by looking at what it built.
_UNWRITABLE = (
    ("custGeom", "a custom-drawn shape"),
    ("gradFill", "a gradient fill"),
    ("pattFill", "a pattern fill"),
    ("alpha", "a semi-transparent fill"),
)

_CLONE_INSTEAD = (
    "this page also holds {what}, which python-pptx cannot write. Code here will not "
    "reproduce it: to keep it, clone the page with `clone_page(prs, prototype(tpl, N))` "
    "and `replace_text` per line rather than redrawing it. To draw anything of "
    "your own into the clone -- a chart, a panel, a figure -- clear the space for it first with "
    "ppt_template.clear_region(slide, box) -- a ppt_layout.Box or a page_box(shape), not "
    "four bare numbers, which cannot say whether they are two corners or a corner and a "
    "size. Whatever the template drew there is still "
    "there, and it is never one shape. The call says what it removed and what it left "
    "over your box."
)

# A chart is the one thing on a template page that a clone does not keep. Nothing in
# this engine writes chart data -- `replace_text` reaches text frames and a chart's
# labels are in its own part -- so a cloned chart arrives holding the template's own
# numbers, unfillable by any route, and a live run that was told to clone one went to
# the shell to measure its axis type by hand. It is also the one thing here that the
# author can draw better than the template did, `ppt_charts` being twenty-three forms
# wide. So it is reported apart from what cloning keeps, and with the box it leaves
# behind, because the arrangement around it is worth cloning and the new chart has to
# land in the space the old one had.
_REDRAW_CHART = (
    "the layout may be cloned, but the chart must be redrawn with ppt_charts from your own data and "
    "placed in the same position, and the box it occupies is on its # [n] line below. Cloning it keeps "
    "the template's own numbers, which nothing here can rewrite: a chart's labels live in its own part "
    "rather than in a text frame, so replace_text does not reach them."
)
# Said as an exception to the clone rather than beside it, when a page has both. Two
# separate notes read as two separate instructions, and the first of them ends on
# "replace its text and pictures", which is exactly what a chart does not answer to.
_BUT_THE_CHART = " A chart on it is the exception: "

# Every name the emitted code may use, and where it comes from. Only the ones a
# page actually needs are stated, because an import list longer than the page is
# read as boilerplate and skipped -- along with the one line that mattered.
_IMPORTS = (
    ("Inches", "from pptx.util import Inches"),
    ("Pt", "from pptx.util import Pt"),
    ("RGBColor", "from pptx.dml.color import RGBColor"),
    ("MSO_SHAPE", "from pptx.enum.shapes import MSO_SHAPE"),
    ("MSO_CONNECTOR", "from pptx.enum.shapes import MSO_CONNECTOR"),
    ("MSO_ANCHOR", "from pptx.enum.text import MSO_ANCHOR"),
    ("PP_ALIGN", "from pptx.enum.text import PP_ALIGN"),
)

_ANCHORS = {"ctr": "MIDDLE", "b": "BOTTOM"}
_ALIGNMENTS = {"ctr": "CENTER", "r": "RIGHT", "just": "JUSTIFY"}


@dataclass(frozen=True)
class PageSource:
    """One example page as readable code, with the render it produces."""

    index: int
    layout: str
    source: str
    picture_files: tuple[str, ...] = ()
    unredrawable: tuple[str, ...] = ()
    # The box of each chart on the page, in the form every other box here is written
    # in. Not part of `unredrawable`: cloning keeps those and does not keep a chart.
    redraw_yourself: tuple[str, ...] = ()

    def summary(self, *, imports: bool = True) -> str:
        """The page as a block an author reads: its header, then its code.

        `imports=False` leaves the import lines to a caller that states them once
        for several pages, instead of once per page.
        """
        head = [f"# page {self.index} of the template, layout {self.layout!r}"]
        if imports:
            head += [f"# {line}" for line in _needed_imports(self.source)]
        if self.unredrawable:
            note = _CLONE_INSTEAD.format(what=", ".join(self.unredrawable))
            head.append("# " + note + (_BUT_THE_CHART + _REDRAW_CHART if self.redraw_yourself else ""))
        elif self.redraw_yourself:
            head.append("# " + _REDRAW_CHART)
        return "\n".join((*head, self.source))


# The theme colour slots, in the order the enum names them, so a shape filled from
# the theme can be written as the colour it actually is.
_THEME_SLOTS = {
    "DARK_1": "dk1",
    "LIGHT_1": "lt1",
    "DARK_2": "dk2",
    "LIGHT_2": "lt2",
    "TEXT_1": "dk1",
    "BACKGROUND_1": "lt1",
    "TEXT_2": "dk2",
    "BACKGROUND_2": "lt2",
    "ACCENT_1": "accent1",
    "ACCENT_2": "accent2",
    "ACCENT_3": "accent3",
    "ACCENT_4": "accent4",
    "ACCENT_5": "accent5",
    "ACCENT_6": "accent6",
    "HYPERLINK": "hlink",
    "FOLLOWED_HYPERLINK": "folHlink",
}


def decompile(path: Path, index: int, *, images_dir: Path | None = None) -> PageSource | None:
    """Page `index` of the template, as python-pptx source.

    `images_dir` is where the page's pictures are written so the code can refer to
    them; without it a picture becomes a commented placeholder, which is the right
    answer when the caller only wants to read the composition.
    """
    try:
        from pptx import Presentation
    except ImportError:  # pragma: no cover - python-pptx ships with the extra
        return None
    try:
        presentation = Presentation(str(path))
        slide = presentation.slides[index]
    except Exception:  # noqa: BLE001 -- a missing page is not an error here
        return None

    # Theme colours are resolved to the values they resolve to. Writing the enum
    # name instead produced a reference whose cards came back light grey where the
    # template's are near-black: the name is a slot, and which slot a name means
    # depends on a mapping the reader cannot see. A literal is also simply more
    # useful to read.
    design = _Design(
        palette=_palette(presentation),
        mapping=_colour_map(presentation),
        placeholders=_layout_placeholders(slide),
        master=_master_styles(slide),
    )
    lines: list[str] = [_opening(presentation, slide)]
    pictures: list[str] = []
    lost: list[str] = []
    charts: list[str] = []
    # The ordinal is printed beside every shape because it is the key `shape_at` takes.
    # An author read this reference, counted the shapes it shows, and reached for a
    # fifteenth shape the engine did not agree on -- it was numbering text frames and
    # the reference was numbering shapes. One numbering, stated where it is read.
    # Consecutive custom-drawn shapes are said once. A Bauhaus page carries a
    # hundred of them as pattern, and two lines each put one page at 12,800
    # characters -- most of a reply -- for shapes no code here can draw anyway.
    run: list[tuple[int, str]] = []

    def flush() -> None:
        if run:
            lines.append(_folded(run))
            run.clear()

    for ordinal, shape in enumerate(_flatten(slide.shapes), start=1):
        emitted = _shape_source(shape, images_dir, len(pictures), design)
        if emitted.decoration:
            run.append((ordinal, _box(shape)))
        else:
            flush()
            if emitted.source:
                lines.append(f"# [{ordinal}]")
                lines.append(emitted.source)
            else:
                lines.append(f"# [{ordinal}] kept by cloning -- python-pptx cannot draw it")
        if emitted.picture:
            pictures.append(emitted.picture)
        lost.extend(emitted.lost)
        charts.extend(emitted.redraw)
    flush()
    return PageSource(
        index=index,
        layout=_layout_name(slide),
        source="\n".join(lines) or "# this page draws nothing of its own",
        picture_files=tuple(pictures),
        # Ordered by first appearance rather than sorted: the note reads as a walk
        # over the page, which is the order the author will look for them in.
        unredrawable=tuple(dict.fromkeys(lost)),
        redraw_yourself=tuple(charts),
    )


@dataclass(frozen=True)
class _Design:
    """What this page's own shapes leave to the design underneath them.

    A template's cover is the case that made this necessary. Its illustration and
    its type both live on the layout, so the page's own shapes are four text boxes
    stating nothing at all, and a reference that read only the slide came back with
    a bare page and no type on it -- while the render beside it was a full-bleed
    painting. The palette resolves theme colours, and the placeholders are where a
    line whose size and colour are "whatever the layout says" gets its answer.
    """

    palette: dict[str, str]
    mapping: dict[str, str]
    placeholders: dict[int, Any] = field(default_factory=dict)
    master: dict[str, tuple[Any, ...]] = field(default_factory=dict)

    def inherited(self, shape):
        """The layout placeholder this shape takes its unstated properties from."""
        try:
            if not shape.is_placeholder:
                return None
            return self.placeholders.get(shape.placeholder_format.idx)
        except (AttributeError, ValueError):
            return None

    def stated(self, shape) -> tuple[Any, ...]:
        """Every node this shape's unstated type could come from, nearest first.

        Measured across forty of the user's templates: not one text shape on any
        page states its own size. 62% of them get it from the layout's placeholder
        and the rest from the master's text styles, so a reference that walked
        neither rung reported every heading in every template as having no type.
        """
        return _declared(self.inherited(shape)) + self.master.get(_family(shape), ())


def page_design(presentation, slide) -> _Design:
    """What this page's shapes inherit from: theme palette, colour map, layout placeholders, master styles."""
    return _Design(
        palette=_palette(presentation),
        mapping=_colour_map(presentation),
        placeholders=_layout_placeholders(slide),
        master=_master_styles(slide),
    )


def run_ink(run, design: _Design) -> str | None:
    """The colour one run states, srgb or resolved through the theme, as RRGGBB; None when it states none."""
    properties = run._r.find(f"{_A}rPr")
    if properties is None:
        return None
    solid = properties.find(f"{_A}solidFill")
    if solid is None:
        return None
    found = _colour_of(solid, design, _A)
    return found[0].upper() if found and found[0] else None


def inherited_ink(shape, design: _Design) -> str | None:
    """The colour a shape's text gets from the layout or the master when its runs state none.

    The measurements read the file for the ink they judge, and a cloned template page
    states almost nothing on the page itself -- twelve of thirteen blocks on one
    measured page -- so a 1.09:1 body line went unjudged. This is the rung the
    reference already walks; the checks walk it too.
    """
    return _declared_colour(design.stated(shape), design)


def inherited_size(shape, design: _Design) -> float | None:
    """The point size a shape's text gets from the layout or the master when its runs state none."""
    return _declared_size(design.stated(shape))


def _layout_placeholders(slide) -> dict[int, Any]:
    try:
        return {placeholder.placeholder_format.idx: placeholder for placeholder in slide.slide_layout.placeholders}
    except Exception:  # noqa: BLE001 -- a slide may reference a layout that is gone
        return {}


def _master_styles(slide) -> dict[str, tuple[Any, ...]]:
    """The type the master sets for titles, for body copy and for everything else.

    The last rung of the chain, and on these templates it is where the type that
    the layout does not state actually lives.
    """
    try:
        styles = slide.slide_layout.slide_master._element.find(f"{_P}txStyles")
    except Exception:  # noqa: BLE001 -- a master is required, but be safe
        return {}
    if styles is None:
        return {}
    found: dict[str, tuple[Any, ...]] = {}
    for tag, family in (("titleStyle", "title"), ("bodyStyle", "body"), ("otherStyle", "other")):
        level = styles.find(f"{_P}{tag}/{_A}lvl1pPr")
        default = level.find(f"{_A}defRPr") if level is not None else None
        if default is not None:
            found[family] = (default,)
    return found


def _family(shape) -> str:
    """Which of the master's text styles this shape would inherit from."""
    try:
        return _FAMILY.get(shape.placeholder_format.type.name, "other")
    except (AttributeError, ValueError):
        return "other"


def _opening(presentation, slide) -> str:
    """The line that makes the rest of the reference a recipe rather than a list.

    Most of what a template's cover looks like is on its layout, and none of that
    reaches `slide.shapes` -- so an author handed only the shape code draws the
    page on a blank background and wonders where the design went. Naming the
    layout, by the index it is actually indexed with, is the difference between a
    page that sits inside the template and one that merely has its canvas size.
    """
    index = _layout_index(presentation, slide)
    if index is None:
        return "slide = prs.slides.add_slide(prs.slide_layouts[0])"
    name = _layout_name(slide)
    line = f"slide = prs.slides.add_slide(prs.slide_layouts[{index}])  # {name!r}"
    drawn = _layout_decoration(slide)
    if drawn:
        line += (
            f"\n# {drawn} element(s) of this page -- its background and decoration -- come from that "
            "layout, not from the code below, and they arrive with the slide"
        )
    return line


def _layout_index(presentation, slide) -> int | None:
    try:
        target = slide.slide_layout._element
        return next(i for i, layout in enumerate(presentation.slide_layouts) if layout._element is target)
    except Exception:  # noqa: BLE001 -- a layout from another master is not indexable this way
        return None


def _layout_decoration(slide) -> int:
    try:
        return sum(1 for shape in slide.slide_layout.shapes if not shape.is_placeholder)
    except Exception:  # noqa: BLE001 -- see above
        return 0


def _needed_imports(source: str) -> tuple[str, ...]:
    """The import lines this page's code needs, in a stable order."""
    return tuple(dict.fromkeys(line for name, line in _IMPORTS if name in source))


def needed_imports(sources: Iterable[str]) -> tuple[str, ...]:
    """The import lines several pages' code needs between them, stated once."""
    return tuple(dict.fromkeys(line for source in sources for line in _needed_imports(source)))


def _folded(run: list[tuple[int, str]]) -> str:
    """One line for a run of custom-drawn shapes: their ordinals and the box they span."""
    if len(run) == 1:
        ordinal, box = run[0]
        return f"# [{ordinal}]\n# a custom-drawn shape at {box}"
    boxes = [[float(v) for v in re.findall(r"Inches\(([-0-9.]+)\)", box)] for _o, box in run]
    x0 = min(b[0] for b in boxes if len(b) == 4)
    y0 = min(b[1] for b in boxes if len(b) == 4)
    x1 = max(b[0] + b[2] for b in boxes if len(b) == 4)
    y1 = max(b[1] + b[3] for b in boxes if len(b) == 4)
    return (
        f"# [{run[0][0]}]-[{run[-1][0]}] {len(run)} custom-drawn shapes between "
        f"Inches({x0:g}), Inches({y0:g}) and Inches({x1:g}), Inches({y1:g}) -- decoration, kept by cloning"
    )


def _flatten(shapes, transform: "_Transform | None" = None):
    """Every shape, with groups opened and their children in page coordinates.

    A group is a container, not a composition: left nested, the reference would read
    as `group.shapes[2].shapes[0]`, and the point of this is that an author can copy
    a line of it.

    The transform is the part that has to be right. A group declares its own child
    coordinate space -- an offset *and* an extent -- and PowerPoint maps that space
    onto the group's box on the page. Applying only the offset put shapes at
    `Inches(21.61)` on a 13.33-inch canvas, which is the kind of wrong that reads as
    a decompiler bug to anyone who sees the output.
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _flatten(shape.shapes, _child_transform(shape, transform))
        else:
            yield _Positioned(shape, transform)


@dataclass(frozen=True)
class _Transform:
    """Child space to page space: scale about the child origin, then translate."""

    dx: int = 0
    dy: int = 0
    sx: float = 1.0
    sy: float = 1.0

    def x(self, value: int) -> int:
        return int(self.dx + value * self.sx)

    def y(self, value: int) -> int:
        return int(self.dy + value * self.sy)

    def w(self, value: int) -> int:
        return int(value * self.sx)

    def h(self, value: int) -> int:
        return int(value * self.sy)


def _child_transform(group, outer: _Transform | None) -> _Transform:
    outer = outer or _Transform()
    xfrm = group._element.find("{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm") or group._element.find(
        ".//{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm"
    )
    off = ext = child_off = child_ext = None
    if xfrm is not None:
        ns = _A
        off, ext = xfrm.find(f"{ns}off"), xfrm.find(f"{ns}ext")
        child_off, child_ext = xfrm.find(f"{ns}chOff"), xfrm.find(f"{ns}chExt")
    if off is None or ext is None or child_off is None or child_ext is None:
        return outer
    scale_x = _ratio(ext.get("cx"), child_ext.get("cx"))
    scale_y = _ratio(ext.get("cy"), child_ext.get("cy"))
    dx = int(off.get("x") or 0) - int(child_off.get("x") or 0) * scale_x
    dy = int(off.get("y") or 0) - int(child_off.get("y") or 0) * scale_y
    return _Transform(
        dx=int(outer.x(int(dx))),
        dy=int(outer.y(int(dy))),
        sx=outer.sx * scale_x,
        sy=outer.sy * scale_y,
    )


def _ratio(outer: str | None, inner: str | None) -> float:
    try:
        top, bottom = float(outer or 0), float(inner or 0)
    except (TypeError, ValueError):
        return 1.0
    return top / bottom if bottom else 1.0


class _Positioned:
    """A shape in page coordinates, whatever group it came out of."""

    def __init__(self, shape, transform: _Transform | None) -> None:
        transform = transform or _Transform()
        self.shape = shape
        self.left = transform.x(int(shape.left or 0))
        self.top = transform.y(int(shape.top or 0))
        self.width = transform.w(int(shape.width or 0))
        self.height = transform.h(int(shape.height or 0))

    def __getattr__(self, name):
        return getattr(self.shape, name)


def _palette(presentation) -> dict[str, str]:
    """The theme's colour scheme by slot name, or {} when there is none."""
    from raven_ppt.services.template.inventory import _theme_colours, _theme_root

    return {name: value.lstrip("#") for name, value in _theme_colours(_theme_root(presentation))}


def _colour_map(presentation) -> dict[str, str]:
    """The master's colour map: which theme slot each scheme name refers to.

    Templates routinely map `tx2` to `lt2` and `bg1` to `dk1` -- an inverted deck
    is exactly that map -- so skipping it inverts every colour the reference emits.
    """
    try:
        master = presentation.slide_masters[0]._element
        node = master.find("{http://schemas.openxmlformats.org/presentationml/2006/main}clrMap")
    except Exception:  # noqa: BLE001 -- a master is required, but be safe
        return {}
    return dict(node.attrib) if node is not None else {}


@dataclass(frozen=True)
class _Emitted:
    """One shape's contribution: code, an extracted picture, and what was lost."""

    source: str = ""
    picture: str | None = None
    lost: tuple[str, ...] = ()
    # A shape that is only named, never drawn: a run of them folds into one line.
    decoration: bool = False
    # The box of a chart, which is neither drawn here nor kept by cloning.
    redraw: tuple[str, ...] = ()


def _shape_source(shape, images_dir: Path | None, ordinal: int, design: _Design) -> _Emitted:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    box = _box(shape)
    lost = _unwritable(shape)
    if getattr(shape, "has_chart", False):
        # Ahead of the picture and table branches: a chart is a graphic frame, so it
        # answers none of them and used to fall through to the empty return, whose
        # "kept by cloning" line said the one thing that is not true of it.
        return _Emitted(_chart_source(shape, box), lost=lost, redraw=(box,))
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        source, picture = _picture_source(shape, box, images_dir, ordinal)
        return _Emitted(source, picture, lost)
    if getattr(shape, "has_table", False):
        table = shape.table
        return _Emitted(
            f"# a {len(table.rows)}x{len(table.columns)} table at {box}\n"
            f"table = slide.shapes.add_table({len(table.rows)}, {len(table.columns)}, {box}).table",
            lost=lost,
        )
    if _is_connector(shape):
        return _Emitted(_connector_source(shape), lost=lost)
    if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        return _Emitted(_text_source(shape, box, design), lost=lost)
    if shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
        return _Emitted(_panel_source(shape, box, design), lost=lost)
    if shape.shape_type == MSO_SHAPE_TYPE.FREEFORM:
        # Named rather than drawn. `add_shape` would put a rectangle here, and 18%
        # of the shapes in the measured templates are freeforms -- the swooshes,
        # cut corners and blobs a design is recognisable by. A page that quietly
        # replaced each of them with a rectangle would read as a bug in the deck
        # rather than as a limit of the reference.
        return _Emitted(f"# a custom-drawn shape at {box}", lost=lost, decoration=True)
    return _Emitted(lost=lost)


def _unwritable(shape) -> tuple[str, ...]:
    """What this shape has that no code written here could reproduce.

    Reported rather than approximated. The alternative -- draw the nearest thing
    python-pptx can express -- produces a page that looks finished and is wrong,
    and the author has no way to tell which of its elements were guesses.
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    element = getattr(shape, "_element", None)
    if element is None:
        return ()
    found = [phrase for tag, phrase in _UNWRITABLE if element.find(f".//{_A}{tag}") is not None]
    effects = element.find(f".//{_A}effectLst")
    if effects is not None and len(effects):
        found.append("a shadow or a glow")
    if shape.shape_type != MSO_SHAPE_TYPE.PICTURE and element.find(f".//{_A}blipFill") is not None:
        found.append("an image used as a fill")
    return tuple(found)


def _chart_source(shape, box: str) -> str:
    """A chart as the box it leaves the author, and the two calls that fill it.

    The handle is positional rather than the `# [n]` ordinal beside it: `adapt`'s
    `drop=` and a short `items=` both delete shapes before the author holds the
    slide, and every ordinal after a deleted one has moved. A position on the page
    is what this reference prints anyway, and `shape_near` raises when it misses.
    """
    left, top, width, height = (_inch(value) for value in _corners(shape))
    return (
        f"# a chart at {box} -- redrawn, not cloned: on the clone, "
        f"drop_shape(shape_near(slide, {left:.2f}, {top:.2f}))\n"
        f"# and draw your own data into Box.at({left:.2f}, {top:.2f}, w={width:.2f}, h={height:.2f}) "
        "with one of ppt_charts' forms (`from ppt_layout import Box`)"
    )


def _picture_source(shape, box: str, images_dir: Path | None, ordinal: int) -> tuple[str, str | None]:
    if images_dir is None:
        return f"# a picture at {box} -- replace it with one of your own", None
    images_dir.mkdir(parents=True, exist_ok=True)
    try:
        image = shape.image
        name = f"template_{ordinal:02d}.{image.ext}"
        (images_dir / name).write_bytes(image.blob)
    except Exception:  # noqa: BLE001 -- a picture whose part is missing is skippable
        return f"# a picture at {box} -- its image could not be read", None
    return (f'slide.shapes.add_picture("{name}", {box})  # swap in your own image', name)


def _is_connector(shape) -> bool:
    element = getattr(shape, "_element", None)
    return element is not None and element.tag.endswith("}cxnSp")


def _connector_source(shape) -> str:
    """A rule or a leader line, as the two points it runs between.

    A connector's box says where it sits, not which way it runs: `flipH` and
    `flipV` decide which corners of that box are its ends, and a reference that
    read only the box draws every backslash as a slash.
    """
    x1, y1 = shape.left, shape.top
    x2, y2 = shape.left + shape.width, shape.top + shape.height
    xfrm = shape._element.find(f".//{_A}xfrm")
    if xfrm is not None and xfrm.get("flipH") in ("1", "true"):
        x1, x2 = x2, x1
    if xfrm is not None and xfrm.get("flipV") in ("1", "true"):
        y1, y2 = y2, y1
    lines = [
        "line = slide.shapes.add_connector("
        f"MSO_CONNECTOR.STRAIGHT, {', '.join(_inches(value) for value in (x1, y1, x2, y2))})"
    ]
    colour = _line_colour(shape)
    if colour:
        lines.append(f'line.line.color.rgb = RGBColor.from_string("{colour}")')
    width = getattr(shape.line, "width", None)
    if width:
        lines.append(f"line.line.width = Pt({width.pt:g})")
    return "\n".join(lines)


def _text_source(shape, box: str, design: _Design) -> str:
    inherited = design.inherited(shape)
    frame = shape.text_frame
    lines = [f"box = slide.shapes.add_textbox({box}); frame = box.text_frame"]
    if frame.word_wrap is not None:
        lines.append(f"frame.word_wrap = {bool(frame.word_wrap)}")
    anchor = _anchor(shape) or (_anchor(inherited) if inherited is not None else None)
    if anchor:
        # The commonest single thing a template does to a text box: 30% of the
        # shapes measured set one. Omitted, every label sits at the top of a box
        # the template centres in, and the whole page reads as slightly fallen.
        lines.append(f"frame.vertical_anchor = MSO_ANCHOR.{anchor}")
    if _rotation(shape):
        lines.append(f"box.rotation = {_rotation(shape):g}")
    written = 0
    for paragraph in frame.paragraphs:
        text = paragraph.text
        if not text.strip():
            continue
        # Text first, then the paragraph. Assigning to `frame.text` rebuilds the
        # paragraph list, so a reference fetched before it is detached from the tree
        # and `para.runs[0]` raises IndexError -- which an author copying this would
        # hit before it hit us. Caught by replaying the output, not by reading it.
        if written == 0:
            lines.append(f"frame.text = {text!r}")
            lines.append("para = frame.paragraphs[0]")
        else:
            lines.append("para = frame.add_paragraph()")
            lines.append(f"para.text = {text!r}")
        written += 1
        alignment = _alignment(paragraph) or _declared_alignment(inherited)
        if alignment:
            lines.append(f"para.alignment = PP_ALIGN.{alignment}")
        if paragraph.level:
            lines.append(f"para.level = {paragraph.level}")
        # Guarded, because a paragraph whose text is a single space produces no run
        # and the reference would stop there.
        # Each falls back to the layout's placeholder, because a template that
        # sets its heading once on the layout states nothing on the page -- and a
        # reference that read only the page said the heading had no type.
        stated = design.stated(shape)
        properties = []
        size = _first_size(paragraph) or _declared_size(stated)
        if size:
            properties.append(f"font.size = Pt({size:g})")
        colour = _run_colour(paragraph, design) or _declared_colour(stated, design)
        if colour:
            properties.append(f'font.color.rgb = RGBColor.from_string("{colour}")')
        if _first_bold(paragraph) or _declared_bold(stated):
            properties.append("font.bold = True")
        name = _first_font(paragraph) or _declared_font(stated)
        if name:
            properties.append(f"font.name = {name!r}")
        # One line, not a block: a page of a dozen text boxes read as sixty lines of
        # run properties, and a six-page reference outran the reply it was read in.
        if properties:
            lines.append("if para.runs: font = para.runs[0].font; " + "; ".join(properties))
    return "\n".join(lines)


def _panel_source(shape, box: str, design: _Design) -> str:
    lines = [f"panel = slide.shapes.add_shape(MSO_SHAPE.{_prst(shape)}, {box})"]
    if _rotation(shape):
        lines.append(f"panel.rotation = {_rotation(shape):g}")
    fill = _fill_colour(shape, design)
    if fill:
        colour, alpha = fill
        lines.append(f'panel.fill.solid(); panel.fill.fore_color.rgb = RGBColor.from_string("{colour}")')
        if alpha < 100:
            # python-pptx cannot set alpha; the value is stated so the effect the
            # template relies on is not silently lost from the reference.
            lines.append(f"# the template draws this at {alpha}% opacity (needs raw XML to reproduce)")
    else:
        lines.append("panel.fill.background()")
    line = _line_colour(shape)
    lines.append(f'panel.line.color.rgb = RGBColor.from_string("{line}")' if line else "panel.line.fill.background()")
    return "\n".join(lines)


def _declared(shape) -> tuple[Any, ...]:
    """Every node a layout's placeholder states run properties on, in priority order.

    Three of them, and the order is the whole point. `defRPr` inside the paragraph
    properties is the default a real run inherits, and it is what a template author
    sets when they size a placeholder. `rPr` belongs to the sample text -- "Click to
    edit Master title style" -- and usually states nothing but a language. Taking
    the first node present rather than the first that states the property read that
    empty `rPr` and reported a 40pt cover heading as having no type at all.
    """
    element = getattr(shape, "_element", None)
    if element is None:
        return ()
    found = (element.find(f".//{_A}{tag}") for tag in ("defRPr", "rPr", "endParaRPr"))
    return tuple(node for node in found if node is not None)


def _declared_size(nodes: tuple[Any, ...]) -> float | None:
    for node in nodes:
        if node.get("sz"):
            return int(node.get("sz")) / 100
    return None


def _declared_bold(nodes: tuple[Any, ...]) -> bool:
    return any(node.get("b") in ("1", "true") for node in nodes)


def _declared_font(nodes: tuple[Any, ...]) -> str | None:
    for node in nodes:
        latin = node.find(f"{_A}latin")
        if latin is not None and latin.get("typeface"):
            return latin.get("typeface")
    return None


def _declared_colour(nodes: tuple[Any, ...], design: _Design) -> str | None:
    for node in nodes:
        solid = node.find(f"{_A}solidFill")
        if solid is not None and (found := _colour_of(solid, design, _A)):
            return found[0]
    return None


def _declared_alignment(shape) -> str | None:
    element = getattr(shape, "_element", None)
    node = element.find(f".//{_A}pPr") if element is not None else None
    return _ALIGNMENTS.get(node.get("algn") or "") if node is not None else None


def _anchor(shape) -> str | None:
    """MIDDLE or BOTTOM where the template sets one; None for the default top."""
    element = getattr(shape, "_element", None)
    body = element.find(f".//{_A}bodyPr") if element is not None else None
    return _ANCHORS.get(body.get("anchor") or "") if body is not None else None


def _alignment(paragraph) -> str | None:
    """CENTER, RIGHT or JUSTIFY where set; None for left, which is the default."""
    value = getattr(paragraph.alignment, "name", None)
    return value if value and value != "LEFT" else None


def _rotation(shape) -> float:
    try:
        return float(shape.rotation or 0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _box(shape) -> str:
    return ", ".join(_inches(value) for value in _corners(shape))


def _corners(shape) -> tuple[int, int, int, int]:
    return (shape.left or 0, shape.top or 0, shape.width or 0, shape.height or 0)


def _inches(value: int) -> str:
    return f"Inches({value / EMU_PER_INCH:.2f})"


def _inch(value: int) -> float:
    """The same number as `_inches`, unwrapped: ppt_charts takes a Box in inches."""
    return value / EMU_PER_INCH


def _prst(shape) -> str:
    try:
        return shape.auto_shape_type.name
    except Exception:  # noqa: BLE001 -- a shape may have no preset geometry
        return "RECTANGLE"


def _resolved(colour: str | None, palette: dict[str, str]) -> str | None:
    """A colour as six hex digits, resolving a theme slot through the palette."""
    if not colour:
        return None
    if not colour.startswith("theme:"):
        return colour
    slot = _THEME_SLOTS.get(colour[6:])
    return palette.get(slot or "", None)


def _fill_colour(shape, design: _Design) -> tuple[str, int] | None:
    """(hex, alpha%) of a solid fill, read off the XML.

    Off the XML rather than through python-pptx's colour objects, because both
    things that decide what a fill looks like are invisible from there. A scheme
    colour names a *slot*, and which theme entry a slot means is decided by the
    master's colour map -- in the template this was checked against, `tx2` maps to
    `lt2`, so reading it as "dark 2" produced a light card where the template has a
    near-black one. And a fill carries alpha: those cards are `lt2` at 15% over a
    black page, which is the entire reason they read as dark. Resolve one and miss
    the other and the reference is still wrong, in a way that looks deliberate.
    """
    ns = _A
    element = getattr(shape, "_element", None)
    properties = element.spPr if element is not None and hasattr(element, "spPr") else None
    if properties is None:
        return None
    solid = properties.find(f"{ns}solidFill")
    if solid is None:
        return None
    return _colour_of(solid, design, ns)


def _colour_of(parent, design: _Design, ns: str) -> tuple[str, int] | None:
    srgb = parent.find(f"{ns}srgbClr")
    scheme = parent.find(f"{ns}schemeClr")
    node = srgb if srgb is not None else scheme
    if node is None:
        return None
    alpha_node = node.find(f"{ns}alpha")
    alpha = int(int(alpha_node.get("val", "100000")) / 1000) if alpha_node is not None else 100
    if srgb is not None:
        return (srgb.get("val", "").upper(), alpha)
    slot = design.mapping.get(scheme.get("val", ""), scheme.get("val", ""))
    value = design.palette.get(slot)
    return (value, alpha) if value else None


def _line_colour(shape) -> str | None:
    try:
        return str(shape.line.color.rgb)
    except Exception:  # noqa: BLE001 -- an inherited line has no rgb
        return None


def _first_size(paragraph) -> float | None:
    for run in paragraph.runs:
        if run.font.size is not None:
            return run.font.size.pt
    return None


def _run_colour(paragraph, design: _Design) -> str | None:
    """The first run's colour, resolved the same way a fill's is."""
    ns = _A
    for run in paragraph.runs:
        properties = run._r.find(f"{ns}rPr")
        if properties is None:
            continue
        solid = properties.find(f"{ns}solidFill")
        if solid is None:
            continue
        found = _colour_of(solid, design, ns)
        if found:
            return found[0]
    return None


def _first_bold(paragraph) -> bool:
    return any(run.font.bold for run in paragraph.runs)


def _first_font(paragraph) -> str | None:
    for run in paragraph.runs:
        if run.font.name:
            return run.font.name
    return None


def _layout_name(slide) -> str:
    try:
        return slide.slide_layout.name or "unnamed"
    except Exception:  # noqa: BLE001 -- a slide may reference a missing layout
        return "unknown"
