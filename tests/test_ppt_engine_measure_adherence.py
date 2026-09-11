"""Whether "this page came from the template" can be measured. Calibrated, not guessed."""

from __future__ import annotations

from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches

from raven_ppt.contracts.findings import Severity
from raven_ppt.services.measure.adherence import (
    FROM_PROTOTYPE,
    MIN_SHAPES,
    SHARES_LITTLE,
    TOLERANCE_IN,
    template_adherence,
    template_pictures,
    unit_marks,
)
from raven_ppt.services.template.compose import clone_page, drop_shape


def _template(path: Path) -> Path:
    """Two designed pages, each a cluster of boxes nobody would type by accident."""
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    for page_index in range(2):
        page = presentation.slides.add_slide(presentation.slide_layouts[6])
        for index in range(6):
            left = 0.63 + index * 1.87 + page_index * 0.11
            page.shapes.add_textbox(Inches(left), Inches(2.13), Inches(1.71), Inches(0.83))
    presentation.save(str(path))
    return path


def test_the_thresholds_are_the_measured_ones() -> None:
    """Three measured populations, not two: 1.00 adapted, 0.86 adapted-and-changed-
    hard, 0.02-0.06 for a deck that used its template as a background colour."""
    assert FROM_PROTOTYPE == 0.5
    assert SHARES_LITTLE == 0.2
    assert MIN_SHAPES == 4
    assert TOLERANCE_IN == 0.05


def test_a_page_changed_hard_is_still_its_own(tmp_path: Path) -> None:
    """Adapting means changing: deleting a third, moving some, adding some.

    The measurement has to survive that or it is a rule against editing. Deleting
    costs nothing -- a shape that is gone is not counted -- moving inside the
    tolerance costs nothing, and what is added costs only its own share.
    """
    template = _template(tmp_path / "template.pptx")
    source = Presentation(str(template))
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    slide = clone_page(deck, source.slides[0])
    for shape in list(slide.shapes)[:2]:
        drop_shape(shape)
    kept = list(slide.shapes)
    kept[0].left += Inches(0.04)  # nudged inside the tolerance
    slide.shapes.add_textbox(Inches(9.9), Inches(4.7), Inches(2.2), Inches(1.1))

    built = tmp_path / "changed.pptx"
    deck.save(str(built))
    assert template_adherence(built, template) == []


def test_a_page_that_kept_only_the_frame_is_counted_not_named(tmp_path: Path) -> None:
    """Between the two thresholds: the template's frame with a body rebuilt in it.

    A legitimate way to work, so it appears in the count and is not one of the pages
    the finding names -- and if every page is like that, there is nothing to report.
    """
    template = _template(tmp_path / "template.pptx")
    source = Presentation(str(template))
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    slide = clone_page(deck, source.slides[0])
    for shape in list(slide.shapes)[:4]:
        drop_shape(shape)
    for index in range(5):
        slide.shapes.add_textbox(Inches(0.6 + index * 2.4), Inches(5.1), Inches(2.1), Inches(0.9))

    built = tmp_path / "framed.pptx"
    deck.save(str(built))
    assert template_adherence(built, template) == []


def test_a_page_adapted_from_a_prototype_reads_as_adapted(tmp_path: Path) -> None:
    template = _template(tmp_path / "template.pptx")
    source = Presentation(str(template))
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    clone_page(deck, source.slides[0])
    built = tmp_path / "cloned.pptx"
    deck.save(str(built))

    assert template_adherence(built, template) == []


def test_an_adapted_page_stays_adapted_after_editing(tmp_path: Path) -> None:
    """A third of the shapes deleted and the words replaced: still the template's page.

    Cloning deep-copies the XML, so what survives every edit short of moving things
    is where the shapes are -- which is why geometry is what this measures.
    """
    template = _template(tmp_path / "template.pptx")
    source = Presentation(str(template))
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    slide = clone_page(deck, source.slides[0])
    for shape in list(slide.shapes)[:2]:
        drop_shape(shape)
    built = tmp_path / "edited.pptx"
    deck.save(str(built))

    assert template_adherence(built, template) == []


