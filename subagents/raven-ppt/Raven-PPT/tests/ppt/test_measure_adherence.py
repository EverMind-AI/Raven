"""Whether "this page came from the template" can be measured. Calibrated, not guessed."""

from __future__ import annotations

from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches

from raven.ppt.services.measure.adherence import (
    FROM_PROTOTYPE,
    MIN_SHAPES,
    SHARES_LITTLE,
    TOLERANCE_IN,
    template_adherence,
    template_pictures,
)
from raven.ppt.services.template.compose import adapt, drop_shape


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
    from raven.ppt.services.template import adapt

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
