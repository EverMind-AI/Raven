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
from raven_ppt.services.measure.geometry import Rect, has_text, is_panel, iter_shapes, pages, shape_rect_emu

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


def over_layout_art(pptx_path: Path) -> list[Finding]:
    """Copy laid over the layout's decoration, where the layout kept it clear."""
    found: list[Finding] = []
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
            found.append(_clash(number, shape.text_frame.text.strip(), on_art))
    return found


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
    return is_panel(shape) and not has_text(shape)


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
