"""Whether a page says which page it is, in the smallest file per state.

Two readings and four states between them: no feet at all, feet without numbers,
feet with numbers, and a deck too short to have a habit either way. The number has to
be a `slidenum` field rather than a digit, and the case for that is a test of its own
because a digit passes every other check in this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

from raven_ppt.services.measure.furniture import (
    ENOUGH_PAGES,
    footer_findings,
    grid_findings,
)
from tests._ppt_engine_fixtures import DeckBuilder

LONG = "这一段正文写得足够长，好让它算作一段文字而不是一个标签。"


def _dir(root: Path, name: str) -> Path:
    made = root / name
    made.mkdir(parents=True, exist_ok=True)
    return made


def _pages(builder: DeckBuilder, count: int, *, note: str | None = None, numbered: bool = False) -> Path:
    for index in range(count):
        page = builder.page()
        builder.text(page, (LONG, 16.0), left=0.7, top=1.3, width=11.9, height=4.6, wrap=True)
        if note:
            builder.text(page, (note, 12.0), left=0.7, top=6.9, width=6.0, height=0.3)
        if numbered:
            box = builder.text(page, ("00", 12.0), left=11.4, top=6.9, width=1.2, height=0.3)
            _as_field(box)
    return builder.save("deck.pptx")


def _as_field(shape) -> None:
    """The `slidenum` field, written the way `ppt_layout.footer` writes it."""
    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    paragraph = shape.text_frame.paragraphs[0]._p
    run = paragraph.find(f"{{{namespace}}}r")
    field = paragraph.makeelement(f"{{{namespace}}}fld", {"id": "{1}", "type": "slidenum"})
    text = field.makeelement(f"{{{namespace}}}t", {})
    text.text = "1"
    field.append(text)
    paragraph.replace(run, field)


def test_a_deck_whose_pages_all_trail_off_is_reported(tmp_path: Path) -> None:
    path = _pages(DeckBuilder(tmp_path), 6)

    findings = footer_findings(path)

    assert [finding.kind for finding in findings] == ["no_footer"]
    assert findings[0].detail["absent"] == [1, 2, 3, 4, 5, 6]
    assert "page(footer=True)" in findings[0].message


def test_a_template_with_no_foot_of_its_own_is_not_asked_for_one(tmp_path: Path) -> None:
    """Eight of the twelve bundled templates put text in the bottom band on no page at
    all, and the other four on one or two of their fifteen to twenty-five. Asking a deck
    built in one of those for a foot asks it to leave the design it was given."""
    deck = _pages(DeckBuilder(_dir(tmp_path, "deck")), 6)
    template = _pages(DeckBuilder(_dir(tmp_path, "template")), 6)

    assert [one.kind for one in footer_findings(deck)] == ["no_footer"]
    assert footer_findings(deck, (), template) == []


def test_a_template_that_does_foot_its_pages_still_asks(tmp_path: Path) -> None:
    template = _pages(DeckBuilder(_dir(tmp_path, "template")), 6, note="a source line")
    deck = _pages(DeckBuilder(_dir(tmp_path, "deck")), 6)

    assert [one.kind for one in footer_findings(deck, (), template)] == ["no_footer"]


def test_feet_without_numbers_are_a_different_reading(tmp_path: Path) -> None:
    """Half the job: the page ends visibly and still does not say which page it is,
    and the remedy is one keyword rather than a layout change."""
    path = _pages(DeckBuilder(tmp_path), 6, note="来源：公开报道整理")

    findings = footer_findings(path)

    assert [finding.kind for finding in findings] == ["unnumbered_pages"]
    assert "slidenum" in findings[0].message


def test_a_foot_with_a_field_in_it_is_clean(tmp_path: Path) -> None:
    path = _pages(DeckBuilder(tmp_path), 6, note="来源：公开报道整理", numbered=True)

    assert footer_findings(path) == []


def test_the_number_is_looked_for_across_the_whole_foot(tmp_path: Path) -> None:
    """The bug this replaced stopped at the first text in the band.

    A foot carries a source line on the left and the number on the right, and whichever
    the script drew first is the one that was read -- which reported a numbered deck as
    unnumbered. The note is drawn first here for exactly that reason.
    """
    builder = DeckBuilder(tmp_path)
    for _ in range(6):
        page = builder.page()
        builder.text(page, (LONG, 16.0), left=0.7, top=1.3, width=11.9, height=4.6, wrap=True)
        builder.text(page, ("来源：公开报道整理", 12.0), left=0.7, top=6.9, width=6.0, height=0.3)
        _as_field(builder.text(page, ("00", 12.0), left=11.4, top=6.9, width=1.2, height=0.3))
    path = builder.save("ordered.pptx")

    assert footer_findings(path) == []


def test_structural_pages_are_not_asked_for_a_foot(tmp_path: Path) -> None:
    """A cover and a divider are full-bleed by design and carry no page number in any
    deck anyone ships."""
    builder = DeckBuilder(tmp_path)
    for index in range(8):
        page = builder.page()
        builder.text(page, (LONG, 16.0), left=0.7, top=1.3, width=11.9, height=4.6, wrap=True)
        if index >= 3:
            builder.text(page, ("来源：公开报道整理", 12.0), left=0.7, top=6.9, width=6.0, height=0.3)
            _as_field(builder.text(page, ("00", 12.0), left=11.4, top=6.9, width=1.2, height=0.3))
    path = builder.save("mixed.pptx")

    assert footer_findings(path, structural=(1, 2, 3)) == []


def test_a_deck_too_short_to_have_a_habit_says_nothing(tmp_path: Path) -> None:
    path = _pages(DeckBuilder(tmp_path), ENOUGH_PAGES - 1)

    assert footer_findings(path) == []


def _columned(builder: DeckBuilder, count: int, *, on_planes: bool) -> Path:
    for _ in range(count):
        page = builder.page()
        builder.text(page, ("页面标题", 28.0), left=0.7, top=0.2, width=11.9, height=0.9)
        for slot in range(3):
            left = 0.7 + slot * 4.1
            if on_planes:
                builder.panel(page, left=left, top=2.0, width=3.7, height=2.6)
            builder.text(
                page,
                (f"第 {slot + 1} 栏的说明文字，长到算作一段而不是一个标签。", 14.0),
                left=left + 0.2 if on_planes else left,
                top=2.2 if on_planes else 2.0,
                width=3.3 if on_planes else 3.7,
                height=2.2,
                wrap=True,
            )
    return builder.save("columned.pptx")


def _two_rows(builder: DeckBuilder, panels: tuple[float, ...], copies: tuple[float, ...]) -> Path:
    """A row of panels and a row of copy under it, each row's left edges given."""
    page = builder.page()
    builder.text(page, ("页面标题", 28.0), left=0.7, top=0.2, width=11.9, height=0.9)
    for left in panels:
        builder.panel(page, left=left, top=2.0, width=3.2, height=0.9)
    for left in copies:
        builder.text(page, (LONG, 14.0), left=left, top=3.1, width=3.2, height=0.8, wrap=True)
    return builder.save("rows.pptx")


