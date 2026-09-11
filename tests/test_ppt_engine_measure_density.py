"""The three floors: a panel's fill, a body's anchor, and a page's copy.

Each test is a page a real deck produced or a page a real template ships. The two
decks the thresholds were measured on live outside the repo, so what they proved is
pinned here as the shapes they were: a 4.8in placeholder panel holding one line, a
table page whose only anchor is drawn rather than placed, and a card page carrying
three dozen characters where the accepted deck's thinnest carries 172.

The negative half matters as much: a row band centring one line measures 32% full and
is not a defect, and the accepted deck has seven of them on one page. A floor that
reported those would report a good deck once a page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven_ppt.contracts import WordBox
from raven_ppt.contracts.findings import Severity
from raven_ppt.contracts.masters import Bands
from raven_ppt.contracts.outline import Outline, PagePlan
from raven_ppt.services.measure.density import (
    ANCHOR_FIGURE_SHARE,
    ANCHOR_TYPE_PT,
    CONTAINER_FILL,
    CONTAINER_SLACK_IN,
    COPY_BLOCKS_BEFORE_DIVIDING,
    COPY_FLOOR_CARDS,
    COPY_FLOOR_CONTENT,
    COPY_FLOOR_DATA,
    Floors,
    sparse_containers,
    thin_copy,
    unanchored_pages,
    undivided_bodies,
)
from raven_ppt.services.measure.type_size import Span
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

CANVAS_W, CANVAS_H = 13.333, 7.5
TITLE_BOTTOM, BODY_BOTTOM = 1.5, 6.5

BANDS = Bands(0.0, TITLE_BOTTOM, BODY_BOTTOM, CANVAS_H, CANVAS_W, CANVAS_H)


def spans(*sizes_and_tops: tuple[float, float], page: int = 1) -> list[Span]:
    """Rendered type at the given (size in points, top in inches), one span each."""
    return [
        Span(page=page, size_pt=size, text="x", x0=72.0, y0=top * 72.0, x1=144.0, y1=(top + size / 72.0) * 72.0)
        for size, top in sizes_and_tops
    ]


def painted(*tops_and_heights: tuple[float, float], page: int = 1, left: float = 1.3) -> list[WordBox]:
    """Rendered lines at the given (top, height) in inches, in the points a PDF reads in."""
    return [
        WordBox(
            page=page,
            text="x",
            x0=left * 72.0,
            y0=top * 72.0,
            x1=(left + 2.0) * 72.0,
            y1=(top + height) * 72.0,
        )
        for top, height in tops_and_heights
    ]


def plan(page: int, **fields: object) -> PagePlan:
    return PagePlan(page=page, claim=fields.pop("claim", "a claim the page makes"), **fields)  # type: ignore[arg-type]


def test_the_floors_are_the_numbers_measured_on_the_two_decks() -> None:
    assert (CONTAINER_FILL, CONTAINER_SLACK_IN) == (0.35, 1.5)
    assert (ANCHOR_TYPE_PT, ANCHOR_FIGURE_SHARE) == (20.0, 0.20)
    assert (COPY_FLOOR_CONTENT, COPY_FLOOR_CARDS, COPY_FLOOR_DATA) == (150, 120, 90)


def test_every_finding_here_only_reports(deck: DeckBuilder) -> None:
    """None of the three refuses a deck -- see the module docstring, and D17."""
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=5.0, height=4.0)
    deck.text(page, "one line", left=1.2, top=2.2, width=4.6, height=0.3)
    built = deck.save()

    reported = sparse_containers(built)
    reported += unanchored_pages(built, spans((14.0, 3.0)), BANDS)
    reported += thin_copy(built, Outline(takeaway="t", pages=(plan(1),)))

    assert reported
    assert {finding.severity for finding in reported} == {Severity.WARNING}


# --- container fill -------------------------------------------------------------


def test_a_tall_panel_holding_one_line_is_reported(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=5.0, height=4.0)
    deck.text(page, "one line", left=1.2, top=2.2, width=4.6, height=0.3)

    findings = sparse_containers(deck.save())

    assert [finding.kind for finding in findings] == ["sparse_container"]
    assert findings[0].page == 1
    assert findings[0].detail["used_share"] == 0.07
    assert findings[0].detail["slack_in"] == 3.7
    assert "card_size()" in findings[0].message  # a next step, not a verdict (D6)


def test_a_panel_its_content_fills_is_left_alone(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=5.0, height=4.0)
    deck.text(page, "a heading", left=1.2, top=2.1, width=4.6, height=0.5)
    deck.text(page, "the body of the card", left=1.2, top=2.7, width=4.6, height=3.1)

    assert sparse_containers(deck.save()) == []


def test_the_page_ground_is_not_a_container(deck: DeckBuilder) -> None:
    """A full-bleed fill is the page, and its emptiness is the page's own layout."""
    page = deck.page()
    deck.panel(page, left=0.0, top=0.0, width=CANVAS_W, height=CANVAS_H)
    deck.text(page, "one line", left=1.2, top=2.2, width=4.6, height=0.3)

    assert sparse_containers(deck.save()) == []


