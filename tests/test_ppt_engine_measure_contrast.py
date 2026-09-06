"""Text measured against the ground it landed on, and who drew the shape it sits in.

The ratio was pinned through `check_deck` and nothing exercised this module directly,
which is how a warning tier came to sit over the whole 2-to-3 band and report the
template designer's own accent panels. It is gone; what is measured here is that the
band is silent, that the refusal below it is not, and that the refusal names who drew
the shape -- a live deck was refused on six chevrons its own program drew and the
author waved it away as the template's own accent colour, and the ratios of the two
cases are 2.24:1 and 2.30:1, so the number can never tell them apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pptx")

from pptx import Presentation  # noqa: E402 -- after the skip, or collection errors
from pptx.dml.color import RGBColor  # noqa: E402
from pptx.util import Inches, Pt  # noqa: E402

from raven_ppt.contracts.findings import Severity  # noqa: E402
from raven_ppt.services.measure.contrast import contrast_findings  # noqa: E402
from raven_ppt.services.template.compose import adapt  # noqa: E402
from tests._ppt_engine_fixtures import deck, image, noise_image, noise_png, product_page, template_file  # noqa: F401

# Three of the bound template's own theme roles, and the two ratios they make between
# themselves: `background` on `accent` is 2.24:1, which is the band nothing reports,
# and `background` on `surface` is 1.07:1, which is no text at all.
_ACCENT = (0x50, 0xBB, 0xB6)
_SURFACE = (0xED, 0xF6, 0xF6)
_BACKGROUND = (0xFC, 0xFC, 0xFC)
# 13.333x7.5in at 72px per inch, which is the dpi the check renders at.
_PX_PER_INCH = 72
_CANVAS = (960, 540)
# The template's own words, which is how `adapt` finds the shape to write into.
_KICKER = "the template's own kicker"


def _label(page, text: str, *, left: float, top: float, ink: tuple[int, int, int] = _BACKGROUND) -> None:
    """A text box stating its own colour, which is the only ink this check judges."""
    box = page.shapes.add_textbox(Inches(left), Inches(top), Inches(3.0), Inches(0.6))
    run = box.text_frame.paragraphs[0].add_run()
    run.text = text
    run.font.size = Pt(20)
    run.font.color.rgb = RGBColor(*ink)


def _template(path: Path) -> Path:
    """A prototype page: filler nobody would type by accident, and a white label."""
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    for index in range(5):
        page.shapes.add_textbox(Inches(0.63 + index * 1.87), Inches(2.13), Inches(1.71), Inches(0.83))
    _label(page, _KICKER, left=1.41, top=4.63, ink=(0xFF, 0xFF, 0xFF))
    presentation.save(str(path))
    return path


def _prepared(path: Path) -> Path:
    """The copy the build is pointed at: a real template with its example pages gone."""
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    presentation.save(str(path))
    return path


def _rendered(path: Path, *boxes: tuple[float, float, float, float], ground: tuple[int, int, int] = _SURFACE) -> Path:
    """A page painted white, with a stated ground under each box named."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", _CANVAS, (0xFF, 0xFF, 0xFF))
    draw = ImageDraw.Draw(image)
    for left, top, width, height in boxes:
        draw.rectangle(
            [
                left * _PX_PER_INCH,
                top * _PX_PER_INCH,
                (left + width) * _PX_PER_INCH,
                (top + height) * _PX_PER_INCH,
            ],
            fill=ground,
        )
    image.save(path)
    return path


def _composed(path: Path, text: str = "创建/加入 项目") -> Path:
    """A page the program drew: enough shapes to place, at nobody else's coordinates."""
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    for index in range(4):
        page.shapes.add_textbox(Inches(7.22 + index * 0.31), Inches(0.44), Inches(0.29), Inches(0.27))
    _label(page, text, left=8.11, top=3.07)
    presentation.save(str(path))
    return path


def _cloned(path: Path, template: Path) -> Path:
    """The same page, arrived at the way a build arrives at one: cloned from the file.

    The label is named in `texts=` because `adapt` empties every shape it is not told
    about, and a shape with no words in it is not measured.
    """
    source = Presentation(str(template))
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    adapt(deck, source.slides[0], texts={_KICKER: "本页自己的小标题"})
    deck.save(str(path))
    return path


def _one(deck: Path, page: Path, prototypes: Path | None):
    findings = contrast_findings(deck, None, pages=[page], prototypes=prototypes)
    assert len(findings) == 1, [finding.message for finding in findings]
    return findings[0]


