"""What the outline stage measures about the plan itself.

Both checks here were written against a real twelve-page plan and only after it was
read: three of its pages were section dividers announcing the claim of the page
behind them, and none of its twelve pages planned to show a figure while the
materials held thirteen.
"""

from __future__ import annotations

import re
from pathlib import Path

from raven.ppt.contracts.outline import Outline, PagePlan
from raven.ppt.tools.outline import (
    SAME_PROTOTYPE_IS_STRUCTURAL,
    STRUCTURAL_SHARE,
    _structural,
    _thin_pages,
)


class _State:
    def __init__(self, figures: int) -> None:
        self.figures = tuple(type("Figure", (), {"figure_id": f"fig_{index}"})() for index in range(figures))


def _page(number: int, *, says: int = 3, prototype: int | None = None, figures: tuple[str, ...] = ()) -> PagePlan:
    return PagePlan(
        page=number,
        claim=f"claim {number}",
        says=tuple(f"point {index}" for index in range(says)),
        prototype=prototype,
        figures=figures,
    )


def test_the_shares_are_the_measured_ones() -> None:
    assert STRUCTURAL_SHARE == 0.25
    assert SAME_PROTOTYPE_IS_STRUCTURAL == 3


def test_three_dividers_in_twelve_pages_are_reported() -> None:
    """The live case: three pages adapting the same template page, one line each."""
    pages = tuple(
        _page(number, says=1 if number in (3, 5, 8) else 3, prototype=3 if number in (3, 5, 8) else number)
        for number in range(1, 13)
    )
    findings = _structural(Outline(takeaway="t", pages=pages))

    assert [f.kind for f in findings] == ["structural_pages"]
    assert findings[0].detail == {"pages": [3, 5, 8], "of": 12}


def test_one_divider_in_twelve_pages_is_not_worth_saying() -> None:
    pages = tuple(_page(number, says=1 if number == 5 else 3, prototype=number) for number in range(1, 13))

    assert _structural(Outline(takeaway="t", pages=pages)) == []


class _Template:
    def __init__(self, source) -> None:
        self.source = source


class _Bound:
    def __init__(self, source) -> None:
        self.template = _Template(source)
        self.figures = ()


def _house_template(path):
    """A template that says what its own pages are for: cover, agenda, closing."""
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    for heading in ("A deck about something", "Agenda", "A content page", "谢谢观看"):
        page = presentation.slides.add_slide(presentation.slide_layouts[6])
        page.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1)).text_frame.text = heading
    presentation.save(str(path))
    return path


def test_the_cover_and_the_closing_page_have_to_be_the_templates(tmp_path) -> None:
    """The one requirement, not advice: a deck that draws its own cover reads as not
    the user's before a word of it is read."""
    from raven.ppt.tools.outline import _house_pages

    template = _house_template(tmp_path / "house.pptx")
    pages = tuple(_page(number, prototype=None) for number in range(1, 13))
    findings = _house_pages(Outline(takeaway="t", pages=pages), _Bound(template))

    roles = {f.detail["role"] for f in findings}
    assert roles == {"cover", "agenda", "closing"}
    assert all(f.severity.value == "blocking" for f in findings)


def test_a_plan_that_uses_them_passes(tmp_path) -> None:
    from raven.ppt.tools.outline import _house_pages

    template = _house_template(tmp_path / "house.pptx")
    pages = (
        _page(1, prototype=1),
        _page(2, prototype=2),
        *(_page(number, prototype=3) for number in range(3, 12)),
        _page(12, prototype=4),
    )

    assert _house_pages(Outline(takeaway="t", pages=pages), _Bound(template)) == []


def test_a_short_deck_needs_no_index(tmp_path) -> None:
    """Five pages do not need a table of contents."""
    from raven.ppt.tools.outline import _house_pages

    template = _house_template(tmp_path / "house.pptx")
    pages = (_page(1, prototype=1), _page(2), _page(3), _page(4), _page(5, prototype=4))
    findings = _house_pages(Outline(takeaway="t", pages=pages), _Bound(template))

    assert [f.detail["role"] for f in findings] == []


