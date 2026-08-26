"""Whether a page shows anything, and how wide a table gets.

There used to be a character ceiling here as well. It was one number for every
language, and a character is not one thing: measured in one box at one size,
Chinese fills it at 400 and English at 1168, so 700 fired long after a Chinese
page had overflowed and never at all on an English one. What it was for is done
by measuring the render -- `card_overflow`, `clipped_copy`, `crowded_panel` --
which is a fact about the built page rather than a count standing in for one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.contracts.findings import Severity
from raven.ppt.services.measure.content import (
    COLUMN_SQUEEZE,
    DIAGRAM_SHAPES,
    EVIDENCE_SHARE,
    SAFE_MARGIN_IN,
    banded_tables,
    evidence_coverage,
    flat_formulas,
    native_tables,
    planned_tables,
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


def test_a_column_is_squeezed_when_it_holds_more_than_it_has_room_for() -> None:
    assert COLUMN_SQUEEZE > 1.0, "a column exactly as wide as its text is tight, not squeezed"


def test_a_table_is_reported_for_its_narrow_columns_and_not_for_its_column_count(deck: DeckBuilder) -> None:
    """The readable question is the width of a column, not how many there are.

    This used to fire past eight columns, which is the wrong question twice: eight
    short columns of figures read fine across a 13.3in canvas, and four columns of
    phrases crammed into 2.4in do not. So the wide table below, whose columns have
    room, is not reported -- and the narrow one, which has fewer columns, is.
    """
    page = deck.page()
    deck.table(page, 3, 12, top=1.0, width=12.0, cell="Q4")
    deck.table(page, 3, 4, top=4.0, width=2.4, cell="Professional services transformation")

    findings = wide_tables(deck.save())

    assert len(findings) == 1, [finding.message for finding in findings]
    assert findings[0].detail["columns"] == 4
    assert findings[0].severity is Severity.WARNING
    assert findings[0].detail["squeezed"][0]["needs_in"] > findings[0].detail["squeezed"][0]["has_in"]


# A plan whose five columns of phrases want more width than a 13.333in page carries
# even with every cell wrapped onto a second line. The same reading `ppt_outline`
# makes before the program is written, which a live run answered with "the renderer
# will wrap the cells onto a second line" and shipped.
_WIDE_PLAN = {
    "columns": [
        "Professional services transformation",
        "Managed detection and response retainer",
        "Regulatory reporting and assurance desk",
        "Platform modernisation programme office",
        "Sustainability advisory and reporting",
    ],
    "rows": [
        ["Annual contract value committed", "1.4", "2.2", "0.9", "3.1"],
        ["Gross margin after ramp", "41%", "37%", "52%", "29%"],
    ],
    "reading": "which line carries the margin",
}
_NARROW_PLAN = {
    "columns": ["Task", "TarViS", "Specialist"],
    "rows": [["VIS", "48.3", "46.3"], ["VPS", "58.2", "56.1"]],
    "reading": "one model against four specialists",
}


def _outline(plans: dict[int, dict]):
    """An outline of numbered pages, each carrying the table plan given for it."""
    from raven.ppt.contracts.outline import Outline, PagePlan

    return Outline(
        takeaway="one model, four tasks",
        pages=tuple(
            PagePlan(page=number, claim="a comparison", table_plan=plan) for number, plan in sorted(plans.items())
        ),
    )


def test_a_page_that_planned_a_table_and_drew_none_is_refused(deck: DeckBuilder) -> None:
    """The twin of `unplaced_figure`: a promise in the plan, read back off the file.

    Blocking, because it is a disagreement between the plan and the deck rather than
    a reading of a rendered page -- nothing about it is answered by making the page
    smaller, and both ways out are one edit.
    """
    page = deck.page()
    deck.text(page, "the comparison, described in prose instead", height=2.0)

    findings = planned_tables(deck.save(), _outline({1: _NARROW_PLAN}))

    assert [finding.kind for finding in findings] == ["unplaced_table"]
    assert findings[0].severity is Severity.BLOCKING
    assert findings[0].page == 1
    assert findings[0].detail == {"columns": 3, "rows": 2}
    # The plan named, so the author knows which table, and both answers offered.
    assert "Task | TarViS | Specialist" in findings[0].message
    assert "ppt_layout.table(" in findings[0].message
    assert "ppt_outline again" in findings[0].message


def test_a_page_that_drew_the_table_it_planned_is_reported_for_nothing(deck: DeckBuilder) -> None:
    page = deck.page()
    deck.table(page, 3, 3, top=1.0, width=9.0, cell="48.3")

    assert planned_tables(deck.save(), _outline({1: _NARROW_PLAN})) == []


def test_a_page_the_plan_gave_no_table_is_measured_for_nothing(deck: DeckBuilder) -> None:
    """A table nobody planned is `wide_table`'s business; this reading has no plan."""
    page = deck.page()
    deck.table(page, 3, 4, top=1.0, width=2.4, cell="Professional services transformation")

    assert planned_tables(deck.save(), _outline({1: {}})) == []
    assert planned_tables(deck.save(), None) == []