def test_a_decorative_strip_is_not_a_container(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=8.0, height=0.3)
    deck.text(page, "a kicker", left=1.1, top=2.05, width=4.0, height=0.2)

    assert sparse_containers(deck.save()) == []


def test_a_row_band_centring_one_line_is_not_reported(deck: DeckBuilder) -> None:
    """The shape the accepted deck's hand-drawn table is made of, seven times over: a
    0.7in band holding a 0.25in line measures 36% full, and the blank is padding."""
    page = deck.page()
    for index in range(4):
        top = 2.0 + index * 0.7
        deck.panel(page, left=1.0, top=top, width=11.0, height=0.7)
        deck.text(page, "a table row", left=1.1, top=top + 0.22, width=10.8, height=0.25)

    assert sparse_containers(deck.save()) == []


def test_a_hub_disc_with_a_label_on_it_is_not_a_container(deck: DeckBuilder) -> None:
    """`v3_gold` page 7: a 4.05in disc at the middle of a hub-and-spoke diagram, with
    its name in a text box over it, measured 14% full. A circle covers 79% of its own
    box at best, so there is no way to fill one and no defect in not having."""
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    page = deck.page()
    disc = page.shapes.add_shape(MSO_SHAPE.OVAL, Inches(4.6), Inches(1.9), Inches(4.05), Inches(4.05))
    disc.fill.solid()
    disc.fill.fore_color.rgb = RGBColor(0x9A, 0x86, 0x6C)
    deck.text(page, "the hub", left=5.2, top=3.6, width=2.8, height=0.56)

    assert sparse_containers(deck.save()) == []


def test_the_same_geometry_as_a_card_is_still_reported(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.panel(page, left=4.6, top=1.9, width=4.05, height=4.05)
    deck.text(page, "the hub", left=5.2, top=2.0, width=2.8, height=0.56)

    assert [finding.kind for finding in sparse_containers(deck.save())] == ["sparse_container"]


def test_a_panel_nothing_sits_in_is_not_judged(deck: DeckBuilder) -> None:
    """No copy in it, no container: an empty fill is a ground or a colour block, and
    which of those it is is not a question this measurement can answer."""
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=5.0, height=4.0)

    assert sparse_containers(deck.save()) == []


def test_a_picture_in_the_panel_counts_as_content(deck: DeckBuilder, image) -> None:
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=5.0, height=4.0)
    deck.text(page, "a caption", left=1.2, top=2.1, width=4.6, height=0.3)
    deck.picture(page, image("shot.png", (20, 90, 140)), left=1.2, top=2.5, width=4.6, height=3.4)

    assert sparse_containers(deck.save()) == []


