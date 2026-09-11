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
from raven_ppt.contracts.rendered import WordBox  # noqa: E402
from raven_ppt.services.measure.contrast import UNREADABLE_RATIO, contrast_findings  # noqa: E402
from raven_ppt.services.template.compose import clone_page, replace_text  # noqa: E402
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
# The template's own words, which is how `replace_text` finds the shape to write into.
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

    The label is written with `replace_text` because this is the shape the check is
    about: the measurement is of the words the deck itself put on the page.
    """
    source = Presentation(str(template))
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    replace_text(clone_page(deck, source.slides[0]), _KICKER, "本页自己的小标题")
    deck.save(str(path))
    return path


def _one(deck: Path, page: Path, prototypes: Path | None):
    findings = contrast_findings(deck, None, pages=[page], prototypes=prototypes)
    assert len(findings) == 1, [finding.message for finding in findings]
    return findings[0]


def _mixed(path: Path, stated: str, inherited: str) -> Path:
    """One block whose runs are split between a stated colour and an inherited one."""
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    for index in range(4):
        page.shapes.add_textbox(Inches(7.22 + index * 0.31), Inches(0.44), Inches(0.29), Inches(0.27))
    box = page.shapes.add_textbox(Inches(8.11), Inches(3.07), Inches(3.0), Inches(0.6))
    para = box.text_frame.paragraphs[0]
    quiet = para.add_run()
    quiet.text = inherited
    quiet.font.size = Pt(20)
    loud = para.add_run()
    loud.text = stated
    loud.font.size = Pt(20)
    loud.font.color.rgb = RGBColor(*_BACKGROUND)
    presentation.save(str(path))
    return path


def test_a_block_most_of_which_inherits_its_colour_is_not_judged_on_the_rest(tmp_path: Path) -> None:
    """The largest run of the *block*, not of whichever runs happen to state a colour.

    A bundled template has a body block of seventeen characters where sixteen inherit
    their colour and one states #F8F8F8. Judged on that character, the block read as
    near-white type -- and the ground under it is the white page, because the copy a
    reader sees there is the dark that the sixteen inherited. Seven of that template's
    own pages came back blocking at 1.0:1 with nothing wrong on any of them.
    """
    ground = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6), ground=_SURFACE)

    # One character stating it against sixteen inheriting: not stated here, not judged.
    minority = _mixed(tmp_path / "minority.pptx", "跨", "单击此处添加文本单击此处添加")
    assert contrast_findings(minority, None, pages=[ground]) == []

    # And the other way, which is what the rule is for: the stated runs carry the block,
    # so the block is judged and the same ink on the same ground is still refused.
    majority = _mixed(tmp_path / "majority.pptx", "本页要记住的一句话是这个", "跨")
    found = contrast_findings(majority, None, pages=[ground])
    assert [one.kind for one in found] == ["unreadable"], [one.message for one in found]
    assert found[0].severity is Severity.BLOCKING


def test_type_whose_colour_is_the_theme_s_is_judged_on_what_it_resolves_to(tmp_path: Path) -> None:
    """A run coloured by a theme slot states no rgb, and the check used to skip it as
    'not stated here'. A cloned template page colours nearly everything that way --
    twelve of thirteen blocks on one measured page -- so a 1.09:1 body line went
    unjudged. Resolved through the palette the way the reference resolves it, white
    type on the white page is refused; the theme's dark text on the same page is not."""
    from pptx.enum.dml import MSO_THEME_COLOR

    ground = _rendered(tmp_path / "page-001.png", (8.11, 3.07, 3.0, 0.6), ground=(0xFF, 0xFF, 0xFF))
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = page.shapes.add_textbox(Inches(8.11), Inches(3.07), Inches(3.0), Inches(0.6))
    run = box.text_frame.paragraphs[0].add_run()
    run.text = "本页要记住的一句话是这个"
    run.font.size = Pt(20)
    run.font.color.theme_color = MSO_THEME_COLOR.BACKGROUND_1
    path = tmp_path / "theme.pptx"
    presentation.save(str(path))

    found = contrast_findings(path, None, pages=[ground])
    assert [one.kind for one in found] == ["unreadable"], [one.message for one in found]
    assert found[0].detail["ink"] == "FFFFFF"

    run.font.color.theme_color = MSO_THEME_COLOR.TEXT_1
    presentation.save(str(path))
    assert contrast_findings(path, None, pages=[ground]) == []


