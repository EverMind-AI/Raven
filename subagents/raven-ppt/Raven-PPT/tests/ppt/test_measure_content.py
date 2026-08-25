"""Whether a page shows anything, and how wide a table gets.

There used to be a character ceiling here as well. It was one number for every
language, and a character is not one thing: measured in one box at one size,
Chinese fills it at 400 and English at 1168, so 700 fired long after a Chinese
page had overflowed and never at all on an English one. What it was for is done
by measuring the render -- `card_overflow`, `clipped_copy`, `crowded_panel` --
which is a fact about the built page rather than a count standing in for one.
"""

from __future__ import annotations

import pytest

from raven.ppt.contracts.findings import Audience, Severity
from raven.ppt.services.measure.content import (
    DIAGRAM_SHAPES,
    EVIDENCE_SHARE,
    MAX_TABLE_COLUMNS,
    evidence_coverage,
    flat_formulas,
    native_tables,
    unmarked_points,
    wide_tables,
)
from tests.ppt.conftest import DeckBuilder

pytest.importorskip("pptx")


def test_the_evidence_share_is_seven_tenths_of_the_pages() -> None:
    assert EVIDENCE_SHARE == 0.8


def test_a_deck_of_prose_pages_is_reported_once(deck: DeckBuilder) -> None:
    for index in range(5):
        page = deck.page()
        deck.text(page, "prose that is read out loud " * 8, height=3.0)
        if index == 0:
            deck.table(page, 2, 2, top=5.0)

    findings = evidence_coverage(deck.save())

    assert len(findings) == 1  # one deck-wide finding, not one per prose page
    assert findings[0].page is None
    assert findings[0].detail == {"pages_with_evidence": 1, "pages": 5, "share": 0.8, "structural": []}
    assert findings[0].audience is Audience.AUTHOR
    assert "of 5 content pages" in findings[0].message


def test_the_pages_the_template_owns_are_not_counted(deck: DeckBuilder) -> None:
    """A cover and a closing page cannot show a figure, so counting them is two pages of
    proof a deck never had: one live deck read as 11 of 13 when it was 11 of 11 on the
    pages this check is about, and another chased the warning through eight rebuilds."""
    for index in range(4):
        page = deck.page()
        deck.text(page, "prose that is read out loud " * 8, height=3.0)
        if index in (1, 2, 3):
            deck.table(page, 2, 2, top=5.0)
    built = deck.save()

    assert evidence_coverage(built) != [], "3 of 4 is under the share when the cover counts"
    assert evidence_coverage(built, structural=[1]) == [], "3 of 3 content pages show something"


def test_eight_pages_in_ten_showing_something_clears_the_share(deck: DeckBuilder) -> None:
    for index in range(10):
        page = deck.page()
        deck.text(page, "prose that is read out loud " * 4, height=3.0)
        if index < 8:
            deck.table(page, 2, 2, top=5.0)

    assert evidence_coverage(deck.save()) == []


def test_seven_pages_in_ten_does_not(deck: DeckBuilder) -> None:
    """0.7 was the share while a deck's cover and closing counted among the pages that
    failed to show anything. With those excluded a good deck is at or near every content
    page, and the two live decks measured are 11 of 11 and 7 of 9."""
    for index in range(10):
        page = deck.page()
        deck.text(page, "prose that is read out loud " * 4, height=3.0)
        if index < 7:
            deck.table(page, 2, 2, top=5.0)

    assert evidence_coverage(deck.save()) != []


def test_six_pages_in_ten_does_not(deck: DeckBuilder) -> None:
    for index in range(10):
        page = deck.page()
        deck.text(page, "prose that is read out loud " * 4, height=3.0)
        if index < 6:
            deck.table(page, 2, 2, top=5.0)

    assert evidence_coverage(deck.save())[0].detail["pages_with_evidence"] == 6


def test_an_empty_deck_reports_nothing(deck: DeckBuilder) -> None:
    assert evidence_coverage(deck.save()) == []


def test_a_diagram_drawn_from_panels_counts_as_evidence(deck: DeckBuilder) -> None:
    """The clause that never fired, now measured rather than guessed.

    It asked for a filled shape with no text *frame*, and every fillable shape in
    python-pptx has one, so a deck that drew its own flow, cards, table and chart was
    told "0 of 8 pages show anything". Two published decks say the panels these
    programs draw hold no text themselves -- the copy sits in a textbox over them --
    so an empty shape is the right test and both land at 6 of 8 pages.
    """
    assert DIAGRAM_SHAPES == 4
    for _ in range(5):
        page = deck.page()
        deck.text(page, "a page whose diagram is drawn from panels", height=1.0)
        for column in range(4):
            deck.panel(page, left=0.5 + 3 * column, top=2.5, width=2.5, height=2.0)

    assert evidence_coverage(deck.save()) == []


