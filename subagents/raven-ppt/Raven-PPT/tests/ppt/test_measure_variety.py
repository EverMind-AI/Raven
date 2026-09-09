"""Whether a deck's composed pages are all the same page.

The gap this closes: a deck can pass every other measurement -- type on the ramp,
nothing colliding, nothing off the canvas, the title row where the template puts it --
and still be eleven pages of one composition, which is what one delivered 20-page deck
was. Nothing read the deck as a whole until this.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.services.measure.geometry import open_deck
from raven.ppt.services.measure.variety import (
    CONCENTRATED_PAGES,
    MIN_PAGES,
    layout_variety,
    page_signature,
)
from tests.ppt.conftest import DeckBuilder

pytest.importorskip("pptx")


def _alike(tmp_path: Path, count: int, name: str = "alike.pptx") -> Path:
    """`count` pages of one panel, one paragraph beside it, always in the same place."""
    builder = DeckBuilder(tmp_path)
    for number in range(count):
        page = builder.page()
        builder.panel(page, left=0.7, top=1.3, width=5.6, height=4.6)
        builder.text(
            page,
            (f"第 {number + 1} 页的正文，写得足够长以算作一段而不是一个标签。", 16.0),
            left=7.0,
            top=1.3,
            width=5.6,
            height=4.6,
        )
    return builder.save(name)


def _varied(tmp_path: Path, image) -> Path:
    """Six pages, six compositions: the kinds differ, the grid differs, or both."""
    builder = DeckBuilder(tmp_path)
    figure = image("fig.png", (40, 60, 200))

    page = builder.page()
    builder.picture(page, figure, left=0.7, top=1.3, width=6.0)
    builder.text(page, ("一段够长的正文，放在图的右边充当读法。", 16.0), left=7.2, top=1.3, width=5.4, height=4.0)

    page = builder.page()
    builder.text(page, ("整页只有一句结论，别的什么也没有，字号也拉到最大。", 30.0), left=1.5, top=2.5, width=10.0)

    page = builder.page()
    for index in range(3):
        builder.panel(page, left=0.7 + index * 4.1, top=1.3, width=3.7, height=2.0)

    page = builder.page()
    builder.table(page, 4, 3, left=0.7, top=1.3, width=6.0, cell="46.3")
    builder.text(page, ("表格右边的读法，写得足够长以算作一段正文。", 16.0), left=7.2, top=1.3, width=5.4, height=3.0)

    page = builder.page()
    builder.picture(page, figure, left=0.7, top=1.3, width=11.9)
    builder.text(
        page, ("整页一张大图，正文压在下面一条窄带里说明来源。", 16.0), left=0.7, top=5.6, width=11.9, height=0.8
    )

    page = builder.page()
    builder.panel(page, left=0.0, top=0.0, width=3.6, height=7.5)
    builder.text(
        page, ("侧栏里的正文，写得足够长以算作一段而不是一个标签。", 16.0), left=0.4, top=2.0, width=2.8, height=3.0
    )
    builder.picture(page, figure, left=4.2, top=1.3, width=8.4)

    return builder.save("varied.pptx")


def test_a_deck_of_one_composition_is_reported(tmp_path: Path) -> None:
    findings = layout_variety(_alike(tmp_path, 8))

    assert [finding.kind for finding in findings] == ["layout_variety"]
    assert findings[0].detail["repeated"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert findings[0].detail["distinct"] == 1
    # It reports rather than refuses: "varied enough" is not a property a page has, and
    # a series of pages built alike so a reader can compare them is good work.
    assert findings[0].severity.value == "warning"


def test_a_deck_that_composed_each_page_differently_is_not(tmp_path: Path, image) -> None:
    assert layout_variety(_varied(tmp_path, image)) == []


def test_the_structural_pages_are_left_out(tmp_path: Path) -> None:
    """A cover, a contents list, a divider and a closing are meant to be alike.

    Counting them is how a correct deck gets reported for the four pages it was
    supposed to clone from the template, which is the false positive this argument
    exists to stop.
    """
    deck = _alike(tmp_path, 8)

    assert layout_variety(deck, structural=[1, 2, 3, 4, 5]) == [], "three pages left is under the floor"
    assert layout_variety(deck, structural=[1, 2]) != []


def test_a_short_deck_says_nothing(tmp_path: Path) -> None:
    """Three pages of one shape out of four is a short deck, not a habit."""
    assert MIN_PAGES > CONCENTRATED_PAGES
    assert layout_variety(_alike(tmp_path, MIN_PAGES - 1, "short.pptx")) == []
    assert layout_variety(_alike(tmp_path, MIN_PAGES, "long.pptx")) != []


def test_the_signature_reads_what_carries_the_page_and_how_it_is_arranged(tmp_path: Path, image) -> None:
    """Both halves, because either alone calls too many pages alike.

    A table beside prose and a picture beside prose fall in the same 2x1 grid; a row of
    three panels and one panel beside prose carry the same kind. Only the pair tells all
    three apart.
    """
    slides = list(open_deck(_varied(tmp_path, image)).slides)
    signatures = [page_signature(slide) for slide in slides]

    assert len(set(signatures)) == len(signatures), signatures
    assert "picture" in signatures[0] and "text" in signatures[0]
    assert "table" in signatures[3]
    assert signatures[2].startswith("panel in 3x")


def test_a_page_with_nothing_arranged_on_it_is_not_measured(tmp_path: Path) -> None:
    """A cover is a page however it was built, and it is not a composition."""
    builder = DeckBuilder(tmp_path)
    page = builder.page()
    builder.text(page, ("封面", 44.0), left=1.0, top=3.0, width=6.0, height=1.0)

    assert page_signature(list(open_deck(builder.save("cover.pptx")).slides)[0]) is None
