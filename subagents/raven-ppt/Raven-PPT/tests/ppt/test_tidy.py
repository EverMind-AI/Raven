"""The corrections every deck gets, and the things they must leave alone.

All three defects were measured on delivered decks and none of them shows up in a
render, so this file is the only place they can be caught: the pipeline judges decks
by rendering them, and LibreOffice draws all three as if they were fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pptx", reason="ppt extra not installed")

from pptx import Presentation
from pptx.util import Inches

from raven.ppt.services.tidy import EDGES, GALLERY_STYLE, NO_EFFECT, tidy

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _order(cell) -> list[str]:
    properties = cell._tc.find(f"{_A}tcPr")
    return [element.tag.replace(_A, "") for element in properties] if properties is not None else []


def _styles(path: Path) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    """(shape name, ((child tag, idx), ...)) for every `p:style` in the deck, groups walked into."""
    from raven.ppt.services.measure.geometry import iter_shapes

    found = []
    for slide in Presentation(str(path)).slides:
        for shape in iter_shapes(slide.shapes):
            for style in shape._element.findall(f"{_P}style"):
                found.append((shape.name, tuple((c.tag.replace(_A, ""), c.get("idx")) for c in style)))
    return found


def test_an_empty_placeholder_goes_and_a_filled_one_stays(tmp_path: Path) -> None:
    """Ten of thirteen pages of a delivered deck carried an empty title placeholder,
    every one at the (0.72, 0.14) 11.88x0.98in the template puts its title row at.
    Invisible in the render and in every measurement taken off it; in PowerPoint,
    ten pages with a frame and "Click to add title" across the top.
    """
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    layout = presentation.slide_layouts[1]  # title and content: two placeholders
    written = presentation.slides.add_slide(layout)
    written.placeholders[0].text_frame.text = "架构：Backbone → 时序颈 → 共享解码器"
    presentation.slides.add_slide(layout)
    built = tmp_path / "deck.pptx"
    presentation.save(str(built))

    changed = tidy(built)
    assert len(changed) == 3, changed  # one page's title kept, three placeholders empty
    assert all("Click to add" in line for line in changed)

    after = Presentation(str(built))
    assert [shape.text_frame.text for shape in after.slides[0].shapes] == ["架构：Backbone → 时序颈 → 共享解码器"]
    assert len(after.slides[1].shapes) == 0


def test_a_placeholder_holding_a_picture_stays(tmp_path: Path) -> None:
    """A photograph in a body placeholder is a `p:sp` with a blip fill and no text at
    all, so a check that only asked about words would delete the picture.
    """
    from lxml import etree
    from PIL import Image

    photo = tmp_path / "photo.png"
    Image.new("RGB", (600, 400), (120, 90, 60)).save(photo)

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    shape = slide.placeholders[1]
    _, relationship = shape.part.get_or_add_image_part(str(photo))
    rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    fill = etree.SubElement(shape._element.spPr, f"{_A}blipFill")
    etree.SubElement(fill, f"{_A}blip").set(f"{{{rels}}}embed", relationship)
    built = tmp_path / "deck.pptx"
    presentation.save(str(built))

    tidy(built)
    kept = Presentation(str(built)).slides[0].shapes
    assert len(kept) == 1, "the picture placeholder was deleted for having no text"


def test_the_gallery_table_style_comes_off_and_the_borders_go_in_order(tmp_path: Path) -> None:
    """What PowerPoint sees that this pipeline cannot.

    `add_table` stamps "Medium Style 2 - Accent 1" on every table and there is no
    python-pptx API to take it off, so a deck whose cells are all styled explicitly
    still opens blue-banded wherever a cell is silent. And a program that writes the
    four border elements in any order but lnL, lnR, lnT, lnB is not writing the
    format: four delivered decks carried `lnB, lnT, lnR, lnL` in all 175 cells,
    every render said they were clean, and PowerPoint dropped the properties and
    drew the gallery style underneath.
    """
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(6), Inches(1.5)).table
    for row in table.rows:
        for cell in row.cells:
            properties = cell._tc.get_or_add_tcPr()
            for edge in EDGES:  # reversed, the way the shipped helper wrote them
                element = properties.makeelement(f"{_A}{edge}", {})
                element.append(element.makeelement(f"{_A}noFill", {}))
                properties.insert(0, element)
    built = tmp_path / "deck.pptx"
    presentation.save(str(built))
    assert _order(table.cell(0, 0)) == ["lnB", "lnT", "lnR", "lnL"]

    changed = tidy(built)
    assert any("gallery table style" in line for line in changed), changed
    assert any("4 cells" in line for line in changed), changed

    after = Presentation(str(built)).slides[0].shapes[0].table
    assert _order(after.cell(0, 0)) == list(EDGES)
    assert list(after._tbl.iter(f"{_A}tableStyleId")) == []


def test_a_templates_own_table_style_is_left_on(tmp_path: Path) -> None:
    """Only the one value python-pptx stamps is taken off. A table cloned out of a
    template carries the style its designer chose, and that one is the deck's.
    """
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(6), Inches(1.5)).table
    for element in table._tbl.iter(f"{_A}tableStyleId"):
        element.text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"  # "No Style, No Grid"
    built = tmp_path / "deck.pptx"
    presentation.save(str(built))

    tidy(built)
    after = Presentation(str(built)).slides[0].shapes[0].table
    assert [e.text for e in after._tbl.iter(f"{_A}tableStyleId")] == ["{2D5ABB26-0587-4C30-8999-92F81FD0307C}"]


def test_the_style_matched_is_the_one_python_pptx_writes() -> None:
    """The constant is the value, so a python-pptx that changed its default would
    fail here rather than silently ship blue tables again.
    """
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(6), Inches(1.5)).table
    written = [(e.text or "").upper() for e in table._tbl.iter(f"{_A}tableStyleId")]
    assert written == [GALLERY_STYLE]


def test_the_theme_s_drop_shadow_comes_off_everything_a_program_drew(tmp_path: Path) -> None:
    """The one defect corrected here that a render does show, and it showed on every page.

    `add_shape`, `add_connector` and `convert_to_shape` each stamp a `p:style` whose
    `a:effectRef` names one of the theme's effects, and on the templates in use that
    is a drop shadow. `shape.shadow.inherit = False` does not help -- it writes an
    empty `a:effectLst`, and a renderer resolves the reference separately from the
    list -- so every plane, rule and mark came out with a grey shadow down its right
    side while the program read as though shadows were off.

    The layout helpers strip their own style, which covers the pages that used them.
    This covers the two they cannot: a page that reached for `add_shape` directly,
    which is what `template/decompile.py` hands the author as the reference, and a
    shape inside a group, which nothing walking the top level would ever see.
    """
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    drawn = slide.shapes.add_shape(MSO_SHAPE.CHEVRON, Inches(1), Inches(1), Inches(2), Inches(1))
    drawn.shadow.inherit = False  # the call an author reaches for, which is not enough
    slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(1), Inches(3), Inches(4), Inches(3))
    builder = slide.shapes.build_freeform(Inches(1), Inches(4))
    builder.add_line_segments([(Inches(3), Inches(4)), (Inches(3), Inches(5))], close=False)
    builder.convert_to_shape()
    group = slide.shapes.add_group_shape()
    group.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(6), Inches(4), Inches(1), Inches(1))
    built = tmp_path / "deck.pptx"
    presentation.save(str(built))
    assert [idx for _, style in _styles(built) for tag, idx in style if tag == "effectRef"] == ["2", "1", "2", "2"]

    changed = tidy(built)
    assert len(changed) == 4, changed
    assert all("drop shadow" in line for line in changed)
    assert any("Rounded Rectangle" in line for line in changed), "the shape inside the group was not reached"

    after = _styles(built)
    assert len(after) == 4
    for name, style in after:
        assert dict(style)["effectRef"] == NO_EFFECT, f"{name} still refers to an effect"

    # Idempotent, which is what makes running it on every build safe.
    before = built.read_bytes()
    assert tidy(built) == ()
    assert built.read_bytes() == before


def test_the_style_s_line_fill_and_font_stay_where_the_designer_put_them(tmp_path: Path) -> None:
    """Only the effect goes, because a page cloned out of a template depends on the rest.

    `template/compose.py` copies a template's own slide into the deck, and the
    `p:style` on each of its shapes comes with it: 1048 of them across the ten
    shipped templates, carrying the line, fill and font references the page is
    designed out of. Deleting the element -- which is what the layout helpers do,
    correctly, because every caller there states all three explicitly -- would take
    the design off those pages along with the shadow. And `a:effectRef` cannot be
    deleted on its own either: `CT_ShapeStyle` requires all four children.
    """
    from pptx.enum.shapes import MSO_SHAPE

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(1), Inches(2), Inches(1))
    style = shape._element.find(f"{_P}style")
    for tag, idx in (("lnRef", "2"), ("fillRef", "1"), ("effectRef", "3"), ("fontRef", "major")):
        style.find(f"{_A}{tag}").set("idx", idx)
    built = tmp_path / "deck.pptx"
    presentation.save(str(built))

    assert len(tidy(built)) == 1
    assert _styles(built) == [
        ("Rectangle 1", (("lnRef", "2"), ("fillRef", "1"), ("effectRef", NO_EFFECT), ("fontRef", "major")))
    ]


def test_a_shape_style_that_already_names_no_effect_is_left_alone(tmp_path: Path) -> None:
    """`idx="0"` is the standard's "no style" and PowerPoint's own way of writing it:
    1044 of the 1048 `p:style` elements in the shipped templates already say it. A
    sweep that rewrote them would report thousands of corrections it did not make and
    would rewrite a deck it had nothing to do to.
    """
    from pptx.enum.shapes import MSO_SHAPE

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(1), Inches(2), Inches(1))
    shape._element.find(f"{_P}style").find(f"{_A}effectRef").set("idx", NO_EFFECT)
    built = tmp_path / "deck.pptx"
    presentation.save(str(built))
    before = built.read_bytes()

    assert tidy(built) == ()
    assert built.read_bytes() == before


def test_the_effect_reference_python_pptx_writes_is_the_one_this_corrects() -> None:
    """The constant is the behaviour, so a python-pptx that stopped stamping a shadow
    -- or started stamping a different one -- would fail here rather than quietly
    leave the sweep with nothing to find.
    """
    from pptx.enum.shapes import MSO_SHAPE

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(1), Inches(2), Inches(1))
    style = shape._element.find(f"{_P}style")

    assert style is not None, "python-pptx no longer stamps a shape style"
    assert [child.tag.replace(_A, "") for child in style] == ["lnRef", "fillRef", "effectRef", "fontRef"]
    assert style.find(f"{_A}effectRef").get("idx") != NO_EFFECT


def test_a_deck_with_nothing_to_correct_is_not_rewritten(tmp_path: Path) -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text_frame.text = "已经干净"
    built = tmp_path / "deck.pptx"
    presentation.save(str(built))
    before = built.read_bytes()

    assert tidy(built) == ()
    assert built.read_bytes() == before