def test_two_grids_on_one_page_are_reported(tmp_path: Path) -> None:
    """The reading this check was built for, measured on the page that showed it.

    A delivered page set its panels every 3.84in and the copy under them every 3.96in,
    so the two grids drifted apart across the row: 0.12in at the first column, 0.24 at
    the second, 0.36 at the third. Every pair is close enough to read as one edge and
    far enough not to be one.
    """
    path = _two_rows(DeckBuilder(tmp_path), (0.72, 4.56, 8.40), (0.84, 4.80, 8.76))

    findings = grid_findings(path)

    assert [finding.kind for finding in findings] == ["grid_drift"]
    assert findings[0].detail["steps"] == [3.84, 3.96]
    assert findings[0].detail["drift"] == 0.36, "the miss at the last column"


def test_one_grid_is_clean(tmp_path: Path) -> None:
    path = _two_rows(DeckBuilder(tmp_path), (0.72, 4.56, 8.40), (0.72, 4.56, 8.40))

    assert grid_findings(path) == []


def test_a_staircase_is_a_step_repeated_and_not_a_drift(tmp_path: Path) -> None:
    """`layouts-multiples.md` staggers its evidence cards 0.45in each, three times, and
    this check reported all of it -- the passage and the reading contradicted each other
    on a page the passage was written from. What separates them is the step: a staircase
    repeats one, and two grids have two different pitches, which is what makes the gap
    grow across the row.
    """
    builder = DeckBuilder(tmp_path)
    page = builder.page()
    builder.text(page, ("页面标题", 28.0), left=0.7, top=0.2, width=11.9, height=0.9)
    for index, left in enumerate((6.20, 6.65, 7.10)):
        builder.text(page, (LONG, 14.0), left=left, top=2.0 + index * 1.3, width=5.3, height=1.1, wrap=True)
    path = builder.save("stagger.pptx")

    assert grid_findings(path) == []


