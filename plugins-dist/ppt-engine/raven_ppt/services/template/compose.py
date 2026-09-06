"""Building a page out of one the template already drew.

A template's visual elements live on its example slides and not on its layouts, so a
page built with `add_slide(layout)` gets the template's placeholders and almost none
of its design: covers come out right, because covers are decorated on the layout, and
content pages leave the template entirely.

The operations here are the ones that let an author work the way the design
actually supports: take the example page closest to what this page has to say,
replace its words and its pictures, delete what is left over. Each is something
python-pptx has no API for, and each has a way of going quietly wrong that a
reader of the resulting file would not connect to its cause:

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
"""

# Where a template keeps its design, measured on twenty real templates: 85% of the
# visual elements are on the example slides and not on the layouts -- 29 per template
# against 5.

from __future__ import annotations

import copy
import io
import re
import warnings
from pathlib import Path
from typing import Any

from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

# How far a picture's proportions may differ from its frame's before fitting it stops
# being a fit. A template's portrait photo slot is around 0.6 wide-to-tall and a paper's
# architecture figure is around 2.4: contained inside that slot the figure becomes a
# strip a quarter of the frame's height with empty space above and below it, which is
# what a live page did to Figure 2. Past this ratio the page needs rearranging, and only
# the author can decide how.
FIT_RATIO_LIMIT = 2.0

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

    **The copy arrives carrying the prototype's words.** Replace them --
    `replace_text` per shape, or `adapt` for the page at once -- and do not add a text
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
    """The slide's shape `number`, in the numbering the reference and `adapt` print.

    One numbering, three spellings of it: `# [5]` above a shape in the reference,
    `texts={5: ...}` in an `adapt` call, and `shape_at(slide, 5)` afterwards all mean
    the same shape -- counting from 1 over every shape on the page, groups walked into.

    It exists because a template page is a starting point rather than a form. `adapt`
    returns the slide and the next thing an author wants is usually an adjustment to
    it -- move the frame a landscape figure went into, close the hole two deleted
    units left, take a panel out from over a picture -- and each of those needs a
    handle on one shape.
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
    for shape in every:
        if with_text and not (getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip()):
            continue
        at = _where(shape)
        if at is None:
            unplaceable += 1
            continue
        if abs(at[0] - left) <= tol and abs(at[1] - top) <= tol:
            return shape
    raise KeyError(
        f"no shape within {tol:g}in of ({left:g}, {top:g}) on this page"
        + (" carrying text" if with_text else "")
        + ". Positions are on the page, groups resolved -- `shape.left` inside a group is not one. "
        + (
            f"{unplaceable} of them sit in a group the template flipped or rotated and have no position "
            "to compare against; take those by their copy with `shape_saying`. "
            if unplaceable
            else ""
        )
        + "The page holds: "
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

    `adapt(texts={...})` matches the same way for a whole page at once; this is the
    single handle for the adjustment that comes after. Its refusal lists the copy the
    page actually holds, because the string an author is matching against is usually
    the template's and usually not quite what it remembered.
    """
    said = []
    for shape in _all_shapes(container.shapes):
        if not getattr(shape, "has_text_frame", False):
            continue
        text = shape.text_frame.text.strip()
        if not text:
            continue
        if text.startswith(prefix):
            return shape
        said.append(text[:40])
    raise KeyError(
        f"no shape on this page starts with {prefix!r}. Its copy reads: " + "; ".join(repr(one) for one in said)
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
) -> None:
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
    `alpha=0.25` keeps it a background. `None` leaves whatever wash the frame had.
    """
    _, relationship = shape.part.get_or_add_image_part(str(image))
    fill = _blip_fill(shape)
    if fill is None:
        raise ValueError("that shape has no image to replace")
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
        "If the figure matters, give the frame the box it needs -- pictures={n: (image, (left, top, width, "
        "height))} in inches, or place(shape_at(slide, n), box) after adapt returns -- or adapt a prototype "
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
    handed to `replace_picture` -- `pictures={...}` on the cloned page never reaches
    it, and a live deck shipped with the template's photographs on every section page
    for that reason. These are those shapes. `replace_picture(layout_pictures(slide)[0],
    image, "cover", alpha=0.25)` changes the picture for every page on that layout at
    once, which is what a house photograph should do; the wash is for the picture that is
    the size of the page, which is the page's background and has type over it.
    """
    found = [shape for shape in _every_shape(slide.slide_layout.shapes) if _blip_fill(shape) is not None]
    found.sort(key=lambda shape: -((shape.width or 0) * (shape.height or 0)))
    return found