def test_a_deck_that_never_opens_in_the_template_is_named(tmp_path: Path) -> None:
    """What this check is for after the content pages stopped being prototypes.

    Counting how many pages sat on one of the template's was the old question and it is
    the wrong one now: a content page is composed rather than filled, so a deck whose
    every argument page is drawn is right, and the old wording asked it to clone more.
    What still has to hold is the frame -- the cover, the contents, the divider, the
    closing -- and a deck that opens on none of them is a deck in nobody's template.
    """
    template = _template(tmp_path / "template.pptx")
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    for _ in range(2):
        page = deck.slides.add_slide(deck.slide_layouts[6])
        for index in range(6):
            page.shapes.add_textbox(Inches(0.5 + index), Inches(4.0), Inches(0.9), Inches(0.5))
    built = tmp_path / "drawn.pptx"
    deck.save(str(built))

    findings = template_adherence(built, template)
    assert [f.kind for f in findings] == ["template_adherence"]
    assert findings[0].detail["structural"] == {"cover": 1}
    assert "cover (page 1)" in findings[0].message


def test_a_deck_that_opens_in_the_template_is_left_alone(tmp_path: Path) -> None:
    """One cloned page is enough, however many of the others are drawn."""
    from raven_ppt.services.template import clone_page

    template = _template(tmp_path / "template.pptx")
    source = Presentation(str(template))
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    clone_page(deck, source.slides[0])
    for _ in range(3):
        page = deck.slides.add_slide(deck.slide_layouts[6])
        for index in range(6):
            page.shapes.add_textbox(Inches(0.4 + index * 1.1), Inches(5.0), Inches(0.8), Inches(0.6))
    built = tmp_path / "framed.pptx"
    deck.save(str(built))

    assert template_adherence(built, template) == []


def test_a_deck_with_no_template_is_not_measured(tmp_path: Path) -> None:
    deck = Presentation()
    page = deck.slides.add_slide(deck.slide_layouts[6])
    for index in range(6):
        page.shapes.add_textbox(Inches(0.5 + index), Inches(4.0), Inches(0.9), Inches(0.5))
    built = tmp_path / "plain.pptx"
    deck.save(str(built))

    assert template_adherence(built, None) == []
    assert template_adherence(built, tmp_path / "missing.pptx") == []


@pytest.mark.parametrize("shapes", [1, 3])
def test_a_page_with_almost_nothing_on_it_is_not_judged(tmp_path: Path, shapes: int) -> None:
    """A divider or a quote: two boxes can match a template page by coincidence."""
    template = _template(tmp_path / "template.pptx")
    deck = Presentation()
    page = deck.slides.add_slide(deck.slide_layouts[6])
    for index in range(shapes):
        page.shapes.add_textbox(Inches(0.5 + index), Inches(4.0), Inches(0.9), Inches(0.5))
    built = tmp_path / "sparse.pptx"
    deck.save(str(built))

    assert template_adherence(built, template) == []


def test_a_photograph_used_as_a_shape_fill_is_still_the_templates(tmp_path: Path) -> None:
    """A template's photograph is as often a rounded rectangle filled with one as it is
    a picture frame -- that is how a designer gets a soft corner on a photo. python-pptx
    calls the first a PICTURE and the second a FREEFORM, `shape.image` raises on the
    second, and this check missed every one of those: a delivered deck kept the
    template's own stock photograph of a meeting table on its contents page, 27% of the
    canvas, named `PictureMisc1`, and nothing reported it.
    """
    from PIL import Image
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    photo = tmp_path / "photo.png"
    Image.new("RGB", (900, 600), (90, 90, 90)).save(photo)

    def _filled(path: Path) -> Path:
        presentation = Presentation()
        presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(8), Inches(1), Inches(4), Inches(5))
        # The fill python-pptx has no API for, written the way a template writes it.
        _, relationship = shape.part.get_or_add_image_part(str(photo))
        namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
        rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        from lxml import etree

        fill = etree.SubElement(shape._element.spPr, f"{{{namespace}}}blipFill")
        etree.SubElement(fill, f"{{{namespace}}}blip").set(f"{{{rels}}}embed", relationship)
        presentation.save(str(path))
        return path

    template = _filled(tmp_path / "template.pptx")
    deck = _filled(tmp_path / "deck.pptx")

    findings = template_pictures(deck, template)
    assert [f.kind for f in findings] == ["template_picture"]
    assert findings[0].detail["pages"] == {"1": 1}, "the fill counted once, not once per ancestor"


