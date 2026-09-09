"""What the layout draws, which no other measurement here could see.

A master's and a layout's own shapes never reach `slide.shapes`. They render --
they are most of what a template looks like -- but every geometry check in this
package reads the slide, so all of them were blind to them. Two consequences, and
both were verified rather than reasoned about.

The harmless one: a band a corporate template paints across its header is invisible
to the band gate, so using the user's own template is never refused for the user's
own design.

The one this closes: a page can lay its copy straight across the illustration the
template paints and nothing measures a thing. Three real templates, three pages
each, built through the route -- the numbers landed unreadable over the artwork and
the deck published clean.

What is flagged is not copy over decoration, which the template does itself: a
cover sets its title over its own photograph, and that is the design. The template
says where it means copy to go, in the placeholders it provides. So the finding is
copy that lands on the art *and* outside every one of them -- the part of the page
the template kept clear.

Declared geometry rather than the render, so this works on a machine with no
LibreOffice. A wrapped line reaches lower than its box says, which makes this the
lower bound: what it reports is real, and it will miss a line that grew.
"""

from __future__ import annotations

from pathlib import Path

from raven_ppt.contracts import Finding, Severity

# Whether a shape sits where the template puts one is `adherence`'s question and its
# answer, down to the tolerance three live decks took to calibrate. Private and
# imported anyway, the way `contrast` and `type_size` already import them -- a second
# way of deciding what came from the template is how the two come to disagree.
from raven_ppt.services.measure.adherence import _PLACES, _matches, _pages
from raven_ppt.services.measure.geometry import Rect, has_text, is_filled, iter_shapes, pages, shape_rect_emu

# Below this a layout shape is a mark rather than a ground: a rule, a page number,
# a logo in the corner. Copy crossing one is not what this looks for.
_MIN_ART_AREA_EMU = 1_500_000_000.0

# How much of a text box has to sit on the art before it is worth reporting. A
# label whose corner clips a photograph is a different thing from a paragraph laid
# across one.
_ON_THE_ART = 0.35

# How much of a text box has to sit inside a placeholder for the template to have
# meant copy there. Not all of it, because an author writing its own page places
# copy near the placeholder rather than exactly in it.
_WHERE_MEANT = 0.6


def over_layout_art(pptx_path: Path, prototypes: Path | None = None) -> list[Finding]:
    """Copy laid over the layout's decoration, where the layout kept it clear.

    `prototypes` is the user's own template with its example pages, when a template was
    bound. Those pages say where it means copy to go every bit as much as a
    placeholder does, and they are the only ones that can speak for a cloned page --
    `clone` copies the prototype's own text boxes, which are not placeholders, so the
    layout has nothing to offer and every block on the page reads as laid over the art.
    Measured on the designers' own files: four of the twelve bundled templates report
    on themselves, 36 findings over 217 professionally drawn pages, and 21 of the 36
    are one template whose deck then inherited 17 of them.
    """
    found: list[Finding] = []
    stated = _stated(prototypes)
    for number, slide in pages(pptx_path):
        art, meant = _inherited(slide)
        if not art:
            continue
        for shape in iter_shapes(slide.shapes):
            if not has_text(shape) or not shape.text_frame.text.strip():
                continue
            box = shape_rect_emu(shape)
            if box.area <= 0:
                continue
            on_art = max((box.overlap(piece) for piece in art), default=0.0) / box.area
            if on_art < _ON_THE_ART:
                continue
            if any(box.overlap(place) / box.area >= _WHERE_MEANT for place in meant):
                continue
            if _matches(_placed(shape), stated):
                continue
            found.append(_clash(number, shape.text_frame.text.strip(), on_art))
    return found


def _stated(prototypes: Path | None) -> set[tuple[float, float, float, float]]:
    """Where the template's own pages put a shape, in inches.

    The licence is granted to a box that *is* one of these and not to a box that
    overlaps one. `clone` copies a prototype page's shapes across with their geometry
    intact, so a cloned block sits exactly where the template put it and an author's
    own block does not -- and overlap read the licence far wider than the argument
    for it. Measured with a 3.0x0.8in probe swept across `gold_panel_year_end_summary`
    under the overlap reading: its boxes excused 88% of the positions on the canvas,
    the excuse routinely being a cover title from page 1 with nothing to do with the
    page being judged. Positions and not text boxes only, because `adapt` may empty a
    cloned block and a block whose copy the author supplied is still the template's.
    """
    if prototypes is None or not Path(prototypes).is_file():
        return set()
    try:
        return {box for boxes in _pages(Path(prototypes)).values() for box in boxes}
    except Exception:  # noqa: BLE001 -- an unreadable template is not a measurement
        return set()


def _placed(shape) -> tuple[float, float, float, float]:
    """`shape`'s geometry in the shape `_matches` compares."""
    return (
        round((shape.left or 0) / _EMU_PER_INCH, _PLACES),
        round((shape.top or 0) / _EMU_PER_INCH, _PLACES),
        round((shape.width or 0) / _EMU_PER_INCH, _PLACES),
        round((shape.height or 0) / _EMU_PER_INCH, _PLACES),
    )


_EMU_PER_INCH = 914400.0


def _inherited(slide) -> tuple[list[Rect], list[Rect]]:
    """(the layout's decoration, the layout's placeholders), in EMU.

    Placeholders are excluded from the decoration and collected separately: they
    are not something on the page, they are where the template says copy goes.
    """
    try:
        layout = slide.slide_layout
    except Exception:  # noqa: BLE001 -- a slide may reference a layout that is gone
        return ([], [])
    art: list[Rect] = []
    meant: list[Rect] = []
    for shape in iter_shapes(layout.shapes):
        box = shape_rect_emu(shape)
        if getattr(shape, "is_placeholder", False):
            meant.append(box)
        elif box.area >= _MIN_ART_AREA_EMU and _is_ground(shape):
            art.append(box)
    return (art, meant)


def _is_ground(shape) -> bool:
    """Whether this shape is something copy would land *on* rather than beside."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if shape.shape_type in (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.GROUP):
        return True
    return is_filled(shape) and not has_text(shape)


def _clash(page: int, text: str, share: float) -> Finding:
    return Finding(
        kind="over_layout_art",
        severity=Severity.WARNING,
        page=page,
        message=(
            f'{round(share * 100)}% of "{text[:40]}" sits on decoration the layout draws, outside every '
            "placeholder it provides -- the template kept that part of the page clear. Move the block into "
            "the area the template leaves for copy, or onto a page whose layout has room"
        ),
        detail={"on_art": round(share, 2), "text": text[:80]},
    )
