"""Whether copy fits its box, and where the copy that does not fit ends up.

The second half is what this file is mostly about. `overset_copy` measured the first
half from the start and reported it; the boxes it was reporting on were on cloned pages
that declare no type size of their own, and the copy that did not fit was under an
anchor nobody read, so the finding named a frame while the copy was somewhere else on
the page entirely.
"""

from __future__ import annotations

import pytest

from raven_ppt.contracts.findings import Severity
from raven_ppt.services.measure.fit import (
    DISPLACED_GRAZE_PT,
    DISPLACED_IN_COLUMN,
    GROWS_BOTH,
    GROWS_DOWN,
    GROWS_UP,
    LINE_HEIGHT_FACTOR,
    OVERSET_SLACK_LINES,
    RENDER_DRIFT_HEADROOM,
    RENDERED_LINE_ADVANCE,
    SHRINKS,
    capacity_lines,
    overset_copy,
    wrap,
)
from raven_ppt.services.measure.width import DEFAULT_MEASURER
from tests._ppt_engine_fixtures import (  # noqa: F401
    DeckBuilder,
    deck,
    image,
    noise_image,
    noise_png,
    product_page,
    template_file,
)

pytest.importorskip("pptx")

# The card label of a delivered page, to the hundredth of an inch: 20pt copy in a
# 2.36x0.56in box, anchored to the middle of it, over a paragraph 0.10in below.
LABEL = ("CATL-HyperStrong: 60 GWh", 20.0)
BODY = ("A three-year sodium-ion supply partnership, announced April 2026 and verified.", 14.0)


def _label(builder: DeckBuilder, page, anchor, *, top: float = 2.0, auto=None):
    from pptx.enum.text import MSO_ANCHOR

    box = builder.text(page, LABEL, left=1.0, top=top, width=2.36, height=0.56, wrap=True)
    box.text_frame.vertical_anchor = getattr(MSO_ANCHOR, anchor)
    if auto is not None:
        box.text_frame.auto_size = auto
    return box


def _kinds(findings):
    return sorted(finding.kind for finding in findings)


# --- the two constants the placement rests on -----------------------------------


def test_the_capacity_model_and_the_placement_model_lean_opposite_ways() -> None:
    """One is asked whether the copy fits, the other where it went; both must be safe.

    `LINE_HEIGHT_FACTOR` is generous, so a box is credited with fewer lines than it may
    hold and the fit question errs towards silence. Carrying that into the placement
    would push the ink further out of the box than the renderer puts it, which is the
    unsafe direction for a finding that refuses -- so the advance the renderer was
    measured at is its own number, and it is the smaller of the two.
    """
    assert RENDERED_LINE_ADVANCE == 1.2
    assert RENDERED_LINE_ADVANCE < LINE_HEIGHT_FACTOR


def test_the_rendered_ink_model_matches_what_libreoffice_painted() -> None:
    """Four of the measured renders, as arithmetic: size * (1 + 1.2 * (lines - 1)).

    Measured through LibreOffice on a 2.4x0.56in box: 11pt/5 lines painted 0.885in,
    14pt/7 painted 1.593in, 20pt/3 painted 0.946in, 24pt/7 painted 2.733in. The model
    is the row of numbers, so the numbers are the test.
    """
    for size, lines, painted_in in ((11.0, 5, 0.885), (14.0, 7, 1.593), (20.0, 3, 0.946), (24.0, 7, 2.733)):
        modelled = size * (1 + RENDERED_LINE_ADVANCE * (lines - 1)) / 72
        assert abs(modelled - painted_in) < 0.005, (size, lines, modelled, painted_in)


def test_the_placement_counts_lines_at_the_renderer_s_own_width() -> None:
    """The drift reserve belongs to the fit question and not to the placement.

    Held back, the measurer breaks earlier than the renderer does, so the same copy is
    credited with more lines -- which is right for asking whether a box can hold it and
    wrong for saying how far past the box it reaches.
    """
    text = "CATL-HyperStrong sodium-ion supply partnership announced April twenty twenty six"
    room = 2.16 * 96
    reserved = wrap(text, room, 37, measurer=DEFAULT_MEASURER)
    painted = wrap(text, room * RENDER_DRIFT_HEADROOM, 37, measurer=DEFAULT_MEASURER)
    assert len(painted) <= len(reserved)