def _bundled(monkeypatch, tmp_path: Path, stem: str, template: Path) -> Path:
    """Make `template` answer as the bundled template called `stem`."""
    from raven_ppt.services.template import defaults

    folder = tmp_path / "bundled"
    folder.mkdir(exist_ok=True)
    target = folder / f"{stem}.pptx"
    target.write_bytes(template.read_bytes())
    monkeypatch.setattr(defaults, "templates_dir", lambda: folder)
    return target


def test_a_borrowed_page_is_held_to_the_file_it_borrowed_from(tmp_path: Path, monkeypatch) -> None:
    from raven_ppt.services.measure.adherence import prototype_kept

    """The plan says `borrowed` and `prototype`; the check opens that file, not the bound one."""
    from raven_ppt.contracts.outline import Outline, PagePlan

    bound = _template(tmp_path / "bound.pptx")
    other = Presentation()
    other.slide_width, other.slide_height = Inches(13.333), Inches(7.5)
    page = other.slides.add_slide(other.slide_layouts[6])
    for index in range(6):
        page.shapes.add_textbox(Inches(0.5 + index * 2.0), Inches(4.4), Inches(1.5), Inches(0.6))
    other.save(str(tmp_path / "other.pptx"))
    lender = _bundled(monkeypatch, tmp_path, "lender", tmp_path / "other.pptx")

    deck = Presentation(str(lender))
    deck.save(str(tmp_path / "deck.pptx"))
    plan = Outline(takeaway="t", pages=(PagePlan(page=1, claim="c", prototype=1, borrowed="lender"),))

    assert prototype_kept(tmp_path / "deck.pptx", bound, plan) == [], "page 1 is the lender's page 1, kept"

    wrong = Outline(takeaway="t", pages=(PagePlan(page=1, claim="c", prototype=1),))
    found = prototype_kept(tmp_path / "deck.pptx", bound, wrong)
    assert [finding.kind for finding in found] == ["prototype_kept"], "read against the bound template it is not"


def test_a_borrowed_prototype_names_the_lender_in_its_remedy(tmp_path: Path, monkeypatch) -> None:
    from raven_ppt.contracts.outline import Outline, PagePlan
    from raven_ppt.services.measure.adherence import prototype_kept

    bound = _template(tmp_path / "bound.pptx")
    lender = _bundled(monkeypatch, tmp_path, "lender", bound)
    other = Presentation()
    other.slide_width, other.slide_height = Inches(13.333), Inches(7.5)
    page = other.slides.add_slide(other.slide_layouts[6])
    for index in range(6):
        page.shapes.add_textbox(Inches(0.5 + index * 2.0), Inches(4.4), Inches(1.5), Inches(0.6))
    other.save(str(tmp_path / "deck.pptx"))
    plan = Outline(takeaway="t", pages=(PagePlan(page=1, claim="c", prototype=2, borrowed="lender"),))

    found = prototype_kept(tmp_path / "deck.pptx", None, plan)

    assert len(found) == 1
    assert "bundled template lender's page 2" in found[0].message
    assert "prototype(bundled('lender'), 2)" in found[0].message
    assert lender.is_file()


def test_placeholder_copy_reads_the_borrowed_files_too(tmp_path: Path) -> None:
    from raven_ppt.services.measure.adherence import placeholder_copy

    lender = Presentation()
    lender.slide_width, lender.slide_height = Inches(13.333), Inches(7.5)
    page = lender.slides.add_slide(lender.slide_layouts[6])
    box = page.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.text_frame.text = "借来的模板自己的示例文字"
    lender.save(str(tmp_path / "lender.pptx"))
    bound = _template(tmp_path / "bound.pptx")
    lender.save(str(tmp_path / "deck.pptx"))

    assert placeholder_copy(tmp_path / "deck.pptx", bound) == [], "the bound template never said it"
    found = placeholder_copy(tmp_path / "deck.pptx", bound, [tmp_path / "lender.pptx"])
    assert [finding.kind for finding in found] == ["placeholder_copy"]
    assert "借来的模板自己的示例文字" in found[0].message