def test_the_render_says_where_the_copy_landed(deck: DeckBuilder) -> None:
    """A frame the author did not size to its copy: 3.6in declared, one line painted.
    Off the file the card reads as full, and the reader sees an empty card."""
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=5.0, height=4.0)
    deck.text(page, "one line the renderer never wrapped", left=1.2, top=2.2, width=4.6, height=3.6)
    built = deck.save()

    assert sparse_containers(built) == []
    assert sparse_containers(built, painted((2.2, 0.25)))


def test_a_page_the_render_shows_no_copy_on_falls_back_to_the_file(deck: DeckBuilder) -> None:
    """Zero words is no signal, not an empty card -- otherwise a render that dropped a
    page would report every container on it."""
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=5.0, height=4.0)
    deck.text(page, "a heading", left=1.2, top=2.1, width=4.6, height=0.5)
    deck.text(page, "the body of the card", left=1.2, top=2.7, width=4.6, height=3.1)

    assert sparse_containers(deck.save(), painted((2.2, 0.25), page=2)) == []


def test_structural_pages_are_not_judged(deck: DeckBuilder) -> None:
    """The accepted deck's one sparse panel is the sidebar of its contents page."""
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=5.0, height=4.0)
    deck.text(page, "AGENDA", left=1.2, top=2.2, width=4.6, height=0.3)
    built = deck.save()

    assert sparse_containers(built)
    assert sparse_containers(built, structural=[1]) == []


def test_the_fill_floor_can_be_replaced(deck: DeckBuilder) -> None:
    """The point of `Floors`: a project can carry its own without a predicate moving."""
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=5.0, height=4.0)
    deck.text(page, "a heading", left=1.2, top=2.1, width=4.6, height=0.5)
    deck.text(page, "the body of the card", left=1.2, top=2.7, width=4.6, height=3.1)
    built = deck.save()

    assert sparse_containers(built) == []
    assert sparse_containers(built, floors=Floors(container_fill=0.99, container_slack_in=0.1))


# --- visual anchor --------------------------------------------------------------


def test_a_body_set_all_at_one_size_is_reported(deck: DeckBuilder) -> None:
    deck.page()

    findings = unanchored_pages(deck.save(), spans((40.0, 0.6), (14.0, 2.0), (14.0, 3.0)), BANDS)

    assert [finding.kind for finding in findings] == ["no_anchor"]
    assert findings[0].detail["largest_body_pt"] == 14.0
    assert "two steps up the size ladder" in findings[0].message


def test_a_heading_in_the_body_anchors_the_page(deck: DeckBuilder) -> None:
    deck.page()

    assert unanchored_pages(deck.save(), spans((24.0, 2.0), (14.0, 3.0)), BANDS) == []


def test_a_title_over_the_body_does_not_anchor_it(deck: DeckBuilder) -> None:
    """Every page has a title, so a check counting titles could never say no (D20)."""
    deck.page()

    assert unanchored_pages(deck.save(), spans((72.0, 0.4)), BANDS)


def test_a_figure_across_the_body_anchors_the_page(deck: DeckBuilder, image) -> None:
    page = deck.page()
    deck.picture(page, image("chart.png", (30, 70, 130)), left=1.0, top=2.0, width=5.0, height=3.0)

    assert unanchored_pages(deck.save(), spans((14.0, 3.0)), BANDS) == []


def test_a_drawn_table_anchors_the_page_the_way_a_placed_one_would(deck: DeckBuilder) -> None:
    """Both decks calibrated on draw their tables as stacked row bands rather than as a
    GraphicFrame, and the table page was the one this check reported before the bands
    were read as one object."""
    page = deck.page()
    for index in range(3):
        deck.panel(page, left=1.0, top=2.0 + index * 0.6, width=11.0, height=0.6)

    assert unanchored_pages(deck.save(), spans((14.0, 3.0)), BANDS) == []


