"""Building a page out of one the template already drew.

Measured on twenty real templates: 85% of a template's visual elements live on its
example slides, not on its layouts -- 29 per template against 5. So a page built
with `add_slide(layout)` gets the template's placeholders and almost none of its
design, which is exactly what a first attempt produced: correct covers, because
covers are decorated on the layout, and content pages that had left the template
entirely.

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

from __future__ import annotations

import copy
import io
import re
from pathlib import Path
from typing import Any

from pptx.enum.shapes import MSO_SHAPE_TYPE

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


def clone_page(presentation, prototype):
    """A new slide at the end of `presentation`, holding a copy of `prototype`.

    The prototype may come from the same presentation or another one; either way
    every relationship its shapes refer to is carried across and re-pointed, which
    is the part that breaks when this is written by hand.

    **The copy arrives carrying the prototype's words.** Replace them --
    `replace_text` per shape, or `adapt` for the page at once -- and do not add a text
    box over the top: a run that cloned twelve pages and added its copy in new boxes
    shipped a deck with "单击此处添加长一点的副标题" on ten of them, under its own text.
    `placeholder_copy` and `template_underlay` refuse that at the gate.

    Requiring the promise as an argument was tried and cost more than it saved: the one
    model doing it right -- eleven clones, twenty-five replacements -- lost a round to
    the missing keyword, while the model that laid boxes over its clones had already
    moved to `adapt` on the strength of the skill saying so. Replacing is what almost
    everyone means, so it is the default again.
    """
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


def replace_picture(shape, image: Path, fit: str = "contain") -> None:
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
    """
    _, relationship = shape.part.get_or_add_image_part(str(image))
    fill = _blip_fill(shape)
    if fill is None:
        raise ValueError("that shape has no image to replace")
    blip = fill.find(f"{{{_A}}}blip")
    if blip is None:
        raise ValueError("that shape has no image to replace")
    blip.set(f"{{{_R}}}embed", relationship)
    if fit not in ("cover", "contain"):
        return
    _check_shape(shape, image, fit)
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        _fit(shape, image, fit)
    else:
        # A shape *filled* with a picture crops through the fill's source rectangle
        # rather than through a picture frame's crop attributes, and it has no frame to
        # shrink -- so "contain" has nowhere to put the letterboxing and both fits become
        # the same cover crop. Without this the template's own stretch survives and a
        # figure swapped into a rounded panel comes out distorted, which is visible in
        # any screenshot with type in it.
        _fill_crop(shape, fill, image)


_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"


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


def _fill_crop(shape, fill, image: Path) -> None:
    """Crop the fill's source to the shape's proportions, so nothing stretches."""
    size = _picture_size(image)
    if size is None or not shape.width or not shape.height:
        return
    width, height = size
    frame = shape.width / shape.height
    picture = width / height
    from lxml import etree

    for stale in fill.findall(f"{{{_A}}}srcRect"):
        fill.remove(stale)
    if picture > frame:  # wider than the shape: take the middle of its width
        trim = (1 - frame / picture) / 2
        sides = {"l": trim, "r": trim}
    else:  # taller: take the middle of its height
        trim = (1 - picture / frame) / 2
        sides = {"t": trim, "b": trim}
    rect = etree.SubElement(fill, f"{{{_A}}}srcRect")
    for side, share in sides.items():
        rect.set(side, str(int(round(share * 100000))))
    fill.insert(list(fill).index(fill.find(f"{{{_A}}}blip")) + 1, rect)