def _a_page_with_a_chart(path: Path, categories: tuple[str, ...], series: str) -> Path:
    """One page whose only copy is inside a chart: its categories and its series name.

    What page 14 of `20260909_132114_9f5065` shipped. `adapt` cloned the template's
    page 6, emptied the frames the call had not named, and never reached the chart, so
    the template's own quarters stayed in the axis and its "add text here" stayed in
    the legend -- and every check that reads a page's copy looked straight past them,
    because a graphic frame has no text frame.
    """
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    data = CategoryChartData()
    data.categories = categories
    data.add_series(series, (18.6, 24.1, 31.4, 44.9))
    page.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1.0), Inches(1.6), Inches(8.0), Inches(4.5), data)
    presentation.save(str(path))
    return path


def test_a_placeholder_left_in_a_chart_is_found(tmp_path: Path) -> None:
    """The half of this check that was missing. `texts=` cannot reach a chart, so the
    advice says where the copy actually is.

    The four quarters beside it are reported too, and used not to be: a character floor
    of five let them through, and four characters of Chinese is a whole phrase. Page 14
    of `20260909_132114_9f5065` shipped all four."""
    from raven_ppt.services.measure.adherence import placeholder_copy

    quarters = ("第一季度", "第二季度", "第三季度", "第四季度")
    template = _a_page_with_a_chart(tmp_path / "template.pptx", quarters, "单击此处添加文本")
    deck = _a_page_with_a_chart(tmp_path / "deck.pptx", quarters, "单击此处添加文本")

    found = placeholder_copy(deck, template)

    assert sorted(finding.detail["text"] for finding in found) == sorted([*quarters, "单击此处添加文本"])
    assert {finding.detail["holder"] for finding in found} == {"chart"}
    assert {finding.severity for finding in found} == {Severity.BLOCKING}
    assert "`replace_text` does not reach" in found[0].message


def test_a_chart_the_deck_wrote_its_own_readings_into_is_left_alone(tmp_path: Path) -> None:
    from raven_ppt.services.measure.adherence import placeholder_copy

    template = _a_page_with_a_chart(
        tmp_path / "template.pptx", ("第一季度", "第二季度", "第三季度", "第四季度"), "单击此处添加文本"
    )
    deck = _a_page_with_a_chart(tmp_path / "deck.pptx", ("FY2023", "FY2024", "FY2025", "FY2026"), "Data Center")

    assert placeholder_copy(deck, template) == []


def test_a_closing_line_the_plan_asked_for_is_not_a_placeholder(tmp_path: Path) -> None:
    """Page 16 of `20260909_132114_9f5065`. Its plan asked for `Close: Thank you.`, the
    template's own closing page says `Thank you`, and this check refused the deck over
    the coincidence -- so the author's only way through was to deface a correct page,
    while the real placeholder two pages earlier went unreported."""
    from raven_ppt.contracts.outline import Outline, PagePlan
    from raven_ppt.services.measure.adherence import placeholder_copy

    template = Presentation()
    template.slide_width, template.slide_height = Inches(13.333), Inches(7.5)
    closing = template.slides.add_slide(template.slide_layouts[6])
    closing.shapes.add_textbox(Inches(4), Inches(3), Inches(5), Inches(1)).text_frame.text = "Thank you"
    template.save(str(tmp_path / "template.pptx"))
    template.save(str(tmp_path / "deck.pptx"))

    unplanned = Outline(takeaway="t", pages=(PagePlan(page=1, claim="Closing", prototype=1),))
    planned = Outline(
        takeaway="t",
        pages=(PagePlan(page=1, claim="Key takeaways - and thank you", says=("Close: Thank you.",), prototype=1),),
    )

    assert len(placeholder_copy(tmp_path / "deck.pptx", tmp_path / "template.pptx", (), unplanned)) == 1
    assert placeholder_copy(tmp_path / "deck.pptx", tmp_path / "template.pptx", (), planned) == []