def test_two_stacked_bands_are_a_split_and_not_a_table(deck: DeckBuilder) -> None:
    page = deck.page()
    for index in range(2):
        deck.panel(page, left=1.0, top=2.0 + index * 0.6, width=11.0, height=0.6)

    assert unanchored_pages(deck.save(), spans((14.0, 3.0)), BANDS)


def test_bands_apart_by_more_than_a_hairline_are_not_one_object(deck: DeckBuilder) -> None:
    page = deck.page()
    for index in range(3):
        deck.panel(page, left=1.0, top=2.0 + index * 0.8, width=11.0, height=0.6)

    assert unanchored_pages(deck.save(), spans((14.0, 3.0)), BANDS)


def test_without_bands_the_anchor_is_not_judged(deck: DeckBuilder) -> None:
    deck.page()

    assert unanchored_pages(deck.save(), spans((14.0, 3.0)), None) == []


def test_without_a_render_the_anchor_is_not_judged(deck: DeckBuilder) -> None:
    """No spans is no signal, which is not the same answer as a clean page."""
    deck.page()

    assert unanchored_pages(deck.save(), None, BANDS) == []


def test_the_anchor_floors_can_be_replaced(deck: DeckBuilder) -> None:
    deck.page()
    built = deck.save()

    assert unanchored_pages(built, spans((24.0, 2.0)), BANDS) == []
    assert unanchored_pages(built, spans((24.0, 2.0)), BANDS, floors=Floors(anchor_type_pt=30.0))


# --- copy per page --------------------------------------------------------------


