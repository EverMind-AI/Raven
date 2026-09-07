"""Working inside a user's template: preparing it, reading it, reusing its pages.

The measurements behind these tests were taken on the 193 templates the user
supplied, and two of them decide the design:

85% of a template's visual elements live on its example slides rather than on its
layouts, so those slides are the reference and `add_slide(layout)` is not. And two
thirds of those pages hold something python-pptx cannot write -- custom geometry,
a gradient, a fill at 60% opacity -- so cloning a page is not a convenience beside
the decompiler, it is the only way that part of a template reaches a deck at all.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

pytest.importorskip("pptx")

from raven_ppt.services.template import (  # noqa: E402
    clone_page,
    decompile,
    drop_shape,
    inspect_template,
    prepare,
    replace_picture,
    replace_text,
    strip_hidden,
)
from tests._ppt_engine_fixtures import deck, image, noise_image, noise_png, product_page, template_file  # noqa: F401


@pytest.fixture
def template(template_file) -> Path:
    return template_file()


# --- preparing ------------------------------------------------------------


def test_prepare_empties_the_example_slides_and_keeps_the_design(template: Path, tmp_path: Path):
    """The whole point: the master, theme, layouts and canvas come across; the
    stranger's content does not. Every template measured ships example slides, so
    an author that opened the original would produce them plus its own pages."""
    from pptx import Presentation

    before = inspect_template(template)
    prepared = prepare(template, tmp_path / "prepared.pptx")

    assert prepared is not None
    assert prepared.removed_slides == 2
    assert len(Presentation(str(prepared.path)).slides) == 0
    assert prepared.inventory.width_in == pytest.approx(13.333, abs=0.01)
    assert [layout.name for layout in prepared.inventory.layouts] == [layout.name for layout in before.layouts]


def test_prepare_leaves_the_users_file_alone(template: Path, tmp_path: Path):
    """It is their file, and a later run wants to prepare it again from clean."""
    from pptx import Presentation

    original = template.read_bytes()
    prepare(template, tmp_path / "prepared.pptx")

    assert template.read_bytes() == original
    assert len(Presentation(str(template)).slides) == 2


def test_prepare_declines_what_is_not_a_template(tmp_path: Path):
    """No template is a normal state -- most decks have none -- so this returns
    None rather than raising, and the destination is not left half-written."""
    (tmp_path / "notes.txt").write_text("this is not a deck")

    assert prepare(tmp_path / "notes.txt", tmp_path / "out.pptx") is None
    assert prepare(tmp_path / "missing.pptx", tmp_path / "out.pptx") is None
    assert not (tmp_path / "out.pptx").exists()


def test_strip_hidden_removes_the_pages_a_render_will_not_have(template: Path):
    """LibreOffice does not export a hidden slide, so it is in the file and not in
    the PDF. Every reader downstream numbers pages off the file, and one live run
    asked for thirteen pages of an eleven-page render, lost all eleven to the one
    out-of-range number, and told the author renders were unavailable on the host.
    119 of the 193 templates measured ship them."""
    from pptx import Presentation

    presentation = Presentation(str(template))
    presentation.slides[0]._element.set("show", "0")
    presentation.save(str(template))

    assert strip_hidden(template) == 1

    after = Presentation(str(template))
    assert len(after.slides) == 1
    assert "Section title" not in _texts(after.slides[0]), "the hidden page goes, not the last one"


def test_strip_hidden_leaves_a_template_without_any_alone(template: Path):
    """The common case is still a template with none, and it is not worth a rewrite."""
    before = template.read_bytes()

    assert strip_hidden(template) == 0
    assert template.read_bytes() == before


# --- reading it -----------------------------------------------------------


def test_inventory_describes_what_an_author_cannot_see(template: Path):
    inventory = inspect_template(template)

    assert inventory is not None
    assert inventory.example_slides == 2
    assert inventory.layouts
    assert inventory.theme_colours
    assert "Layouts:" in inventory.brief()


# --- decompiling ----------------------------------------------------------


def _run(source: str, prepared: Path, workdir: Path):
    """Execute the emitted reference the way an author would, and return the page.

    Against the prepared template rather than a blank presentation, because the
    reference opens with the `add_slide(prs.slide_layouts[n])` that most of the
    page's design actually arrives on -- an index that only means anything in the
    template it was read out of.
    """
    import os

    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    env = {
        "prs": Presentation(str(prepared)),
        "Inches": Inches,
        "Pt": Pt,
        "RGBColor": RGBColor,
        "MSO_SHAPE": MSO_SHAPE,
        "MSO_CONNECTOR": MSO_CONNECTOR,
        "MSO_ANCHOR": MSO_ANCHOR,
        "PP_ALIGN": PP_ALIGN,
    }
    here = os.getcwd()
    os.chdir(workdir)
    try:
        exec(source, env)  # noqa: S102 -- executing the reference is the assertion
    finally:
        os.chdir(here)
    return env["slide"]


def test_the_reference_runs(template: Path, tmp_path: Path):
    """The one assertion that matters. Four bugs in this decompiler -- a group's
    scale, a paragraph fetched before `frame.text` rebuilt the list, a colour read
    without the master's map, and a page whose whole design was on its layout --
    were all found by replaying the output and none by reading it."""
    prepared = prepare(template, tmp_path / "prepared.pptx")
    source = decompile(template, 0, images_dir=tmp_path / "img")

    assert prepared is not None and source is not None
    slide = _run(source.source, prepared.path, tmp_path / "img")
    assert len(slide.shapes) >= 4


def test_the_reference_opens_on_the_layout_the_page_was_drawn_on(template: Path, tmp_path: Path):
    """Where a template's cover keeps its illustration. None of a layout's own
    decoration reaches `slide.shapes`, so a reference that listed only the page's
    shapes handed the author a blank background and no way to know why."""
    source = decompile(template, 0, images_dir=tmp_path / "img")

    assert source is not None
    assert source.source.splitlines()[0].startswith("slide = prs.slides.add_slide(prs.slide_layouts[")


def test_type_the_template_states_on_its_layout_reaches_the_reference(tmp_path: Path, image):
    """A template that sets its heading once, on the layout, states nothing on the
    page -- so the page's own runs carry no size at all. Read only those and the
    reference says the cover heading has no type, beside a render where it is
    40pt."""
    from pptx import Presentation
    from pptx.util import Pt

    presentation = Presentation()
    layout = presentation.slide_layouts[0]
    layout.placeholders[0].text_frame.paragraphs[0].font.size = Pt(40)
    page = presentation.slides.add_slide(layout)
    page.placeholders[0].text_frame.text = "Inherited heading"
    path = tmp_path / "inheriting.pptx"
    presentation.save(str(path))

    source = decompile(path, 0)

    assert source is not None
    assert "Pt(40)" in source.source


def test_the_reference_states_what_it_cannot_draw(template: Path, tmp_path: Path):
    """Two thirds of real template pages hold something python-pptx cannot write.
    Approximating it silently would produce a page that looks finished and is
    wrong, with no way for the author to tell which elements were guesses."""
    source = decompile(template, 0, images_dir=tmp_path / "img")

    assert source is not None
    assert "a custom-drawn shape" in source.unredrawable
    assert "clone the page" in source.summary()


def test_a_run_of_custom_drawn_shapes_is_said_once(tmp_path: Path):
    """A Bauhaus contents page carries a hundred freeforms as pattern; two lines each
    made one page 12,800 characters of a reply that cuts at 16,000. Consecutive ones
    fold into a single line that keeps their ordinals and the box they span."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text_frame.text = "title"
    for n in range(3):
        builder = slide.shapes.build_freeform(Inches(1 + n), Inches(3), scale=914400)
        builder.add_line_segments([(1, 0), (1, 1), (0, 1)])
        builder.convert_to_shape()
    slide.shapes.add_textbox(Inches(1), Inches(5), Inches(4), Inches(1)).text_frame.text = "after"
    path = tmp_path / "pattern.pptx"
    prs.save(path)

    source = decompile(path, 0)

    assert source is not None
    lines = source.source.splitlines()
    folded = [line for line in lines if "custom-drawn shapes" in line]
    assert len(folded) == 1 and folded[0].startswith("# [2]-[4] 3 custom-drawn shapes between")
    assert "# [5]" in lines, "the shape after the run keeps its own ordinal"
    assert sum("custom-drawn shape at" in line for line in lines) == 0


def test_the_reference_names_the_imports_its_code_needs(template: Path, tmp_path: Path):
    source = decompile(template, 0, images_dir=tmp_path / "img")

    assert source is not None
    head = source.summary()
    assert "from pptx.util import Inches" in head
    assert "from pptx.enum.text import MSO_ANCHOR" in head


def test_the_reference_keeps_the_typography_the_template_set(template: Path, tmp_path: Path):
    """Anchor and alignment are 30% and 14% of the shapes measured. Dropped, every
    label sits at the top-left of a box the template centres in, and the page reads
    as slightly fallen rather than as wrong."""
    source = decompile(template, 0, images_dir=tmp_path / "img")

    assert source is not None
    assert "frame.vertical_anchor = MSO_ANCHOR.MIDDLE" in source.source
    assert "para.alignment = PP_ALIGN.CENTER" in source.source
    assert "Pt(32)" in source.source


def test_type_the_template_states_only_on_its_master_reaches_the_reference(tmp_path: Path):
    """The last rung of the chain, and on the templates measured it is where the
    other 38% of the type is: not one text shape on any page of any of them states
    its own size, 62% take it from the layout's placeholder and the rest from
    here."""
    from pptx import Presentation

    presentation = Presentation()
    master = presentation.slide_masters[0]._element
    title = master.find(
        "{http://schemas.openxmlformats.org/presentationml/2006/main}txStyles/"
        "{http://schemas.openxmlformats.org/presentationml/2006/main}titleStyle/"
        "{http://schemas.openxmlformats.org/drawingml/2006/main}lvl1pPr/"
        "{http://schemas.openxmlformats.org/drawingml/2006/main}defRPr"
    )
    assert title is not None
    title.set("sz", "5400")
    page = presentation.slides.add_slide(presentation.slide_layouts[5])
    page.placeholders[0].text_frame.text = "From the master"
    path = tmp_path / "mastered.pptx"
    presentation.save(str(path))

    source = decompile(path, 0)

    assert source is not None
    assert "Pt(54)" in source.source


def test_the_reference_keeps_rotation_and_connectors(template: Path, tmp_path: Path):
    source = decompile(template, 0, images_dir=tmp_path / "img")

    assert source is not None
    assert "panel.rotation = 15" in source.source
    assert "add_connector(MSO_CONNECTOR.STRAIGHT" in source.source


def test_a_freeform_is_named_rather_than_drawn_as_a_rectangle(template: Path, tmp_path: Path):
    """18% of the shapes measured are freeforms -- the swooshes and cut corners a
    design is recognisable by. `add_shape` would put a rectangle there, which reads
    as a bug in the deck rather than as a limit of the reference."""
    source = decompile(template, 0, images_dir=tmp_path / "img")

    assert source is not None
    assert "# a custom-drawn shape at" in source.source
    assert "MSO_SHAPE.RECTANGLE" not in source.source


def test_a_picture_comes_out_as_a_file_the_code_can_name(template: Path, tmp_path: Path):
    source = decompile(template, 0, images_dir=tmp_path / "img")

    assert source is not None
    assert source.picture_files
    assert (tmp_path / "img" / source.picture_files[0]).is_file()
    assert "add_picture(" in source.source


def test_a_group_reaches_page_coordinates(tmp_path: Path):
    """A group declares its own child space -- an offset *and* an extent. Applying
    only the offset put shapes at `Inches(21.61)` on a 13.33-inch canvas."""
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    group = page.shapes.add_group_shape()
    inner = group.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(1))
    inner.text_frame.text = "inside a group"
    path = tmp_path / "grouped.pptx"
    presentation.save(str(path))

    source = decompile(path, 0)

    assert source is not None
    assert "inside a group" in source.source
    placed = next(line for line in source.source.splitlines() if line.startswith("box = slide.shapes.add_textbox"))
    numbers = [float(value) for value in re.findall(r"Inches\(([-\d.]+)\)", placed)]
    assert numbers and all(0 <= value <= 13.5 for value in numbers)


def test_decompiling_what_is_not_there(template: Path):
    assert decompile(template, 99) is None
    assert decompile(Path("/nonexistent.pptx"), 0) is None


# --- reusing a page -------------------------------------------------------


def test_cloning_a_page_carries_its_pictures(template: Path, tmp_path: Path):
    """Copied shape XML refers to relationships by id, and the new slide has none
    of them, so every picture on a naive copy resolves to nothing -- verified as
    `no relationship with key 'rId4'`. This is why the operation exists."""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    source = Presentation(str(template))
    prepared = prepare(template, tmp_path / "prepared.pptx")
    assert prepared is not None
    target = Presentation(str(prepared.path))

    clone = clone_page(target, source.slides[0])
    out = tmp_path / "cloned.pptx"
    target.save(str(out))

    assert len(clone.shapes) == len(source.slides[0].shapes)
    reopened = Presentation(str(out)).slides[0]
    pictures = [s for s in reopened.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE]
    assert pictures and pictures[0].image.blob