def test_a_dividers_own_numbering_is_not_a_placeholder(tmp_path: Path) -> None:
    """Three of these refused a delivered deck. "PART 01" on a section divider is the
    template's page numbering with a word for what it numbers, and nobody was ever
    meant to replace it."""
    from raven_ppt.services.measure.adherence import placeholder_copy

    template = Presentation()
    template.slide_width, template.slide_height = Inches(13.333), Inches(7.5)
    divider = template.slides.add_slide(template.slide_layouts[6])
    divider.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text_frame.text = "PART 01"
    divider.shapes.add_textbox(Inches(1), Inches(3), Inches(8), Inches(1)).text_frame.text = "单击此处添加章节标题"
    template.save(str(tmp_path / "template.pptx"))
    template.save(str(tmp_path / "deck.pptx"))

    found = placeholder_copy(tmp_path / "deck.pptx", tmp_path / "template.pptx")

    assert [finding.detail["text"] for finding in found] == ["单击此处添加章节标题"]


def _one_page(path: Path, blocks: tuple[str, ...]) -> Path:
    return _text_pages(path, blocks)


def _text_pages(path: Path, *pages: tuple[str, ...]) -> Path:
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    for blocks in pages:
        page = presentation.slides.add_slide(presentation.slide_layouts[6])
        for index, text in enumerate(blocks):
            box = page.shapes.add_textbox(Inches(1), Inches(0.6 + index * 0.8), Inches(6), Inches(0.6))
            box.text_frame.text = text
    presentation.save(str(path))
    return path


def test_four_characters_of_chinese_is_a_phrase_and_not_a_page_number(tmp_path: Path) -> None:
    """The defect this check was blind to. Page 10 of the one-door run's build was an
    otherwise English page whose heading still read `工作感悟`, and the character floor
    that let it through was written for Latin, where four characters is a page number."""
    from raven_ppt.services.measure.adherence import placeholder_copy

    template = _one_page(tmp_path / "template.pptx", ("工作感悟",))
    deck = _one_page(tmp_path / "deck.pptx", ("工作感悟",))

    found = placeholder_copy(deck, template)

    assert [finding.kind for finding in found] == ["placeholder_copy"]
    assert found[0].severity is Severity.BLOCKING
    assert "工作感悟" in found[0].message


def test_two_latin_words_of_the_templates_are_a_phrase(tmp_path: Path) -> None:
    from raven_ppt.services.measure.adherence import placeholder_copy

    template = _one_page(tmp_path / "template.pptx", ("Presenter name",))
    deck = _one_page(tmp_path / "deck.pptx", ("Presenter name",))

    found = placeholder_copy(deck, template)

    assert [finding.severity for finding in found] == [Severity.BLOCKING]


def test_the_marks_a_page_kept_arrive_as_one_warning(tmp_path: Path) -> None:
    """One line per page, not one per mark. A dozen findings about the template's
    glyphs is the noise D35 measured going unanswered, and the fold is what keeps the
    page readable -- the phrase beside them still refuses on its own."""
    from raven_ppt.services.measure.adherence import placeholder_copy

    marks = ("<", ">", ".", "text", "单击此处添加文本")
    template = _one_page(tmp_path / "template.pptx", marks)
    deck = _one_page(tmp_path / "deck.pptx", marks)

    found = placeholder_copy(deck, template)

    assert [finding.kind for finding in found] == ["placeholder_copy", "placeholder_marks"]
    assert found[0].severity is Severity.BLOCKING
    assert found[1].severity is Severity.WARNING
    assert found[1].detail["marks"] == [".", "<", ">", "text"]
    assert "4 of the template's own numerals and marks" in found[1].message
    assert "and 1 more" in found[1].message