def test_no_template_means_no_requirement() -> None:
    from raven.ppt.tools.outline import _house_pages

    class NoTemplate:
        template = None
        figures = ()

    pages = tuple(_page(number) for number in range(1, 13))
    assert _house_pages(Outline(takeaway="t", pages=pages), NoTemplate()) == []


class _Deck:
    """A project whose materials say this much, without one on disk."""

    def __init__(self, chars: int = 0) -> None:
        self.chars = chars
        self.ingest_dir = Path("/nowhere")


def test_the_says_field_names_no_character_count() -> None:
    """A count here is a different page in each language.

    Measured in one body box at one size, Chinese fills it at 400 characters and
    English at 1168. A budget stated to the planner therefore asks one language for
    a full page and the other for a third of one -- so the field says what a page
    has to carry, and whether it fits is settled by measuring the render.
    """
    from pathlib import Path

    from raven.ppt.tools.outline import PptOutlineTool

    said = PptOutlineTool(workspace=Path("/tmp")).parameters["properties"]["pages"]["items"]["properties"]["says"]

    assert not re.search(r"\d{3,} character", said["description"])
    assert "the claim is carried" in said["description"]


class _Bare:
    """A project state with no template bound, so no page counts as structural."""

    template = None


def _planned(number: int, says: tuple[str, ...], **kw) -> PagePlan:
    return PagePlan(page=number, claim=f"claim {number}", says=says, **kw)


def test_a_page_whose_whole_plan_is_one_line_is_reported() -> None:
    outline = Outline(takeaway="t", pages=(_planned(1, ("Only this.",)),))

    found = _thin_pages(outline, _Bare())

    assert [finding.kind for finding in found] == ["thin_page"]
    assert found[0].page == 1
    assert found[0].detail == {"says": 1}


def test_a_page_planning_nothing_at_all_is_reported() -> None:
    assert [f.kind for f in _thin_pages(Outline(takeaway="t", pages=(_planned(1, ()),)), _Bare())] == ["thin_page"]


def test_two_points_are_a_plan_however_short_they_are() -> None:
    """No character floor, deliberately.

    It used to take five points or 140 characters, measured off one deck. A count
    is a different page in each language -- in one body box at one size Chinese
    fills it at 400 characters and English at 1168 -- so the floor asked one
    language for a page and the other for a third of one. How full a page comes out
    is settled by measuring the render, not guessed from the plan.
    """
    outline = Outline(takeaway="t", pages=(_planned(1, ("Up.", "Down.")),))

    assert _thin_pages(outline, _Bare()) == []


def test_a_table_led_page_without_cell_structure_is_reported() -> None:
    outline = Outline(
        takeaway="t",
        pages=(_planned(1, ("one", "two"), carries="pricing table"),),
    )

    findings = _thin_pages(outline, _Bare())
    assert [finding.kind for finding in findings] == ["thin_page"]
    assert findings[0].detail == {"table_plan": False, "carries": "pricing table"}


def test_a_drawn_table_with_complete_cell_rows_is_planned() -> None:
    outline = Outline(
        takeaway="t",
        pages=(
            _planned(
                1,
                ("Compare the plans.",),
                carries="drawn table",
                table_plan={
                    "columns": ("Plan", "Price"),
                    "rows": (("Hobby", "Free"), ("Pro", "$249")),
                    "reading": "compare price",
                },
            ),
        ),
    )

    assert _thin_pages(outline, _Bare()) == []


def test_a_page_carrying_a_figure_plans_its_copy_in_the_figure() -> None:
    outline = Outline(takeaway="t", pages=(_planned(1, ("One line.",), figures=("fig-1",)),))

    assert _thin_pages(outline, _Bare()) == []


def test_a_page_with_an_errand_is_going_to_get_something_to_show() -> None:
    outline = Outline(takeaway="t", pages=(_planned(1, ("One line.",), needs="draw the six-stage path"),))

    assert _thin_pages(outline, _Bare()) == []