def test_cloning_across_two_files_writes_one_package(template: Path, tmp_path: Path):
    """The normal case -- a page from the user's original into the prepared copy --
    and the one that quietly produces a broken file. Handing `add_slide` a layout
    another package owns writes that layout, its master and its theme into the zip
    a second time under names it already holds, and PowerPoint offers to repair
    what opens."""
    import zipfile

    from pptx import Presentation

    prepared = prepare(template, tmp_path / "prepared.pptx")
    assert prepared is not None
    target = Presentation(str(prepared.path))

    clone_page(target, Presentation(str(template)).slides[0])
    out = tmp_path / "cloned.pptx"
    target.save(str(out))

    names = zipfile.ZipFile(out).namelist()
    assert len(names) == len(set(names))
    assert len(Presentation(str(out)).slides) == 1


def test_replacing_a_picture_keeps_the_frame_the_template_chose(template: Path, tmp_path: Path, image):
    """Deleting the frame and adding another loses the crop, the outline, the
    shadow and the z-order. The frame is the design; only the pixels are content."""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    presentation = Presentation(str(template))
    page = presentation.slides[0]
    picture = next(s for s in page.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE)
    where = (picture.left, picture.top, picture.width, picture.height)
    before = picture.image.blob

    other = image("swap.png", (200, 40, 40))
    replace_picture(picture, other, fit="cover")
    out = tmp_path / "swapped.pptx"
    presentation.save(str(out))

    after = next(s for s in Presentation(str(out)).slides[0].shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE)
    assert (after.left, after.top, after.width, after.height) == where
    assert after.image.blob != before
    assert after.image.blob == other.read_bytes()


def test_replacing_text_keeps_how_the_template_set_it(template: Path, tmp_path: Path):
    """Assigning to `.text` drops every run property, so a heading comes back at
    body size in body colour -- the page keeps its geometry and loses its
    typography, which reads as worse than a missing page because it looks
    deliberate."""
    from pptx import Presentation

    presentation = Presentation(str(template))
    heading = next(s for s in presentation.slides[0].shapes if getattr(s, "has_text_frame", False))

    replace_text(heading, "Our results")
    out = tmp_path / "retitled.pptx"
    presentation.save(str(out))

    written = next(s for s in Presentation(str(out)).slides[0].shapes if getattr(s, "has_text_frame", False))
    run = written.text_frame.paragraphs[0].runs[0]
    assert written.text_frame.text == "Our results"
    assert run.font.size.pt == 32
    assert run.font.bold


def test_replacing_text_with_more_lines_than_the_template_had(template: Path, tmp_path: Path):
    """A template drawn with one line has to take three. A new paragraph is copied
    from the last rather than added blank, so it inherits the template's list style
    rather than a python-pptx default."""
    from pptx import Presentation

    presentation = Presentation(str(template))
    heading = next(s for s in presentation.slides[0].shapes if getattr(s, "has_text_frame", False))

    replace_text(heading, "one\ntwo\nthree")

    assert [p.text for p in heading.text_frame.paragraphs] == ["one", "two", "three"]
    assert heading.text_frame.paragraphs[2].runs[0].font.size.pt == 32


def test_replacing_text_leaves_no_line_break_of_the_templates_behind(template: Path):
    """A placeholder written over two lines is run, `<a:br/>`, run. Replacing it drops
    the second run and used to keep the break, so the new text carried a trailing
    empty line: one cover's title sat a line high inside a box that had grown a line
    taller than anything visible in it, and the text read back as "...方法\x0b".
    """
    from pptx import Presentation
    from pptx.oxml.ns import qn

    presentation = Presentation(str(template))
    heading = next(s for s in presentation.slides[0].shapes if getattr(s, "has_text_frame", False))
    paragraph = heading.text_frame.paragraphs[0]
    # The template's own two-line prompt: a break between two runs.
    paragraph._p.append(paragraph._p.makeelement(qn("a:br"), {}))
    second = copy.deepcopy(paragraph.runs[0]._r)
    paragraph._p.append(second)
    assert "\x0b" in heading.text_frame.text

    replace_text(heading, "TarViS：面向目标的视频分割统一方法")

    assert heading.text_frame.text == "TarViS：面向目标的视频分割统一方法"
    assert paragraph._p.findall(qn("a:br")) == []


def test_replacing_text_refuses_a_shape_that_holds_none(template: Path):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    presentation = Presentation(str(template))
    picture = next(s for s in presentation.slides[0].shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE)

    with pytest.raises(ValueError, match="holds no text"):
        replace_text(picture, "anything")


def test_dropping_what_the_page_does_not_need(template: Path):
    """The commonest edit after text: the template's page has six cards and this
    one makes four points."""
    from pptx import Presentation

    presentation = Presentation(str(template))
    page = presentation.slides[0]
    before = len(page.shapes)

    drop_shape(page.shapes[0])

    assert len(page.shapes) == before - 1


# --- repeating units, which is what a template page is made of ------------


def _card_page(path: Path, slots: int = 4, shapes_per_slot: int = 2):
    """A page built the way real templates are: one unit repeated in a row.

    Measured over 119 templates and their 1563 example pages: 77% of pages have
    groups, 75% have a unit repeated at least twice, and the most common shapes of
    it are 3x2, 2x3, 4x2 and 4x3. So this is the page shape the operations below
    exist for, not a contrivance.
    """
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    title = page.shapes.add_textbox(Inches(0.7), Inches(0.5), Inches(9.0), Inches(0.8))
    title.text_frame.text = "单击此处添加页面标题"
    for index in range(slots):
        group = page.shapes.add_group_shape()
        number = group.shapes.add_textbox(Inches(0.7 + index * 3.0), Inches(2.0), Inches(0.6), Inches(0.6))
        number.text_frame.text = f"0{index + 1}"
        heading = group.shapes.add_textbox(Inches(0.7 + index * 3.0), Inches(2.8), Inches(2.6), Inches(0.5))
        heading.text_frame.text = "单击添加小标题"
        if shapes_per_slot > 2:
            body = group.shapes.add_textbox(Inches(0.7 + index * 3.0), Inches(3.4), Inches(2.6), Inches(1.2))
            body.text_frame.text = "单击此处添加文本"
    presentation.save(str(path))
    return path


def test_a_unit_repeated_on_a_page_is_found(tmp_path: Path):
    from pptx import Presentation

    from raven_ppt.services.template import units

    page = Presentation(str(_card_page(tmp_path / "cards.pptx"))).slides[0]
    runs = units(page)
    assert [len(run) for run in runs] == [4]


def test_a_flat_page_repeats_nothing(tmp_path: Path):
    """23% of real example pages are flat, and on those `texts` is the whole story."""
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import units

    presentation = Presentation()
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    page.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text_frame.text = "alone"
    path = tmp_path / "flat.pptx"
    presentation.save(str(path))
    assert units(Presentation(str(path)).slides[0]) == []


def test_filling_fewer_items_than_slots_deletes_the_spares(tmp_path: Path):
    """The failure this closes: a live deck wrote "" into two agenda slots, which
    emptied their text and left two numbered bubbles sitting on the page."""
    from pptx import Presentation

    from raven_ppt.services.template import adapt, units

    source = Presentation(str(_card_page(tmp_path / "cards.pptx")))
    out = Presentation(str(tmp_path / "cards.pptx"))
    slide = adapt(
        out,
        source.slides[0],
        texts={"单击此处添加页面标题": "四类任务本质相同"},
        items=[["01", "视频实例分割"], ["02", "视频全景分割"], ["03", "半监督分割"]],
    )
    assert [len(run) for run in units(slide)] == [3], "the fourth unit is gone, not emptied"
    said = _texts(slide)
    assert "视频实例分割" in said and "半监督分割" in said
    assert "单击添加小标题" not in said, "no slot keeps its placeholder"
    assert "04" not in said


def test_more_items_than_slots_grows_the_run_rather_than_dropping_content(tmp_path: Path):
    """This used to refuse. Ten measured builds died on the refusal, nine of them one or
    two items over, and the authors then cloned units by hand; a regular row grows."""
    from pptx import Presentation

    from raven_ppt.services.template import adapt, units

    source = Presentation(str(_card_page(tmp_path / "cards.pptx")))
    out = Presentation(str(tmp_path / "cards.pptx"))

    slide = adapt(out, source.slides[0], items=[["0%d" % n, "点 %d" % n] for n in range(1, 7)])

    assert [len(run) for run in units(slide)] == [6]
    assert {"点 1", "点 6", "06"} <= _texts(slide), "nothing dropped, the new slots filled and numbered"


def test_text_inside_a_group_is_replaced_and_unnamed_text_is_emptied(tmp_path: Path):
    """77% of real example pages have groups, and `slide.shapes` does not descend
    into them -- so this was the reason a deck shipped with its author's own
    `texts={...}` keys still on the page as placeholders."""
    from pptx import Presentation

    from raven_ppt.services.template import adapt

    source = Presentation(str(_card_page(tmp_path / "cards.pptx", slots=2, shapes_per_slot=3)))
    out = Presentation(str(tmp_path / "cards.pptx"))
    slide = adapt(out, source.slides[0], texts={"单击此处添加页面标题": "标题", 3: "组内被按序号替换"})
    said = _texts(slide)
    assert "组内被按序号替换" in said, "an index reaches a shape inside a group"
    assert "单击添加小标题" not in said and "单击此处添加文本" not in said


def test_a_key_that_matches_nothing_says_what_the_page_holds(tmp_path: Path):
    from pptx import Presentation

    from raven_ppt.services.template import adapt

    source = Presentation(str(_card_page(tmp_path / "cards.pptx")))
    out = Presentation(str(tmp_path / "cards.pptx"))
    with pytest.raises(KeyError) as caught:
        adapt(out, source.slides[0], texts={"没有这段文字": "x"})
    assert "单击此处添加页面标题" in str(caught.value), "the refusal lists what is there"


def test_the_arrangement_of_a_run_is_reported_not_reflowed(tmp_path: Path):
    """29% of real runs follow no grid, so re-flowing on the engine's own initiative
    would destroy the arrangement a template was drawn with. It reports; `place` moves."""
    from pptx import Presentation

    from raven_ppt.services.template import arrangement, boxes, clone_page, place, units

    source = Presentation(str(_card_page(tmp_path / "cards.pptx")))
    out = Presentation(str(tmp_path / "cards.pptx"))
    slide = clone_page(out, source.slides[0])
    run = max(units(slide), key=len)
    assert arrangement(run) == ("row", 1, 4)
    place(run[0], (1.0, 5.0, 2.0, 1.0))
    assert [round(value, 2) for value in boxes(run)[0]] == [1.0, 5.0, 2.0, 1.0]


def _layout_box(x0: float, y0: float, x1: float, y1: float):
    """A `ppt_layout.Box`, taken from the projection an author's script imports.

    The grid module reaches the author as a file beside their script rather than as an
    import of this package, so the class that turns up at `place` is the projected one --
    which is the reason `place` recognises a box by its corners and not by its type.
    """
    from raven_ppt.services.assets.layout import layout_module_source

    namespace: dict = {}
    exec(compile(layout_module_source(), "ppt_layout.py", "exec"), namespace)
    return namespace["Box"].corners(x0, y0, x1, y1)


def _run_of(path: Path):
    from pptx import Presentation

    from raven_ppt.services.template import clone_page, units

    source = Presentation(str(path))
    out = Presentation(str(path))
    return max(units(clone_page(out, source.slides[0])), key=len)


def test_a_layout_box_reaching_place_is_the_two_corners_it_is(tmp_path: Path):
    """One word, two rectangles, and the wrong reading drew a frame off the page.

    `place` takes (left, top, width, height) and `ppt_layout.Box` is (x0, y0, x1, y1),
    and both are called `box`. Unpacked as a size,
    `place(unit, Box.corners(0.72, 1.24, 12.6, 6.7))` made a 12.6x6.7in frame on a
    13.33x7.5in page where those corners name an 11.88x5.46in one -- and it drew that
    frame in silence, which no render reports, because a box that is wrong is still a box.

    A Box says which of the two rectangles it is, so it is converted. Four bare numbers
    cannot, so they stay a size -- which is what every python-pptx call beside them takes.
    """
    from raven_ppt.services.template import boxes, place

    run = _run_of(_card_page(tmp_path / "cards.pptx"))

    place(run[0], _layout_box(0.72, 1.24, 12.6, 6.7))
    assert [round(value, 2) for value in boxes(run)[0]] == [0.72, 1.24, 11.88, 5.46]

    place(run[1], (0.72, 1.24, 12.6, 6.7))
    assert [round(value, 2) for value in boxes(run)[1]] == [0.72, 1.24, 12.6, 6.7]


