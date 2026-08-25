"""Where a shape actually is, which for anything grouped is not what it says.

Every geometric check in this package reads a rectangle off a shape, and for a
shape inside a group the numbers python-pptx hands back are in the group's own
coordinate space. Measured on a real template's agenda page: six section numbers
laid out two columns by three rows all reported at the same (7.67, 4.27) and 4.46in
wide -- so `covered_shape` said five of the six were completely hidden behind the
sixth, and refused to publish a deck whose page was fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pptx", reason="ppt extra not installed")

from lxml import etree
from pptx import Presentation
from pptx.util import Inches

from raven.ppt.services.measure.geometry import EMU_PER_INCH, page_box, shape_rect_emu

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _transform(group, *, at, size, child_at, child_size):
    """Give `group` the child space a designer's template gives it.

    Written after the children are in, not before: python-pptx recomputes a group's
    xfrm from its children every time one is added, and what it writes is the
    identity (off == chOff, ext == chExt). A real template's group is drawn at a
    different size from the space its children were laid out in, which is the whole
    reason a child's own numbers are not page numbers.
    """
    properties = group._element.find(f"{_P}grpSpPr")
    for existing in properties.findall(f"{_A}xfrm"):
        properties.remove(existing)
    frame = etree.SubElement(properties, f"{_A}xfrm")
    for tag, (x, y), keys in (
        ("off", at, ("x", "y")),
        ("ext", size, ("cx", "cy")),
        ("chOff", child_at, ("x", "y")),
        ("chExt", child_size, ("cx", "cy")),
    ):
        element = etree.SubElement(frame, f"{_A}{tag}")
        element.set(keys[0], str(int(x * EMU_PER_INCH)))
        element.set(keys[1], str(int(y * EMU_PER_INCH)))
    return group


def _deck():
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    return presentation, presentation.slides.add_slide(presentation.slide_layouts[6])


def test_a_shape_at_the_top_level_is_where_it_says() -> None:
    _, slide = _deck()
    shape = slide.shapes.add_textbox(Inches(2), Inches(1.5), Inches(3), Inches(0.5))

    box = page_box(shape)

    assert (round(box.x0, 3), round(box.y0, 3)) == (2.0, 1.5)
    assert (round(box.x1 - box.x0, 3), round(box.y1 - box.y0, 3)) == (3.0, 0.5)


def test_a_grouped_shape_is_moved_and_scaled_by_its_group() -> None:
    """The group is drawn at half the size of the space its children were laid out in,
    so a child at (1, 1) of that space lands half an inch into the group."""
    _, slide = _deck()
    group = slide.shapes.add_group_shape()
    child = group.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(0.5))
    _transform(group, at=(5.0, 3.0), size=(4.0, 2.0), child_at=(0.0, 0.0), child_size=(8.0, 4.0))

    box = page_box(child)

    assert (round(box.x0, 3), round(box.y0, 3)) == (5.5, 3.5)
    assert (round(box.x1 - box.x0, 3), round(box.y1 - box.y0, 3)) == (1.0, 0.25)
    # And the EMU form every gate reads goes through the same transform.
    emu = shape_rect_emu(child)
    assert round(emu.x0 / EMU_PER_INCH, 3) == 5.5


def test_a_child_space_with_an_offset_is_subtracted_first() -> None:
    """A template's group is as likely to start its child space at its own offset."""
    _, slide = _deck()
    group = slide.shapes.add_group_shape()
    child = group.shapes.add_textbox(Inches(3), Inches(2), Inches(1), Inches(1))
    _transform(group, at=(1.0, 1.0), size=(4.0, 4.0), child_at=(2.0, 2.0), child_size=(4.0, 4.0))

    box = page_box(child)

    assert (round(box.x0, 3), round(box.y0, 3)) == (2.0, 1.0)


def test_groups_inside_groups_compose() -> None:
    _, slide = _deck()
    outer = slide.shapes.add_group_shape()
    inner = outer.shapes.add_group_shape()
    child = inner.shapes.add_textbox(Inches(4), Inches(0), Inches(2), Inches(2))
    _transform(inner, at=(2.0, 2.0), size=(4.0, 4.0), child_at=(0.0, 0.0), child_size=(8.0, 8.0))
    _transform(outer, at=(4.0, 0.0), size=(4.0, 4.0), child_at=(0.0, 0.0), child_size=(8.0, 8.0))

    box = page_box(child)

    # inner puts the child at (2+2, 2+0) of the outer space, outer halves it again.
    assert (round(box.x0, 3), round(box.y0, 3)) == (6.0, 1.0)
    assert round(box.x1 - box.x0, 3) == 0.5


def test_a_shape_with_no_geometry_has_no_box(tmp_path: Path) -> None:
    """A placeholder inheriting its position from the layout states none of its own."""
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    inherited = [shape for shape in slide.shapes if shape.left is None]
    if not inherited:  # pragma: no cover -- depends on the bundled default template
        pytest.skip("this python-pptx default states every placeholder's position")

    assert page_box(inherited[0]) is None
    assert shape_rect_emu(inherited[0]).area == 0
    del tmp_path