def _check_shape(shape, image: Path, how: str) -> None:
    """Refuse a picture whose proportions the frame cannot hold either way.

    A portrait slot and a landscape figure is a layout decision, not a fitting one:
    contained, the figure is a strip in the middle of an empty frame; covered, most of it
    is cropped away. Both were shipped. So the numbers are stated and the choice goes
    back -- `place(shape, (left, top, width, height))` reshapes the frame, another
    prototype may have a landscape slot, and `drop` plus a shape of your own is always
    available.
    """
    size = _picture_size(image)
    if size is None or not shape.width or not shape.height:
        return
    width, height = size
    frame = shape.width / shape.height
    picture = width / height
    off = max(frame / picture, picture / frame)
    if off <= FIT_RATIO_LIMIT:
        return
    raise ValueError(
        f"{Path(image).name} is {width}x{height} ({picture:.2f} wide-to-tall) and this frame is "
        f"{shape.width / 914400:.2f}x{shape.height / 914400:.2f}in ({frame:.2f}) -- {off:.1f}x apart. "
        f"{'Contained' if how == 'contain' else 'Cropped'} it would "
        f"{'sit as a strip in an otherwise empty frame' if how == 'contain' else 'lose most of the figure'}."
        + _frames_on_page(shape)
        + " Give the frame the box the figure needs -- pictures={n: (image, (left, top, width, height))} in"
        " inches, or place(shape_at(slide, n), box) after adapt returns -- or adapt a prototype whose picture"
        " slot runs the other way, or drop this frame and add a picture of your own"
    )


def _frames_on_page(shape) -> str:
    """Every picture frame on this shape's page, so a refusal can be answered by picking one.

    A live run put a landscape figure into a 6.05x7.50in frame three times, which is the
    page's full-height backdrop photograph rather than its figure slot. The numbers in the
    message were about the frame it had named; what it needed was the two frames on the
    page and their proportions, which is one walk of the same page.
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


def _fit(shape, image: Path, how: str) -> None:
    """Crop the frame's content ("cover") or shrink the frame to the picture ("contain")."""
    size = _picture_size(image)
    if size is None:
        return
    width, height = size
    if not (width and height and shape.width and shape.height):
        return
    shape.crop_left = shape.crop_right = shape.crop_top = shape.crop_bottom = 0
    frame = shape.width / shape.height
    picture = width / height
    if how == "cover":
        if picture > frame:  # wider than the slot: take the middle of its width
            share = (1 - frame / picture) / 2
            shape.crop_left = shape.crop_right = share
        elif picture < frame:  # taller: take the middle of its height
            share = (1 - picture / frame) / 2
            shape.crop_top = shape.crop_bottom = share
        return
    # contain: the frame gives way, and it gives way about its own centre so the
    # composition around it does not shift.
    if picture > frame:
        tall = int(shape.width / picture)
        shape.top = int(shape.top + (shape.height - tall) / 2)
        shape.height = tall
    elif picture < frame:
        wide = int(shape.height * picture)
        shape.left = int(shape.left + (shape.width - wide) / 2)
        shape.width = wide