def test_type_that_states_nothing_is_judged_on_what_the_master_gives_it(tmp_path: Path) -> None:
    """No colour anywhere on the run: the master's text style says what it is, and the
    check judges that instead of looking away. Dark on white reads, so nothing is
    reported -- the point is that the block was measured at all."""
    from PIL import Image

    from raven_ppt.services.measure.contrast import _declared, _measure
    from raven_ppt.services.template.decompile import page_design

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = page.shapes.add_textbox(Inches(1), Inches(1), Inches(3.0), Inches(0.6))
    box.text_frame.text = "本页要记住的一句话是这个"
    design = page_design(presentation, page)

    from PIL import ImageDraw

    rendered = Image.new("RGB", _CANVAS, (0xFF, 0xFF, 0xFF))
    # The glyphs, as the render would paint them: a dark stroke through the box.
    ImageDraw.Draw(rendered).rectangle([80, 88, 280, 100], fill=(0, 0, 0))

    assert _declared(box.text_frame, design) is None
    measured = _measure(box, rendered, 13.333, 7.5, design)
    assert measured is not None, "an inherited colour is a colour the reader sees"
    assert measured[0] > 10, "the master's dark text on white is readable"


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
    page = clone_page(built, source.slides[0])
    replace_text(page, _KICKER, "")
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


# The delivered page this reproduces: two white bullets across the bright crest of a
# green wave, at 12.0in of copy in a box 1.05in tall. The crest is narrow across the
# page, so a full-height column of that box holds two lines and their dark leading and
# the crest is a minority of it; the word the crest crosses sits mostly on it.
_RIDGE = (0x01, 0xE6, 0x98)
_DARK = (0x16, 0x1B, 0x1E)
_LINES = (224, 256)
_WORD_TOP, _WORD_BOTTOM = 220, 246
_WORDS = tuple(150 + index * 70 for index in range(6))
_WORD_WIDE = 60
_CROSSED = 3


def _wave_page(path: Path, *, ridge: bool) -> Path:
    """A dark page with two lines of glyph strokes, and a narrow bright ridge or not."""
    from PIL import Image

    image = Image.new("RGB", _CANVAS, _DARK)
    pixels = image.load()
    if ridge:
        left = _WORDS[_CROSSED] - 5
        for x in range(left, left + _WORD_WIDE - 10):
            for y in range(_WORD_TOP, _WORD_BOTTOM):
                pixels[x, y] = _RIDGE
    for top in _LINES:
        for start in _WORDS:
            for x in range(start, start + _WORD_WIDE, 12):
                for stroke in range(x, min(x + 4, start + _WORD_WIDE)):
                    for y in range(top, top + 16):
                        pixels[stroke, y] = _BACKGROUND
    image.save(path)
    return path


def _two_line_block(path: Path) -> Path:
    """One text box holding two lines, which is what makes a column hold both of them."""
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    for index in range(4):
        page.shapes.add_textbox(Inches(7.22 + index * 0.31), Inches(0.44), Inches(0.29), Inches(0.27))
    box = page.shapes.add_textbox(Inches(2.0), Inches(3.0), Inches(6.0), Inches(1.0))
    frame = box.text_frame
    frame.text = " ".join(_SPOKEN[:6])
    frame.add_paragraph().text = " ".join(_SPOKEN[6:])
    for para in frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(14)
            run.font.color.rgb = RGBColor(*_BACKGROUND)
    presentation.save(str(path))
    return path