def test_the_contents_pages_own_label_is_inherited_rather_than_unreplaced(tmp_path: Path) -> None:
    """Both halves of the label, and only on the page that plays the role.

    Eight delivered decks were refused over the `目录` their template writes on its own
    index page, which is the one string there the deck is meant to keep -- and the
    English half of the same label, `Agenda`, came back as a mark, so the two halves of
    one thing were graded oppositely. The exemption is the role's own name on the page
    cloned from the template's page for that role. Page 2 here is the counter-case: the
    same string in a content page's body is still a slot nobody filled, and the second
    line of the index page still refuses on its own.
    """
    from raven_ppt.contracts.outline import Outline, PagePlan
    from raven_ppt.services.measure.adherence import placeholder_copy

    index = ("目录", "Agenda", "单击添加小标题")
    template = _one_page(tmp_path / "template.pptx", index)
    deck = _text_pages(tmp_path / "deck.pptx", index, ("目录",))
    plan = Outline(
        takeaway="t",
        pages=(PagePlan(page=1, claim="Contents", prototype=1), PagePlan(page=2, claim="Where the money went")),
    )

    found = placeholder_copy(deck, template, (), plan)

    assert [(finding.page, finding.kind, finding.detail["text"]) for finding in found] == [
        (1, "placeholder_copy", "单击添加小标题"),
        (2, "placeholder_copy", "目录"),
    ]


def test_a_photograph_on_the_layout_is_reported_once_per_layout(tmp_path: Path) -> None:
    """The template's picture that `template_pictures` cannot see: it is on the layout
    every page inherits, not on the page. Named once with every page under it, because
    the fix is one call for the whole layout."""
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    from raven_ppt.services.measure.adherence import layout_photographs, layouts_with_photographs
    from tests._ppt_engine_fixtures import layout_picture

    image = tmp_path / "photo.png"
    Image.new("RGB", (800, 600), (90, 90, 90)).save(image)
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    layout = presentation.slide_layouts[6]
    layout_picture(layout, image, 0, 0, 6.4, 4.7)
    layout_picture(layout, image, 12, 7, 0.5, 0.4)  # a mark, not content
    presentation.slides.add_slide(layout)
    presentation.slides.add_slide(presentation.slide_layouts[5])
    presentation.slides.add_slide(layout)
    template = tmp_path / "template.pptx"
    presentation.save(str(template))
    deck = tmp_path / "deck.pptx"
    presentation.save(str(deck))

    carried = layouts_with_photographs(deck)
    assert list(carried.values()) == [([1, 3], ["6.4x4.7in"])]

    findings = layout_photographs(deck, template)
    assert [f.kind for f in findings] == ["layout_picture"]
    assert "under page(s) 1, 3" in findings[0].message
    assert "layout_pictures(slide)" in findings[0].message
    assert layout_photographs(deck, None) == [], "no template bound, nothing to call the template's own"


# --- The marks beside a page's units -----------------------------------------------
#
# Calibrated over the eight bundled templates, ten built decks and three delivered ones
# (297 pages): two pages fire, both of one delivered deck built in the red template --
# page 4 wore the template's three seals over four things (one seal twice), page 18 the
# same three over three phases -- and nothing else does, the templates' own pages
# included. Asked as "one image three times" it fired nowhere: the seals are three
# images.


def _marked_page(
    path: Path, marks: list[Path], *, headings: int = 3, band: bool = False, corners: Path | None = None
) -> Path:
    """`headings` narrow heading+body units in a row, a mark over each from `marks` (cycled)."""
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    title = slide.shapes.add_textbox(Inches(0.85), Inches(0.14), Inches(11.7), Inches(1.0))
    title.text_frame.text = "The page's title"
    if band:
        # A subtitle band the marks touch, the width of the page: the mark on nothing.
        subtitle = slide.shapes.add_textbox(Inches(0.72), Inches(1.5), Inches(11.9), Inches(0.85))
        subtitle.text_frame.text = "One line under the title, running across every unit"
    for index in range(headings):
        left = 0.8 + index * 4.2
        heading = slide.shapes.add_textbox(Inches(left), Inches(4.4), Inches(3.4), Inches(0.7))
        heading.text_frame.text = f"Thing {index + 1}"
        body = slide.shapes.add_textbox(Inches(left), Inches(5.2), Inches(3.4), Inches(1.2))
        body.text_frame.text = f"What thing {index + 1} is about, in a sentence."
        slide.shapes.add_picture(
            str(marks[index % len(marks)]), Inches(left + 0.85), Inches(2.35), Inches(1.7), Inches(1.7)
        )
    if corners is not None:
        slide.shapes.add_picture(str(corners), Inches(0), Inches(6.7), Inches(0.9), Inches(0.8))
        slide.shapes.add_picture(str(corners), Inches(12.5), Inches(0), Inches(0.8), Inches(0.7))
    presentation.save(str(path))
    return path