def test_a_built_table_smaller_than_its_plan_names_what_is_missing(deck: DeckBuilder) -> None:
    """Columns both ways and rows only downwards, with the dropped heading named.

    Reported rather than refused: the plan-time width warning tells an author to drop
    the columns the claim does not rest on, so a refusal here would refuse the fix
    that warning asks for.
    """
    page = deck.page()
    frame = deck.table(page, 2, 2, top=1.0, width=9.0, cell="")
    frame.table.cell(0, 0).text = "Task"
    frame.table.cell(0, 1).text = "TarViS"

    findings = planned_tables(deck.save(), _outline({1: _NARROW_PLAN}))

    assert [finding.kind for finding in findings] == ["table_grid"]
    assert findings[0].severity is Severity.WARNING
    assert findings[0].detail == {
        "planned_columns": 3,
        "planned_rows": 3,
        "drawn_columns": 2,
        "drawn_rows": 2,
        "tables": 1,
    }
    assert "'Specialist'" in findings[0].message
    assert "1 planned row(s) are on no page" in findings[0].message


def test_a_table_with_more_rows_than_its_plan_is_ordinary(deck: DeckBuilder) -> None:
    """`group_rows` turns a row into a band and a total row is drawn under a rule --
    both are rows `ppt_layout.table` adds that no plan names."""
    page = deck.page()
    frame = deck.table(page, 5, 3, top=1.0, width=9.0, cell="")
    for index, head in enumerate(_NARROW_PLAN["columns"]):
        frame.table.cell(0, index).text = head

    assert planned_tables(deck.save(), _outline({1: _NARROW_PLAN})) == []


def test_the_plan_time_width_warning_is_checked_against_the_table_that_was_drawn(deck: DeckBuilder) -> None:
    """The forecast, and the file that settles it, in one finding.

    `ppt_outline` said this page's plan wanted more width than any page carries; the
    answer it got was an opinion about the renderer. Here the drawn table is beside
    it, and the opinion has nothing left to stand on.
    """
    page = deck.page()
    deck.table(page, 3, 5, top=1.0, width=2.4, cell="Professional services transformation")

    findings = planned_tables(deck.save(), _outline({1: _WIDE_PLAN}))

    assert [finding.kind for finding in findings] == ["table_room"]
    assert findings[0].severity is Severity.WARNING
    assert findings[0].detail["width_in"] > findings[0].detail["width_room_in"]
    assert findings[0].detail["page_room_in"] == round(13.333 - 2 * SAFE_MARGIN_IN, 2)
    assert "columns are narrower than what they hold" in findings[0].message
    # Measured at the floor, so the one answer the plan-time reading already refused
    # is refused again rather than being left open.
    assert "wrapping the cells is not one of the answers" in findings[0].message


def test_a_forecast_the_built_table_does_not_bear_out_is_not_reported(deck: DeckBuilder) -> None:
    """Half the finding is the file. A plan measured too wide whose page then drew a
    table that fits -- shorter cells, fewer of them, a second page -- has been
    answered, and repeating the forecast over a page that solved it is the noise this
    reading exists to replace."""
    page = deck.page()
    deck.table(page, 3, 5, top=1.0, width=11.0, cell="4.9")

    assert planned_tables(deck.save(), _outline({1: _WIDE_PLAN})) == []


