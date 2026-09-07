"""Whether "this page came from the template" can be measured. Calibrated, not guessed."""

from __future__ import annotations

from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches

from raven_ppt.services.measure.adherence import (
    FROM_PROTOTYPE,
    MIN_SHAPES,
    SHARES_LITTLE,
    TOLERANCE_IN,
    template_adherence,
    template_pictures,
    unit_marks,
)
from raven_ppt.services.template.compose import adapt, drop_shape


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
    slide = adapt(deck, source.slides[0])
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
    slide = adapt(deck, source.slides[0])
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
    adapt(deck, source.slides[0])
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
    slide = adapt(deck, source.slides[0])
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
    from raven_ppt.services.template import adapt

    template = _template(tmp_path / "template.pptx")
    source = Presentation(str(template))
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    adapt(deck, source.slides[0])
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
    assert "swap_icon" in finding.message and "drop=[n, ...]" in finding.message, "the way out rides the finding"


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