def test_the_band_a_template_designs_in_is_not_reported(tmp_path: Path) -> None:
    """The whole of what the removed warning tier used to say.

    Both pages set the template's own `background` role against its own `accent`. One
    is the program's own composition at 2.24:1 and one is the template's own page
    cloned at 2.30:1 -- the live pair, and no threshold inside the band separates
    them. A bound template's own eleven example pages produced four of these, and a
    category that is usually the designer's own work is what taught a live author to
    answer a real refusal with "acceptable per spec note".
    """
    template = _template(tmp_path / "template.pptx")
    authored = _composed(tmp_path / "authored.pptx")
    cloned = _cloned(tmp_path / "cloned.pptx", template)
    over_accent = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6), (1.41, 4.63, 3.0, 0.6), ground=_ACCENT)

    assert contrast_findings(authored, None, pages=[over_accent], prototypes=template) == []
    assert contrast_findings(cloned, None, pages=[over_accent], prototypes=template) == []


def test_the_same_ink_on_the_theme_s_own_surface_is_refused(tmp_path: Path) -> None:
    """And the row that stays: `background` on `surface`, 1.07:1, the confusion this
    file was built for -- a ground colour reached for as if it were the ink."""
    deck = _composed(tmp_path / "deck.pptx")
    page = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6))

    finding = _one(deck, page, None)

    assert finding.kind == "unreadable"
    assert finding.severity == Severity.BLOCKING
    assert finding.detail["ratio"] == 1.07
    assert "at which the characters stop being there at all" in finding.message


def test_a_shape_the_program_drew_is_named_as_the_programs(tmp_path: Path) -> None:
    """The live case: six chevrons on a page the program composed, waved away as the
    template's accent colour. The excuse is not available, and the finding says so."""
    template = _template(tmp_path / "template.pptx")
    deck = _composed(tmp_path / "deck.pptx")
    page = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6))

    finding = _one(deck, page, template)

    assert finding.detail["drawn_by"] == "authored"
    assert "prototype" not in finding.detail
    assert "this shape is your own program's" in finding.message
    assert "sits nowhere any of the template's own pages puts one" in finding.message


def test_a_shape_the_template_drew_is_named_as_the_templates_and_still_refused(tmp_path: Path) -> None:
    """The other half of the answer, cloned the way a build clones one.

    Naming the template as the author of the shape is not an escape here: 1.1:1 is no
    text whoever drew it, and what the provenance decides is which file the fix goes
    into, not whether there is one.
    """
    template = _template(tmp_path / "template.pptx")
    deck = _cloned(tmp_path / "deck.pptx", template)
    page = _rendered(tmp_path / "page-001.png", (1.41, 4.63, 3.0, 0.6))

    finding = _one(deck, page, template)

    assert finding.kind == "unreadable"
    assert finding.severity == Severity.BLOCKING
    assert finding.detail["drawn_by"] == "template"
    assert finding.detail["prototype"] == 1
    assert "this shape is the template's own" in finding.message
    assert "the page is a clone of the template's page 1" in finding.message


def test_with_no_template_the_finding_says_nothing_about_who_drew_it(tmp_path: Path) -> None:
    """An absent input is not a defect: the finding keeps the question open rather
    than answering it from nothing."""
    deck = _composed(tmp_path / "deck.pptx")
    page = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6))

    finding = _one(deck, page, None)

    assert "drawn_by" not in finding.detail
    assert "your own program's" not in finding.message
    assert "the template's own -- the page is a clone" not in finding.message


def test_a_prepared_copy_holds_no_prototype_and_so_settles_nothing(tmp_path: Path) -> None:
    """The distinction the first version of the adherence check got wrong.

    `template` is the copy the build opens and its example pages are removed, so it
    holds no page a shape could have been cloned from. Compared against that, every
    shape in every deck would read as the author's -- a wrong answer everywhere, where
    silence is the right one.
    """
    deck = _composed(tmp_path / "deck.pptx")
    page = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6))

    finding = _one(deck, page, _prepared(tmp_path / "prepared.pptx"))

    assert "drawn_by" not in finding.detail
    assert "your own program's" not in finding.message


def test_a_page_too_sparse_to_place_says_nothing(tmp_path: Path) -> None:
    """Two boxes can sit where the template puts one by coincidence, which is the
    reason `adherence` refuses to read a page this sparse -- so this one does too."""
    template = _template(tmp_path / "template.pptx")
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    divider = presentation.slides.add_slide(presentation.slide_layouts[6])
    divider.shapes.add_textbox(Inches(0.63), Inches(2.13), Inches(1.71), Inches(0.83))
    _label(divider, "a divider and nothing else", left=1.41, top=4.63)
    deck = tmp_path / "deck.pptx"
    presentation.save(str(deck))
    page = _rendered(tmp_path / "page-001.png", (1.41, 4.63, 3.0, 0.6))

    finding = _one(deck, page, template)

    assert "drawn_by" not in finding.detail
    assert "the template's own -- the page is a clone" not in finding.message