def _mark(path: Path, colour: tuple[int, int, int]) -> Path:
    from PIL import Image

    Image.new("RGB", (120, 120), colour).save(path)
    return path


def test_one_mark_beside_two_things_is_reported(tmp_path: Path) -> None:
    seal = _mark(tmp_path / "seal.png", (200, 30, 40))
    deck = _marked_page(tmp_path / "deck.pptx", [seal], headings=3)

    (finding,) = unit_marks(deck)

    assert finding.kind == "same_mark" and finding.page == 1
    assert finding.detail["things"] == ["Thing 1", "Thing 2", "Thing 3"]
    assert "2 of them repeat a mark" in finding.message
    assert "swap_icon" in finding.message and "drop_shape" in finding.message, "the way out rides the finding"


def test_the_templates_marks_kept_on_a_cloned_page_are_reported_and_its_own_page_is_not(tmp_path: Path) -> None:
    """Three different seals, the template's, over three phases of the author's: they tell
    the phases apart no better than one seal would. On the template's own page the same
    three are each their own, and nothing is said."""
    seals = [_mark(tmp_path / f"seal{index}.png", (200, 30 + index * 40, 40)) for index in range(3)]
    template = _marked_page(tmp_path / "template.pptx", seals)
    deck = _marked_page(tmp_path / "deck.pptx", seals)

    assert unit_marks(template) == [], "the template's own page: three marks, each its own"
    (finding,) = unit_marks(deck, template)
    assert "3 of the 3 marks are the template's own" in finding.message
    assert finding.detail == {"things": ["Thing 1", "Thing 2", "Thing 3"], "template_marks": 3, "repeated_marks": 0}


def test_a_mark_of_the_authors_own_on_each_thing_is_left_alone(tmp_path: Path) -> None:
    seals = [_mark(tmp_path / f"seal{index}.png", (200, 30 + index * 40, 40)) for index in range(3)]
    template = _marked_page(tmp_path / "template.pptx", seals)
    own = [_mark(tmp_path / f"own{index}.png", (30, 60 + index * 50, 200)) for index in range(3)]
    deck = _marked_page(tmp_path / "deck.pptx", own)

    assert unit_marks(deck, template) == []


def test_corner_ornaments_and_the_subtitle_band_mark_nothing(tmp_path: Path) -> None:
    """The red template puts the same ornament in two corners of every page, and its
    subtitle band touches the seals: neither is the mark on a unit. Without the band rule
    every seal on the live page read as the mark on the subtitle, and four marks on four
    things counted as one thing."""
    from raven_ppt.services.measure.adherence import BAND_SHARE, MARK_GAP_IN, MARKED_THINGS

    assert (MARK_GAP_IN, BAND_SHARE, MARKED_THINGS) == (1.0, 0.5, 2), (
        "calibrated on the live page: seals 0.3in over their headings, a subtitle band across the page"
    )
    seal = _mark(tmp_path / "seal.png", (200, 30, 40))
    ornament = _mark(tmp_path / "ornament.png", (120, 120, 120))
    only_corners = _marked_page(tmp_path / "corners.pptx", [seal], headings=0, corners=ornament)
    assert unit_marks(only_corners) == [], "two identical ornaments beside no unit"

    banded = _marked_page(tmp_path / "banded.pptx", [seal], headings=4, band=True)
    (finding,) = unit_marks(banded)
    assert finding.detail["things"] == ["Thing 1", "Thing 2", "Thing 3", "Thing 4"]


