"""What a page borrows from a template it is not cloning.

The content pages of a template stopped being prototypes, so what a composed page
keeps of the house has to be measurable: where the title row sits, the type ladder,
the face, the area the template stays inside, and the layout its background lives on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.services.template.house import house_style
from raven.ppt.services.template.menu import menu

pytest.importorskip("pptx")


def _template(path: Path, *, title_at: float = 0.72) -> Path:
    """A cover, a divider and three content pages that agree on where the title goes."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    cover = presentation.slides.add_slide(presentation.slide_layouts[0])
    cover.shapes.title.text = "封面标题"
    divider = presentation.slides.add_slide(presentation.slide_layouts[5])
    divider.shapes.title.text = "章节标题"
    for index in range(3):
        page = presentation.slides.add_slide(presentation.slide_layouts[5])
        heading = page.shapes.add_textbox(Inches(title_at), Inches(0.4), Inches(11.9), Inches(0.9))
        run = heading.text_frame.paragraphs[0].add_run()
        run.text = "单击此处添加页面标题"
        run.font.size, run.font.bold, run.font.name = Pt(28), True, "Arial"
        body = page.shapes.add_textbox(Inches(title_at), Inches(1.8), Inches(11.9), Inches(4.2))
        body_run = body.text_frame.paragraphs[0].add_run()
        body_run.text = f"单击此处添加文本，第 {index + 1} 页的示例正文，足够长以算作正文而不是标记"
        body_run.font.size, body_run.font.name = Pt(18), "Arial"
    presentation.save(str(path))
    return path


def test_the_title_row_is_where_the_content_pages_agree_it_is(tmp_path: Path) -> None:
    house = house_style(_template(tmp_path / "t.pptx"))

    assert house is not None
    assert house.title is not None
    assert house.title.box == (0.72, 0.4, 11.9, 0.9)
    assert house.title.pages == 3, "three content pages put it in the same place"
    assert house.title.size_pt == 28.0
    assert house.title.face == "Arial"


def test_the_structural_pages_are_named_and_left_out_of_the_measurement(tmp_path: Path) -> None:
    """The cover and the divider are cloned, so they are not what the style is measured
    from -- a cover's 44pt title is not the deck's title size."""
    house = house_style(_template(tmp_path / "t.pptx"))

    assert house is not None
    assert house.structural == {"cover": 1, "section": 2}
    assert house.content_pages == (3, 4, 5)


def test_the_type_ladder_comes_off_the_master(tmp_path: Path) -> None:
    """A template's example pages autofit their own filler down to sizes it does not
    mean; `titleStyle` and `bodyStyle` are what its designer wrote down."""
    house = house_style(_template(tmp_path / "t.pptx"))

    assert house is not None
    assert house.scale["body"] > 0
    assert house.scale["body"] >= house.scale.get("secondary", 0)
    assert house.faces == {"text": "Arial"}


def test_the_body_area_is_the_safe_area_under_the_title(tmp_path: Path) -> None:
    house = house_style(_template(tmp_path / "t.pptx"))

    assert house is not None
    assert house.safe is not None
    left, top, width, height = house.safe
    assert (left, top) == (0.72, 0.4)
    body = house.body_area
    assert body is not None
    assert body[1] > house.title.box[1] + house.title.box[3], "it starts under the title row"
    assert round(body[0], 2) == left


def test_the_brief_hands_the_author_a_line_it_can_paste(tmp_path: Path) -> None:
    """`ppt_layout.Box` takes (x0, y0, x1, y1) and this reports (left, top, width,
    height): one subtraction, done here, because a model doing it in its head is a
    page half an inch off the grid."""
    house = house_style(_template(tmp_path / "t.pptx"))

    assert house is not None
    brief = house.brief()
    assert brief["title_row_box_in"] == [0.72, 0.4, 11.9, 0.9]
    assert brief["face"] == "Arial"
    left, top, width, height = brief["body_area_in"]
    assert f"Box({left:g}, {top:g}, {left + width:g}, {top + height:g})" in brief["body_area_as_code"]


def test_a_template_with_no_content_pages_measures_nothing(tmp_path: Path) -> None:
    """A file of covers and dividers has no house style to lend, and saying so is
    different from reporting a title row measured off a cover."""
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    cover = presentation.slides.add_slide(presentation.slide_layouts[0])
    cover.shapes.title.text = "只有封面"
    path = tmp_path / "covers.pptx"
    presentation.save(str(path))

    house = house_style(path, menu(path))
    assert house is not None
    assert house.content_pages == ()
    assert house.title is None
    assert house.brief().get("title_row") is None