def test_two_edges_alone_are_not_a_grid(tmp_path: Path) -> None:
    """A grid is a step repeated, so two edges cannot be one however close they are.

    Which is also why indentation comes back clean: a badge at the margin, its label a
    quarter inch in and its copy a quarter past that is three edges and no repeated step
    across the page. Nine of the twelve bundled templates were reported for having one
    before the reading asked for the step.
    """
    path = _two_rows(DeckBuilder(tmp_path), (0.72,), (0.92,))

    assert grid_findings(path) == []

    (tmp_path / "nested").mkdir()
    stepped = DeckBuilder(tmp_path / "nested")
    page = stepped.page()
    stepped.text(page, ("页面标题", 28.0), left=0.7, top=0.2, width=11.9, height=0.9)
    for index, left in enumerate((0.72, 0.96, 1.22)):
        stepped.text(page, (LONG, 14.0), left=left, top=2.0 + index * 1.0, width=3.0, height=0.8, wrap=True)

    assert grid_findings(stepped.save("nested.pptx")) == []


def test_a_foot_drawn_by_our_own_helper_counts_as_one(tmp_path: Path) -> None:
    """The band has to cover what `ppt_layout` actually reserves.

    `page(footer=True)` hands back a strip at 6.48-6.78in and `footer()` writes into it
    at 6.58in. The first version of this reading put its floor at 6.60in, so a page whose
    foot was drawn by the helper written for it came back as having no foot -- and every
    test here passed, because each one writes its own foot at 6.9in.
    """
    from raven_ppt.services.assets.layout import layout_module_source
    from raven_ppt.services.assets.script_helpers import script_helper_files

    room = tmp_path / "helpers"
    room.mkdir()
    for name, text in script_helper_files().items():
        (room / name).write_text(text, encoding="utf-8")
    (room / "ppt_layout.py").write_text(layout_module_source(), encoding="utf-8")
    sys.path.insert(0, str(room))
    try:
        import ppt_layout
        from ppt_theme import THEMES

        theme = THEMES[next(iter(THEMES))]
        deck = DeckBuilder(tmp_path)
        for _ in range(6):
            page = deck.page()
            frame = ppt_layout.page(footer=True)
            ppt_layout.write(
                page, frame.body, LONG, size=16.0, colour=theme["foreground"], font="Arial", cjk_font="微软雅黑"
            )
            ppt_layout.footer(page, frame.footer, theme, note="来源：公开报道整理", font="Arial", cjk_font="微软雅黑")
        path = deck.save("helper-feet.pptx")

        assert footer_findings(path) == [], "the helper's own foot has to land in the band"
    finally:
        sys.path.remove(str(room))
        for name in ("ppt_layout", "ppt_theme", "ppt_icons", "ppt_shapes"):
            sys.modules.pop(name, None)