def test_a_single_marked_thing_is_not_read(tmp_path: Path) -> None:
    seal = _mark(tmp_path / "seal.png", (200, 30, 40))
    deck = _marked_page(tmp_path / "deck.pptx", [seal], headings=1)
    assert unit_marks(deck, deck) == [], "one thing wearing one mark says nothing about telling things apart"


def _house(path: Path) -> Path:
    """A template page with enough furniture to be measured, and its own example copy."""
    from pptx import Presentation
    from pptx.util import Inches

    built = Presentation()
    built.slide_width, built.slide_height = Inches(13.333), Inches(7.5)
    page = built.slides.add_slide(built.slide_layouts[6])
    page.shapes.add_textbox(
        Inches(0.72), Inches(0.5), Inches(9.0), Inches(0.9)
    ).text_frame.text = "单击此处添加页面标题"
    for index in range(4):
        left = Inches(0.72 + index * 3.1)
        page.shapes.add_textbox(left, Inches(2.4), Inches(2.8), Inches(0.6)).text_frame.text = "单击添加小标题"
        page.shapes.add_textbox(left, Inches(3.2), Inches(2.8), Inches(1.4)).text_frame.text = "单击此处添加正文内容"
    built.save(str(path))
    return path


def test_underlay_needs_a_box_that_was_actually_added(tmp_path: Path) -> None:
    """The finding says boxes were laid over the page, so it has to establish one was.

    Both halves are measured here because this check went blind and then went wrong.
    While the route since removed emptied the text a call did not name, the
    leftover-copy condition could never hold on it, so the only case this ever saw was
    the hand-built one. Once an unfilled frame keeps the template's words, a plain missed fill
    satisfies leftover-copy and template-geometry both -- and a live page was then
    condemned for a construction it had not used, having added no box at all.
    `placeholder_copy` already names a missed fill string by string, so the cost of
    requiring this one to mean what it says is nothing.
    """
    from pptx import Presentation

    from raven_ppt.services.measure.adherence import placeholder_copy, template_adherence
    from raven_ppt.services.template import clone_page, prototype, replace_text

    house = _house(tmp_path / "house.pptx")
    source = Presentation(str(house))

    # The construction the finding is about: cloned for the background, the copy laid
    # over the top in boxes of the author's own, nothing replaced.
    from pptx.util import Inches

    over = Presentation(str(house))
    for slide in list(over.slides._sldIdLst):
        over.slides._sldIdLst.remove(slide)
    page = clone_page(over, prototype(source, 1))
    for index in range(4):
        box = page.shapes.add_textbox(Inches(0.9 + index * 3.1), Inches(2.5), Inches(2.5), Inches(0.5))
        box.text_frame.text = f"This deck's own heading {index + 1}"
    laid = tmp_path / "laid-over.pptx"
    over.save(str(laid))

    found = [f.detail for f in template_adherence(laid, house) if f.kind == "template_underlay"]
    assert found, "the hand-built construction still has to be refused"
    assert found[0]["underlay"] == [1]
    assert found[0]["blocks_added"]["1"] == 4, "and the finding records the evidence it acted on"

    # A missed fill: the same prototype cloned, one heading written and the rest left as
    # the template wrote them. No box was added, so this is not underlay -- it is what
    # placeholder_copy is for.
    missed = Presentation(str(house))
    for slide in list(missed.slides._sldIdLst):
        missed.slides._sldIdLst.remove(slide)
    replace_text(clone_page(missed, prototype(source, 1)), "单击此处添加页面标题", "This deck's own title")
    quiet = tmp_path / "missed-fill.pptx"
    missed.save(str(quiet))

    assert [f.kind for f in template_adherence(quiet, house) if f.kind == "template_underlay"] == [], (
        "a page that added nothing must not be told it laid boxes over the template"
    )
    refused = placeholder_copy(quiet, house)
    assert refused, "the missed fill is still refused, by the check whose reason is right"
    assert all(f.page == 1 for f in refused)