def test_a_rectangle_in_emu_is_refused_rather_than_placed(tmp_path: Path):
    """`box.pptx()` is the third spelling of a rectangle and the only unreadable one.

    Its four numbers are python-pptx lengths, so `place(unit, box.pptx())` set a 12.6in
    frame to 11521440 inches and said nothing. A shape's own `.left` and `.width` arrive
    the same way, which is how an author copies one unit's geometry onto another.
    """
    from raven_ppt.services.template import place

    run = _run_of(_card_page(tmp_path / "cards.pptx"))

    with pytest.raises(ValueError) as refused:
        place(run[0], _layout_box(0.72, 1.24, 12.6, 6.7).pptx())
    said = str(refused.value)
    assert "EMU" in said and "914400" in said
    assert "0.72, 1.24, 11.88, 5.46" in said, "the inches those lengths stand for"

    other = run[1]
    with pytest.raises(ValueError, match="EMU"):
        place(run[0], (other.left, other.top, other.width, other.height))

    for neither in ((0.72, 1.24, 12.6), 5, ("a", "b", "c", "d")):
        with pytest.raises(ValueError, match="ppt_layout Box"):
            place(run[0], neither)


def test_the_boxes_of_a_run_are_sizes_and_say_so(tmp_path: Path):
    """`boxes` hands back what `place` takes, and the docstring is where that is settled.

    Read as corners, a 2.0in-wide unit at x=1.0 is a unit ending at x=2.0 -- 1.0in wide,
    half of what is on the page -- and the two readings are the same four numbers. So the
    order is written down where an author reads it rather than inferred from the values.
    """
    from raven_ppt.services.template import boxes, place

    run = _run_of(_card_page(tmp_path / "cards.pptx"))
    place(run[0], (1.0, 5.0, 2.0, 1.0))

    spot = boxes(run)[0]
    assert (round(spot[2], 2), round(spot[3], 2)) == (2.0, 1.0)
    assert round(run[0].width / 914400, 2) == 2.0, "the third number is a width, not a far edge"
    assert "(left, top, width, height)" in boxes.__doc__
    assert "ppt_layout.Box" in boxes.__doc__, "the other rectangle the word names"


def test_a_pictures_box_reshapes_the_frame_in_either_spelling(tmp_path: Path, image):
    """The reshape a fitting refusal offers, given as a size and as two corners.

    `pictures={n: (image, box)}` exists because a live run told its landscape figure
    could not go in a portrait frame answered by permuting the shape number three times.
    The box goes through the same reading as `place`: a Box is converted, four numbers are
    a size. Stretch, so the frame stays exactly where it was put and the assertion is
    about the box rather than about the fit.
    """
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.util import Inches

    from raven_ppt.services.template import adapt

    path = _card_page(tmp_path / "cards.pptx")
    once = Presentation(str(path))
    once.slides[0].shapes.add_picture(str(image("shot.png", (90, 90, 90))), Inches(1), Inches(4), width=Inches(3))
    once.save(str(path))

    def framed(box):
        source, out = Presentation(str(path)), Presentation(str(path))
        slide = adapt(out, source.slides[0], pictures={1: (str(image("fig.png", (10, 20, 30))), box, "stretch")})
        frame = next(s for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE)
        return [round(value / 914400, 2) for value in (frame.left, frame.top, frame.width, frame.height)]

    assert framed(_layout_box(0.8, 1.6, 8.2, 5.8)) == [0.8, 1.6, 7.4, 4.2]
    assert framed((0.8, 1.6, 7.4, 4.2)) == [0.8, 1.6, 7.4, 4.2]

    with pytest.raises(ValueError, match="EMU"):
        framed(_layout_box(0.8, 1.6, 8.2, 5.8).pptx())


def _texts(slide) -> set[str]:
    from raven_ppt.services.measure.geometry import iter_shapes

    return {
        " ".join(shape.text_frame.text.split())
        for shape in iter_shapes(slide.shapes)
        if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip()
    }


def test_the_reference_numbering_is_the_numbering_adapt_takes(tmp_path: Path):
    """One numbering for the page, printed where the author reads it.

    A live author read the reference, counted, and wrote `texts={15: ...}` against a
    page whose text frames stopped at fourteen: the reference numbered shapes and
    `adapt` numbered text frames. The two orders have to be the same walk.
    """
    import re

    from pptx import Presentation

    from raven_ppt.services.template import adapt
    from raven_ppt.services.template.compose import _all_shapes
    from raven_ppt.services.template.decompile import _flatten

    path = _card_page(tmp_path / "cards.pptx", slots=3, shapes_per_slot=3)
    source = decompile(path, 0)
    assert source is not None
    printed = [int(number) for number in re.findall(r"^# \[(\d+)\]", source.source, re.M)]
    assert printed == list(range(1, len(printed) + 1)), "the ordinals run 1..n with no gaps"

    page = Presentation(str(path)).slides[0]
    walked = list(_all_shapes(page.shapes))
    assert len(walked) == len(list(_flatten(page.shapes))) == len(printed)

    # And the ordinal reaches the shape the reference showed at that ordinal.
    wanted = next(index for index, shape in enumerate(walked, start=1) if shape.text_frame.text == "02")
    out = Presentation(str(path))
    slide = adapt(out, page, texts={wanted: "第二张卡"})
    assert "第二张卡" in _texts(slide)


def test_an_index_pointing_at_the_wrong_kind_of_shape_is_refused(tmp_path: Path, image):
    """Ambiguity refuses; a page with one picture does not.

    Two models each wrote `pictures={2: ...}` against a page whose picture was shape 3.
    Where there is exactly one frame the index cannot mean anything else, so it is read
    as that one -- refusing there cost a round to teach a number the page could supply.
    With two frames the index has to be right.
    """
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import adapt

    photo = image("shot.png", (90, 90, 90))
    path = _card_page(tmp_path / "cards.pptx")
    twice = Presentation(str(path))
    page = twice.slides[0]
    page.shapes.add_picture(str(photo), Inches(1), Inches(4), width=Inches(3))
    page.shapes.add_picture(str(photo), Inches(6), Inches(4), width=Inches(3))
    twice.save(str(path))

    source, out = Presentation(str(path)), Presentation(str(path))
    with pytest.raises(KeyError, match="picture|shape"):
        adapt(out, source.slides[0], pictures={2: str(photo)})


def test_one_picture_on_the_page_takes_whatever_index_was_meant(tmp_path: Path, image):
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import adapt

    photo = image("shot.png", (90, 90, 90))
    path = _card_page(tmp_path / "cards.pptx")
    once = Presentation(str(path))
    once.slides[0].shapes.add_picture(str(photo), Inches(1), Inches(4), width=Inches(3))
    once.save(str(path))

    source, out = Presentation(str(path)), Presentation(str(path))
    adapt(out, source.slides[0], pictures={2: str(image("other.png", (10, 20, 30)))})


def test_a_landscape_figure_in_a_portrait_slot_is_placed_with_a_warning(tmp_path: Path, image) -> None:
    """A layout decision, handed back rather than hidden.

    A template's portrait photo slot runs about 0.6 wide-to-tall and a paper's
    architecture figure about 2.4. Contained, the figure became a strip a quarter of the
    frame's height with empty space above and below; cropped, it would have lost its
    outer columns. A live page shipped the first. Only the author can decide which way
    the page should go, so the numbers go back with the ways out.
    """
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import replace_picture

    wide = tmp_path / "figure.png"
    Image.new("RGB", (2400, 1000), (40, 60, 200)).save(wide)
    presentation = Presentation()
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    frame = page.shapes.add_picture(str(image("slot.png", (9, 9, 9))), Inches(1), Inches(1), Inches(3.5), Inches(5.8))

    with pytest.warns(UserWarning, match="apart") as caught:
        replace_picture(frame, wide)

    said = str(caught[0].message)
    assert "Placed as asked" in said
    assert "sits as a strip" in said, "contained is the default fit, and the warning says what that does"
    assert frame.image is not None, "the picture was placed all the same"


def test_a_figure_near_its_frames_proportions_is_fitted(tmp_path: Path, image) -> None:
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import replace_picture

    near = tmp_path / "near.png"
    Image.new("RGB", (1600, 900), (40, 60, 200)).save(near)
    presentation = Presentation()
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    frame = page.shapes.add_picture(str(image("slot.png", (9, 9, 9))), Inches(1), Inches(1), Inches(6.0), Inches(4.0))

    replace_picture(frame, near)
    # contain gave way on the height and kept the centre
    assert round(frame.width / frame.height, 2) == round(1600 / 900, 2)


def test_adapt_writes_the_heading_rows_by_role(tmp_path: Path) -> None:
    """The parameter a live author went looking for.

    It called `inspect.signature(adapt)` for a `title`, found none, filled the page with
    `items` alone -- which empties the header, because text this call does not name is
    emptied -- and then wrote its heading back as two new boxes over the clone, which is
    the one construction `template_underlay` refuses. Naming the role is all it wanted.
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt

    from raven_ppt.services.template.compose import adapt

    source = Presentation()
    source.slide_width, source.slide_height = Inches(13.333), Inches(7.5)
    page = source.slides.add_slide(source.slide_layouts[5])
    page.shapes.title.text = "单击此处添加章节标题"
    under = page.shapes.add_textbox(Inches(0.7), Inches(1.3), Inches(6.0), Inches(0.5))
    run = under.text_frame.paragraphs[0].add_run()
    run.text = "单击此处添加副标题"
    run.font.size = Pt(20)
    template = tmp_path / "t.pptx"
    source.save(str(template))

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    slide = adapt(deck, Presentation(str(template)).slides[0], title="实验结果", subtitle="七个基准")

    said = [shape.text_frame.text.strip() for shape in slide.shapes if shape.has_text_frame]
    assert "实验结果" in said and "七个基准" in said
    assert "单击此处添加章节标题" not in said


def test_a_page_with_no_heading_row_says_so(tmp_path: Path) -> None:
    """A refusal rather than a silent miss: the author asked for something this page
    has nowhere to put."""
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template.compose import adapt

    source = Presentation()
    source.slide_width, source.slide_height = Inches(13.333), Inches(7.5)
    page = source.slides.add_slide(source.slide_layouts[6])
    page.shapes.add_textbox(Inches(1), Inches(5.0), Inches(4), Inches(0.5)).text_frame.text = "footnote"
    template = tmp_path / "t.pptx"
    source.save(str(template))

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    with pytest.raises(KeyError, match="title"):
        adapt(deck, Presentation(str(template)).slides[0], title="没有地方放")


def _written_slots(slide) -> dict:
    """What each of the clone's placeholders ended up holding, keyed by its role."""
    return {
        shape.placeholder_format.type: shape.text_frame.text.strip()
        for shape in slide.shapes
        if shape.is_placeholder and shape.has_text_frame
    }


def test_a_cover_heading_is_found_where_the_template_centred_it(tmp_path: Path) -> None:
    """The geometry of a real cover, and what reading the top third made of it.

    Measured across the sixteen bundled templates: the cover title is centred in the
    page, between 1.24in and 4.76in down a 7.5in canvas, and what sits in the top third
    is the presenter line and the date. So `title=` resolved to "Presenter name" on two
    covers and `subtitle=` raised on ten of them. This is the worst of those, with both
    chrome rows above the heading and the whole heading below the band.
    """
    from pptx import Presentation
    from pptx.enum.shapes import PP_PLACEHOLDER
    from pptx.util import Inches

    from raven_ppt.services.template.compose import adapt

    source = Presentation()
    source.slide_width, source.slide_height = Inches(13.333), Inches(7.5)
    page = source.slides.add_slide(source.slide_layouts[0])
    for slot, box, said in (
        (0, (0.72, 4.41, 8.0, 1.59), "棕色商务风工作总结汇报"),
        (1, (0.72, 6.06, 8.0, 0.65), "回望来路，蓄力前行，再谱新篇"),
    ):
        shape = page.placeholders[slot]
        shape.left, shape.top, shape.width, shape.height = (Inches(value) for value in box)
        shape.text_frame.text = said
    for left, said in ((0.72, "Presenter name"), (9.21, "20XX.XX.XX")):
        page.shapes.add_textbox(Inches(left), Inches(0.51), Inches(2.0), Inches(0.41)).text_frame.text = said
    template = tmp_path / "cover.pptx"
    source.save(str(template))

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    slide = adapt(deck, Presentation(str(template)).slides[0], title="七个基准上的结果", subtitle="与六个基线的对比")

    wrote = _written_slots(slide)
    assert wrote[PP_PLACEHOLDER.CENTER_TITLE] == "七个基准上的结果"
    assert wrote[PP_PLACEHOLDER.SUBTITLE] == "与六个基线的对比"
    assert "Presenter name" not in [shape.text_frame.text.strip() for shape in slide.shapes if shape.has_text_frame]


