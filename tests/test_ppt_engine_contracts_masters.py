"""The band grid as a shape: its derived edges, and what survives a round trip.

Which band a shape belongs to is a measurement and is tested with the measurements
(`test_ppt_engine_measure_bands.py`); the grid itself is four edges and a canvas.
"""

from __future__ import annotations

import pytest

from raven_ppt.contracts.masters import Bands

# A grid with round edges, so the shares below are exact rather than nearly exact:
# the title band is 1in, the body band 5in and the footer band 1.5in of a 7.5in page.
GRID = Bands(title_top=0.0, title_bottom=1.0, body_bottom=6.0, footer_bottom=7.5, canvas_w=10.0, canvas_h=7.5)


def test_the_body_is_the_band_between_the_other_two() -> None:
    assert GRID.body == (1.0, 6.0)


def test_the_body_area_is_the_denominator_a_share_needs() -> None:
    """Square inches of content band: a picture filling it is 100% of the body and
    67% of the canvas, and only the first of those answers "is this page one image"."""
    assert GRID.body_area == pytest.approx(50.0)


def test_the_grid_survives_a_round_trip() -> None:
    assert Bands.from_json(GRID.to_json()) == GRID


def test_a_round_trip_keeps_every_digit() -> None:
    """Rounding on the way out would move a band edge by a hundredth of an inch on
    every save, and the file is re-read by every build after the one that wrote it."""
    grid = Bands(
        title_top=0.0,
        title_bottom=1.123456789,
        body_bottom=7.014285714,
        footer_bottom=7.5,
        canvas_w=13.333333333,
        canvas_h=7.5,
    )

    assert Bands.from_json(grid.to_json()) == grid


def test_extra_keys_in_the_file_are_ignored() -> None:
    stored = {**GRID.to_json(), "measured_from": "example pages"}

    assert Bands.from_json(stored) == GRID


@pytest.mark.parametrize(
    "stored",
    [
        {},
        {"title_top": 0.0, "title_bottom": 1.0},
        {**GRID.to_json(), "body_bottom": "seven"},
        "not an object",
    ],
)
def test_something_that_is_not_a_grid_is_refused(stored: object) -> None:
    with pytest.raises(ValueError):
        Bands.from_json(stored)  # type: ignore[arg-type]


def test_edges_out_of_order_are_refused() -> None:
    """A corrupt file read into a grid does not fail, it starts answering questions
    about a coordinate system nothing measured."""
    with pytest.raises(ValueError):
        Bands.from_json({**GRID.to_json(), "body_bottom": 0.5})


def test_a_canvas_with_no_size_is_refused() -> None:
    with pytest.raises(ValueError):
        Bands.from_json({**GRID.to_json(), "canvas_w": 0.0})
