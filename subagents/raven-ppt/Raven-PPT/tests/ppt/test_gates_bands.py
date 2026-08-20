"""The one band this deck draws is the one a title row sits on.

The exception is the interesting half of this gate and was asked for explicitly:
a band carrying the title is the house treatment, a bar across the head of a page
carrying nothing is the decoration the gate exists for, and the same band further
down the page divides what type should be dividing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.contracts.findings import Audience, Severity
from raven.ppt.services.gates.bands import (
    BAND_MAX_HEIGHT_EMU,
    BAND_MIN_WIDTH_FRACTION,
    RULE_MAX_EMU,
    STRIP_MAX_SHORT_EMU,
    STRIP_MIN_ASPECT,
    TITLE_COVERAGE_SHARE,
    band_findings,
    data_mark_ids,
)
from raven.ppt.services.measure.geometry import EMU_PER_INCH
from tests.ppt.conftest import DeckBuilder

pytest.importorskip("pptx")


def _deck(
    builder: DeckBuilder,
    *,
    top: float,
    width: float = 13.333,
    height: float = 0.9,
    title_on_band: bool = False,
    title_elsewhere: bool = False,
    text_in_band: bool = False,
    name: str = "deck.pptx",
) -> Path:
    page = builder.page()
    builder.panel(
        page,
        left=0.0,
        top=top,
        width=width,
        height=height,
        text="Results: Video Panoptic Segmentation" if text_in_band else None,
    )
    if title_on_band or title_elsewhere:
        builder.text(
            page,
            ("Results: Video Panoptic Segmentation", 28.0),
            left=0.5,
            top=top + 0.15 if title_on_band else top + height + 0.4,
            width=9.0,
            height=0.5,
        )
    return builder.save(name)


def test_the_band_thresholds_are_the_ones_calibrated_against_the_reference() -> None:
    """Every one of these separates a treatment from a tell, by measurement."""
    assert RULE_MAX_EMU == int(0.0625 * EMU_PER_INCH)  # 4.5pt, the hairline the measurements use
    assert STRIP_MAX_SHORT_EMU == int(0.22 * EMU_PER_INCH)
    assert STRIP_MIN_ASPECT == 6.0
    assert BAND_MIN_WIDTH_FRACTION == 0.85
    assert BAND_MAX_HEIGHT_EMU == int(1.3 * EMU_PER_INCH)
    assert TITLE_COVERAGE_SHARE == 0.5


def test_a_title_set_on_a_top_band_is_the_house_treatment(deck: DeckBuilder) -> None:
    assert band_findings(_deck(deck, top=0.3, title_on_band=True)) == []


def test_a_title_written_into_the_band_is_the_same_thing(deck: DeckBuilder) -> None:
    """A band with its own text on it is not a bar carrying nothing."""
    assert band_findings(_deck(deck, top=0.3, text_in_band=True)) == []


def test_a_top_band_with_the_title_below_it_is_decoration(deck: DeckBuilder) -> None:
    findings = band_findings(_deck(deck, top=0.3, title_elsewhere=True))

    assert len(findings) == 1
    assert findings[0].detail["reason"] == "a band with nothing on it"
    assert findings[0].kind == "band"
    assert findings[0].severity is Severity.BLOCKING
    assert findings[0].audience is Audience.DESIGNER
    assert "hairline in the secondary grey" in findings[0].message


def test_a_plane_with_copy_on_it_is_grouping_wherever_it_sits(deck: DeckBuilder) -> None:
    """Mid-page, and allowed: what the band gate refuses is a bar carrying nothing.

    This used to require the top inch and a half, and a 20-page academic deck was
    refused five times over for tinted planes carrying formulas, a training schedule
    and a takeaway row -- with up to six text boxes on each, under a message that
    said "a band with nothing on it". A plane 0.06in taller passed on the same page,
    being a panel rather than a bar, which is the arbitrariness the author saw.
    """
    assert band_findings(_deck(deck, top=3.2, title_on_band=True)) == []


def test_an_empty_band_is_refused_wherever_it_sits(deck: DeckBuilder) -> None:
    """The other half of the same rule, and the reason the gate exists."""
    assert len(band_findings(_deck(deck, top=3.2, title_elsewhere=True))) == 1


def test_a_narrow_accent_strip_is_refused_wherever_it_sits(deck: DeckBuilder) -> None:
    """Not only at the head of a page: a strip welded to a card edge is the same
    tell, and this is the shape with no allowed position."""
    findings = band_findings(_deck(deck, top=0.4, width=0.15, height=0.9))

    assert len(findings) == 1
    assert findings[0].detail["reason"] == "an accent strip"


def test_a_strip_on_a_title_band_is_still_a_strip(deck: DeckBuilder) -> None:
    """The strip test runs before the band exception, so a narrow accent cannot
    borrow the title row's permission."""
    page = deck.page()
    deck.panel(page, left=0.0, top=0.3, width=13.333, height=0.9)
    deck.panel(page, left=0.2, top=0.35, width=0.1, height=0.8)
    deck.text(page, ("Results: Video Panoptic Segmentation", 28.0), left=0.5, top=0.45, width=9.0, height=0.5)

    findings = band_findings(deck.save())

    assert [finding.detail["reason"] for finding in findings] == ["an accent strip"]