def replace_text(target, text: str, new: str | None = None) -> None:
    """Write new words into a shape, keeping how the template set them.

    Two forms, because both are what an author reaches for:

        replace_text(shape, "新文字")                # this shape
        replace_text(slide, "单击此处添加标题", "新文字")  # whatever on this page holds that

    The second was added after a live run wrote 479 lines against it -- twenty-five
    calls, every one `replace_text(slide, old, new)` -- and got
    `takes 2 positional arguments but 3 were given` from all of them. Finding the shape
    is the tedious half of the operation and the page already knows how; asking the
    author to walk the shapes first was the API making its own convenience the author's
    problem. Groups are searched, since that is where a template keeps its text.

    Assigning to `.text` drops every run property, so a heading returns at body size in
    body colour -- the page keeps its geometry and loses its typography, which reads as
    a worse bug than a missing page because it looks deliberate.
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
    lines = text.split("\n")
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


def _write(paragraph, line: str) -> None:
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
    if not runs:
        paragraph.text = line
        return
    runs[0].text = line
    for extra in runs[1:]:
        extra._r.getparent().remove(extra._r)


def drop_shape(shape) -> None:
    """Remove an element the page does not need. The commonest edit after text."""
    shape._element.getparent().remove(shape._element)


def adapt(presentation, prototype, texts=None, pictures=None, drop=(), keep=(), items=None, title=None, subtitle=None):
    """Clone `prototype` and fill it in, in one call.

    The four operations above are the vocabulary; this is the sentence an author
    actually wants to write, and it exists because the alternative measured badly. A
    live run received six template pages as source, wrote its own layout system
    instead, and never imported this module -- fifteen lines of clone, walk the
    shapes, match them up and replace is more work than drawing a rectangle, so the
    rectangle won and the template's design was lost.

    `texts` maps a shape's current text -- or its 1-based index among the shapes that
    have text -- to what should replace it. `pictures` maps the same keys to image
    paths. `drop` names shapes to remove, by the same keys.

    **Text this call does not name is emptied, and shapes are otherwise left alone.**
    A shape is the design; the words in it are the template's example copy. The
    earlier rule -- keep everything not named -- shipped a twelve-page deck where
    pages 2 through 11 each carried three untouched placeholders:

        单击此处添加文本单击此处添加文本单击此处添加文本单击此处添加文本
        单击此处添加长一点的副标题
        单击添加小标题

    Twenty-nine of them, mostly hidden behind the copy and the figures the author
    did write, and two showing: one at subtitle size in the middle of a page, one
    reduced to its last character behind a photograph. Emptied rather than deleted
    because the box may be part of the design (a tinted panel, a numbered circle),
    and an empty box shows nothing while a deleted one takes its panel with it.

    `items` fills the page's repeating unit -- its card row, its agenda list -- one
    entry per unit, and **deletes the units left over**. Six items on a page that
    ships eight slots leaves six, with the other two gone rather than emptied. Each
    entry is a list positional over the unit's text shapes (`None` keeps one as it is)
    or a dict keyed by the text a shape holds now. This is the operation these pages
    exist for: 75% of the example pages across 119 templates are built out of a
    repeated unit.

    `title` and `subtitle` write the page's heading rows without needing to know which
    shape they are. A live author went looking for exactly this: it called
    `inspect.signature(adapt)` for a `title` parameter, found none, filled the page with
    `items` alone -- which emptied the header, because text this call does not name is
    emptied -- then wrote "目录" and "Agenda" back as two new boxes over the clone, which
    is the one construction `template_underlay` refuses. The parameter it looked for now
    exists: the title is the page's own title placeholder, or the topmost line in the top
    third of it, which is what a reader would point at.

    `keep` names text to leave exactly as the template wrote it, by the same keys --
    for the step number in a circle, or a label the template owns.

    Groups are looked inside, which matters more than it sounds: a template page
    typically has two or three shapes at the top level and everything else one group
    down. A key that matches nothing raises, listing what the page does hold -- an
    unmatched key used to be skipped in silence, which is how a deck shipped with the
    placeholders its author had named still on the page.

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


def _heading_rows(slide, presentation):
    """The page's title and subtitle shapes, as a reader would point at them.

    The title placeholder when the page has one, and otherwise the topmost line in the
    top third -- which is the title on every template page measured. The subtitle is the
    next line below it in that same band.
    """
    band = (presentation.slide_height or 0) * 0.35
    found = {}
    for shape in _all_shapes(slide.shapes):
        if not getattr(shape, "has_text_frame", False) or shape.top is None:
            continue
        if shape.top > band or not shape.text_frame.text.strip():
            continue
        if getattr(shape, "is_placeholder", False) and "TITLE" in str(
            getattr(getattr(shape, "placeholder_format", None), "type", "")
        ):
            found.setdefault("title", shape)
            continue
        found.setdefault("_rows", []).append(shape)
    rows = sorted(found.pop("_rows", []), key=lambda shape: (shape.top, -(shape.width or 0)))
    if "title" not in found and rows:
        found["title"] = rows.pop(0)
    elif rows and found.get("title") is not None:
        rows = [shape for shape in rows if shape._element is not found["title"]._element]
    if rows:
        found["subtitle"] = rows[0]
    return found