def test_a_section_divider_subtitle_is_the_line_under_its_title(tmp_path: Path) -> None:
    """The other half of the same measurement: every one of the sixteen dividers puts
    its one line under the title below the top third, so `subtitle=` raised KeyError on
    all sixteen. The line is 0 to 0.34in under the title's own box and at exactly its
    left edge, which is what makes the two of them one heading block."""
    from pptx import Presentation
    from pptx.enum.shapes import PP_PLACEHOLDER
    from pptx.util import Inches

    from raven_ppt.services.template.compose import adapt

    source = Presentation()
    source.slide_width, source.slide_height = Inches(13.333), Inches(7.5)
    page = source.slides.add_slide(source.slide_layouts[2])
    for slot, box, said in (
        (0, (0.72, 1.24, 8.0, 1.78), "单击此处添加章节标题"),
        (1, (0.72, 3.09, 8.0, 0.67), "单击此处添加章节页描述内容"),
    ):
        shape = page.placeholders[slot]
        shape.left, shape.top, shape.width, shape.height = (Inches(value) for value in box)
        shape.text_frame.text = said
    template = tmp_path / "section.pptx"
    source.save(str(template))

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    slide = adapt(deck, Presentation(str(template)).slides[0], title="实验设置", subtitle="数据、基线与指标")

    wrote = _written_slots(slide)
    assert wrote[PP_PLACEHOLDER.TITLE] == "实验设置"
    assert wrote[PP_PLACEHOLDER.BODY] == "数据、基线与指标"


def test_a_presenter_row_above_the_title_is_neither_heading(tmp_path: Path) -> None:
    """A closing page carries a title and two lines of chrome and no subtitle at all.

    Both facts have to survive: the title is the title however far down the page it is,
    and a page with no subtitle row refuses `subtitle=` rather than writing the author's
    line onto the presenter's name -- which is what six of the sixteen closing pages did.
    """
    from pptx import Presentation
    from pptx.enum.shapes import PP_PLACEHOLDER
    from pptx.util import Inches

    from raven_ppt.services.template.compose import adapt

    source = Presentation()
    source.slide_width, source.slide_height = Inches(13.333), Inches(7.5)
    page = source.slides.add_slide(source.slide_layouts[5])
    heading = page.placeholders[0]
    heading.left, heading.top, heading.width, heading.height = Inches(0.72), Inches(3.5), Inches(8.0), Inches(2.0)
    heading.text_frame.text = "谢谢观看"
    for left, said in ((0.72, "Presenter name"), (3.76, "20XX.XX.XX")):
        page.shapes.add_textbox(Inches(left), Inches(0.68), Inches(2.0), Inches(0.31)).text_frame.text = said
    template = tmp_path / "closing.pptx"
    source.save(str(template))

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    slide = adapt(deck, Presentation(str(template)).slides[0], title="谢谢")
    assert _written_slots(slide)[PP_PLACEHOLDER.TITLE] == "谢谢"

    with pytest.raises(KeyError, match="subtitle"):
        adapt(deck, Presentation(str(template)).slides[0], title="谢谢", subtitle="有问题欢迎交流")


def test_a_subtitle_stranded_mid_page_is_found_by_its_type_size(tmp_path: Path) -> None:
    """The last resort, and the tie that refuses it.

    One content page in the bundled sixteen sets its title at the very top and its
    subtitle at 4.20in, over a row of cards -- out of reach of the top third and too far
    under the title to be one block with it. Its size says what it is: 24pt against a
    column that declares nothing else. An agenda page reached the same way declares one
    size across eight numbered slots, and picking any of them would put the author's
    subtitle inside slot 01, so a tie is a refusal.
    """
    from pptx import Presentation
    from pptx.enum.shapes import PP_PLACEHOLDER
    from pptx.util import Inches, Pt

    from raven_ppt.services.template.compose import adapt

    def build(path: Path, sizes: tuple[int, ...]):
        source = Presentation()
        source.slide_width, source.slide_height = Inches(13.333), Inches(7.5)
        page = source.slides.add_slide(source.slide_layouts[5])
        heading = page.placeholders[0]
        heading.left, heading.top, heading.width, heading.height = Inches(0.72), Inches(0.14), Inches(8.0), Inches(0.98)
        heading.text_frame.text = "单击此处添加页面标题"
        for index, size in enumerate(sizes):
            box = page.shapes.add_textbox(Inches(0.72), Inches(4.2 + index * 0.9), Inches(6.0), Inches(0.62))
            run = box.text_frame.paragraphs[0].add_run()
            run.text = f"单击此处添加长一点的副标题 {index}"
            run.font.size = Pt(size)
        source.save(str(path))
        return path

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    stranded = Presentation(str(build(tmp_path / "stranded.pptx", (24, 12)))).slides[0]
    slide = adapt(deck, stranded, title="实验结果", subtitle="七个基准")

    assert _written_slots(slide)[PP_PLACEHOLDER.TITLE] == "实验结果"
    assert "七个基准" in [shape.text_frame.text.strip() for shape in slide.shapes if shape.has_text_frame]

    tied = Presentation(str(build(tmp_path / "tied.pptx", (20, 20)))).slides[0]
    with pytest.raises(KeyError, match="subtitle"):
        adapt(deck, tied, title="实验结果", subtitle="七个基准")


def test_replace_picture_swaps_a_photograph_used_as_a_shape_fill(tmp_path: Path) -> None:
    """`template_picture` tells an author to replace the template's photograph, and on a
    picture-*filled* shape that advice used to end in `AttributeError: blipFill`: the two
    spellings differ (`p:pic/p:blipFill` against `p:sp/p:spPr/a:blipFill`) and only the
    first was known. The fill also has no frame to shrink, so the swap crops through the
    source rectangle instead -- without which the template's own stretch survives and a
    figure comes out distorted.
    """
    from lxml import etree
    from PIL import Image
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    from raven_ppt.services.template.compose import replace_picture

    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    Image.new("RGB", (800, 600), (20, 20, 20)).save(first)
    Image.new("RGB", (1600, 900), (200, 200, 200)).save(second)

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(8), Inches(1), Inches(4), Inches(3))
    _, relationship = shape.part.get_or_add_image_part(str(first))
    fill = etree.SubElement(shape._element.spPr, f"{{{namespace}}}blipFill")
    etree.SubElement(fill, f"{{{namespace}}}blip").set(f"{{{rels}}}embed", relationship)

    replace_picture(shape, second, fit="cover")

    blip = shape._element.spPr.find(f"{{{namespace}}}blipFill/{{{namespace}}}blip")
    assert blip.get(f"{{{rels}}}embed") != relationship, "the fill points at the new image"
    crop = shape._element.spPr.find(f"{{{namespace}}}blipFill/{{{namespace}}}srcRect")
    assert crop is not None, "the source is cropped rather than stretched"
    assert {side for side in ("l", "r", "t", "b") if crop.get(side)} == {"l", "r"}, (
        "a 1.78 picture in a 1.33 frame is trimmed on its sides"
    )


def test_replace_picture_drops_the_inset_the_template_fitted_its_own_photo_with(tmp_path: Path) -> None:
    """The crop is stated against the frame, so a surviving `fillRect` contradicts it.

    Measured on `warm_bauhaus_quarterly_review` page 4: the replacement got a 23.9%
    crop for the frame and kept the template's -32% inset for a box half again as
    wide, and the two together read as a 1.64x stretch.
    """
    from lxml import etree
    from PIL import Image
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    from raven_ppt.services.template.compose import replace_picture

    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    first, second = tmp_path / "one.png", tmp_path / "two.png"
    Image.new("RGB", (800, 600), (20, 20, 20)).save(first)
    Image.new("RGB", (800, 458), (200, 200, 200)).save(second)

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0), Inches(1.49), Inches(5.49), Inches(6.01))
    _, relationship = shape.part.get_or_add_image_part(str(first))
    fill = etree.SubElement(shape._element.spPr, f"{{{namespace}}}blipFill")
    etree.SubElement(fill, f"{{{namespace}}}blip").set(f"{{{rels}}}embed", relationship)
    stretch = etree.SubElement(fill, f"{{{namespace}}}stretch")
    inset = etree.SubElement(stretch, f"{{{namespace}}}fillRect")
    inset.set("l", "-32200")
    inset.set("r", "-32000")

    replace_picture(shape, second, fit="cover")

    fill = shape._element.spPr.find(f"{{{namespace}}}blipFill")
    assert fill.find(f"{{{namespace}}}srcRect") is not None, "the crop states the fit"
    assert fill.find(f"{{{namespace}}}stretch/{{{namespace}}}fillRect") is None, (
        "the template's inset went with the image it was cut for"
    )


def test_a_shape_in_a_group_is_found_at_the_position_the_page_shows_it(tmp_path: Path) -> None:
    """`shape.left` inside a group is in the group's own space, scaled by its extents.

    Three of four live authors compared it against a page coordinate and got
    `no text shape near (1.56, 2.47)` for a shape that was exactly there.
    """
    from pptx import Presentation
    from pptx.util import Emu, Inches, Pt

    from raven_ppt.services.template.compose import page_position, shape_near

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    first = slide.shapes.add_textbox(Inches(2.0), Inches(1.0), Inches(2.0), Inches(0.5))
    first.text_frame.text = "inside the group"
    second = slide.shapes.add_textbox(Inches(5.0), Inches(1.0), Inches(2.0), Inches(0.5))
    second.text_frame.text = "also inside"
    group = slide.shapes.add_group_shape([first, second])
    # A group whose children are drawn at half scale and offset a page inch down.
    group.left, group.top = Inches(1.0), Inches(3.0)
    group.width, group.height = Emu(group.width // 2), Emu(group.height // 2)

    at_left, at_top = page_position(first)

    assert (at_left, at_top) != pytest.approx((2.0, 1.0)), "the declared numbers are the group's"
    # python-pptx builds a fresh proxy per access, so identity is the element.
    assert shape_near(slide, at_left, at_top, with_text=True)._element is first._element
    assert Pt  # the import is what the author's own script does


def test_a_position_that_matches_nothing_says_what_the_page_holds(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template.compose import shape_near

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1.0), Inches(1.0), Inches(2.0), Inches(0.5))
    box.text_frame.text = "the only copy"

    with pytest.raises(KeyError) as refused:
        shape_near(slide, 9.0, 6.0)

    assert "the page holds" in str(refused.value).casefold()
    assert "(1.00, 1.00)" in str(refused.value)


def test_copy_the_template_states_under_the_floor_is_lifted_and_stops_refitting(tmp_path: Path) -> None:
    """Two live authors wrote this for themselves, same name, same 14pt default."""
    from pptx import Presentation
    from pptx.enum.text import MSO_AUTO_SIZE
    from pptx.util import Inches, Pt

    from raven_ppt.services.template.compose import raise_type

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    body = slide.shapes.add_textbox(Inches(1.0), Inches(1.0), Inches(4.0), Inches(0.6))
    run = body.text_frame.paragraphs[0].add_run()
    run.text = "a paragraph long enough to be body copy rather than a label"
    run.font.size = Pt(12)
    body.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    label = slide.shapes.add_textbox(Inches(6.0), Inches(1.0), Inches(1.0), Inches(0.4))
    tiny = label.text_frame.paragraphs[0].add_run()
    tiny.text = "01"
    tiny.font.size = Pt(10)

    assert raise_type(slide) == 1

    assert body.text_frame.paragraphs[0].runs[0].font.size == Pt(14)
    assert body.text_frame.auto_size == MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT, (
        "the autofit is what shrank it, and turning it off alone leaves the lifted copy running "
        "out of a box drawn for the smaller type"
    )
    assert label.text_frame.paragraphs[0].runs[0].font.size == Pt(10), "a two-character label is set small on purpose"