# --- the fit question, unchanged -------------------------------------------------


def test_copy_that_fits_its_box_is_not_reported(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.text(page, ("One line of copy, and a box with the room for four of them.", 14.0), top=2.0, height=1.6)

    assert overset_copy(deck.save()) == []


def test_a_box_is_given_one_whole_line_of_slack(deck: DeckBuilder) -> None:
    """A box sized to its text exactly is normal authoring, so the slack is a line."""
    page = deck.page()
    text = "Sodium-ion arrives as a complement to lithium iron phosphate rather than a replacement for it."
    box = deck.text(page, (text, 14.0), left=1.0, top=2.0, width=4.0, height=0.62, wrap=True)
    inner = (box.height - (box.text_frame.margin_top + box.text_frame.margin_bottom)) / 914400

    lines = len(wrap(text, (4.0 - 0.2) * 96, 19, measurer=DEFAULT_MEASURER))
    assert lines == capacity_lines(inner * 96, 19) + OVERSET_SLACK_LINES
    assert overset_copy(deck.save()) == []


def test_copy_growing_down_into_the_page_s_own_room_only_reports(deck: DeckBuilder) -> None:
    """The direction design doc D2 keeps a report, and the ten templates' own case.

    Every one of the 42 overset boxes across the bundled templates is this: copy under a
    top anchor running on below its frame. Refusing it would refuse the trade D2 says a
    refusal must not force -- height taken from a neighbour, or a line cut.
    """
    page = deck.page()
    _label(deck, page, "TOP")

    reported = overset_copy(deck.save())
    assert _kinds(reported) == ["overset_copy"]
    assert reported[0].severity is Severity.WARNING
    assert reported[0].detail["growth"] == GROWS_DOWN


def test_a_frame_that_shrinks_its_own_type_displaces_nothing(deck: DeckBuilder) -> None:
    """`normAutofit` is the renderer resizing the copy, not moving it.

    Which is why the refusal never asks for the trade the type floor would then refuse:
    the frames that would answer a refusal by shrinking are the frames it does not
    report.
    """
    from pptx.enum.text import MSO_AUTO_SIZE

    page = deck.page()
    _label(deck, page, "MIDDLE", auto=MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE)
    deck.text(page, BODY, left=0.8, top=2.65, width=3.2, height=1.2, wrap=True)

    reported = overset_copy(deck.save())
    assert _kinds(reported) == ["overset_copy"]
    assert reported[0].detail["growth"] == SHRINKS


# --- the placement, which is what refuses ---------------------------------------


def test_a_middle_anchored_label_over_the_copy_beneath_it_refuses(deck: DeckBuilder) -> None:
    """The delivered page, rebuilt: three lines in a box that shows one, centred.

    Centred, the two lines the box cannot hold are split above and below the first, so
    the label's last line is set into the paragraph under it -- which the render of the
    page this came from shows as ': 60 GWh' painted across '3-year sodium supply deal'.
    """
    page = deck.page()
    _label(deck, page, "MIDDLE")
    deck.text(page, BODY, left=0.8, top=2.65, width=3.2, height=1.2, wrap=True)

    refused = [f for f in overset_copy(deck.save()) if f.kind == "displaced_copy"]
    assert len(refused) == 1
    assert refused[0].severity is Severity.BLOCKING
    assert refused[0].detail["growth"] == GROWS_BOTH
    assert refused[0].page == 1
    assert "over the copy below it" in refused[0].message
    # The move it names costs the page nothing, which is what D2 asks of a refusal.
    assert "Anchor the frame to the top of its box" in refused[0].message


def test_a_bottom_anchored_headline_off_the_top_of_the_page_refuses(deck: DeckBuilder) -> None:
    """The recurrence `template/house.py` recorded and nothing refused.

    A title placeholder whose anchor comes from the master, in a row at the top of the
    page: one line sits on the row's bottom edge and looks right, and the second line is
    added above it, off the canvas. Measured on a live deck state at 72pt: 3.83in of the
    headline above the top edge of the slide.
    """
    from pptx.enum.text import MSO_ANCHOR

    page = deck.page()
    box = deck.text(
        page,
        ("Market outlook and the committee's takeaways", 40.0),
        left=0.7,
        top=0.14,
        width=4.0,
        height=0.98,
        wrap=True,
    )
    box.text_frame.vertical_anchor = MSO_ANCHOR.BOTTOM

    refused = [f for f in overset_copy(deck.save()) if f.kind == "displaced_copy"]
    assert len(refused) == 1
    assert refused[0].detail["growth"] == GROWS_UP
    assert "above the top edge of the page" in refused[0].message


def test_copy_growing_up_into_nothing_at_all_only_reports(deck: DeckBuilder) -> None:
    """Where it lands is the question, not how far it went.

    A middle-anchored pill in open space grows backwards too, and three of them on a
    delivered page render perfectly legibly inside the shape drawn around them. So
    growing the wrong way is not the finding; growing the wrong way into something is.
    """
    page = deck.page()
    _label(deck, page, "MIDDLE", top=3.0)

    reported = overset_copy(deck.save())
    assert _kinds(reported) == ["overset_copy"]
    assert reported[0].detail["growth"] == GROWS_BOTH


def test_a_neighbour_beside_the_column_is_not_something_the_copy_landed_on(deck: DeckBuilder) -> None:
    """Design doc D11's rejected reading, kept rejected.

    Two text boxes overlapping as declared is not this finding -- a full-width title box
    and a corner page number overlap on every good deck. The copy has to be set in the
    same column as what it reaches, which is the share `DISPLACED_IN_COLUMN` names.
    """
    page = deck.page()
    _label(deck, page, "MIDDLE")
    deck.text(page, BODY, left=6.0, top=2.65, width=3.2, height=1.2, wrap=True)

    assert _kinds(overset_copy(deck.save())) == ["overset_copy"]
    assert DISPLACED_IN_COLUMN == 0.5


def test_a_graze_at_either_end_is_not_a_place_the_copy_landed(deck: DeckBuilder) -> None:
    """Half a line of 12pt copy: a line's ink is not its line box."""
    assert DISPLACED_GRAZE_PT == 3.0
    page = deck.page()
    _label(deck, page, "MIDDLE")
    # 0.24in below the label's box, which its 0.10in of displaced ink does not reach.
    deck.text(page, BODY, left=0.8, top=2.80, width=3.2, height=1.2, wrap=True)

    assert _kinds(overset_copy(deck.save())) == ["overset_copy"]


def test_a_box_already_off_the_canvas_is_left_to_off_page(deck: DeckBuilder) -> None:
    """One template ships four of these: a box whose own top is 11.48in down a 7.5in page.

    Its copy is off the page because the box is, which is `off_page`'s finding and not a
    second one about where the copy inside it went.
    """
    page = deck.page()
    _label(deck, page, "MIDDLE", top=11.48)

    assert "displaced_copy" not in _kinds(overset_copy(deck.save()))


# --- the type size the page never stated ----------------------------------------


def test_a_size_the_page_does_not_state_is_resolved_and_not_skipped(tmp_path) -> None:
    """A placeholder declares nothing, and reading only its runs answered "no size".

    Which is exactly where the copy this was written for is: `replace_text` writes into
    a template's own boxes, a cloned page states nothing of its own, and every box on it
    took its type -- and its anchor -- from the layout or the master. So the check was
    blind on the pages it was for, and its own warning was quoting a size it had got
    from somewhere else. Resolved the way `spilled_copy` already resolves it.
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    title = slide.shapes.title
    title.left, title.top, title.width, title.height = Inches(0.7), Inches(0.14), Inches(4.0), Inches(0.98)
    title.text_frame.word_wrap = True
    title.text_frame.text = "Market outlook and the committee's takeaways"
    body = slide.shapes.add_textbox(Inches(0.7), Inches(1.2), Inches(4.0), Inches(1.0))
    run = body.text_frame.paragraphs[0].add_run()
    run.text = "Five takeaways for the committee, in the order they matter."
    run.font.size = Pt(14)
    built = tmp_path / "inherited.pptx"
    presentation.save(built)

    assert not [r for para in title.text_frame.paragraphs for r in para.runs if r.font.size is not None]
    refused = [f for f in overset_copy(built) if f.kind == "displaced_copy"]
    assert len(refused) == 1
    assert refused[0].detail["inherited_size"] is True
    assert refused[0].detail["size_pt"] == 44.0
    assert refused[0].detail["growth"] == GROWS_BOTH