_SPOKEN = (
    "market",
    "heading",
    "toward",
    "flexibility",
    "across",
    "grids",
    "returns",
    "depend",
    "wholly",
    "upon",
    "revenue",
    "discipline",
)


def _painted_words() -> list[WordBox]:
    """Where the render put each word, which is what a real run reads out of the PDF."""
    boxes = []
    for line, top in enumerate(_LINES):
        for index, left in enumerate(_WORDS):
            boxes.append(
                WordBox(
                    page=1,
                    text=_SPOKEN[line * 6 + index],
                    x0=left,
                    y0=_WORD_TOP,
                    x1=left + _WORD_WIDE,
                    y1=_WORD_BOTTOM,
                )
            )
    return boxes


def test_a_ridge_across_one_word_is_read_where_the_word_is(tmp_path: Path) -> None:
    """The delivered miss: a column of the declared box cannot see a narrow ridge.

    Page 12 of a delivered deck set two white bullets across the bright crest of a
    green wave. The declared box is 1707x151px over two lines, a column of it is
    226px wide, the crest is a minority of every column, and every column's commonest
    band came back #161B1E for 17.4:1 -- past all four guards, with none of them the
    cause. The same page's title, white on the dark half, reads 17.4:1 truthfully, so
    the page carried a passing and a failing case of one colour pair.
    """
    deck = _two_line_block(tmp_path / "deck.pptx")
    page = _wave_page(tmp_path / "page-001.png", ridge=True)

    assert contrast_findings(deck, None, pages=[page]) == []

    findings = contrast_findings(deck, None, pages=[page], words=_painted_words())

    assert [one.kind for one in findings] == ["unreadable"]
    assert findings[0].severity is Severity.BLOCKING
    assert findings[0].detail["word"] == _SPOKEN[_CROSSED]
    assert findings[0].detail["ratio"] < UNREADABLE_RATIO


def test_the_words_of_a_block_on_one_ground_report_nothing(tmp_path: Path) -> None:
    """The other half: word boxes may only make the reading worse, never noisier.

    The same block and the same words with the ridge taken away. A word box is tighter
    than a declared box and holds a larger share of glyphs, and reading the glyphs'
    own antialiasing as the ground would report every page on a dark deck.
    """
    deck = _two_line_block(tmp_path / "deck.pptx")
    page = _wave_page(tmp_path / "page-001.png", ridge=False)

    assert contrast_findings(deck, None, pages=[page], words=_painted_words()) == []