def test_a_table_running_past_the_margin_bears_the_forecast_out_too(deck: DeckBuilder) -> None:
    """The other way a page carries the forecast into the file: columns wide enough
    for their own cells, on a table that simply does not fit between the margins."""
    page = deck.page()
    deck.table(page, 3, 5, top=1.0, left=1.0, width=12.0, cell="4.9")

    findings = planned_tables(deck.save(), _outline({1: _WIDE_PLAN}))

    assert [finding.kind for finding in findings] == ["table_room"]
    assert "past the page's right margin" in findings[0].message


def test_the_page_a_plan_is_held_against_is_the_grids_own_margin() -> None:
    """The one number of the grid restated here, held against `ppt_layout`'s source.

    The canvas is not restated: it comes off the built file, so a deck on a 4:3
    template is measured against the page it really has.
    """
    from raven.ppt.services.assets.layout import layout_module_source

    assert "\nMARGIN = 0.72\n" in layout_module_source()
    assert SAFE_MARGIN_IN == 0.72


def _projected(directory: Path, filename: str):
    """A generated helper module as a build script imports it.

    `ppt_layout` and `ppt_theme` are source strings projected into the build directory
    rather than importable packages, so a test that wants the deck's own table helper
    -- or the plain-dict themes it takes -- has to install them the way the script
    backend does.
    """
    from types import SimpleNamespace

    from raven.ppt.services.assets.script_helpers import script_helper_files

    for name, text in script_helper_files().items():
        (directory / name).write_text(text, encoding="utf-8")
    path = directory / filename
    namespace: dict = {"__file__": str(path), "__name__": path.stem}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)  # noqa: S102
    return SimpleNamespace(**namespace)


def test_a_table_wearing_the_office_default_is_reported_for_design_rebuild(deck: DeckBuilder) -> None:
    """A bare `add_table`, which python-pptx stamps with banding and the gallery style."""
    page = deck.page()
    deck.table(page, 4, 3, top=1.0)

    findings = native_tables(deck.save())

    assert [finding.kind for finding in findings] == ["native_table"]
    assert findings[0].severity is Severity.WARNING
    assert findings[0].detail["tables"] == 1
    assert findings[0].detail["wearing"] == ["banded rows", "the Office gallery style"]


def test_the_decks_own_table_helper_is_not_reported(deck: DeckBuilder, tmp_path: Path) -> None:
    """The gate used to fire on `has_table`, so `ppt_layout.table()` reported itself.

    It is built on `add_table` -- nothing else puts a table in a .pptx -- and it takes
    the banding and the gallery style off, which is the whole difference a reader sees.
    A gate that fires on the best answer available teaches the author to avoid it.
    """
    layout = _projected(tmp_path, "ppt_layout.py")
    theme = _projected(tmp_path, "ppt_theme.py").THEMES["warm-paper"]
    rows = [
        ["Benchmark", "TarViS", "Prior", "Delta"],
        ["YouTube-VIS 2021", "51.2", "46.3", "+4.9"],
        ["OVIS", "31.1", "27.4", "+3.7"],
    ]
    layout.table(deck.page(), layout.Box(0.72, 1.24, 12.60, 4.40), rows, theme)

    assert native_tables(deck.save()) == []


def test_every_style_the_table_helper_offers_stays_unreported(deck: DeckBuilder, tmp_path: Path) -> None:
    """`header_tint` and `row_rules` add fills and rules of the deck's own, not Office's."""
    layout = _projected(tmp_path, "ppt_layout.py")
    theme = _projected(tmp_path, "ppt_theme.py").THEMES["ink-graphite"]
    for style in ("minimal", "header_tint", "row_rules", "compact"):
        layout.table(
            deck.page(),
            layout.Box(0.72, 1.24, 12.60, 4.40),
            [["Head", "Value"], ["Row", "1"]],
            theme,
            style=style,
        )

    assert native_tables(deck.save()) == []