def units(container):
    """The repeating units on a page: sibling groups built the same way.

    Measured across 119 real templates and their 1563 example pages: every single
    template ships pages built this way, 75% of all example pages have at least one
    repeating unit, and 77% have groups at all. A card row, an agenda list, a set of
    steps -- each is one small group repeated, `[number, heading]` or
    `[number, body, heading]`. The most common shapes of it are 3x2, 2x3, 4x2 and
    4x3, and one template's agenda page is 8x2.

    Returns a list of runs, each run being the sibling groups that share a signature,
    in page order. A page with nothing repeating returns [] -- 23% of example pages
    are flat, and on those `texts` is the whole story.
    """
    from collections import Counter

    runs = []

    def signature(group):
        return tuple(str(shape.shape_type) for shape in group.shapes)

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


def arrangement(run):
    """How a run of units is laid out: ("row"|"column"|"grid"|"irregular", rows, cols).

    Measured over the 1222 runs in 119 templates: 29% are a single row, 17% a single
    column, 25% a regular grid, and 29% follow no grid at all -- staggered, fanned,
    around a circle. Two thirds have even spacing along their main axis.

    That 29% is why nothing here re-flows a page by itself. Deleting two of eight
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
    """Each unit's (left, top, width, height) in inches, in page order."""
    found = []
    for unit in run:
        try:
            found.append((unit.left / 914400, unit.top / 914400, unit.width / 914400, unit.height / 914400))
        except TypeError:  # a unit with no geometry of its own
            return []
    return found


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
    """
    from pptx.util import Inches

    left, top, width, height = box
    unit.left, unit.top = Inches(left), Inches(top)
    unit.width, unit.height = Inches(width), Inches(height)
    return unit


def fill(run, items):
    """Fill a run of repeating units with `items`, and delete the ones left over.

    This is the operation a template page is *for*. A page ships eight agenda slots
    and the deck has six sections: the six get filled and the seventh and eighth are
    removed, group and all, rather than left holding an empty circle -- which is what
    a live deck shipped, because writing "" into a slot empties its text and leaves
    its numbered bubble sitting there.

    Each item is either a list, positional over the unit's text shapes with `None`
    meaning "leave this one alone", or a dict keyed by the text a shape currently
    holds. A list shorter than the unit leaves the rest alone, the same as `None`
    would -- a unit often holds a shape the author has no opinion about. `items` longer than the run raises: a page with four slots cannot show
    six points, and quietly dropping two of them is the failure this is here to stop.

    One value is not taken literally: **an empty string written over a unit's number
    restates the number for its new position** rather than emptying it. A template's
    agenda numbers its slots 01 to 08; a deck with six sections that wrote "" into
    that shape shipped six blank circles, which looks worse than the template it came
    from. Measured on a live run, where the author spent three requests trying to work
    out which of the unit's two shapes was the number -- `["...", "01"]`, then
    `["...", None]`, then `["...", ""]` -- and shipped the one that blanked them. The
    zero padding is the template's own: 01 stays two digits, 1 stays one.

    Returns the shapes it wrote, which is what `adapt` needs to know they are spoken
    for -- without it, the pass that empties unnamed text would empty these too.
    """
    written = []
    if len(items) > len(run):
        raise ValueError(
            f"this page repeats {len(run)} units and {len(items)} items were given. "
            f"Use a prototype with more slots, split the content over two pages, or say less"
        )
    for position, (unit, item) in enumerate(zip(run, items), start=1):
        frames = [s for s in _all_shapes(unit.shapes) if getattr(s, "has_text_frame", False)]
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
            # Neither joining the extras onto the last slot nor cloning a slot for them is
            # this function's call to make: the first sets body copy at heading size, the
            # second guesses where the new shape goes, and both were tried and made the
            # page worse. What each unit holds is stated here so the author can choose --
            # a prototype with more slots, one fewer point, or a shape of their own after
            # `adapt` returns.
            raise ValueError(
                f"a unit on this page holds {len(frames)} text shape(s) and {len(item)} values were given: "
                + ", ".join(repr(_head(shape)) for shape in frames)
                + ". Give one value per shape (None keeps one as it is), pick a prototype whose units hold "
                "more, or add your own shape to the slide after adapt returns"
            )
        for index, shape in enumerate(frames):
            # Short of the unit's shapes means "I have nothing to say about the rest",
            # not "empty them". The zip that used to be here stopped at the shorter
            # list and left the remainder unclaimed, so the pass that empties unnamed
            # text emptied them: a real template's agenda unit holds a title box, the
            # folder shape and its number, an author gave two values, and all eight
            # numbers came off the page.
            value = item[index] if index < len(item) else None
            if value is None:
                # Claimed even though nothing is written: `adapt` empties every text
                # it was not told about, so a shape left out of `written` is a shape
                # emptied a moment later -- which made `None` do exactly what "" did.
                # A live author tried `["...", "01"]`, `["...", None]` and `["...", ""]`
                # in three consecutive requests looking for the one that kept the
                # template's number, and none of the three did.
                written.append(shape)
                continue
            if not str(value).strip():
                restated = _renumbered(shape.text_frame.text, position)
                if restated is not None:
                    replace_text(shape, restated)
                    written.append(shape)
                    continue
            replace_text(shape, value)
            written.append(shape)
    for spare in run[len(items) :]:
        drop_shape(spare)
    return written


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
    to that box first, and exists because of what a live run did with the refusal
    below: told its landscape figure could not go in a full-height portrait frame, it
    answered by permuting the shape number three times and never produced a deck. The
    escape the message named -- `place(shape, box)` -- needed a shape `adapt` had not
    handed back yet, so from inside one call there was no way out.

    A third element is the fit ("contain", "cover", "stretch") for the rare frame that
    wants cropping rather than shrinking.
    """
    if isinstance(value, (tuple, list)):
        if len(value) not in (2, 3):
            raise ValueError(
                f"a picture is an image, (image, box) or (image, box, fit), not {len(value)} values. "
                "A box is (left, top, width, height) in inches"
            )
        box = tuple(float(number) for number in value[1])
        if len(box) != 4:
            raise ValueError(f"a box is (left, top, width, height) in inches, not {value[1]!r}")
        return Path(value[0]), box, str(value[2]) if len(value) == 3 else "contain"
    return Path(value), None, "contain"


def _all_shapes(shapes):
    """Every shape on the page, including the ones inside groups.

    A template's content is mostly inside groups, and this was measured the hard way:
    the page a live deck adapted had two shapes at the top level and fourteen with
    text one group down -- the four cards, their numbers, their headings and their
    body copy. `slide.shapes` does not descend, so `adapt` could not see thirteen of
    the fourteen placeholders, replaced none of them, and said nothing about it. The
    deck shipped with "单击此处添加文本" on ten pages while its author's own
    `texts={...}` had named exactly those strings.
    """
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _all_shapes(shape.shapes)
        else:
            yield shape


def _advice(missed, texts, pictures, images, frames):
    """The sentence that turns a listing into a fix.

    Both halves are from live runs. One asked for `pictures={2: ...}` on a page with no
    picture frame anywhere -- the listing said "[2] shape" twenty-two times and never
    said the obvious thing. Another emptied four agenda slots by passing `''` to each,
    which clears their text and leaves four numbered bubbles on the page.
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
    alternative -- counting text frames for `texts`, pictures for `pictures` -- gave
    three numberings for one page, and a live author wrote `texts={15: ...}` from the
    reference against a page whose text frames stopped at fourteen.
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