def _two_grounds(path: Path, right: tuple[int, int, int]) -> Path:
    """A page where the label's box crosses from one ground onto a second one.

    The live shape this reproduces is a chart's own label at (8.72, 5.66) 1.75in wide:
    its first four characters sit on the panel and its last five on the cream wedge
    beside it, and one ground for the whole box is the panel.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", _CANVAS, (0xFF, 0xFF, 0xFF))
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        [8.0 * _PX_PER_INCH, 2.9 * _PX_PER_INCH, 11.2 * _PX_PER_INCH, 3.8 * _PX_PER_INCH], fill=(0x96, 0x83, 0x6E)
    )
    draw.rectangle([9.7 * _PX_PER_INCH, 2.9 * _PX_PER_INCH, 11.2 * _PX_PER_INCH, 3.8 * _PX_PER_INCH], fill=right)
    image.save(path)
    return path


def test_a_label_that_crosses_onto_a_second_ground_is_judged_on_the_worse_one(tmp_path: Path):
    """The live miss: white on brown for four characters and white on cream for five.

    One ground per block read the brown at 3.43:1 and published; the cream the last
    five characters actually sit on is 1.08:1, which is what a reader gets.
    """
    deck = _composed(tmp_path / "deck.pptx", text="the label")
    page = _two_grounds(tmp_path / "page-001.png", right=(0xF5, 0xEF, 0xE4))

    findings = contrast_findings(deck, None, pages=[page])

    assert [one.kind for one in findings] == ["unreadable"]
    assert findings[0].detail["ratio"] < 1.3
    assert findings[0].severity is Severity.BLOCKING


def test_the_far_end_of_one_gradient_is_not_a_second_ground(tmp_path: Path):
    """Four pages across two bundled templates set white type over a gradient pill.

    Every one is readable, and every one was reported the moment slices were read at
    all. A gradient's two ends measure 1.26 to 1.77 against each other where a genuine
    second ground measured 3.18, so the ends of one fill are one ground.
    """
    deck = _composed(tmp_path / "deck.pptx", text="the label")
    page = _two_grounds(tmp_path / "page-001.png", right=(0xE3, 0xB7, 0x73))

    assert contrast_findings(deck, None, pages=[page]) == []


def test_a_bold_title_on_a_photograph_is_not_measured_against_its_own_strokes() -> None:
    """A cover's title fills half its line with glyphs, and on a photograph no colour is
    common, so the commonest bucket was the ink itself: '#2F2F2F on a ground that renders
    #2F2F2F, 1.0:1' on a page anyone could read, and the deck was never published."""
    import numpy as np

    from raven_ppt.services.measure.contrast import _ratio, _worst_ground

    rng = np.random.default_rng(7)
    crop = rng.integers(150, 250, size=(60, 400, 3), dtype=np.uint8)
    ink = (0x2F, 0x2F, 0x2F)
    for left in range(10, 390, 20):
        crop[8:52, left : left + 11] = ink

    read = _worst_ground(crop, ink)

    assert read is not None
    ground, _ = read
    assert _ratio(ink, ground) > 4, f"the ground is the photograph, not the strokes: {ground}"


def test_black_type_on_a_black_panel_is_still_read_as_black_on_black() -> None:
    """Excluding the ink's bucket must not hide the case the check exists for: when glyphs
    and panel are one colour the bucket is the whole crop, and it stays the ground."""
    import numpy as np

    from raven_ppt.services.measure.contrast import _ratio, _worst_ground

    crop = np.full((60, 400, 3), (0x1A, 0x1A, 0x1A), dtype=np.uint8)
    crop[0:2, :] = (0x20, 0x20, 0x20)
    ink = (0x1A, 0x1A, 0x1A)

    read = _worst_ground(crop, ink)

    assert read is not None and _ratio(ink, read[0]) < 1.2


def test_ink_the_renderer_painted_one_level_off_is_still_the_ink() -> None:
    """#2F2F2F type comes off LibreOffice as #303030, across a bucket edge; a bucket test
    took those pixels for the ground and read three legible pages at 1.0:1."""
    import numpy as np

    from raven_ppt.services.measure.contrast import _ratio, _worst_ground

    crop = np.full((60, 400, 3), (0xF4, 0xE6, 0xD2), dtype=np.uint8)
    for left in range(10, 390, 20):
        crop[8:52, left : left + 11] = (0x30, 0x30, 0x30)

    read = _worst_ground(crop, (0x2F, 0x2F, 0x2F))

    assert read is not None and _ratio((0x2F, 0x2F, 0x2F), read[0]) > 8


def test_a_dark_photograph_beside_the_words_is_not_read_as_ink() -> None:
    """A title box that runs on over the photograph next to it: #110D0A photograph pixels
    shared a bucket with #2F2F2F type, so the span reached into the photograph and the
    title was judged against it. The ink is what is near the ink's colour, not its bucket."""
    import numpy as np

    from raven_ppt.services.measure.contrast import _ink_span

    crop = np.full((60, 400, 3), (0xF4, 0xE6, 0xD2), dtype=np.uint8)
    crop[:, 200:] = (0x11, 0x0D, 0x0A)
    for left in range(10, 180, 20):
        crop[8:52, left : left + 11] = (0x2F, 0x2F, 0x2F)

    span = _ink_span(crop, (0x2F, 0x2F, 0x2F))

    assert span is not None and span[1] <= 200, span