def _copy(chars: int) -> str:
    """Copy of an exact stripped length, so a floor can be tested either side of."""
    return "note" * (chars // 4)


def _card_page(deck: DeckBuilder, copy: str, image=None) -> None:
    page = deck.page()
    for index in range(3):
        left = 1.0 + index * 4.0
        deck.panel(page, left=left, top=2.0, width=3.5, height=2.5)
        deck.text(page, copy, left=left + 0.2, top=2.2, width=3.1, height=2.1)
    if image is not None:
        deck.picture(page, image, left=1.0, top=5.0, width=5.0, height=1.5)


def test_a_card_page_of_placeholder_labels_is_reported(deck: DeckBuilder) -> None:
    _card_page(deck, _copy(12))

    findings = thin_copy(deck.save(), Outline(takeaway="t", pages=(plan(1),)))

    assert [finding.kind for finding in findings] == ["thin_copy"]
    assert findings[0].detail == {"page": 1, "chars": 36, "floor": COPY_FLOOR_CARDS, "page_kind": "cards"}
    assert "fold it into the page beside it" in findings[0].message


def test_a_data_page_answers_to_a_lower_floor(deck: DeckBuilder, image) -> None:
    """The same copy, and a figure carrying the argument instead of the sentences."""
    deck_copy = _copy(36)  # 108 characters over three cards: under the card floor, over the data one
    _card_page(deck, deck_copy, image=image("chart.png", (20, 90, 140)))
    built = deck.save()

    assert thin_copy(built, Outline(takeaway="t", pages=(plan(1),))) == []

    bare = DeckBuilder(built.parent)
    _card_page(bare, deck_copy)
    assert thin_copy(bare.save("cards.pptx"), Outline(takeaway="t", pages=(plan(1),)))


def test_a_prose_page_answers_to_the_highest_floor(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.text(page, _copy(120), left=1.0, top=2.0, width=11.0, height=3.0)

    findings = thin_copy(deck.save(), Outline(takeaway="t", pages=(plan(1),)))

    assert [finding.detail["page_kind"] for finding in findings] == ["content"]
    assert findings[0].detail["floor"] == COPY_FLOOR_CONTENT


def test_a_prose_page_that_says_enough_is_left_alone(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.text(page, _copy(160), left=1.0, top=2.0, width=11.0, height=3.0)

    assert thin_copy(deck.save(), Outline(takeaway="t", pages=(plan(1),))) == []


def test_the_plans_own_word_marks_a_page_as_furniture(deck: DeckBuilder) -> None:
    _card_page(deck, _copy(12))
    built = deck.save()

    assert thin_copy(built, Outline(takeaway="t", pages=(plan(1),)))
    assert thin_copy(built, Outline(takeaway="t", pages=(plan(1, carries="封面"),))) == []


def test_the_template_role_of_the_bound_prototype_marks_furniture(deck: DeckBuilder, tmp_path: Path) -> None:
    _card_page(deck, _copy(12))
    built = deck.save()
    template = _template_with_an_agenda(tmp_path)

    outline = Outline(takeaway="t", pages=(plan(1, prototype=2),))

    assert thin_copy(built, outline)
    assert thin_copy(built, outline, prototypes=template) == []


def test_the_plan_can_name_the_page_a_figure_page(deck: DeckBuilder) -> None:
    _card_page(deck, _copy(36))
    built = deck.save()

    assert thin_copy(built, Outline(takeaway="t", pages=(plan(1),)))
    assert thin_copy(built, Outline(takeaway="t", pages=(plan(1, carries="a chart of the split"),))) == []


def test_without_an_outline_no_page_has_a_kind(deck: DeckBuilder) -> None:
    _card_page(deck, _copy(12))
    built = deck.save()

    assert thin_copy(built, None) == []
    assert thin_copy(built, Outline(takeaway="t")) == []


def test_a_page_the_plan_does_not_name_is_not_judged(deck: DeckBuilder) -> None:
    _card_page(deck, _copy(12))

    assert thin_copy(deck.save(), Outline(takeaway="t", pages=(plan(2),))) == []


def test_the_copy_floors_can_be_replaced(deck: DeckBuilder) -> None:
    _card_page(deck, _copy(44))
    built = deck.save()
    outline = Outline(takeaway="t", pages=(plan(1),))

    assert thin_copy(built, outline) == []
    assert thin_copy(built, outline, floors=Floors(copy_cards=400))


# -- a page nobody wrote into ----------------------------------------------------


def _a_cloned_page_nobody_filled(deck: DeckBuilder, *, filled: int = 1, emptied: int = 15) -> None:
    """A cloned page holding its heading and a row of boxes with nothing in them.

    The shape of pages 7, 8, 9, 10, 12, 13, 14 and 15 of the deck this reading was
    added for (`20260909_132114_9f5065`): one call wrote a title and a subtitle, emptied
    every text the call had not named, and the helper meant to fill the rest looked for
    the template's own words on a page they had already been cleared from. Each page
    kept one or two frames of copy against thirteen to twenty-six empty ones.
    """
    page = deck.page()
    for index in range(filled):
        deck.text(page, (f"护城河是 CUDA {index + 1}。", 24.0), left=0.7, top=0.3 + index * 0.7, width=8.0, height=0.6)
    for index in range(emptied):
        deck.text(
            page,
            ("", 14.0),
            left=0.7 + (index % 5) * 2.5,
            top=1.8 + (index // 5) * 1.6,
            width=2.3,
            height=1.4,
            wrap=True,
        )


def test_a_page_whose_frames_were_emptied_refuses_publication(deck: DeckBuilder) -> None:
    _a_cloned_page_nobody_filled(deck)

    findings = thin_copy(deck.save(), Outline(takeaway="t", pages=(plan(1),)))

    assert [finding.kind for finding in findings] == ["emptied_page"]
    assert findings[0].severity is Severity.BLOCKING
    assert findings[0].detail["frames_emptied"] == 15
    assert findings[0].detail["frames_filled"] == 1
    assert "emptied, not removed" in findings[0].message
    assert "`remove_unit`" in findings[0].message


def test_a_page_merely_under_its_floor_still_only_warns(deck: DeckBuilder) -> None:
    """The shape of the one page the floor reported across eleven delivered decks: page
    12 of `20260906_170229_40d232`, a dozen characters short of the prose floor, with 14
    frames of copy against 4 left empty. Being that far short is a judgement about how
    much a page says, and the only way to answer a refusal over it is to pad the page --
    the oscillation design doc D2 ends with no deck at all."""
    page = deck.page()
    for index in range(14):
        deck.text(
            page,
            (_copy(32 if index == 0 else 8), 14.0),
            left=0.7 + (index % 5) * 2.5,
            top=0.4 + (index // 5) * 1.6,
            width=2.3,
            height=1.4,
        )
    for index in range(4):
        deck.text(page, ("", 14.0), left=0.7 + index * 2.5, top=5.6, width=2.3, height=1.0)

    findings = thin_copy(deck.save(), Outline(takeaway="t", pages=(plan(1),)))

    assert [finding.kind for finding in findings] == ["thin_copy"]
    assert findings[0].severity is Severity.WARNING


def test_a_table_counts_as_copy_the_page_carries(deck: DeckBuilder) -> None:
    """A page arguing from a table holds one text frame and its argument in a graphic
    frame. Counted as copy it is thin; counted as nothing it is one frame against two
    empty ones, and the page shape that needs no prose of its own would be refused."""
    page = deck.page()
    deck.text(page, ("三年的账。", 24.0), left=0.7, top=0.3, width=8.0, height=0.6)
    deck.table(page, 3, 2, left=0.7, top=1.6, width=6.0, cell="60")
    for index in range(2):
        deck.text(page, ("", 14.0), left=7.5 + index * 1.8, top=1.8, width=1.6, height=1.2)

    findings = thin_copy(deck.save(), Outline(takeaway="t", pages=(plan(1),)))

    assert [finding.kind for finding in findings] == ["thin_copy"], "thin, and not unwritten"


def _template_with_an_agenda(tmp_path: Path) -> Path:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(CANVAS_W), Inches(CANVAS_H)
    for heading in ("A cover", "Agenda"):
        page = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = page.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
        run = box.text_frame.paragraphs[0].add_run()
        run.text = heading
        run.font.size = Pt(32)
    path = tmp_path / "template.pptx"
    presentation.save(str(path))
    return path


_GRID = Bands(title_top=0.0, title_bottom=1.2, body_bottom=6.6, footer_bottom=7.5, canvas_w=13.333, canvas_h=7.5)


def _paragraphs(builder: DeckBuilder, slide: object, count: int, *, top: float = 1.6) -> None:
    """`count` blocks of copy side by side across the body, each long enough to be one."""
    for slot in range(count):
        builder.text(
            slide,
            (f"第 {slot + 1} 段的正文，写得足够长以算作一段而不是一个标签。", 14.0),
            left=0.7 + slot * 4.1,
            top=top,
            width=3.8,
            height=2.2,
            wrap=True,
        )


def test_three_blocks_of_copy_and_nothing_drawn_between_them(tmp_path: Path) -> None:
    """The page a run produced with four `write` calls and no other helper.

    A reader gets four paragraphs at four x-offsets and has to work out from the
    offsets alone which of them belong together.
    """
    builder = DeckBuilder(tmp_path)
    _paragraphs(builder, builder.page(), 3)

    found = undivided_bodies(builder.save("plain.pptx"), _GRID)

    assert [finding.kind for finding in found] == ["undivided_body"]
    assert found[0].severity == Severity.WARNING
    assert found[0].page == 1
    assert found[0].detail["copy_blocks"] == 3
    assert "card_group" in found[0].message


def test_one_panel_divides_a_body(tmp_path: Path) -> None:
    """One device is the whole floor: 104 of the bundled templates' 105 content pages
    draw at least one, and the reading is about the page that draws none."""
    builder = DeckBuilder(tmp_path)
    page = builder.page()
    _paragraphs(builder, page, 3)
    builder.panel(page, left=0.7, top=4.2, width=3.8, height=1.6)

    assert undivided_bodies(builder.save("panelled.pptx"), _GRID) == []


def test_a_picture_beside_the_copy_divides_it(tmp_path: Path) -> None:
    """Five of the eight templates' pages carry exactly one device and it is a picture:
    a figure the copy is set against is a division whatever else the page draws."""
    from PIL import Image

    image = tmp_path / "figure.png"
    Image.new("RGB", (800, 600), (0x60, 0x60, 0x60)).save(image)
    builder = DeckBuilder(tmp_path)
    page = builder.page()
    _paragraphs(builder, page, 3)
    builder.picture(page, image, left=0.7, top=4.2, width=4.0, height=2.0)

    assert undivided_bodies(builder.save("pictured.pptx"), _GRID) == []


def test_an_outlined_ring_divides_a_body(tmp_path: Path) -> None:
    """One bundled template numbers five columns with outline-only rings and fills
    nothing: a stroke a reader sees is a division, and no fill reading finds one."""
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    builder = DeckBuilder(tmp_path)
    page = builder.page()
    _paragraphs(builder, page, 3)
    ring = builder.panel(page, left=0.9, top=4.4, width=0.6, height=0.6, filled=False)
    ring.line.color.rgb = RGBColor(0x9A, 0x5A, 0x21)
    ring.line.width = Pt(1.5)

    assert undivided_bodies(builder.save("ringed.pptx"), _GRID) == []


def test_a_connector_between_two_columns_divides_a_body(tmp_path: Path) -> None:
    """The same template separates its columns with `p:cxnSp`, which carries neither a
    fill nor copy -- the census that read fills alone called that page undivided."""
    from pptx.enum.shapes import MSO_CONNECTOR
    from pptx.util import Inches

    builder = DeckBuilder(tmp_path)
    page = builder.page()
    _paragraphs(builder, page, 3)
    page.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(4.5), Inches(1.6), Inches(4.5), Inches(3.8))

    assert undivided_bodies(builder.save("ruled.pptx"), _GRID) == []


def test_two_blocks_read_as_a_pair_with_nothing_between_them(tmp_path: Path) -> None:
    """A claim and its qualifier, a before and an after: two blocks say which is which
    by their positions, and three stop doing that."""
    builder = DeckBuilder(tmp_path)
    _paragraphs(builder, builder.page(), COPY_BLOCKS_BEFORE_DIVIDING - 1)

    assert undivided_bodies(builder.save("pair.pptx"), _GRID) == []


def test_a_panel_in_the_title_band_does_not_divide_the_body(tmp_path: Path) -> None:
    """Inside the band and nowhere else, for the reason `unanchored_pages` gives: a
    title plate is furniture, and counting it would clear every page that has one."""
    builder = DeckBuilder(tmp_path)
    page = builder.page()
    _paragraphs(builder, page, 3)
    builder.panel(page, left=0.7, top=0.2, width=11.9, height=0.8)

    found = undivided_bodies(builder.save("plated.pptx"), _GRID)

    assert [finding.page for finding in found] == [1]


def test_without_a_grid_there_is_no_body_to_read(tmp_path: Path) -> None:
    """Design doc D20: no bands, no coordinate system, no finding."""
    builder = DeckBuilder(tmp_path)
    _paragraphs(builder, builder.page(), 3)

    assert undivided_bodies(builder.save("ungridded.pptx"), None) == []


def test_a_structural_page_is_not_asked_to_divide_itself(tmp_path: Path) -> None:
    """A section marker is three words and a rule by design."""
    builder = DeckBuilder(tmp_path)
    _paragraphs(builder, builder.page(), 3)

    assert undivided_bodies(builder.save("structural.pptx"), _GRID, structural=[1]) == []
