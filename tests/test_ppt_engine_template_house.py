"""What a page borrows from a template it is not cloning.

The content pages of a template stopped being prototypes, so what a composed page
keeps of the house has to be measurable: where the title row sits, the type ladder,
the face, the area the template stays inside, and the layout its background lives on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven_ppt.services.template.house import house_style
from raven_ppt.services.template.menu import menu

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
    """A box takes (x0, y0, x1, y1) and the measurement is (left, top, width, height): one
    subtraction, done here, because a model doing it in its head is a page half an inch off
    the grid. It is done for every box in the brief and not only for the paste-able line:
    reporting the size one line above a `Box.corners(...)` built from the same rectangle put
    two silently different readings of it a line apart, and one live run read the whole call
    by the two numbers they share and then wrote a size into a box of its own."""
    house = house_style(_template(tmp_path / "t.pptx"))

    assert house is not None
    brief = house.brief()
    assert brief["title_row_box_corners_in"] == [0.72, 0.4, 12.62, 1.3], "two corners, not a size"
    assert brief["face"] == "Arial"
    corners = brief["body_area_corners_in"]
    assert f"Box.corners({corners[0]:g}, {corners[1]:g}, {corners[2]:g}, {corners[3]:g})" in brief["body_area_as_code"]
    left, top, width, height = house.body_area
    assert corners == [left, top, round(left + width, 2), round(top + height, 2)], (
        "the far corner, the subtraction already done"
    )
    row = brief["title_row_box_corners_in"]
    assert row[2] > row[0] and row[3] > row[1], "a far corner reads past its origin, a size need not"
    assert not any(key.endswith("_box_in") or key in {"safe_area_in", "body_area_in"} for key in brief), (
        f"no bare size ships beside the corners: {sorted(brief)}"
    )


def _anchored_template(path: Path) -> Path:
    """A template whose master anchors its title placeholder to the bottom of the row.

    Which is what a real one does: `blue_minimal_general_analysis.pptx` declares
    `anchor="b"` on the master's title and nothing on the layouts or the slides, so
    every reading taken off a slide alone says "nothing declared" for the one setting
    that decides where the title's ink lands.
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt

    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    presentation = Presentation()
    for master in presentation.slide_masters:
        for placeholder in master.placeholders:
            if "TITLE" in str(placeholder.placeholder_format.type):
                placeholder.text_frame._txBody.find(f"{A}bodyPr").set("anchor", "b")
    cover = presentation.slides.add_slide(presentation.slide_layouts[0])
    cover.shapes.title.text = "封面标题"
    for index in range(3):
        page = presentation.slides.add_slide(presentation.slide_layouts[5])
        page.shapes.title.text = f"第 {index + 1} 页的标题"
        body = page.shapes.add_textbox(Inches(0.72), Inches(2.0), Inches(11.9), Inches(4.0))
        run = body.text_frame.paragraphs[0].add_run()
        run.text = f"单击此处添加文本，第 {index + 1} 页的示例正文，足够长以算作正文而不是标记"
        run.font.size = Pt(18)
    presentation.save(str(path))
    return path


def test_the_title_row_carries_the_anchor_the_template_inherits(tmp_path: Path) -> None:
    """The box says where the row is; the anchor says where the ink lands in it.

    Both are the template's decision and only one of them used to be reported. The two
    readings of one 0.98in row are 0.42in apart on the render -- measured at 150 DPI on
    a bundled template, cloned title ink at y=99px against composed at y=36px with the
    same 55px glyph -- and `write`'s default is the wrong one of the two.
    """
    house = house_style(_anchored_template(tmp_path / "anchored.pptx"))

    assert house is not None
    assert house.title is not None
    assert house.title.anchor == "bottom", "the master declares it and nothing below overrides it"
    assert "anchored bottom" in house.title.line()
    assert 'anchor="bottom"' in house.brief()["title_row_as_code"]