def test_a_shape_is_found_by_the_copy_it_starts_with(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template.compose import shape_saying

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    wanted = slide.shapes.add_textbox(Inches(1.0), Inches(1.0), Inches(4.0), Inches(0.6))
    wanted.text_frame.text = "Method: three steps"

    assert shape_saying(slide, "Method")._element is wanted._element
    with pytest.raises(KeyError, match="[Ii]ts copy reads"):
        shape_saying(slide, "Results")


def test_an_empty_value_over_a_number_restates_it_instead_of_blanking_it(tmp_path: Path):
    """A template numbers its slots 01..08 and the deck has fewer sections.

    Measured on a live run: the author spent three requests working out which of the
    unit's two shapes was the number -- `["...", "01"]`, then `["...", None]`, then
    `["...", ""]` -- and shipped the one that blanked all six, so the page came out
    with six empty circles where the template had numbers. An empty string over a
    number now means "this unit's number", which is the only thing it could sensibly
    mean.
    """
    from pptx import Presentation

    from raven_ppt.services.template import adapt

    source = Presentation(str(_card_page(tmp_path / "cards.pptx", slots=6)))
    out = Presentation(str(tmp_path / "cards.pptx"))
    slide = adapt(
        out,
        source.slides[0],
        texts={"单击此处添加页面标题": "四类任务本质相同"},
        items=[["", f"第 {n} 条"] for n in range(1, 5)],
    )

    said = _texts(slide)
    assert [n for n in ("01", "02", "03", "04") if n in said] == ["01", "02", "03", "04"]
    assert "05" not in said and "06" not in said, "the spare units are gone, numbers and all"


def test_a_single_digit_template_keeps_a_single_digit(tmp_path: Path):
    """The padding is the template's, not this function's."""
    from pptx import Presentation

    from raven_ppt.services.template import adapt

    path = _card_page(tmp_path / "cards.pptx", slots=4)
    source = Presentation(str(path))
    for group in source.slides[0].shapes:
        for shape in getattr(group, "shapes", []):
            if shape.text_frame.text.strip().startswith("0"):
                shape.text_frame.text = shape.text_frame.text.strip().lstrip("0")
    source.save(str(path))

    source = Presentation(str(path))
    out = Presentation(str(path))
    slide = adapt(out, source.slides[0], items=[["", "甲"], ["", "乙"]])

    said = _texts(slide)
    assert "1" in said and "2" in said
    assert "01" not in said


def test_an_empty_value_over_words_still_empties_them(tmp_path: Path):
    """Only a number is restated. Everything else means what it says."""
    from pptx import Presentation

    from raven_ppt.services.template import adapt

    source = Presentation(str(_card_page(tmp_path / "cards.pptx", slots=3)))
    out = Presentation(str(tmp_path / "cards.pptx"))
    slide = adapt(out, source.slides[0], items=[["01", ""], ["02", ""]])

    assert "单击添加小标题" not in _texts(slide)


def test_a_number_kept_with_none_is_the_templates_own(tmp_path: Path):
    """`None` has not changed: it leaves the shape exactly as the template wrote it."""
    from pptx import Presentation

    from raven_ppt.services.template import adapt

    source = Presentation(str(_card_page(tmp_path / "cards.pptx", slots=4)))
    out = Presentation(str(tmp_path / "cards.pptx"))
    slide = adapt(out, source.slides[0], items=[[None, "甲"], [None, "乙"]])

    said = _texts(slide)
    assert "01" in said and "02" in said


def test_a_short_item_keeps_the_units_number_and_drops_its_placeholder(tmp_path: Path):
    """What a short list keeps, and what it must not.

    Two failures meet in this one call. Taking the numbers off a real template's
    agenda: the unit holds the number beside the copy, the author gave fewer values
    than shapes, and the pass that empties unnamed text emptied all six of them, so
    the page shipped blank folders where the template had 01 to 08. And leaving a
    placeholder standing: a delivered page's four cards each kept "单击添加小标题"
    under the author's own heading, which `placeholder_copy` refuses at the gate --
    ten builds went by with the author editing elsewhere.

    So running off the end of the list cannot mean one thing for both. A number is
    the template's own content and is restated; anything else the template wrote is
    its example copy and goes. An explicit ``None`` still keeps whatever it names --
    that is the escape for a fixed label worth keeping.
    """
    from pptx import Presentation

    from raven_ppt.services.template import adapt

    source = Presentation(str(_card_page(tmp_path / "cards.pptx", slots=4, shapes_per_slot=3)))
    out = Presentation(str(tmp_path / "cards.pptx"))
    slide = adapt(out, source.slides[0], items=[["01", "甲"], ["02", "乙"]])

    said = _texts(slide)
    assert "01" in said and "甲" in said
    assert "单击此处添加文本" not in said, "a placeholder past the end of the list is example copy, not furniture"
    assert "单击添加小标题" not in said, "the heading was named, so nothing of the template's is left in it"


def test_a_short_item_restates_a_number_it_never_reached(tmp_path: Path):
    """The agenda case on its own: the number is the tail, and it survives.

    `_card_page` puts the number first, so the test above gives it a value. Here the
    unit is walked so the number is what the list runs out before -- the shape the
    author had no opinion about -- and it has to come back renumbered for its new
    position rather than emptied.
    """
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import adapt

    # Built here rather than from `_card_page`, which puts the number first: the unit
    # this protects is the one the docstring above describes, a label with the number
    # after it, so a one-value list runs out exactly before the number.
    built = Presentation()
    built.slide_width, built.slide_height = Inches(13.333), Inches(7.5)
    page = built.slides.add_slide(built.slide_layouts[6])
    for index in range(4):
        group = page.shapes.add_group_shape()
        label = group.shapes.add_textbox(Inches(0.7 + index * 3.0), Inches(2.8), Inches(2.6), Inches(0.5))
        label.text_frame.text = "单击添加小标题"
        number = group.shapes.add_textbox(Inches(0.7 + index * 3.0), Inches(2.0), Inches(0.6), Inches(0.6))
        number.text_frame.text = f"0{index + 1}"
    built.save(str(tmp_path / "agenda.pptx"))

    source = Presentation(str(tmp_path / "agenda.pptx"))
    out = Presentation(str(tmp_path / "agenda.pptx"))
    slide = adapt(out, source.slides[0], items=[["甲"], ["乙"], ["丙"]])

    said = _texts(slide)
    assert {"甲", "乙", "丙"} <= said
    assert {"01", "02", "03"} <= said, "a number past the end of the list is the template's own"
    assert "04" not in said, "the fourth unit was not filled, so it is gone rather than emptied"
    assert "单击添加小标题" not in said


def test_a_hidden_page_keeps_its_number_but_is_not_offered(tmp_path) -> None:
    """The bundled templates each shipped two hidden pages of the vendor's own
    advertising, and nothing stopped an author naming one as a prototype.

    Numbering has to survive the fix: a page is asked for by its place in the file,
    so hiding page 2 must not turn page 3 into page 2.
    """
    from pptx import Presentation

    from raven_ppt.services.template.menu import menu

    presentation = Presentation()
    for _ in range(3):
        presentation.slides.add_slide(presentation.slide_layouts[6])
    presentation.slides[1].element.set("show", "0")
    path = tmp_path / "with_a_hidden_page.pptx"
    presentation.save(path)

    listing = menu(path)

    assert [entry.number for entry in listing] == [1, 2, 3]
    assert [entry.hidden for entry in listing] == [False, True, False]
    assert "hidden in the file" in listing[1].line()


def _unit_page(tmp_path: Path, name: str, build) -> Path:
    """A page of repeated units laid out by `build(group, index)`, saved and returned."""
    from pptx import Presentation
    from pptx.util import Inches

    built = Presentation()
    built.slide_width, built.slide_height = Inches(13.333), Inches(7.5)
    page = built.slides.add_slide(built.slide_layouts[6])
    for index in range(4):
        build(page.shapes.add_group_shape(), index)
    path = tmp_path / name
    built.save(str(path))
    return path


def _stat_card(group, index: int) -> None:
    """A card whose number is drawn last: heading, body, then the 60pt figure above them."""
    from pptx.util import Inches, Pt

    left = Inches(0.9 + index * 3.0)
    heading = group.shapes.add_textbox(left, Inches(4.5), Inches(2.6), Inches(0.4))
    heading.text_frame.text = "单击添加小标题"
    body = group.shapes.add_textbox(left, Inches(5.3), Inches(2.6), Inches(1.0))
    body.text_frame.text = "单击此处添加文本"
    figure = group.shapes.add_textbox(left, Inches(2.9), Inches(2.6), Inches(1.2))
    figure.text_frame.text = "68%"
    figure.text_frame.paragraphs[0].runs[0].font.size = Pt(60)


def test_items_are_counted_in_reading_order_not_file_order(tmp_path: Path) -> None:
    """The number box a reader sees first is stored last on the reference page this
    was measured on, and a positional item counted the way a reader counts put the body
    copy into the 60pt figure box and the figure into the heading slot."""
    from pptx import Presentation

    from raven_ppt.services.template import adapt

    path = _unit_page(tmp_path, "stats.pptx", _stat_card)
    source, out = Presentation(str(path)), Presentation(str(path))

    slide = adapt(out, source.slides[0], items=[["72%", "过夜访客", "约七成过夜。"]] * 4)

    from raven_ppt.services.template import units

    first = min(units(slide)[0], key=lambda unit: unit.left)
    by_top = sorted((s.top, s.text_frame.text) for s in first.shapes if getattr(s, "has_text_frame", False))
    assert [text for _, text in by_top] == ["72%", "过夜访客", "约七成过夜。"]


def test_units_match_siblings_whatever_order_their_shapes_were_saved_in(tmp_path: Path) -> None:
    """A reference page's fourth row is the same four shapes as the three above it,
    saved icon-first; read as an ordered signature it was not a sibling, its texts were
    emptied with everyone else's and its icon was left standing beside nothing."""
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import units

    def row(group, index: int) -> None:
        top = Inches(1.6 + index * 1.2)
        parts = [
            lambda: group.shapes.add_textbox(Inches(6.5), top, Inches(3.0), Inches(0.4)),
            lambda: group.shapes.add_shape(9, Inches(5.9), top, Inches(0.5), Inches(0.5)),
        ]
        if index == 3:
            parts.reverse()
        for make in parts:
            shape = make()
            if getattr(shape, "has_text_frame", False) and shape.width > Inches(1):
                shape.text_frame.text = f"第 {index + 1} 条"

    path = _unit_page(tmp_path, "rows.pptx", row)
    runs = units(Presentation(str(path)).slides[0])

    assert [len(run) for run in runs] == [4]


def test_the_survivors_of_a_row_share_its_original_width(tmp_path: Path) -> None:
    """Three items on a four-card row used to leave three cards left-aligned with a
    card-sized hole on the right, which every author closed by hand or shipped."""
    from pptx import Presentation

    from raven_ppt.services.template import adapt, units

    path = _unit_page(tmp_path, "row.pptx", _stat_card)
    source, out = Presentation(str(path)), Presentation(str(path))
    before = units(source.slides[0])[0]
    left0 = min(unit.left for unit in before)
    right0 = max(unit.left + unit.width for unit in before)

    slide = adapt(out, source.slides[0], items=[["1", "甲", "一"], ["2", "乙", "二"], ["3", "丙", "三"]])

    after = sorted(units(slide)[0], key=lambda unit: unit.left)
    assert len(after) == 3
    assert after[0].left == left0, "the first card keeps the row's left edge"
    assert abs((after[-1].left + after[-1].width) - right0) < 12700, "the last card reaches the row's right edge"
    gaps = [after[i + 1].left - (after[i].left + after[i].width) for i in range(2)]
    assert abs(gaps[0] - gaps[1]) < 12700, "the survivors are evenly spaced"


def test_a_short_item_skips_the_number_that_reads_first(tmp_path: Path) -> None:
    """`["甲"]` on a `[01, label]` unit keeps the 01 and writes 甲 into the label, whichever
    of the two the file stored first -- the failure this closes wrote 甲 over the 01."""
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import adapt

    def numbered(group, index: int) -> None:
        number = group.shapes.add_textbox(Inches(0.7 + index * 3.0), Inches(2.0), Inches(0.6), Inches(0.6))
        number.text_frame.text = f"0{index + 1}"
        label = group.shapes.add_textbox(Inches(1.4 + index * 3.0), Inches(2.05), Inches(2.0), Inches(0.5))
        label.text_frame.text = "单击添加小标题"

    path = _unit_page(tmp_path, "numbered.pptx", numbered)
    source, out = Presentation(str(path)), Presentation(str(path))

    slide = adapt(out, source.slides[0], items=[["甲"], ["乙"]])

    said = _texts(slide)
    assert {"甲", "乙", "01", "02"} <= said
    assert "03" not in said and "单击添加小标题" not in said


def test_a_full_item_addresses_every_shape_including_the_number(tmp_path: Path) -> None:
    """The author who wants their own numbering writes one value per shape."""
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import adapt

    def numbered(group, index: int) -> None:
        number = group.shapes.add_textbox(Inches(0.7 + index * 3.0), Inches(2.0), Inches(0.6), Inches(0.6))
        number.text_frame.text = f"0{index + 1}"
        label = group.shapes.add_textbox(Inches(1.4 + index * 3.0), Inches(2.05), Inches(2.0), Inches(0.5))
        label.text_frame.text = "单击添加小标题"

    path = _unit_page(tmp_path, "quarters.pptx", numbered)
    source, out = Presentation(str(path)), Presentation(str(path))

    slide = adapt(out, source.slides[0], items=[["Q1", "甲"], ["Q2", "乙"]])

    assert {"Q1", "Q2", "甲", "乙"} <= _texts(slide)
    assert not {"01", "02"} & _texts(slide)


def test_an_irregular_run_is_not_moved(tmp_path: Path) -> None:
    """Pills along a path: deleting the spare must not re-space the rest, because any
    move is a guess at the design."""
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import adapt, units

    spots = [(1.0, 4.5), (4.0, 6.0), (7.0, 2.0), (10.0, 3.5)]

    def pill(group, index: int) -> None:
        left, top = spots[index]
        shape = group.shapes.add_shape(1, Inches(left), Inches(top), Inches(1.8), Inches(0.5))
        shape.text_frame.text = "单击添加小标题"

    path = _unit_page(tmp_path, "path.pptx", pill)
    source, out = Presentation(str(path)), Presentation(str(path))

    slide = adapt(out, source.slides[0], items=[["甲"], ["乙"], ["丙"]])

    kept = sorted((round(u.left / 914400, 2), round(u.top / 914400, 2)) for u in units(slide)[0])
    assert kept == sorted(spots[i] for i in (2, 0, 3)) or len(kept) == 3
    assert all(spot in [(l, t) for l, t in spots] for spot in kept), "every survivor stands where the template put it"


def test_bundled_opens_a_template_by_stem_and_names_the_rest_when_wrong(tmp_path: Path, monkeypatch) -> None:
    from pptx import Presentation

    from raven_ppt.services.template.compose import bundled

    built = Presentation()
    built.slides.add_slide(built.slide_layouts[6])
    (tmp_path / "shelf").mkdir()
    built.save(str(tmp_path / "shelf" / "one_template.pptx"))
    monkeypatch.setenv("PPT_BUNDLED_TEMPLATES", str(tmp_path / "shelf"))

    assert len(bundled("one_template").slides) == 1
    assert len(bundled("one_template.pptx").slides) == 1, "the suffix is forgiven"
    with pytest.raises(FileNotFoundError, match="one_template"):
        bundled("two_template")
    monkeypatch.delenv("PPT_BUNDLED_TEMPLATES")
    with pytest.raises(RuntimeError, match="PPT_BUNDLED_TEMPLATES"):
        bundled("one_template")


def test_a_page_borrowed_across_templates_lands_on_the_deck_s_own_layout_and_theme(tmp_path: Path) -> None:
    """Measured on four such clones rendered beside their sources: the arrangement comes
    across, the colours and the master are the deck's. Here the two facts that make
    that so -- the layout is matched by name inside the deck's package, and a scheme
    colour is left a scheme colour -- and the file holds each layout part once."""
    import zipfile

    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.dml import MSO_THEME_COLOR
    from pptx.util import Inches

    from raven_ppt.services.template.compose import adapt

    def deck(path: Path, theme_hint: str):
        built = Presentation()
        built.slide_width, built.slide_height = Inches(13.333), Inches(7.5)
        page = built.slides.add_slide(built.slide_layouts[5])
        card = page.shapes.add_shape(1, Inches(1), Inches(2), Inches(3), Inches(2))
        card.fill.solid()
        card.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_1
        card.text_frame.text = theme_hint
        built.save(str(path))
        return path

    lender = Presentation(str(deck(tmp_path / "lender.pptx", "借来的卡")))
    target = Presentation(str(deck(tmp_path / "target.pptx", "自己的卡")))

    slide = adapt(target, lender.slides[0], texts={"借来的卡": "换了字"})
    target.save(str(tmp_path / "out.pptx"))

    assert slide.slide_layout.name == lender.slides[0].slide_layout.name
    assert slide.slide_layout.part.package is target.part.package, "the deck's own layout, not the lender's"
    card = next(s for s in slide.shapes if getattr(s, "has_text_frame", False) and s.text_frame.text == "换了字")
    assert card.fill.fore_color.theme_color == MSO_THEME_COLOR.ACCENT_1, "a theme colour stays one, so it re-themes"
    names = [n for n in zipfile.ZipFile(tmp_path / "out.pptx").namelist() if "slideLayout" in n and n.endswith(".xml")]
    assert len(names) == len(set(names)), "no layout part written twice"
    assert not isinstance(RGBColor, str)


def test_replace_text_takes_a_list_of_strings_and_nested_lists_as_paragraphs(tmp_path: Path) -> None:
    """`[["选址评估"], ["六维模型"]]` crashed a live build with `'list' object has no
    attribute 'replace'`: two paragraphs, each a list holding one plain string."""
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template.compose import replace_text

    built = Presentation()
    slide = built.slides.add_slide(built.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(2))
    box.text_frame.text = "旧"

    replace_text(box, [["选址评估"], ["六维模型"]])
    assert [p.text for p in box.text_frame.paragraphs] == ["选址评估", "六维模型"]

    replace_text(box, ["第一段", "第二段\n第三段"])
    assert [p.text for p in box.text_frame.paragraphs] == ["第一段", "第二段", "第三段"]

    with pytest.raises(TypeError, match="each paragraph"):
        replace_text(box, [{"not": "a paragraph"}])


def test_place_takes_a_page_box_for_a_shape_inside_a_scaled_group(tmp_path: Path) -> None:
    """A group's children keep their numbers in the group's child space. A live
    program found `place` equivalent to assigning `.top` and wrote its own
    conversion; the box an author gives is on the page, so it is converted here."""
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template.compose import page_position, place

    built = Presentation()
    built.slide_width, built.slide_height = Inches(13.333), Inches(7.5)
    slide = built.slides.add_slide(built.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    child = group.shapes.add_shape(1, Inches(1.0), Inches(1.0), Inches(2.0), Inches(1.0))
    # Shrink the group on the page to half its child extent, and move it: the
    # child's numbers now mean something else on the page.
    group.left, group.top = Inches(4.0), Inches(3.0)
    group.width, group.height = Inches(1.0), Inches(0.5)

    place(child, (5.0, 3.5, 0.5, 0.25))

    x, y = page_position(child)
    assert (round(x, 3), round(y, 3)) == (5.0, 3.5), "the child sits where the page box said"
    assert child.width == Inches(1.0) and child.height == Inches(0.5), "its size is scaled into the group's space"


def _ids_on(slide) -> list[str]:
    return [
        node.get("id")
        for node in slide._element.iter("{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr")
    ]


def test_more_items_than_slots_grows_a_row_within_its_width(tmp_path: Path) -> None:
    """Ten measured builds died on 'this page repeats N units and N+1 items were given',
    and the authors then wrote their own clone helper. Five items on a four-card row are
    five cards across the same width, shrunk alike, and the fifth is filled."""
    from pptx import Presentation

    from raven_ppt.services.template import adapt, units

    path = _unit_page(tmp_path, "grow.pptx", _stat_card)
    source, out = Presentation(str(path)), Presentation(str(path))
    before = units(source.slides[0])[0]
    left0 = min(u.left for u in before)
    right0 = max(u.left + u.width for u in before)
    width0 = before[0].width

    slide = adapt(out, source.slides[0], items=[[f"{n}0%", f"第 {n} 项", "正文"] for n in range(1, 6)])

    grown = sorted(units(slide)[0], key=lambda u: u.left)
    assert len(grown) == 5
    assert grown[0].left == left0 and abs((grown[-1].left + grown[-1].width) - right0) < 12700
    assert all(u.width == grown[0].width for u in grown), "shrunk alike"
    assert grown[0].width < width0
    assert {"10%", "50%", "第 5 项"} <= _texts(slide), "the fifth unit is a slot like the others"
    ids = _ids_on(slide)
    assert len(ids) == len(set(ids)), "no two shapes share an id"


def test_add_unit_on_a_column_grows_it_down_its_own_extent(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import add_unit, units

    def row(group, index: int) -> None:
        top = Inches(1.6 + index * 1.3)
        badge = group.shapes.add_shape(9, Inches(6.0), top, Inches(0.5), Inches(0.5))
        badge.text_frame.text = f"0{index + 1}"
        label = group.shapes.add_textbox(Inches(6.7), top, Inches(4.0), Inches(0.5))
        label.text_frame.text = "单击添加小标题"

    path = _unit_page(tmp_path, "column.pptx", row)
    deck = Presentation(str(path))
    slide = deck.slides[0]
    run = units(slide)[0]
    top0 = min(u.top for u in run)
    bottom0 = max(u.top + u.height for u in run)

    added = add_unit(run, 1)

    assert len(added) == 1
    grown = sorted(units(slide)[0], key=lambda u: u.top)
    assert len(grown) == 5
    assert grown[0].top == top0 and abs((grown[-1].top + grown[-1].height) - bottom0) < 12700
    gaps = {round((grown[i + 1].top - grown[i].top - grown[i].height) / 914400, 2) for i in range(4)}
    assert len(gaps) == 1, f"even gaps, not {gaps}"


def test_a_grid_gains_a_row_at_its_own_pitch(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import add_unit, units

    built = Presentation()
    built.slide_width, built.slide_height = Inches(13.333), Inches(7.5)
    page = built.slides.add_slide(built.slide_layouts[6])
    for index in range(6):
        group = page.shapes.add_group_shape()
        col, row = index % 3, index // 3
        card = group.shapes.add_shape(1, Inches(0.7 + col * 4.1), Inches(1.5 + row * 1.8), Inches(3.8), Inches(1.5))
        card.text_frame.text = f"卡 {index + 1}"
    built.save(str(tmp_path / "grid.pptx"))
    deck = Presentation(str(tmp_path / "grid.pptx"))
    slide = deck.slides[0]

    added = add_unit(units(slide)[0], 2)

    tops = sorted({round(u.top / 914400, 2) for u in units(slide)[0]})
    assert tops == [1.5, 3.3, 5.1], "a third row at the grid's own pitch"
    lefts_last_row = sorted(round(u.left / 914400, 2) for u in added)
    assert lefts_last_row == [2.75, 6.85], "the short last row is centred"


def test_growth_refuses_where_a_unit_could_no_longer_carry_copy(tmp_path: Path) -> None:
    from pptx import Presentation

    from raven_ppt.services.template import add_unit, units

    path = _unit_page(tmp_path, "toomany.pptx", _stat_card)
    slide = Presentation(str(path)).slides[0]

    with pytest.raises(ValueError, match="slots"):
        add_unit(units(slide)[0], 6)


def test_an_irregular_run_is_not_grown(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import adapt

    spots = [(1.0, 4.5), (4.0, 6.0), (7.0, 2.0), (10.0, 3.5)]

    def pill(group, index: int) -> None:
        left, top = spots[index]
        shape = group.shapes.add_shape(1, Inches(left), Inches(top), Inches(1.8), Inches(0.5))
        shape.text_frame.text = "单击添加小标题"

    path = _unit_page(tmp_path, "curve.pptx", pill)
    source, out = Presentation(str(path)), Presentation(str(path))

    with pytest.raises(ValueError, match="no row, column or grid") as refused:
        adapt(out, source.slides[0], items=[["甲"], ["乙"], ["丙"], ["丁"], ["戊"]])
    # The way out is named, with the geometry to take it: two measured runs met this
    # refusal on a template's diagonal pair and had only "another prototype" to go on.
    assert "clone_shape(run[-1], (left, top, width, height))" in str(refused.value)
    assert "(1.00, 4.50, 1.80, 0.50)" in str(refused.value)


def test_remove_unit_closes_the_gap_and_clone_shape_lands_at_its_box(tmp_path: Path) -> None:
    from pptx import Presentation

    from raven_ppt.services.template import clone_shape, remove_unit, units
    from raven_ppt.services.template.compose import page_position

    path = _unit_page(tmp_path, "remove.pptx", _stat_card)
    slide = Presentation(str(path)).slides[0]
    run = sorted(units(slide)[0], key=lambda u: u.left)
    left0, right0 = run[0].left, run[-1].left + run[-1].width

    remove_unit(run[1])

    left = sorted(units(slide)[0], key=lambda u: u.left)
    assert len(left) == 3
    assert left[0].left == left0 and abs((left[-1].left + left[-1].width) - right0) < 12700

    copy_ = clone_shape(left[0], (0.5, 6.5, 2.0, 0.6))
    assert (round(page_position(copy_)[0], 2), round(page_position(copy_)[1], 2)) == (0.5, 6.5)
    ids = _ids_on(slide)
    assert len(ids) == len(set(ids))


# --- a picture behind the page, and the pictures a layout carries -----------------------


def _canvas(tmp_path: Path, picture_size=(800, 600), shade=(20, 20, 20)):
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    image = tmp_path / "wash.png"
    Image.new("RGB", picture_size, shade).save(image)
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.text_frame.text = "the title"
    return presentation, slide, image


def test_backdrop_sits_behind_the_page_cover_cropped_and_washed(tmp_path: Path) -> None:
    """The one generated picture that never poses as evidence: full-bleed, first in the
    z-order so the title already there stays over it, cropped rather than stretched to
    the canvas, and washed through its own `alphaModFix` so the page's ground shows."""
    from raven_ppt.services.template.compose import backdrop

    presentation, slide, image = _canvas(tmp_path)
    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"

    picture = backdrop(slide, image, alpha=0.3)

    tree = slide.shapes._spTree
    assert list(tree).index(picture._element) == 2, "behind everything: first drawn shape in the tree"
    assert picture.name == "backdrop"
    assert (picture.left, picture.top) == (0, 0)
    assert (picture.width, picture.height) == (presentation.slide_width, presentation.slide_height)
    assert picture.crop_top > 0 and picture.crop_bottom > 0 and picture.crop_left == 0, (
        "a 4:3 picture behind a 16:9 page loses its top and bottom, not its proportions"
    )
    fix = picture._element.find(f".//{{{namespace}}}blip/{{{namespace}}}alphaModFix")
    assert fix is not None and fix.get("amt") == "30000"
    assert slide.shapes[-1].text_frame.text == "the title", "the page's own shapes are untouched"


def test_backdrop_takes_a_box_and_refuses_an_alpha_that_is_not_a_wash(tmp_path: Path) -> None:
    from pptx.util import Inches

    from raven_ppt.services.template.compose import backdrop

    _, slide, image = _canvas(tmp_path)

    picture = backdrop(slide, image, alpha=1.0, box=(0, 3.5, 13.333, 4))
    assert (picture.left, picture.top) == (0, Inches(3.5))
    assert picture.height == Inches(4)

    with pytest.raises(ValueError, match="alpha=0 "):
        backdrop(slide, image, alpha=0)
    with pytest.raises(ValueError, match="alpha=1.5"):
        backdrop(slide, image, alpha=1.5)
    with pytest.raises(ValueError, match="not one"):
        backdrop(slide, image.with_name("missing.png"))


def test_layout_pictures_names_the_layouts_photographs_and_replace_picture_swaps_them(tmp_path: Path) -> None:
    """A template's cover photograph is as often on the layout as on the page, where
    nothing on the page can be handed to `replace_picture`. These are those shapes, and
    the swap reaches every page built on the layout."""
    from PIL import Image

    from raven_ppt.services.template.compose import layout_pictures, replace_picture
    from tests._ppt_engine_fixtures import layout_picture

    presentation, slide, image = _canvas(tmp_path)
    rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    layout = slide.slide_layout
    small = layout_picture(layout, image, 1, 1, 2, 1.5)
    large = layout_picture(layout, image, 6, 0, 7.3, 7.5)
    other = presentation.slides.add_slide(layout)

    found = layout_pictures(slide)

    assert [shape.shape_id for shape in found] == [large.shape_id, small.shape_id], "largest first"
    assert layout_pictures(other)[0].shape_id == large.shape_id, "the same shapes, seen from any page on the layout"

    replacement = tmp_path / "new.png"
    Image.new("RGB", (1600, 900), (200, 200, 200)).save(replacement)
    before = large._element.blipFill.blip.get(f"{{{rels}}}embed")
    replace_picture(found[0], replacement, "cover")
    assert large._element.blipFill.blip.get(f"{{{rels}}}embed") != before
    assert large.crop_left > 0, "a 16:9 picture in a portrait frame is trimmed on its sides"


@pytest.mark.skipif(
    not __import__("raven_ppt.services.render", fromlist=["available"]).available().can_convert,
    reason="needs LibreOffice to render",
)
def test_a_washed_backdrop_renders_as_a_blend_not_a_slab(tmp_path: Path) -> None:
    """`alphaModFix` is the transparency the renderer honours: a black picture at 0.3
    over a white page has to come out grey, or the wash exists only in the XML."""
    import asyncio

    from PIL import Image

    from raven_ppt.services.render import available
    from raven_ppt.services.template.compose import backdrop
    from raven_ppt.stages._views import DeckViews

    if not available().can_rasterise:
        pytest.skip("needs a PDF rasteriser")
    presentation, slide, image = _canvas(tmp_path, shade=(0, 0, 0))
    backdrop(slide, image, alpha=0.3)
    deck = tmp_path / "deck.pptx"
    presentation.save(str(deck))

    views = DeckViews(dpi=48)
    rendered = asyncio.run(views.pages(deck, tmp_path / "render", [1]))
    page = Image.open(rendered[1]).convert("RGB")
    sample = page.getpixel((page.width // 2, int(page.height * 0.8)))
    assert 150 <= sample[0] <= 200, f"black at alpha 0.3 over white should read around 178, not {sample}"


def test_replace_picture_washes_the_new_picture_when_asked(tmp_path: Path) -> None:
    """A layout picture the size of the page is the page's background; a photograph
    swapped in at full strength drowns the type, so the swap takes the same `alpha`
    a backdrop does -- on a picture frame and on a picture-filled shape alike."""
    from lxml import etree
    from PIL import Image
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    from raven_ppt.services.template.compose import replace_picture

    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    _, slide, image = _canvas(tmp_path)
    replacement = tmp_path / "new.png"
    Image.new("RGB", (1600, 900), (200, 200, 200)).save(replacement)
    frame = slide.shapes.add_picture(str(image), Inches(0), Inches(0), Inches(13.333), Inches(7.5))
    filled = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(13.333), Inches(7.5))
    _, relationship = filled.part.get_or_add_image_part(str(image))
    fill = etree.SubElement(filled._element.spPr, f"{{{namespace}}}blipFill")
    etree.SubElement(fill, f"{{{namespace}}}blip").set(f"{{{rels}}}embed", relationship)

    replace_picture(frame, replacement, "cover", alpha=0.25)
    replace_picture(filled, replacement, "cover", alpha=0.25)

    for shape in (frame, filled):
        fix = shape._element.find(f".//{{{namespace}}}blip/{{{namespace}}}alphaModFix")
        assert fix is not None and fix.get("amt") == "25000", shape.name
    replace_picture(frame, replacement, "cover", alpha=0.6)
    assert len(frame._element.findall(f".//{{{namespace}}}alphaModFix")) == 1, "a second wash replaces the first"
    replace_picture(frame, replacement, "cover")
    assert frame._element.find(f".//{{{namespace}}}alphaModFix").get("amt") == "60000", "None leaves the wash alone"
    with pytest.raises(ValueError, match="alpha=2"):
        replace_picture(frame, replacement, "cover", alpha=2)


def test_surplus_empty_values_are_dropped_rather_than_refused(tmp_path: Path) -> None:
    """`["", title, sub]` against a two-frame unit: the empty string is the placeholder an
    author writes for a number tile it means to leave alone, and it would have written
    nothing. A live build ended on the refusal over exactly that. Values that would
    have written something are still refused when they outnumber the frames."""
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template import adapt, units

    def two_lines(group, index: int) -> None:
        left = Inches(0.9 + index * 3.0)
        title = group.shapes.add_textbox(left, Inches(2.0), Inches(2.6), Inches(0.5))
        title.text_frame.text = "工作内容回顾"
        sub = group.shapes.add_textbox(left, Inches(2.6), Inches(2.6), Inches(0.4))
        sub.text_frame.text = "Review of the work content"

    path = _unit_page(tmp_path, "agenda.pptx", two_lines)
    source, out = Presentation(str(path)), Presentation(str(path))

    slide = adapt(out, source.slides[0], items=[["", "标杆案例调研", "国内外四个样本"]] * 4)

    first = min(units(slide)[0], key=lambda unit: unit.left)
    texts = [
        s.text_frame.text for s in sorted(first.shapes, key=lambda s: s.top) if getattr(s, "has_text_frame", False)
    ]
    assert texts == ["标杆案例调研", "国内外四个样本"]

    with pytest.raises(ValueError, match="holds 2 text shape"):
        adapt(Presentation(str(path)), source.slides[0], items=[["甲", "乙", "丙"]] * 4)


def test_wash_sets_any_pictures_transparency_and_refuses_a_solid_fill(tmp_path: Path) -> None:
    """The share `backdrop` and `replace_picture(alpha=)` take, on a picture already on
    the page -- a frame, or a shape filled with one -- and a refusal for a shape that
    shows no picture, whose solid fill has a transparency of its own."""
    from lxml import etree
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    from raven_ppt.services.template.compose import wash

    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    _, slide, image = _canvas(tmp_path)
    frame = slide.shapes.add_picture(str(image), Inches(1), Inches(2), Inches(4), Inches(3))
    filled = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(6), Inches(2), Inches(4), Inches(3))
    _, relationship = filled.part.get_or_add_image_part(str(image))
    fill = etree.SubElement(filled._element.spPr, f"{{{namespace}}}blipFill")
    etree.SubElement(fill, f"{{{namespace}}}blip").set(f"{{{rels}}}embed", relationship)
    solid = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(6), Inches(2), Inches(1))

    assert wash(frame, 0.3) is frame
    wash(filled, 0.5)
    wash(frame, 0.6)

    assert frame._element.find(f".//{{{namespace}}}blip/{{{namespace}}}alphaModFix").get("amt") == "60000"
    assert len(frame._element.findall(f".//{{{namespace}}}alphaModFix")) == 1, "a second wash replaces the first"
    assert filled._element.find(f".//{{{namespace}}}blip/{{{namespace}}}alphaModFix").get("amt") == "50000"
    with pytest.raises(ValueError, match="shows none"):
        wash(solid, 0.3)
    with pytest.raises(ValueError, match="alpha=0 "):
        wash(frame, 0)


def test_a_shaped_picture_frame_is_covered_not_shrunk(tmp_path: Path) -> None:
    """A frame cut to a curve is the page's design. Contain shrank a 13.35in wave-edged
    frame to 7.56in and left the photograph in a plain rectangle beside the panel it
    was drawn to complete; a shaped frame takes cover unless the author says otherwise."""
    from PIL import Image
    from pptx.util import Inches

    from raven_ppt.services.template.compose import replace_picture

    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    _, slide, image = _canvas(tmp_path, picture_size=(1200, 800))
    frame = slide.shapes.add_picture(str(image), Inches(0), Inches(3.25), Inches(13.35), Inches(4.25))
    geometry = frame._element.find(f".//{{{namespace}}}prstGeom")
    geometry.set("prst", "wave")
    replacement = tmp_path / "photo.png"
    Image.new("RGB", (1600, 1000), (60, 60, 60)).save(replacement)

    with pytest.warns(UserWarning, match="shaped picture frame"):
        replace_picture(frame, replacement)

    assert frame.width == Inches(13.35) and frame.left == 0, "the frame keeps its size and place"
    assert frame.crop_left > 0 or frame.crop_top > 0, "the picture is cropped into it"

    plain = slide.shapes.add_picture(str(image), Inches(1), Inches(1), Inches(4), Inches(3))
    replace_picture(plain, replacement)
    assert plain.height < Inches(3), "a plain rectangle still gives way to the picture under contain"


def _drawing_page(tmp_path: Path):
    """A page whose illustration is a group of freeforms with a loose star beside it, under a title."""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    title = slide.shapes.add_textbox(Inches(1), Inches(0.5), Inches(8), Inches(1))
    title.text_frame.text = "Where the market sits"
    cartoon = slide.shapes.add_group_shape()
    cartoon.shapes.add_shape(MSO_SHAPE.OVAL, Inches(8), Inches(2), Inches(2), Inches(2))
    cartoon.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(8.5), Inches(4), Inches(1), Inches(2))
    cartoon.name = "cartoon"
    star = slide.shapes.add_shape(MSO_SHAPE.STAR_5_POINT, Inches(10.2), Inches(2.2), Inches(1), Inches(1))
    panel = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(2), Inches(6), Inches(4))
    panel.text_frame.text = "A panel of copy the picture must not take"
    return presentation, slide, cartoon, star, panel


def test_replace_picture_puts_a_picture_where_a_drawn_illustration_was(tmp_path: Path, image) -> None:
    """A section page's cartoon is a group of freeforms, not a picture, and `replace_picture`
    used to refuse it ("no image to replace") -- so the template's illustration stayed on
    every borrowed page. The picture takes the drawing's box and depth, and the drawing goes."""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    from raven_ppt.services.template.compose import replace_picture

    presentation, slide, cartoon, star, panel = _drawing_page(tmp_path)
    tree = cartoon._element.getparent()
    depth = tree.index(cartoon._element)
    where = (cartoon.left, cartoon.top, cartoon.width, cartoon.height)
    figure = image("cartoon.png", (30, 120, 120))

    picture = replace_picture(cartoon, figure)
    out = tmp_path / "swapped.pptx"
    presentation.save(str(out))

    page = Presentation(str(out)).slides[0]
    kinds = [shape.shape_type for shape in page.shapes]
    assert MSO_SHAPE_TYPE.GROUP not in kinds, "the drawing is gone"
    assert kinds.count(MSO_SHAPE_TYPE.PICTURE) == 1
    assert tree.index(picture._element) == depth, "the picture sits where the drawing sat in the z-order"
    frame = next(shape for shape in page.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE)
    assert frame.image.blob == figure.read_bytes()
    assert frame.left >= where[0] and frame.top >= where[1], "contain keeps the picture inside the drawing's box"
    assert frame.left + frame.width <= where[0] + where[2] + 1 and frame.top + frame.height <= where[1] + where[3] + 1
    assert frame.name == "cartoon"


def test_replace_picture_takes_a_group_member_with_its_whole_group(tmp_path: Path, image) -> None:
    """`adapt` numbers a group's members too, so an author points at the oval inside the
    cartoon; a group is one drawing, and half a cartoon left behind is worse than none."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    from raven_ppt.services.template.compose import replace_picture

    presentation, slide, cartoon, star, panel = _drawing_page(tmp_path)
    oval = cartoon.shapes[0]

    replace_picture(oval, image("cartoon.png", (30, 120, 120)))

    kinds = [shape.shape_type for shape in slide.shapes]
    assert MSO_SHAPE_TYPE.GROUP not in kinds and kinds.count(MSO_SHAPE_TYPE.PICTURE) == 1
    assert any(shape.shape_type == MSO_SHAPE_TYPE.TEXT_BOX for shape in slide.shapes), "the title is untouched"


def test_adapt_swaps_a_drawing_and_several_loose_shapes_for_one_picture(tmp_path: Path, image) -> None:
    """`pictures={n: ...}` on a page whose illustration is drawn used to report the key as
    missed, so the author fell back to drawing over it. `adapt` numbers a group's members
    and not the group, so a member stands for its whole cartoon; a tuple of keys names the
    loose parts of one drawing together, and the picture spans their union."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    from raven_ppt.services.template.compose import _all_shapes, adapt

    presentation, slide, cartoon, star, panel = _drawing_page(tmp_path)
    every = list(_all_shapes(slide.shapes))
    oval_index = every.index(cartoon.shapes[0]) + 1
    star_index = every.index(star) + 1
    figure = image("scene.png", (200, 120, 40))

    page = adapt(presentation, slide, title="Night market", pictures={(oval_index, star_index): figure})

    kinds = [shape.shape_type for shape in page.shapes]
    assert MSO_SHAPE_TYPE.GROUP not in kinds and kinds.count(MSO_SHAPE_TYPE.PICTURE) == 1
    assert not any(shape.name == star.name for shape in page.shapes), "the loose star went with the group"
    frame = next(shape for shape in page.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE)
    assert frame.left >= cartoon.left and frame.left + frame.width <= star.left + star.width + 1, (
        "the picture spans the union of the drawing and the star"
    )
    assert any("Night market" in shape.text_frame.text for shape in page.shapes if shape.has_text_frame)


def test_adapt_does_not_read_a_text_panel_as_an_illustration(tmp_path: Path, image) -> None:
    """A shape that holds copy is a miscount, not a drawing: an index landing on the copy
    panel is refused as before, rather than the panel giving way to a picture."""
    import pytest

    from raven_ppt.services.template.compose import _all_shapes, adapt

    presentation, slide, cartoon, star, panel = _drawing_page(tmp_path)
    every = list(_all_shapes(slide.shapes))

    with pytest.raises(KeyError):
        adapt(presentation, slide, title="Night market", pictures={every.index(panel) + 1: image("x.png", (1, 2, 3))})


def test_a_drawing_inside_a_card_takes_only_the_wordless_group_around_it(tmp_path: Path, image) -> None:
    """Template pages keep everything one group down -- the card, its icon, its copy in one
    group -- and climbing to the top swapped a whole page's content for one picture (the
    blue template's four-card page). The icon's own group goes; the card and its words stay."""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
    from pptx.util import Inches

    from raven_ppt.services.template.compose import replace_picture

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    content = slide.shapes.add_group_shape()
    card = content.shapes.add_group_shape()
    body = card.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1), Inches(2), Inches(3), Inches(4))
    body.text_frame.text = "Sales up"
    icon = card.shapes.add_group_shape()
    icon.shapes.add_shape(MSO_SHAPE.OVAL, Inches(1.5), Inches(2.5), Inches(1), Inches(1))
    icon.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1.8), Inches(2.8), Inches(0.4), Inches(0.4))
    icon.name = "icon"

    picture = replace_picture(icon.shapes[1], image("icon.png", (10, 10, 200)))

    assert picture.name == "icon"
    assert [shape.shape_type for shape in card.shapes] == [MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.PICTURE]
    assert card.shapes[0].text_frame.text == "Sales up"
    assert len(slide.shapes) == 1 and slide.shapes[0].shape_type == MSO_SHAPE_TYPE.GROUP