# Where a backdrop's picture may sit between invisible and opaque. Under 0.05 nothing
# shows and the call was a mistake; 1.0 is the photograph as it is, which is a figure and
# not a backdrop, but an author who wants a full-bleed picture behind a title over a dark
# wash of its own is allowed it.
BACKDROP_ALPHA_MIN = 0.05


def backdrop(slide, image, *, alpha: float = 0.22, box=None, anchor: str = "centre", trim=None, zoom: float = 1.0):
    """A picture behind everything on the page, washed to `alpha`.

    The one generated picture that never poses as evidence: a cover, a section page or a
    closing page wants atmosphere more than a figure, and the templates' own photographs
    are placeholders. Full-bleed by default, or into `box` -- (left, top, width, height)
    in inches, or a `ppt_layout.Box` -- and cover-cropped to it, so a 4:3 render behind a
    16:9 page loses its top and bottom rather than stretching; `anchor`, `trim` and
    `zoom` place the window the way `replace_picture` does.

    `alpha` is the picture's share of itself: 0.2 is a wash the template's ground and
    type stay legible over, 0.35 is as far as copy over it can go, 1 is the photograph
    as it is. The wash is the picture's own (`alphaModFix`), not a plane laid over it,
    so the page's own colour shows through and nothing is added to the z-order but the
    one picture -- first in it, behind the layout's furniture's own layer and every
    shape already on the page.

    Returns the picture shape. The contrast reading (§10) is taken off the pixels, so a
    wash that buries the title comes back as unreadable type, not as a wash: look at the
    render.
    """
    from pptx.util import Inches

    path = Path(image)
    if not path.is_file():
        raise ValueError(f"backdrop wants a picture file, and {image!r} is not one")
    share = _alpha_share(alpha)
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
    return picture


def wash(shape, alpha: float):
    """Set a picture's transparency: `alpha` is the picture's share of itself.

    Any picture on the page -- a frame the template drew, one `adapt(pictures=...)`
    filled, one `add_picture` placed, a rounded panel filled with a photograph -- and
    the same share `backdrop` and `replace_picture(alpha=...)` take: 0.2 is a wash
    under copy, 0.35 as far as copy over it can go, 1 the picture as it is. Written
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
            f"alpha={share:g} is outside {BACKDROP_ALPHA_MIN:g}..1: 0.2 is a wash under copy, 0.35 is as far as "
            "copy over it can go, 1 is the photograph as it is"
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
    """
    shape = target
    if new is not None:
        frames = [s for s in _all_shapes(target.shapes) if getattr(s, "has_text_frame", False)]
        shape = _pick(text, frames, frames)
        if shape is None:
            raise KeyError(
                f"no text on this page matches {text!r}. The page holds: "
                + "; ".join(f"[{index}] {_describe(s)}" for index, s in enumerate(_all_shapes(target.shapes), start=1))
            )
        text = new
    if not getattr(shape, "has_text_frame", False):
        raise ValueError("that shape holds no text")
    frame = shape.text_frame
    # `\x0b` counts as a break as much as `\n` does: it is what PowerPoint's format uses
    # for a soft one, so it is what `.text` hands back from a template's own placeholder
    # and what an author writes after reading one. Passed through, XML cannot carry it
    # and python-pptx spells it -- a delivered cover printed "EverMind AI_x000B_给 AI
    # 智能体" at title size.
    # A run sequence is one paragraph by construction: the breaks a string carries are
    # what splits it, and a sequence of runs states its pieces instead. A caller wanting
    # two emphasised paragraphs calls this twice, or passes a list of sequences.
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