def test_a_box_the_program_laid_over_a_cloned_page_is_the_programs(tmp_path: Path) -> None:
    """Cloning the page for its background and adding the copy on top -- the shape
    `template_underlay` is about. The page is the template's; this box is not."""
    template = _template(tmp_path / "template.pptx")
    deck = tmp_path / "deck.pptx"
    source = Presentation(str(template))
    built = Presentation()
    built.slide_width, built.slide_height = Inches(13.333), Inches(7.5)
    page = adapt(built, source.slides[0], texts={_KICKER: ""})
    _label(page, "the program's own copy", left=8.11, top=3.07)
    built.save(str(deck))

    finding = _one(deck, _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6)), template)

    assert finding.detail["drawn_by"] == "authored"
    assert "this shape is your own program's" in finding.message


def test_provenance_changes_what_the_finding_says_and_nothing_else(tmp_path: Path) -> None:
    """Same kind, same severity, same ratio, same page, with and without the template.

    The change is what the finding says about the shape it measured. Which shapes are
    measured, where the floor sits and what a deck is refused for are all untouched.
    """
    template = _template(tmp_path / "template.pptx")
    deck = _composed(tmp_path / "deck.pptx")
    page = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6))

    known = _one(deck, page, template)
    unknown = _one(deck, page, None)

    assert known.kind == unknown.kind == "unreadable"
    assert known.severity == unknown.severity == Severity.BLOCKING
    assert known.page == unknown.page == 1
    assert known.detail["ratio"] == unknown.detail["ratio"] == 1.07
    assert known.detail["blocks"] == unknown.detail["blocks"]


def test_a_page_whose_worst_block_is_one_mark_is_not_refused(tmp_path: Path) -> None:
    """A `·` is a few pixels of glyph in a crop of ground, so it measures as the ground
    against itself. It used to cost a warning and now it would cost the deck, which is
    the failure this file was rewritten once for when every comma was reported."""
    deck = _composed(tmp_path / "deck.pptx", text="·")
    page = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6))

    assert contrast_findings(deck, None, pages=[page], prototypes=_template(tmp_path / "template.pptx")) == []


def test_a_mark_measured_worse_than_the_copy_beside_it_does_not_clear_the_page(tmp_path: Path) -> None:
    """The page carries both, and the mark measures worse. It is not the mark that is
    refused, and it does not get to speak for the copy: filtering the page's single
    worst block instead of the candidates let one dim `·` take the refusal down with
    it, and the title nobody could see went out."""
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    for index in range(4):
        page.shapes.add_textbox(Inches(7.22 + index * 0.31), Inches(0.44), Inches(0.29), Inches(0.27))
    # The mark on `surface`, which reads as the ground against itself; the copy on the
    # accent, which is the 2.24:1 case this file is built around, in `background` ink.
    _label(page, "\u00b7", left=8.11, top=3.07)
    _label(page, "the title nobody can see", left=1.20, top=3.07)
    deck = tmp_path / "deck.pptx"
    presentation.save(str(deck))
    rendered = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6), (1.20, 3.07, 3.0, 0.6))

    found = contrast_findings(deck, None, pages=[rendered], prototypes=_template(tmp_path / "template.pptx"))

    assert [f.kind for f in found] == ["unreadable"]
    assert found[0].severity is Severity.BLOCKING
    assert "the title nobody can see" in found[0].detail["text"]
    # One block, not two: the mark was never a candidate, so it is not counted either.
    assert found[0].detail["blocks"] == 1


def test_the_refusal_names_who_drew_the_shape_on_a_dark_template_too(tmp_path: Path) -> None:
    """The case this check was built for, and the other direction of the confusion:
    `surface` reached for as the ink on a dark deck, #1A1A1A on #000000."""
    template = _template(tmp_path / "template.pptx")
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    for index in range(4):
        page.shapes.add_textbox(Inches(7.22 + index * 0.31), Inches(0.44), Inches(0.29), Inches(0.27))
    _label(page, "the title nobody can see", left=8.11, top=3.07, ink=(0x1A, 0x1A, 0x1A))
    deck = tmp_path / "deck.pptx"
    presentation.save(str(deck))
    rendered = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6), ground=(0x00, 0x00, 0x00))

    finding = _one(deck, rendered, template)

    assert finding.kind == "unreadable"
    assert finding.severity == Severity.BLOCKING
    assert finding.detail["drawn_by"] == "authored"
    assert "this shape is your own program's" in finding.message