def test_the_menu_lists_picture_filled_shapes_and_drawings_as_picture_slots(tmp_path: Path, image) -> None:
    """The amber template draws every photograph as a rounded rectangle filled with one and
    a section page's cartoon as a group of freeforms; counting `p:pic` alone told an author
    those pages held no picture, and its `pictures={2: ...}` was refused on three pages of
    one live run. Both are slots now, numbered as `adapt` numbers; a card icon is not."""
    from lxml import etree
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    from raven_ppt.services.template.menu import menu

    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    title = slide.shapes.add_textbox(Inches(0.7), Inches(0.3), Inches(8), Inches(1))
    title.text_frame.text = "Where the market sits"
    photo = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.7), Inches(1.5), Inches(5.4), Inches(3.6))
    _, relationship = photo.part.get_or_add_image_part(str(image("photo.png", (90, 90, 90))))
    fill = etree.SubElement(photo._element.spPr, f"{{{namespace}}}blipFill")
    etree.SubElement(fill, f"{{{namespace}}}blip").set(f"{{{rels}}}embed", relationship)
    cartoon = slide.shapes.add_group_shape()
    cartoon.shapes.add_shape(MSO_SHAPE.OVAL, Inches(7), Inches(2), Inches(2.4), Inches(2.4))
    cartoon.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(7.5), Inches(4.4), Inches(1.4), Inches(1.1))
    cartoon.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(8), Inches(4.4), Inches(0.3), Inches(1.1))
    band = slide.shapes.add_group_shape()
    band.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(6.4), Inches(13.333), Inches(1.1))
    band.shapes.add_shape(MSO_SHAPE.OVAL, Inches(1), Inches(6.5), Inches(1.5), Inches(1))
    card = slide.shapes.add_group_shape()
    body = card.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(10), Inches(2), Inches(2.9), Inches(4))
    body.text_frame.text = "Sales up"
    icon = card.shapes.add_group_shape()
    icon.shapes.add_shape(MSO_SHAPE.OVAL, Inches(10.2), Inches(2.2), Inches(0.7), Inches(0.7))
    icon.shapes.build_freeform(Inches(10.4), Inches(2.4)).add_line_segments(
        [(Inches(10.6), Inches(2.4)), (Inches(10.5), Inches(2.7))]
    ).convert_to_shape()
    path = tmp_path / "slots.pptx"
    presentation.save(str(path))

    (entry,) = menu(path)

    assert entry.pictures == 1
    assert entry.picture_slots == ("[2] 5.4x3.6in photo", "[3] 2.4x3.5in drawing"), (
        "the photograph, the cartoon by its first member; not the page-wide band, not the card, not its icon"
    )
    assert "picture slots [2] 5.4x3.6in photo, [3] 2.4x3.5in drawing" in entry.line()