def test_cards_that_hold_their_own_copy_are_compartments_not_a_diagram(deck: DeckBuilder) -> None:
    """Four panels with a paragraph inside each is a page split into boxes.

    The distinction the empty-shape test draws, and the reason it is not "any four
    filled shapes": prose in compartments is the thing `evidence` exists to name.
    """
    for _ in range(5):
        page = deck.page()
        for column in range(4):
            panel = deck.panel(page, left=0.5 + 3 * column, top=2.5, width=2.5, height=2.0)
            panel.text_frame.text = "a paragraph of copy that happens to sit inside the card itself"

    assert evidence_coverage(deck.save())[0].detail["pages_with_evidence"] == 0


def test_a_page_that_places_a_picture_shows_something(deck: DeckBuilder, image) -> None:
    for _ in range(2):
        page = deck.page()
        deck.text(page, "a claim standing on a figure", height=1.0)
        deck.picture(page, image("fig.png", (200, 40, 40)))

    assert evidence_coverage(deck.save()) == []


def test_the_table_width_ceiling_is_eight_columns() -> None:
    assert MAX_TABLE_COLUMNS == 8


def test_a_table_past_the_ceiling_is_reported(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.table(page, 3, 9, top=1.0)
    deck.table(page, 3, 8, top=4.0)

    findings = wide_tables(deck.save())

    assert [finding.detail["columns"] for finding in findings] == [9]
    assert findings[0].severity is Severity.WARNING
    assert findings[0].audience is Audience.AUTHOR


def test_a_native_office_table_is_reported_for_design_rebuild(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.table(page, 4, 3, top=1.0)

    findings = native_tables(deck.save())

    assert [finding.kind for finding in findings] == ["native_table"]
    assert findings[0].severity is Severity.WARNING
    assert findings[0].audience is Audience.DESIGNER
    assert findings[0].detail == {"tables": 1}


def test_an_escape_printed_as_characters_is_refused(deck: DeckBuilder) -> None:
    """Seen on a page in a live build: two cards read
    "YTVIS：46.3 → 48.3\\nOVIS：29.8 → 31.1" with the backslash-n printed, because the
    author's string went through a JSON round trip on its way into the tool and came out
    with its escape escaped. The delivered version of the same page had a real newline,
    so nothing but the file tells the two apart."""
    from raven.ppt.services.measure.content import literal_escapes

    deck.text(deck.page(), "YTVIS：46.3 → 48.3\\nOVIS：29.8 → 31.1")

    findings = literal_escapes(deck.save())
    assert [f.kind for f in findings] == ["literal_escape"]
    assert findings[0].severity is Severity.BLOCKING
    assert findings[0].detail["escapes"] == ["\\n"]


def test_a_real_line_break_is_not(deck: DeckBuilder) -> None:
    from raven.ppt.services.measure.content import literal_escapes

    deck.text(deck.page(), "YTVIS：46.3 → 48.3\nOVIS：29.8 → 31.1")

    assert literal_escapes(deck.save()) == []


def test_a_page_showing_code_keeps_its_escape(deck: DeckBuilder) -> None:
    """A slide about escaping is the one place `\\n` belongs on a page, and a quote or a
    bracket in the same line is what says so."""
    from raven.ppt.services.measure.content import literal_escapes

    deck.text(deck.page(), 'print("a\\nb") 打印两行')

    assert literal_escapes(deck.save()) == []


def test_an_expression_written_as_prose_is_reported(deck: DeckBuilder) -> None:
    """Verbatim from a delivered deck, in a 4.7in column: the render broke it after
    the third comma with ")" alone on the next line, and every subscript in it was
    flat -- Qsem reading as a word rather than as Q with a subscript. The page beside
    it set the same notation with `formula` and came out right, which is what makes
    this measurable rather than a matter of taste.
    """
    page = deck.page()
    deck.text(page, ("Qin = concat(Qsem, Qinst, Qobj, Qbg)", 16.0), left=1.0, top=2.0, width=4.7, height=0.5)
    findings = flat_formulas(deck.save())

    assert [f.kind for f in findings] == ["flat_formula"]
    assert findings[0].severity is Severity.WARNING
    assert findings[0].audience is Audience.AUTHOR
    assert "formula()" in findings[0].message


def test_an_arrow_assignment_counts_too(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.text(page, ("Qobj ← EncodeObjects(G, F)", 16.0), left=1.0, top=2.0, width=4.0, height=0.5)

    assert [f.kind for f in flat_formulas(deck.save())] == ["flat_formula"]


@pytest.mark.parametrize(
    "line",
    [
        # A relation and no grouping mark: a sentence about a number.
        "训练成本 = 32 张 A100，批大小 32",
        # A grouping mark and no relation: a caption.
        "Figure 5：单次前向同时执行 VIS 与 VOS（论文原图）",
        # An arrow between two numbers, which is a delta and not an expression.
        "C-VPS 49.7→53.3 是涨的；PET 34.7→30.9 是明确代价",
        # Prose long enough that whoever wrote it is the one who can tell which part
        # of it is notation.
        "分类不再走全连接头：类别被建模成网络的动态输入（语义表示只通过损失监督学到），"
        "架构因此与任务定义解耦，同一套权重在推理时按需拼装查询集合即可热切换任务。",
    ],
)
def test_a_sentence_that_merely_holds_a_symbol_is_not(deck: DeckBuilder, line: str) -> None:
    page = deck.page()
    deck.text(page, (line, 16.0), left=1.0, top=2.0, width=8.0, height=0.6)

    assert flat_formulas(deck.save()) == []


def test_a_line_already_set_as_a_formula_is_left_alone(tmp_path) -> None:
    """The test for "already a formula" is a raised or lowered run, because that is
    the thing `formula` produces and nothing else does.
    """
    import sys

    from pptx import Presentation

    from raven.ppt.services.assets.layout import layout_module_source
    from raven.ppt.services.assets.script_helpers import script_helper_files

    for name, text in script_helper_files().items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "ppt_layout.py").write_text(layout_module_source(), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = __import__("ppt_layout")
        from ppt_theme import THEMES

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        module.formula(
            slide,
            module.Box(0.7, 2.0, 8.0, 2.6),
            "Q_{in} = concat(Q_{sem}, Q_{inst}, Q_{bg})",
            THEMES["ink-graphite"],
            size=16,
        )
        built = tmp_path / "deck.pptx"
        presentation.save(str(built))

        assert flat_formulas(built) == []
    finally:
        sys.path.remove(str(tmp_path))
        for name in ("ppt_layout", "ppt_icons", "ppt_theme"):
            sys.modules.pop(name, None)


def test_parallel_claims_with_no_mark_are_reported(deck: DeckBuilder) -> None:
    """Verbatim from a delivered page: two sentences of 47 and 58 characters under a
    rule, nothing in front of either, so a reader has to work out that they are two
    things rather than one paragraph with a line break in it.
    """
    page = deck.page()
    deck.text(
        page,
        ("分类不再走全连接头：类别被建模成网络的动态输入，语义表示只通过损失监督学到，架构因此与任务定义解耦。", 16.0),
        ("同一套权重在推理时按需拼装查询集合即可热切换任务；论文指出这条接口还能容纳文本 prompt。", 16.0),
        left=1.0,
        top=2.0,
        width=8.0,
        height=1.4,
    )
    findings = unmarked_points(deck.save())

    assert [f.kind for f in findings] == ["unmarked_points"]
    assert findings[0].audience is Audience.AUTHOR
    assert findings[0].detail["points"] == 2
    assert "points()" in findings[0].message


def test_a_mark_typed_at_the_front_counts(deck: DeckBuilder) -> None:
    """Not ideal -- the wrap does not hang -- but the reader can see the list, and
    that is what this measures.
    """
    page = deck.page()
    deck.text(
        page,
        ("· 分类不再走全连接头：类别被建模成网络的动态输入，语义只通过损失监督学到。", 16.0),
        ("· 同一套权重在推理时按需拼装查询集合即可热切换任务，无需任务特定微调。", 16.0),
        left=1.0,
        top=2.0,
        width=8.0,
        height=1.4,
    )

    assert unmarked_points(deck.save()) == []


def test_a_stack_of_short_lines_is_left_alone(deck: DeckBuilder) -> None:
    """A legend, an axis or a list of names. Marks on those are clutter."""
    page = deck.page()
    deck.text(
        page,
        ("R-50 骨干", 16.0),
        ("Swin-T 骨干", 16.0),
        ("Swin-L 骨干", 16.0),
        left=1.0,
        top=2.0,
        width=8.0,
        height=1.4,
    )

    assert unmarked_points(deck.save()) == []


def test_points_writes_a_real_bullet_and_passes(tmp_path) -> None:
    """The call the finding names has to satisfy the finding."""
    import sys

    from pptx import Presentation

    from raven.ppt.services.assets.layout import layout_module_source
    from raven.ppt.services.assets.script_helpers import script_helper_files

    for name, text in script_helper_files().items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "ppt_layout.py").write_text(layout_module_source(), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = __import__("ppt_layout")
        from ppt_theme import THEMES

        presentation = Presentation()
        presentation.slide_width, presentation.slide_height = module.Inches(13.333), module.Inches(7.5)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        frame = module.points(
            slide,
            module.Box(0.7, 2.0, 12.6, 3.4),
            THEMES["ink-graphite"],
            [
                "分类不再走全连接头：类别被建模成网络的动态输入，语义只通过损失监督学到。",
                "同一套权重在推理时按需拼装查询集合即可热切换任务，无需任务特定微调。",
            ],
        )
        built = tmp_path / "deck.pptx"
        presentation.save(str(built))

        assert unmarked_points(built) == []
        # A real bullet with a hanging indent, not a character typed in front.
        namespace = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        first = frame.paragraphs[0]._pPr
        assert first.find(f"{namespace}buChar") is not None
        assert int(first.get("indent")) == -int(first.get("marL"))
        assert not frame.paragraphs[0].text.startswith(("•", "·"))
    finally:
        sys.path.remove(str(tmp_path))
        for name in ("ppt_layout", "ppt_icons", "ppt_theme"):
            sys.modules.pop(name, None)