def test_a_composed_page_that_anchors_its_title_elsewhere_is_reported(tmp_path: Path) -> None:
    """And the same reading, held against a deck that did not take it."""
    from raven_ppt.services.gates.house_style import title_row_findings

    template = _anchored_template(tmp_path / "anchored.pptx")
    house = house_style(template)
    assert house is not None and house.title is not None
    left, top, width, height = house.title.box

    from pptx import Presentation
    from pptx.enum.text import MSO_ANCHOR
    from pptx.util import Inches, Pt

    for anchor, expected in ((MSO_ANCHOR.TOP, ["title_row"]), (MSO_ANCHOR.BOTTOM, [])):
        presentation = Presentation()
        page = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = page.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        box.text_frame.vertical_anchor = anchor
        run = box.text_frame.paragraphs[0].add_run()
        run.text = "自绘页的标题"
        run.font.size = Pt(28)
        deck = tmp_path / f"deck-{anchor}.pptx"
        presentation.save(str(deck))

        findings = title_row_findings(deck, template)

        assert [finding.kind for finding in findings] == expected, anchor
        if findings:
            assert "anchored top" in findings[0].message
            assert findings[0].detail["house_anchor"] == "bottom"


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


def _deck_with(path: Path, *, title_top: float, kicker: bool) -> Path:
    """One composed page whose title sits at `title_top`, optionally under a kicker."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    if kicker:
        eyebrow = page.shapes.add_textbox(Inches(0.72), Inches(0.14), Inches(11.9), Inches(0.2))
        run = eyebrow.text_frame.paragraphs[0].add_run()
        run.text = "01 问题：任务碎片化"
        run.font.size = Pt(12)
    heading = page.shapes.add_textbox(Inches(0.72), Inches(title_top), Inches(11.9), Inches(0.58))
    run = heading.text_frame.paragraphs[0].add_run()
    run.text = "四类任务的差别，主要在目标怎么定义"
    run.font.size, run.font.bold = Pt(27), True
    presentation.save(str(path))
    return path


def test_a_kicker_over_the_title_is_not_the_title(tmp_path: Path) -> None:
    """The topmost text in the title band is not necessarily the title.

    A page that sets a kicker over its heading -- "01 问题" above "四类任务的差别" --
    puts the kicker 0.29in above where the template's title sits. Reading that as the
    title reported eight pages of a live deck for a title row that was in exactly the
    right place, and left the author nothing it could fix.
    """
    from raven_ppt.services.gates.house_style import title_row_findings

    template = _template(tmp_path / "template.pptx")
    deck = _deck_with(tmp_path / "deck.pptx", title_top=0.4, kicker=True)

    assert title_row_findings(deck, template) == []


def test_a_title_that_really_drifted_is_still_reported(tmp_path: Path) -> None:
    """And the check still bites -- picking the tallest wide shape must not become
    picking whichever shape makes the page pass."""
    from raven_ppt.services.gates.house_style import title_row_findings

    template = _template(tmp_path / "template.pptx")
    deck = _deck_with(tmp_path / "deck.pptx", title_top=1.6, kicker=True)

    findings = title_row_findings(deck, template)

    assert [finding.kind for finding in findings] == ["title_row"]
    assert findings[0].detail["at"][1] == pytest.approx(1.6, abs=0.01)


def test_a_template_accent_too_pale_to_write_with_gets_one_that_reads(tmp_path: Path) -> None:
    """An accent is mixed towards the background to make `accent_soft`, so the two
    share a hue and the accent cannot be read on its own tint. One template's #78A4AF
    measures 2.33:1 on its own #E7EFF1 and 2.69:1 on white -- there is no ground in
    that deck it reads on, so a page that used the theme's own colour for a key
    number was reported for contrast every time, with no colour to move to.
    """
    from raven_ppt.services.template.theme import _contrast, _readable

    pale, tint, ink = "#78A4AF", "#E7EFF1", "#000000"
    assert _contrast(pale, tint) < 3.0, "the case has to still bite"

    written = _readable(pale, tint, ink)

    assert _contrast(written, tint) >= 3.0
    assert written != pale


def test_an_accent_that_already_reads_is_left_exactly_as_the_template_set_it() -> None:
    """Darkening a strong accent would put a colour in the deck that is in nobody's
    house style."""
    from raven_ppt.services.template.theme import _readable

    strong = "#1A4C8B"
    assert _readable(strong, "#FFFFFF", "#000000") == strong
