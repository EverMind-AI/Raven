"""The three layout budgets, each read off a file built to hold exactly one of them.

Every case here is a deck a reader would describe the same way the finding does, so a
threshold that drifts far enough to change what a reader sees fails a test. The two
readings that a whole class of pages hinges on -- a card and the text frame inside it
counted once, and a strip of labels not counted as a row of cards -- get a case each,
because both were wrong in the first version of this file and neither showed up as a
wrong number: they showed up as the right number arrived at by accident.
"""

from __future__ import annotations

from pathlib import Path

from raven_ppt.services.measure.quotas import (
    _signatures,
    equal_card_habit,
    quota_findings,
    repeated_layout,
    symmetry_habit,
)
from tests._ppt_engine_fixtures import DeckBuilder

LONG = "这一段正文写得足够长，好让它算作一段文字而不是一个标签。"


def _prose(builder: DeckBuilder, page: object, **where: float) -> None:
    builder.text(page, (LONG, 16.0), wrap=True, **where)


def _cards(builder: DeckBuilder, page: object, *, count: int = 3, top: float = 4.4, width: float = 3.8) -> None:
    """A row of `count` equal cards, each holding a narrower text frame."""
    gap = 0.3
    for slot in range(count):
        left = 0.7 + slot * (width + gap)
        builder.panel(page, left=left, top=top, width=width, height=2.0)
        builder.text(page, (LONG, 12.0), left=left + 0.2, top=top + 0.2, width=width - 0.4, height=1.6, wrap=True)


def _split(builder: DeckBuilder, page: object, *, left_width: float, right_width: float) -> None:
    builder.panel(page, left=0.7, top=1.3, width=left_width, height=2.6)
    _prose(builder, page, left=0.7 + left_width + 0.3, top=1.3, width=right_width, height=2.6)


def test_a_page_built_like_the_one_before_it_is_reported(tmp_path: Path) -> None:
    builder = DeckBuilder(tmp_path)
    for _ in range(2):
        page = builder.page()
        _prose(builder, page, left=0.7, top=0.6, width=11.9, height=1.0)
        _cards(builder, page)
    path = builder.save("twice.pptx")

    findings = repeated_layout(path)

    assert [finding.page for finding in findings] == [2]
    assert findings[0].detail["same_as"] == 1


def test_two_pages_of_the_same_shape_that_are_not_adjacent_are_left_alone(tmp_path: Path) -> None:
    """`layout_variety` is the deck-wide reading. This one is about the pair a reader
    meets back to back, which is the only pair they can compare without remembering.

    The page between them is prose and nothing else: two pages differ under
    `page_signature` when what carries them differs, and a panel beside a column of copy
    falls into the same grid as a row of cards often enough that swapping the arrangement
    alone does not make a different page.
    """
    builder = DeckBuilder(tmp_path)
    for number in range(3):
        page = builder.page()
        _prose(builder, page, left=0.7, top=0.6, width=11.9, height=1.0)
        if number == 1:
            _prose(builder, page, left=0.7, top=2.0, width=11.9, height=4.0)
        else:
            _cards(builder, page)
    path = builder.save("apart.pptx")

    assert repeated_layout(path) == []


def test_two_pages_that_only_share_a_grid_are_left_alone(tmp_path: Path) -> None:
    """The signature is a sieve, not the test.

    `page_signature` names what carries a page and the grid its regions fall into, and
    on a deck whose every region is a tinted panel the first half says nothing -- so two
    pages match on the grid alone. Both of these hold three cards in three columns and a
    reader recognises neither as the other, because the columns are not in the same
    places.
    """
    builder = DeckBuilder(tmp_path)
    for offset in (0.0, 0.9):
        page = builder.page()
        for slot in range(3):
            left = 0.7 + offset + slot * 4.2
            builder.panel(page, left=left, top=4.4, width=3.8, height=2.0)
            builder.text(page, (LONG, 12.0), left=left + 0.2, top=4.6, width=3.4, height=1.6, wrap=True)
    path = builder.save("shifted.pptx")

    # The two halves this case is made of, asserted separately: a version of it whose
    # pages differ in the signature too would pass without the geometry ever being read.
    signatures = {shape for _page, shape in _signatures(path, ())}
    assert len(signatures) == 1
    assert repeated_layout(path) == []