def drop_shape(shape) -> None:
    """Remove an element the page does not need. The commonest edit after text."""
    shape._element.getparent().remove(shape._element)


def adapt(presentation, prototype, texts=None, pictures=None, drop=(), keep=(), items=None, title=None, subtitle=None):
    """Clone `prototype` and fill it in, in one call.

    The four operations above are the vocabulary; this is the sentence an author
    actually wants to write. Fifteen lines of clone, walk the shapes, match them up
    and replace is more work than drawing a rectangle, so the rectangle wins and the
    template's design goes with it.

    `texts` maps a shape's current text -- or its 1-based index over every shape on the
    page, groups opened and counted whether or not it holds text, which is the number
    `ppt_template` prints above each shape as `# [n]` -- to what should replace it. One
    numbering serves `texts`, `pictures`, `drop` and `keep`; counting only the shapes
    that hold text gives three numberings for one page.
    `pictures` maps the same keys to image paths, or to `(image, box)` to reshape the
    frame first, where the box is `(left, top, width, height)` in inches -- a size, as
    `place` takes, and not the two corners a `ppt_layout.Box` holds, though a Box may be
    passed and is converted. `drop` names shapes to remove, by the same keys.

    **Text this call does not name is emptied, and shapes are otherwise left alone.**
    A shape is the design; the words in it are the template's example copy. Keeping
    everything not named leaves the template's own placeholders on every page it did
    not touch:

        单击此处添加文本单击此处添加文本单击此处添加文本单击此处添加文本
        单击此处添加长一点的副标题
        单击添加小标题

    -- mostly hidden behind the copy and the figures the author did write, and some of
    them showing: one at subtitle size in the middle of a page, one reduced to its last
    character behind a photograph. Emptied rather than deleted
    because the box may be part of the design (a tinted panel, a numbered circle),
    and an empty box shows nothing while a deleted one takes its panel with it.

    `items` fills the page's repeating unit -- its card row, its agenda list -- one
    entry per unit, and **deletes the units left over**. Six items on a page that
    ships eight slots leaves six, with the other two gone rather than emptied. Each
    entry is a list positional over the unit's text shapes (`None` keeps one as it is)
    or a dict keyed by the text a shape holds now. This is the operation these pages
    exist for: most template example pages are built out of a repeated unit.

    `title` and `subtitle` write the page's heading rows without needing to know which
    shape they are. Filling a page with `items` alone empties its header, because text
    this call does not name is emptied, and writing the heading back as new boxes over
    the clone is the one construction `template_underlay` refuses. The title is the
    page's own title placeholder wherever the template put it -- a cover's is centred
    in the page, not at the top of it -- and on a page the template left unlabelled,
    the topmost line in the top third, which is what a reader would point at. A page
    with no row to take the value says so rather than guessing.

    `keep` names text to leave exactly as the template wrote it, by the same keys --
    for the step number in a circle, or a label the template owns.

    Groups are looked inside, which matters more than it sounds: a template page
    typically has two or three shapes at the top level and everything else one group
    down. A key that matches nothing raises, listing what the page does hold: skipped
    in silence, it leaves the placeholders the author had named still on the page.

    Returns the new slide, so a caller can still reach into it for anything this
    does not cover.
    """
    slide = clone_page(presentation, prototype)
    # The XML elements this call has spoken for, held as objects rather than as ids.
    # lxml builds an element proxy on demand and drops it when nothing refers to it, so
    # `id(shape._element)` goes stale -- and gets reused -- the moment the shape falls out
    # of scope. That cost an afternoon: the items were written, then emptied again by the
    # pass below, in a pattern that looked like every other one surviving.
    spoken = []
    # Numbered before anything is added or removed, because an integer key counts the
    # shapes the reference printed. `items` deletes the units it does not fill, and doing
    # that first renumbered everything after them -- a live program asked for
    # `pictures={2: ...}` and the picture had become shape 3.
    every = list(_all_shapes(slide.shapes))
    with_text = [shape for shape in every if getattr(shape, "has_text_frame", False)]
    frames = [shape for shape in with_text if shape.text_frame.text.strip()]
    images = [shape for shape in every if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
    missed = []

    # Everything is resolved first and written afterwards. Resolving as it wrote meant a
    # refusal listed the page half-replaced -- the shape the author's key no longer
    # matched had already become the new text, so the message contradicted itself -- and
    # it meant a key that failed late left the earlier ones applied.
    words: list[tuple[Any, list[tuple[int, str]]]] = []
    heads = _heading_rows(slide, presentation)
    for role, value in (("title", title), ("subtitle", subtitle)):
        if value is None:
            continue
        shape = heads.get(role)
        if shape is None:
            missed.append(role)
            continue
        words.append((shape, [(-1, str(value))]))
    for key, value in (texts or {}).items():
        shape = _pick(key, every, with_text)
        if shape is None or not getattr(shape, "has_text_frame", False):
            missed.append(key)
            continue
        where = shape.text_frame.text.find(str(key)) if not isinstance(key, int) else -1
        for held, values in words:
            if held._element is shape._element:
                values.append((where, str(value)))
                break
        else:
            words.append((shape, [(where, str(value))]))

    swaps: list[tuple[Any, Path, tuple[float, float, float, float] | None, str]] = []
    for key, value in (pictures or {}).items():
        image, box, how = _picture_spec(value)
        shape = _pick(key, every, images)
        if shape is None or getattr(shape, "shape_type", None) != MSO_SHAPE_TYPE.PICTURE:
            # A page with exactly one picture leaves no room for doubt about which frame
            # was meant, so an index landing elsewhere is read as that one: two models
            # each wrote `pictures={2: ...}` against a page whose picture was shape 3.
            # Two frames and the index has to be right.
            if isinstance(key, int) and len(images) == 1:
                shape = images[0]
            else:
                missed.append(key)
                continue
        swaps.append((shape, image, box, how))

    for key in keep:
        shape = _pick(key, every, with_text)
        if shape is None:
            missed.append(key)
            continue
        spoken.append(shape._element)

    doomed = []
    for key in drop:
        shape = _pick(key, every, every)
        if shape is None:
            missed.append(key)
            continue
        doomed.append(shape)

    if missed:
        raise KeyError(
            f"nothing usable on this page matches {missed!r}."
            + _advice(missed, texts, pictures, images, frames)
            + " These are the numbers ppt_template prints above each shape as `# [n]`: counting from 1, and"
            " counting every shape whether or not it holds text. The page holds: "
            + "; ".join(f"[{index}] {_describe(shape)}" for index, shape in enumerate(every, start=1))
        )

    for shape, values in words:
        # Two keys on one block are two halves of one paragraph -- a template subtitle
        # wraps to two lines in the render, and three times across two models an author
        # read those lines as two boxes and named both.
        replace_text(shape, values[0][1] if len(values) == 1 else " ".join(v for _, v in sorted(values)))
        spoken.append(shape._element)
    for shape, path, box, how in swaps:
        # Reshaped before it is filled, because a frame's proportions decide what
        # fitting the picture can even have: contain shrinks the frame to the picture
        # inside the box it was given, so the box is the author's decision about where
        # the figure goes and the fit is arithmetic afterwards.
        if box is not None:
            place(shape, box)
        replace_picture(shape, path, how)
    for shape in doomed:
        spoken.append(shape._element)
        drop_shape(shape)
    if items is not None:
        runs = units(slide)
        if not runs:
            raise ValueError("nothing repeats on this page, so `items` has nothing to fill -- use `texts`")
        # The longest run is the page's content row; a page with two runs has a row of
        # cards and something smaller like a pair of labels, and the cards are what an
        # author means by "the items on this page".
        spoken.extend(shape._element for shape in fill(max(runs, key=len), items))
    for shape in frames:
        if not any(shape._element is element for element in spoken):
            replace_text(shape, "")
    return slide


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

    Returns the shapes it wrote, which is what `adapt` needs to know they are spoken
    for -- without it, the pass that empties unnamed text would empty these too.
    """
    written = []
    if len(items) > len(run):
        # Grown rather than refused. Ten builds across the measured runs died on this
        # refusal, nine of them one or two items over; the authors then wrote their own
        # `clone_panel` -- deepcopy the element, hang it on the tree, set a box -- which is
        # `add_unit` without the re-flow. A run that follows no grid still refuses, with
        # the slot counts the template menu now prints as the way out.
        run = list(run) + add_unit(run, len(items) - len(run))
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
            # Only while the prototype's own words are still there. An empty frame reads
            # as a spacer because the ones beside it hold text; `adapt` empties every
            # text it was not told about, so on the second run of a page it has already
            # adapted -- the sequence this function is documented for -- every frame is
            # empty, the filter has nothing to tell them apart by, and leaving it on
            # raised "0 text shape(s)" against the count the author could see. One live
            # run answered that by editing its program nineteen times and shipping no
            # page.
            if spoken:
                frames = spoken
        if isinstance(item, dict):
            for key, value in item.items():
                shape = _pick(key, frames, frames)
                if shape is None:
                    raise KeyError(
                        f"no text in this unit matches {key!r}; it holds " + ", ".join(repr(_head(f)) for f in frames)
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
            # a prototype with more slots, one fewer point, or a shape of their own after
            # `adapt` returns.
            raise ValueError(
                f"a unit on this page holds {len(frames)} text shape(s) and {len(item)} values were given"
                + (": " + ", ".join(repr(_head(shape)) for shape in frames) if frames else "")
                + ". Give one value per shape (None keeps one as it is), pick a prototype whose units hold "
                "more, or add your own shape to the slide after adapt returns"
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
            # Short of the unit's shapes means "I have nothing to say about the rest",
            # not "empty them". The zip that used to be here stopped at the shorter
            # list and left the remainder unclaimed, so the pass that empties unnamed
            # text emptied them: a real template's agenda unit holds a title box, the
            # folder shape and its number, an author gave two values, and all eight
            # numbers came off the page.
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
                # Claimed even though nothing is written: `adapt` empties every text
                # it was not told about, so a shape left out of `written` is a shape
                # emptied a moment later -- which made `None` do exactly what "" did.
                # A live author tried `["...", "01"]`, `["...", None]` and `["...", ""]`
                # in three consecutive requests looking for the one that kept the
                # template's number, and none of the three did.
                #
                # A *short list* is the one case where claiming it is wrong. An
                # explicit None says "this shape is the template's"; running off the
                # end of the list says nothing at all, and what it left standing was
                # "单击添加小标题" on four cards of a delivered page, refused by
                # `placeholder_copy` at the gate after ten builds spent elsewhere. The
                # number is the exception the agenda case is about, and it is already
                # recognisable: `_renumbered` answers for a shape that holds one, which
                # is the same line `placeholder_copy` draws when it skips pure digits.
                if index >= len(values) and ordinal is None:
                    continue
                if index >= len(values):
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
    it in the same container, so `fill` and `adapt(items=...)` treat them as slots like
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
        raise ValueError(
            f"this page repeats {len(run)} units along no row, column or grid, so it cannot take {n}: where the "
            f"next one goes is the design's to say. Pick a prototype with {n} slots -- ppt_template prints each "
            f"page's slot count -- or split the content over two pages"
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
    does not fill when `adapt(items=...)` was not the way the page was written. The
    survivors are laid out again the way `add_unit` and `fill` lay theirs out; a unit
    that repeats along no grid is removed and nothing else moves.
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


def _picture_spec(value):
    """A `pictures` value: an image, or an image with the box its frame should take.

    `pictures={4: fig("fig2")}` fills the frame as the template drew it, which is the
    common case. `pictures={4: (fig("fig2"), (0.8, 1.6, 7.4, 4.2))}` reshapes the frame
    to that box first, and exists because the escape the refusal below names --
    `place(shape, box)` -- needs a shape `adapt` has not handed back yet, so from
    inside one call a landscape figure in a full-height portrait frame otherwise has
    no way out.

    The box is `(left, top, width, height)` in inches, in that order -- the same size
    `place` takes, not the two corners a `ppt_layout.Box` holds. A Box may be passed and
    is converted; four numbers are read as a size.

    A third element is the fit ("contain", "cover", "stretch") for the rare frame that
    wants cropping rather than shrinking.
    """
    if isinstance(value, (tuple, list)):
        if len(value) not in (2, 3):
            raise ValueError(
                f"a picture is an image, (image, box) or (image, box, fit), not {len(value)} values. "
                "A box is (left, top, width, height) in inches"
            )
        box = _as_size(value[1], "a picture's box")
        return Path(value[0]), box, str(value[2]) if len(value) == 3 else "contain"
    return Path(value), None, "contain"


def _all_shapes(shapes):
    """Every shape on the page, including the ones inside groups.

    A template's content is mostly inside groups: a card page has two shapes at the
    top level and fourteen with text one group down -- the four cards, their numbers,
    their headings and their body copy. `slide.shapes` does not descend, so without
    this `adapt` cannot see thirteen of the fourteen placeholders, replaces none of
    them, and says nothing about it, leaving "单击此处添加文本" on a page whose own
    `texts={...}` named exactly those strings.
    """
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _all_shapes(shape.shapes)
        else:
            yield shape


def _advice(missed, texts, pictures, images, frames):
    """The sentence that turns a listing into a fix.

    Two cases the listing alone cannot answer: `pictures={2: ...}` on a page with no
    picture frame anywhere, where the listing says "[2] shape" twenty-two times and
    never says the obvious thing; and `''` passed to each of four agenda slots, which
    clears their text and leaves four numbered bubbles on the page.
    """
    said = []
    if pictures and any(key in pictures for key in missed) and not images:
        said.append(
            " This page holds no picture frame at all, so `pictures` has nowhere to put one -- "
            "add it after adapt returns with `slide.shapes.add_picture(path, left, top, width=...)`."
        )
    blanks = sum(1 for value in (texts or {}).values() if isinstance(value, str) and not value.strip())
    if blanks >= 2:
        said.append(
            f" You are also passing '' to {blanks} keys: that empties their text and leaves the shapes -- a "
            "numbered bubble with nothing beside it. Use `items=[...]` with one entry per unit you are keeping "
            "and the spare units are deleted instead."
        )
    return "".join(said)


def _describe(shape, limit=26):
    """A shape as the refusal names it: its place, its kind, and its words."""
    kind = "picture" if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE else "shape"
    text = _head(shape, limit) if getattr(shape, "has_text_frame", False) else ""
    return f"{kind} {text!r}" if text else kind


def _head(shape, limit=30):
    text = " ".join(shape.text_frame.text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pick(key, by_index, by_text):
    """A shape named by its 1-based place on the page, or by the text it holds now.

    One numbering for every argument, and it is the page's own: shape N is the Nth
    shape `decompile` prints, groups opened, whether or not it holds text. The
    alternative -- counting text frames for `texts`, pictures for `pictures` -- gives
    three numberings for one page, so `texts={15: ...}` read off the reference lands on
    a page whose text frames stop at fourteen.
    """
    if isinstance(key, int):
        return by_index[key - 1] if 1 <= key <= len(by_index) else None
    wanted = str(key).strip()
    for shape in by_text:
        if getattr(shape, "has_text_frame", False) and wanted in shape.text_frame.text:
            return shape
    return None


def helper_source() -> str:
    """This module's own text, to write beside a build script that needs it.

    The author's program runs in a subprocess that has python-pptx and nothing of
    this package, so these operations reach it as a file it imports rather than as
    a call it makes. Its own source rather than a second copy kept in a string:
    two spellings of `clone_page` would drift, and the one that drifts is the one
    no test runs.
    """
    return Path(__file__).read_text(encoding="utf-8")