def test_a_template_s_own_table_style_is_left_alone(deck: DeckBuilder) -> None:
    """The gallery style is recognised by value, not by presence.

    A table cloned out of a user's template carries the style its designer chose, and
    that one is the house style -- the same rule `ppt_layout._drop_gallery_style`
    follows when it takes python-pptx's own off.
    """
    from raven.ppt.services.measure.content import _DRAWINGML

    page = deck.page()
    shape = deck.table(page, 3, 3, top=1.0)
    shape.table.horz_banding = False
    shape.table.vert_banding = False
    for named in shape.table._tbl.iter(f"{{{_DRAWINGML}}}tableStyleId"):  # noqa: SLF001 -- no API
        named.text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"

    assert native_tables(deck.save()) == []


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
        for name in ("ppt_layout", "ppt_icons", "ppt_shapes", "ppt_theme"):
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
        for name in ("ppt_layout", "ppt_icons", "ppt_shapes", "ppt_theme"):
            sys.modules.pop(name, None)


def _paint(table, row: int, colour: str) -> None:
    """Fill every cell of one row, the way `banding` does."""
    from pptx.dml.color import RGBColor

    for cell in table.rows[row].cells:
        cell.fill.solid()
        cell.fill.fore_color.rgb = RGBColor.from_string(colour)


def test_a_table_tinting_alternate_rows_is_reported(deck: DeckBuilder) -> None:
    """The one table dial the deck's own style does not want.

    `table()` has banding off and says why; a run that read the first hundred lines of
    `tables.md` and none of the dials past them turned it on for all three of its
    tables, over a template already doing that work with its own palette.
    """
    page = deck.page()
    shape = deck.table(page, 5, 4, top=1.0, width=12.0, cell="Mem0")
    _paint(shape.table, 2, "E2CEA8")
    _paint(shape.table, 4, "E2CEA8")

    findings = banded_tables(deck.save())

    assert [finding.kind for finding in findings] == ["banded_table"]
    assert findings[0].detail["banded"] == [2, 4]
    assert findings[0].severity is Severity.WARNING


def test_a_band_crossing_a_filled_column_is_still_a_band(deck: DeckBuilder) -> None:
    """The case the first version of this missed on every table it was written for.

    A deck that filled its own column -- `fills={(None, 2): "ours"}`, which the dials
    invite -- gives every banded row two tones, and a reading that wanted one tone
    across the row found no band on three banded tables.
    """
    page = deck.page()
    shape = deck.table(page, 5, 4, top=1.0, width=12.0, cell="Mem0")
    from pptx.dml.color import RGBColor

    for row in range(5):
        cell = shape.table.rows[row].cells[2]
        cell.fill.solid()
        cell.fill.fore_color.rgb = RGBColor.from_string("E0C39D")
    _paint(shape.table, 2, "E2CEA8")
    _paint(shape.table, 4, "E2CEA8")
    for row in (2, 4):
        cell = shape.table.rows[row].cells[2]
        cell.fill.solid()
        cell.fill.fore_color.rgb = RGBColor.from_string("E0C39D")

    findings = banded_tables(deck.save())

    assert [finding.detail["banded"] for finding in findings] == [[2, 4]]


def test_one_tinted_row_is_emphasis_and_not_banding(deck: DeckBuilder) -> None:
    """`emphasize_rows` paints the row carrying the page's point. It does not alternate."""
    page = deck.page()
    shape = deck.table(page, 6, 4, top=1.0, width=12.0, cell="Mem0")
    _paint(shape.table, 2, "E2CEA8")

    assert banded_tables(deck.save()) == []


def test_a_short_table_is_not_read_for_a_pattern(deck: DeckBuilder) -> None:
    """Two body rows, one of them tinted, is not a pattern in either direction."""
    page = deck.page()
    shape = deck.table(page, 3, 4, top=1.0, width=12.0, cell="Mem0")
    _paint(shape.table, 2, "E2CEA8")

    assert banded_tables(deck.save()) == []