def test_adapt_names_a_drawing_a_picture_may_stand_in_for_when_a_key_misses(tmp_path: Path, image) -> None:
    """The refusal used to list every wordless shape as `shape`, so an author reading it could
    not tell the cartoon from a band; it now says which shapes a picture may take over."""
    import pytest

    from raven_ppt.services.template.compose import adapt

    presentation, slide, cartoon, star, panel = _drawing_page(tmp_path)

    with pytest.raises(KeyError) as caught:
        adapt(presentation, slide, title="Night market", pictures={99: image("x.png", (1, 2, 3))})

    assert "[2] drawing 2.0x4.0in (a picture may take its place)" in str(caught.value), "a member is named by its whole"
    assert "name a drawing listed below" in str(caught.value)


def test_the_menu_names_a_small_picture_an_icon_slot(tmp_path: Path) -> None:
    """The red template's page 4 carries three 1.7in seals over its three cards, and a live
    deck kept them as the marks on three phases of its own; the menu had called them
    photos. A picture no longer than ICON_MAX_IN a side is an icon slot, one per unit."""
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.measure.geometry import ICON_MAX_IN
    from raven_ppt.services.template.menu import ICON_SLOT_NOTE, menu

    seal = tmp_path / "seal.png"
    Image.new("RGB", (120, 120), (200, 30, 40)).save(seal)
    photo = tmp_path / "photo.png"
    Image.new("RGB", (400, 300), (90, 90, 90)).save(photo)
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_picture(str(seal), Inches(1.6), Inches(2.5), Inches(1.7), Inches(1.7))
    slide.shapes.add_picture(str(photo), Inches(5.0), Inches(2.5), Inches(5.3), Inches(2.9))
    path = tmp_path / "slots.pptx"
    presentation.save(str(path))

    (entry,) = menu(path)

    assert ICON_MAX_IN == 1.8
    assert entry.picture_slots == ("[1] 1.7x1.7in icon", "[2] 5.3x2.9in photo")
    assert "one per unit" in ICON_SLOT_NOTE and "swap_icon" in ICON_SLOT_NOTE and "drop=[n, ...]" in ICON_SLOT_NOTE