def test_a_small_block_beside_a_title_is_not_a_strip(deck: DeckBuilder) -> None:
    """Below the aspect ratio it is a block, and a block is a legitimate mark."""
    assert band_findings(_deck(deck, top=0.4, width=0.5, height=0.9)) == []


def test_a_hairline_rule_stays_legitimate(deck: DeckBuilder) -> None:
    """A divider is a legitimate thing; what is banned is a block of colour."""
    from pptx.util import Emu

    page = deck.page()
    shape = deck.panel(page, left=0.5, top=3.0, width=12.0, height=1.0)
    shape.height = Emu(RULE_MAX_EMU)

    assert band_findings(deck.save()) == []


def test_a_band_below_the_width_fraction_is_not_a_band(deck: DeckBuilder) -> None:
    """0.85 of the canvas: narrower than that and the shape is a panel."""
    assert band_findings(_deck(deck, top=0.3, width=11.0, height=1.2, title_elsewhere=True)) == []


def test_a_full_width_shape_taller_than_the_band_ceiling_is_a_panel(deck: DeckBuilder) -> None:
    """Past 1.3in it reads as a region of the page rather than a bar across it."""
    assert band_findings(_deck(deck, top=0.3, height=1.4, title_elsewhere=True)) == []


def test_a_picture_running_the_width_of_the_page_is_not_a_band(deck: DeckBuilder, image) -> None:
    from pptx.util import Inches

    page = deck.page()
    picture = page.shapes.add_picture(str(image("wide.png", (9, 9, 9))), Inches(0), Inches(0.3), width=Inches(13.333))
    picture.height = Inches(0.9)

    assert band_findings(deck.save()) == []


def test_a_bar_chart_drawn_from_rectangles_is_exempt(deck: DeckBuilder) -> None:
    """The best way to put a compact comparison beside a claim is often to draw
    it, and a bar is geometrically an accent strip."""
    page = deck.page()
    for index, length in enumerate((2.0, 3.5, 5.0, 6.5)):
        deck.panel(page, left=1.0, top=2.0 + 0.4 * index, width=length, height=0.18)

    assert band_findings(deck.save()) == []


def test_a_card_and_the_strip_welded_to_its_edge_do_not_pass_as_a_series(deck: DeckBuilder) -> None:
    """Same thickness, same left edge, different widths -- which is exactly the
    decoration the exemption must not cover. What separates a real series is that
    its members sit one per row."""
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=6.0, height=0.2)
    deck.panel(page, left=1.0, top=2.0, width=2.0, height=0.2)

    findings = band_findings(deck.save())

    assert [finding.detail["reason"] for finding in findings] == ["an accent strip", "an accent strip"]


def test_two_bars_of_the_same_length_are_not_a_series(deck: DeckBuilder) -> None:
    """A series differs in length, because that is what encodes the values."""
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=4.0, height=0.18)
    deck.panel(page, left=1.0, top=2.5, width=4.0, height=0.18)

    assert len(band_findings(deck.save())) == 2


def test_a_lone_bar_is_never_a_series(deck: DeckBuilder) -> None:
    page = deck.page()
    bar = deck.panel(page, left=1.0, top=2.0, width=4.0, height=0.18)

    assert data_mark_ids([bar]) == set()


def test_a_kicker_rule_under_a_title_is_not_a_strip(deck: DeckBuilder) -> None:
    """The device a real template uses, and a live deck was refused eight times for.

    A short accent rule under the title -- 1.05in by 0.04in, orange, correct in the
    render. The gate stopped at 2.5pt while the measurement module called anything
    under 4.5pt a hairline, so this landed in the gap between two definitions of the
    same word.
    """
    page = deck.page()
    deck.panel(page, left=0.72, top=1.34, width=1.05, height=0.04)
    deck.text(page, ("TarViS reaches or beats the comparisons", 28.0), left=0.72, top=0.6, width=9.0, height=0.6)

    assert band_findings(deck.save()) == []


def test_a_track_and_its_value_on_each_row_are_bars_not_strips(deck) -> None:
    """The commonest way to draw a bar: a full-length track with the value over it.

    A live deck's page had eight such shapes on four rows -- tracks at 2.55in, values
    at 2.26, 2.36, 2.17 and 2.20 -- and all eight came back BLOCKING as accent strips,
    on a page the skill itself asks for ("ranking -> sorted horizontal bars"). The
    series test read "one shape per row" literally.
    """
    page = deck.page()
    for row, value in enumerate((2.26, 2.36, 2.17, 2.20)):
        top = 2.5 + row * 0.63
        deck.panel(page, left=3.19, top=top, width=2.55, height=0.14)
        deck.panel(page, left=3.19, top=top, width=value, height=0.14)

    assert band_findings(deck.save()) == []


def test_a_strip_welded_to_one_card_is_still_a_strip(deck) -> None:
    """What the row test is actually for: one shape on one row, exempting itself by
    sitting on the edge of the thing it decorates."""
    page = deck.page()
    deck.panel(page, left=1.0, top=2.0, width=4.0, height=2.0)
    deck.panel(page, left=1.0, top=2.0, width=4.0, height=0.14)

    kinds = [finding.detail["reason"] for finding in band_findings(deck.save())]
    assert "an accent strip" in kinds
