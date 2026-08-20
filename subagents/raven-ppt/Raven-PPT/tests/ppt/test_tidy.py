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

from raven.ppt.services.tidy import EDGES, GALLERY_STYLE, tidy

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _order(cell) -> list[str]:
    properties = cell._tc.find(f"{_A}tcPr")
    return [element.tag.replace(_A, "") for element in properties] if properties is not None else []


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


def test_a_deck_with_nothing_to_correct_is_not_rewritten(tmp_path: Path) -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text_frame.text = "已经干净"
    built = tmp_path / "deck.pptx"
    presentation.save(str(built))
    before = built.read_bytes()

    assert tidy(built) == ()
    assert built.read_bytes() == before