def test_a_small_transparent_glyph_is_an_icon_slot_and_the_same_glyph_grown_is_a_cut_out(tmp_path: Path) -> None:
    """Size before transparency: a transparent glyph within ICON_MAX_IN is a mark whichever
    way it is drawn, and a slot named `cut-out` would not carry ICON_SLOT_NOTE's one-per-unit
    ask. The same PNG past that size is the floating illustration a cut-out slot is for."""
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template.menu import menu

    glyph = tmp_path / "glyph.png"
    canvas = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    canvas.paste((30, 120, 120, 255), (60, 60, 140, 140))
    canvas.save(glyph)
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_picture(str(glyph), Inches(1.0), Inches(2.5), Inches(1.5), Inches(1.5))
    slide.shapes.add_picture(str(glyph), Inches(6.0), Inches(2.0), Inches(3.0), Inches(3.0))
    path = tmp_path / "glyphs.pptx"
    presentation.save(str(path))

    (entry,) = menu(path)

    assert entry.picture_slots == ("[1] 1.5x1.5in icon", "[2] 3.0x3.0in cut-out")


def test_the_menu_names_a_transparent_illustration_a_cut_out(tmp_path: Path) -> None:
    """The teal template's cartoons are PNGs two thirds transparent, floating on the page's
    ground with boxes that run into the title row; the menu called them photos, and an
    author put a photograph in one -- which hugged the title on the live page."""
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template.menu import menu

    cut = tmp_path / "cartoon.png"
    canvas = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    canvas.paste((30, 120, 120, 255), (100, 60, 300, 240))
    canvas.save(cut)
    photo = tmp_path / "photo.png"
    Image.new("RGB", (400, 300), (90, 90, 90)).save(photo)
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_picture(str(cut), Inches(7.1), Inches(1.0), Inches(5.0), Inches(3.3))
    slide.shapes.add_picture(str(photo), Inches(0.7), Inches(3.3), Inches(5.7), Inches(3.5))
    path = tmp_path / "slots.pptx"
    presentation.save(str(path))

    (entry,) = menu(path)

    assert entry.picture_slots == ("[1] 5.0x3.3in cut-out", "[2] 5.7x3.5in photo")


def test_an_opaque_picture_in_a_cut_outs_box_is_warned_about_and_a_cut_out_is_not(tmp_path: Path) -> None:
    import warnings

    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.template.compose import replace_picture

    cut = tmp_path / "cartoon.png"
    canvas = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    canvas.paste((30, 120, 120, 255), (100, 60, 300, 240))
    canvas.save(cut)
    photo = tmp_path / "photo.png"
    Image.new("RGB", (400, 300), (90, 90, 90)).save(photo)
    another = tmp_path / "stall.png"
    canvas.save(another)
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    frame = slide.shapes.add_picture(str(cut), Inches(7.1), Inches(1.0), Inches(5.0), Inches(3.3))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        replace_picture(frame, photo, "cover")
    said = [str(w.message) for w in caught if "cut-out" in str(w.message)]
    assert len(said) == 1 and "5.0x3.3in at 7.10, 1.00" in said[0] and "transparent=true" in said[0]

    frame2 = slide.shapes.add_picture(str(cut), Inches(1), Inches(1), Inches(5.0), Inches(3.3))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        replace_picture(frame2, another, "contain")
    assert not [w for w in caught if "cut-out" in str(w.message)], "a cut-out for a cut-out is what the slot wants"