def test_a_card_and_the_text_inside_it_count_as_one_card(tmp_path: Path) -> None:
    """The reading the first version of this got right by accident.

    Counting both reads a row of three cards as six boxes of two alternating widths --
    which is three of one width, so the row still qualifies, for the wrong reason. A
    row of two cards is where the accident shows: four boxes, two of each width.
    """
    builder = DeckBuilder(tmp_path)
    for _ in range(4):
        page = builder.page()
        _prose(builder, page, left=0.7, top=0.6, width=11.9, height=1.0)
        _cards(builder, page, count=2, width=5.8)
    path = builder.save("pairs.pptx")

    assert equal_card_habit(path) == []


def test_a_strip_of_labels_is_not_a_row_of_cards(tmp_path: Path) -> None:
    """Six equal chips tucked into a corner are a row of equal things and are not the
    division of the body this budget is about."""
    builder = DeckBuilder(tmp_path)
    for _ in range(5):
        page = builder.page()
        _prose(builder, page, left=0.7, top=1.3, width=11.9, height=4.0)
        for slot in range(6):
            builder.panel(page, left=0.7 + slot * 0.8, top=6.6, width=0.7, height=0.4)
    path = builder.save("chips.pptx")

    assert equal_card_habit(path) == []


def test_a_deck_that_reaches_for_the_row_of_cards_is_reported(tmp_path: Path) -> None:
    builder = DeckBuilder(tmp_path)
    for number in range(8):
        page = builder.page()
        _prose(builder, page, left=0.7, top=0.6, width=11.9, height=1.0)
        if number < 5:
            _cards(builder, page)
        else:
            _split(builder, page, left_width=8.0, right_width=3.6)
    path = builder.save("habit.pptx")

    findings = equal_card_habit(path)

    assert [finding.detail["pages"] for finding in findings] == [[1, 2, 3, 4, 5]]
    assert findings[0].detail["composed"] == 8


def test_the_budget_is_a_share_and_not_a_page_count(tmp_path: Path) -> None:
    """Three comparison pages in a long deck are three arguments, not a habit."""
    builder = DeckBuilder(tmp_path)
    for number in range(16):
        page = builder.page()
        _prose(builder, page, left=0.7, top=0.6, width=11.9, height=1.0)
        if number < 3:
            _cards(builder, page)
        else:
            _split(builder, page, left_width=8.0, right_width=3.6)
    path = builder.save("long.pptx")

    assert equal_card_habit(path) == []


def test_structural_pages_are_not_counted_either_way(tmp_path: Path) -> None:
    """An agenda page of equal chips is page furniture, and counting it both adds a
    card row and enlarges the deck it is a share of."""
    builder = DeckBuilder(tmp_path)
    for number in range(8):
        page = builder.page()
        _prose(builder, page, left=0.7, top=0.6, width=11.9, height=1.0)
        _cards(builder, page) if number < 5 else _split(builder, page, left_width=8.0, right_width=3.6)
    path = builder.save("furniture.pptx")

    kept = {finding.kind for finding in quota_findings(path, structural=(1, 2, 3, 4))}

    assert "equal_card_habit" not in kept


def test_a_deck_of_equal_divisions_is_reported(tmp_path: Path) -> None:
    builder = DeckBuilder(tmp_path)
    for _ in range(8):
        page = builder.page()
        _prose(builder, page, left=0.7, top=0.6, width=11.9, height=1.0)
        _split(builder, page, left_width=5.6, right_width=5.6)
    path = builder.save("even.pptx")

    findings = symmetry_habit(path)

    assert [finding.detail["share"] for finding in findings] == [0.0]


def test_a_deck_that_divides_its_pages_unequally_is_left_alone(tmp_path: Path) -> None:
    builder = DeckBuilder(tmp_path)
    for number in range(8):
        page = builder.page()
        _prose(builder, page, left=0.7, top=0.6, width=11.9, height=1.0)
        if number % 2:
            _split(builder, page, left_width=8.4, right_width=3.2)
        else:
            _split(builder, page, left_width=5.6, right_width=5.6)
    path = builder.save("mixed.pptx")

    assert symmetry_habit(path) == []


def test_a_short_deck_has_no_budget_to_spend(tmp_path: Path) -> None:
    """Four pages of one shape is not a habit, and the deck-wide readings say nothing
    about a file that has not yet decided what it is."""
    builder = DeckBuilder(tmp_path)
    for _ in range(4):
        page = builder.page()
        _prose(builder, page, left=0.7, top=0.6, width=11.9, height=1.0)
        _split(builder, page, left_width=5.6, right_width=5.6)
    path = builder.save("short.pptx")

    assert symmetry_habit(path) == []
