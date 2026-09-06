"""Which band a shape belongs to, measured against the grid.

Membership is by overlapping area rather than by midpoint, which is the whole
reason the policy exists -- a card that starts in the title row and ends halfway
down the page has its midpoint in the body and is not a body shape.
"""

from __future__ import annotations

import pytest

from raven_ppt.contracts.masters import Bands
from raven_ppt.services.measure.bands import band_of

# A grid with round edges, so the shares below are exact rather than nearly exact:
# the title band is 1in, the body band 5in and the footer band 1.5in of a 7.5in page.
GRID = Bands(title_top=0.0, title_bottom=1.0, body_bottom=6.0, footer_bottom=7.5, canvas_w=10.0, canvas_h=7.5)


def test_a_shape_inside_one_band_belongs_to_it() -> None:
    assert band_of(GRID, 0.1, 0.9) == "title"
    assert band_of(GRID, 2.0, 5.0) == "body"
    assert band_of(GRID, 6.5, 7.2) == "footer"


def test_a_shape_touching_a_second_band_still_belongs_to_the_first() -> None:
    """Four fifths of it is the bar, and the boundary case is the one worth pinning:
    1in in the title row against 4in in the body is exactly 0.8 and is a body shape."""
    assert band_of(GRID, 0.0, 5.0) == "body"


def test_a_shape_no_band_mostly_holds_is_spanning() -> None:
    """1in of title against 3in of body is 0.75, which is a shape crossing the grid."""
    assert band_of(GRID, 0.0, 4.0) == "spanning"
    assert band_of(GRID, 0.0, 7.5) == "spanning", "a full-bleed background is over all three"


def test_the_edge_between_two_bands_belongs_to_the_lower_one() -> None:
    assert band_of(GRID, 1.0, 3.0) == "body"
    assert band_of(GRID, 6.0, 7.0) == "footer"


def test_a_rule_has_no_height_and_is_placed_by_where_it_sits() -> None:
    """A hairline overlaps nothing at all, so area cannot answer for it."""
    assert band_of(GRID, 0.5, 0.5) == "title"
    assert band_of(GRID, 3.0, 3.0) == "body"
    assert band_of(GRID, 6.5, 6.5) == "footer"


def test_the_two_edges_may_arrive_in_either_order() -> None:
    assert band_of(GRID, 5.0, 2.0) == band_of(GRID, 2.0, 5.0)


def test_an_empty_footer_band_has_no_members() -> None:
    """A template that reserves nothing along its bottom edge gets a zero-height
    footer band rather than an invented one, and the content band runs to the page."""
    grid = Bands(title_top=0.0, title_bottom=1.0, body_bottom=7.5, footer_bottom=7.5, canvas_w=10.0, canvas_h=7.5)

    assert band_of(grid, 6.0, 7.4) == "body"
    assert grid.body_area == pytest.approx(65.0)
