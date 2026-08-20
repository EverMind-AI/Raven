"""The type census, and the two numbers a built page has to clear."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.contracts.findings import Audience, Severity
from raven.ppt.services.measure.type_size import (
    BODY_FLOOR_PT,
    MIN_FLOOR_PT,
    census,
    type_findings,
    type_floors,
)
from tests.ppt.conftest import DeckBuilder

pytest.importorskip("pptx")


def test_the_floors_are_two_constants(deck: DeckBuilder) -> None:
    """Every PowerPoint canvas is 7.5in tall, so the floors do not scale.

    The page height is still accepted, and still ignored: the day a 5.625in
    canvas turns up, this test is where the exception gets written.
    """
    assert (BODY_FLOOR_PT, MIN_FLOOR_PT) == (14.0, 10.8)
    assert type_floors() == (14.0, 10.8)
    assert type_floors(7.5) == (14.0, 10.8)
    assert type_floors(5.625) == (14.0, 10.8)


def test_body_size_is_what_most_of_the_copy_runs_at(deck: DeckBuilder) -> None:
    """A 32pt title does not make a page of 12pt copy a 32pt page."""
    page = deck.page()
    deck.text(page, ("Title of the page", 32.0), ("body copy here" * 6, 12.0))

    measured = census(deck.save())[0]

    assert measured.body_pt == 12.0
    assert measured.under_floor is True


def test_table_cells_count_toward_the_census(deck: DeckBuilder) -> None:
    """Results decks put their smallest type in tables, so tables are read."""
    from pptx.util import Pt

    page = deck.page()
    deck.text(page, ("a heading that clears the floor", 18.0), height=1.0)
    table = deck.table(page, 2, 2, top=2.0)
    for row in table.table.rows:
        for cell in row.cells:
            run = cell.text_frame.paragraphs[0].add_run()
            run.text = "a cell of table copy"
            run.font.size = Pt(9.0)
    built = deck.save()

    measured = census(built)[0]

    assert measured.smallest_pt == 9.0
    assert measured.below_hard_floor > 0
    assert [finding.detail["smallest_pt"] for finding in type_findings(built)] == [9.0]


def test_short_marks_are_not_held_to_the_body_floor(deck: DeckBuilder) -> None:
    """Axis labels and page numbers are set small on purpose."""
    page = deck.page()
    deck.text(
        page,
        ("Findings across the three benchmarks", 20.0),
        ("body copy that carries the page" * 3, 16.0),
        ("7", 9.0),
    )
    built = deck.save()

    measured = census(built)[0]

    assert measured.body_pt == 16.0
    assert measured.smallest_pt == 16.0  # the 9pt page number never entered the census
    assert type_findings(built) == []


def test_findings_are_reported_per_page(deck: DeckBuilder) -> None:
    """A deck is rarely wrong everywhere, and the fix is per page."""
    for text, size in (
        ("copy that clears the floor comfortably" * 3, 16.0),
        ("copy set too small to project" * 3, 11.5),
        ("copy that clears the floor comfortably" * 3, 15.0),
    ):
        deck.text(deck.page(), (text, size))

    findings = type_findings(deck.save())

    assert [finding.page for finding in findings] == [2]
    assert findings[0].detail["body_pt"] == 11.5
    assert findings[0].detail["body_floor_pt"] == 14.0
    assert "under the 14.0pt floor" in findings[0].message


def test_the_floor_is_a_warning_addressed_to_the_design_pass(deck: DeckBuilder) -> None:
    """Raising a size costs room, and only the pass that arranges a page has it.

    A warning rather than a refusal because the room comes from the copy: a gate
    that blocked publication until the floor was met could be answered by
    shrinking the copy back, which is the oscillation D2 describes.
    """
    deck.text(deck.page(), ("copy set too small to project" * 3, 11.0))

    finding = type_findings(deck.save())[0]

    assert finding.kind == "type_floor"
    assert finding.severity is Severity.WARNING
    assert finding.audience is Audience.DESIGNER
    assert "Do not shrink it back to fit" in finding.message


def test_a_deck_that_clears_the_floor_reports_nothing(deck: DeckBuilder) -> None:
    for _ in range(3):
        deck.text(deck.page(), ("copy that clears the floor" * 4, 15.0))

    assert type_findings(deck.save()) == []


def test_a_page_with_no_sized_copy_is_not_a_finding(deck: DeckBuilder) -> None:
    """A page of pictures has no body size to be under a floor."""
    deck.page()

    built = deck.save()

    assert census(built)[0].body_pt is None
    assert type_findings(built) == []


def test_a_source_line_is_not_held_to_the_body_floor(tmp_path: Path) -> None:
    """ "来源：TarViS 原论文（CVPR 2023）" at 11pt is legible, deliberate, and 24
    characters long, so no length rule tells it from copy. It appeared on seven pages
    of one delivered deck as the same finding, which is how a check teaches an author
    to stop reading it."""
    from raven.ppt.services.measure.type_size import Span, type_findings

    deck = _deck(
        tmp_path,
        [
            ("来源：TarViS 原论文（CVPR 2023），Table 2", 1.0, 6.9, 6.0, 0.3),
            ("这一段是页面的正文，长度足够被当作正文而不是标记来判断", 1.0, 2.0, 6.0, 1.0),
        ],
    )
    spans = [
        Span(page=1, size_pt=11.0, text="来源：TarViS 原论文（CVPR 2023），Table 2", x0=75, y0=500, x1=400, y1=515),
        Span(
            page=1,
            size_pt=17.0,
            text="这一段是页面的正文，长度足够被当作正文而不是标记来判断",
            x0=75,
            y0=150,
            x1=460,
            y1=170,
        ),
    ]

    assert type_findings(deck, spans) == []


def test_body_copy_under_the_floor_still_reports(tmp_path: Path) -> None:
    from raven.ppt.services.measure.type_size import Span, type_findings

    deck = _deck(tmp_path, [("这一段正文被框压到了读者看不清的字号，需要报出来给设计环", 1.0, 2.0, 6.0, 1.0)])
    spans = [
        Span(
            page=1,
            size_pt=10.8,
            text="这一段正文被框压到了读者看不清的字号，需要报出来给设计环",
            x0=75,
            y0=150,
            x1=460,
            y1=165,
        )
    ]

    findings = type_findings(deck, spans)
    assert [f.kind for f in findings] == ["type_floor"]
    assert findings[0].detail["sizes_pt"] == [10.8]


def _deck(tmp_path: Path, boxes: list[tuple[str, float, float, float, float]]) -> Path:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    for text, left, top, width, height in boxes:
        box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        box.text_frame.text = text
    path = tmp_path / "deck.pptx"
    presentation.save(str(path))
    return path
