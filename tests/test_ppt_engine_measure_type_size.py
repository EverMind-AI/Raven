"""The type census, and the two numbers a built page has to clear."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven_ppt.contracts.findings import Severity
from raven_ppt.services.measure.type_size import (
    BODY_FLOOR_PT,
    BODY_PT,
    LABEL_PT,
    MIN_FLOOR_PT,
    census,
    scale_findings,
    type_findings,
    type_floors,
)
from tests._ppt_engine_fixtures import (  # noqa: F401
    DeckBuilder,
    deck,
    image,
    noise_image,
    noise_png,
    product_page,
    template_file,
)

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


def test_the_floor_is_a_warning_and_not_a_refusal(deck: DeckBuilder) -> None:
    """Raising a size costs room, and the room has to come from somewhere.

    A warning rather than a refusal because the room comes from the copy: a gate
    that blocked publication until the floor was met could be answered by
    shrinking the copy back, which is the oscillation D2 describes.
    """
    deck.text(deck.page(), ("copy set too small to project" * 3, 11.0))

    finding = type_findings(deck.save())[0]

    assert finding.kind == "type_floor"
    assert finding.severity is Severity.WARNING
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
    from raven_ppt.services.measure.type_size import Span, type_findings

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
    from raven_ppt.services.measure.type_size import Span, type_findings

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


def _deck(
    tmp_path: Path,
    boxes: list[tuple[str, float, float, float, float]],
    *,
    name: str = "deck",
    size: float | None = None,
) -> Path:
    """One page of boxes at stated positions. `size` states the runs' own point size.

    Without it the boxes declare nothing, which is the state a template placeholder is
    in and what the census is silent about."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    for text, left, top, width, height in boxes:
        box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        if size is None:
            box.text_frame.text = text
            continue
        run = box.text_frame.paragraphs[0].add_run()
        run.text = text
        run.font.size = Pt(size)
    path = tmp_path / f"{name}.pptx"
    presentation.save(str(path))
    return path


# The band between the floor and the body size, which is where a hand-picked integer
# lands. Its own tests, because the claim is different from the floor's: not "a reader
# cannot read this" but "nobody chose this size".
COPY = "这一段是页面的正文，长度足够被当作正文而不是一个标记来判断"


def test_copy_between_the_floor_and_the_body_size_is_reported(deck: DeckBuilder) -> None:
    """15pt clears every floor there is and is still not a step of the ramp.

    The size 15 of 43 copy blocks across 34 generated decks came out at -- more than
    any other -- and the one `type_floor` is structurally unable to see, because the
    tier that applies to copy is 14pt and 15 clears it.
    """
    deck.text(deck.page(), (COPY, 15.0), top=2.0, height=1.0)

    findings = scale_findings(deck.save())

    assert [finding.kind for finding in findings] == ["type_scale"]
    assert findings[0].page == 1
    assert findings[0].detail["sizes_pt"] == [15.0]
    assert findings[0].detail["reaches_body_pt"] is False
    assert "not a step of the ramp" in findings[0].message
    assert "under BODY_PT (16pt)" in findings[0].message
    assert "`size=BODY_PT`" in findings[0].message


def test_the_scale_reports_and_never_refuses(deck: DeckBuilder) -> None:
    """Invariant 3. Raising a size costs room and the room comes from the copy, so a
    refusal here could be answered by cutting the page back -- the oscillation of
    design doc D2, which the floor above declines for the same reason."""
    deck.text(deck.page(), (COPY, 15.0), top=2.0, height=1.0)

    assert scale_findings(deck.save())[0].severity is Severity.WARNING


def test_a_step_of_the_ramp_is_not_a_finding(deck: DeckBuilder) -> None:
    """`BODY_PT` and the one step under it are the two sizes copy may be set at.

    14pt is in the ramp on purpose -- the comment beside it says body has room to drop
    one step and still clear the floor -- so a card body at 14pt is a decision and not
    a drift. Reporting it would have fired on 7 more of the 43 copy blocks measured.
    """
    deck.text(deck.page(), (COPY, 16.0), top=2.0, height=1.0)
    deck.text(deck.page(), (COPY, 14.0), top=2.0, height=1.0)
    deck.text(deck.page(), (COPY, 20.0), top=2.0, height=1.0)

    assert scale_findings(deck.save()) == []


def test_copy_under_the_floor_is_left_to_the_floor(deck: DeckBuilder) -> None:
    """Two findings on one box is a finding an author learns to skip.

    12pt copy is already reported, by name and with the same move -- bring it up. So
    this check starts at the floor and says nothing below it.
    """
    deck.text(deck.page(), (COPY, 12.0), top=2.0, height=1.0)
    built = deck.save()

    assert [finding.kind for finding in type_findings(built)] == ["type_floor"]
    assert scale_findings(built) == []


def test_a_short_label_off_the_ramp_is_not_copy(deck: DeckBuilder) -> None:
    """The 20-character line this file already draws for its floor tier, reused.

    A byline, a unit, a chart's axis: "公司内部技术评审" at 12pt is a label, and both
    models fought the floor over exactly that until the tier existed.
    """
    deck.text(deck.page(), ("公司内部技术评审", 15.0), top=2.0, height=0.4)

    assert scale_findings(deck.save()) == []


def test_a_source_line_and_a_footer_are_not_copy(deck: DeckBuilder) -> None:
    """A deck sets its credits smaller on purpose, which is why `_is_caption` exists."""
    page = deck.page()
    deck.text(page, ("来源：TarViS 原论文（CVPR 2023），Table 2", 15.0), top=2.0, height=0.4)
    deck.text(page, ("TarViS · CVPR 2023 · arXiv:2301.02657 · 第 4 页", 15.0), top=7.0, height=0.3)

    assert scale_findings(deck.save()) == []


def test_a_page_of_pictures_is_not_a_finding(deck: DeckBuilder) -> None:
    deck.page()

    assert scale_findings(deck.save()) == []


def test_the_templates_own_copy_is_not_the_authors_choice(tmp_path: Path) -> None:
    """A deck inside a template inherits sizes it cannot name the ramp for.

    The ten bundled templates set 335 of their 356 copy blocks under 16pt, 269 of them
    at 12pt, so a check held to our ramp with no notion of a clone would report every
    page of every templated deck -- and ask for a fix that means abandoning the
    template, which is the dead end invariant 6 forbids. A cloned page keeps its
    prototype's positions exactly, so position is the answer.

    Asserted in both directions: the same deck fires when nothing says the box came
    from a template, so it is the scope suppressing it and not the band.
    """
    template = _deck(tmp_path, [(COPY, 1.0, 2.0, 6.0, 1.0)], name="template", size=15.0)
    cloned = _deck(tmp_path, [(COPY, 1.0, 2.0, 6.0, 1.0)], name="built", size=15.0)

    assert scale_findings(cloned, template) == []
    assert [finding.kind for finding in scale_findings(cloned)] == ["type_scale"]


def test_a_box_the_author_added_inside_a_template_is_still_the_authors(tmp_path: Path) -> None:
    """The mix a real templated deck is: the template's frame, the author's body.

    Per shape rather than per page, because `adapt` clones a prototype and the author
    then composes inside it -- scoping by page would have excluded the copy they wrote.
    """
    template = _deck(tmp_path, [(COPY, 1.0, 2.0, 6.0, 1.0)], name="template", size=15.0)
    built = _deck(
        tmp_path,
        [(COPY, 1.0, 2.0, 6.0, 1.0), (COPY, 7.0, 2.0, 5.0, 1.0)],
        name="built",
        size=15.0,
    )

    findings = scale_findings(built, template)

    assert [finding.detail["sizes_pt"] for finding in findings] == [[15.0]]


def test_the_ramp_is_the_one_ppt_layout_hands_the_author(tmp_path: Path) -> None:
    """The two constants, pinned against the module the author actually imports.

    `ppt_layout` reaches the author as text -- `script_helpers` writes it beside the
    build script -- so the engine cannot import it and the sizes are stated twice. This
    is the only place the two copies can be held to each other.
    """
    import sys

    from raven_ppt.services.assets.script_helpers import script_helper_files

    for name, text in script_helper_files().items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        for stale in ("ppt_layout",):
            sys.modules.pop(stale, None)
        import ppt_layout
    finally:
        sys.path.remove(str(tmp_path))

    assert (BODY_PT, LABEL_PT) == (float(ppt_layout.BODY_PT), float(ppt_layout.LABEL_PT))
    # And that the band this check owns is the gap the ramp leaves: the only step
    # between the floor and the body size is `LABEL_PT` itself.
    assert [step for step in ppt_layout._RAMP if BODY_FLOOR_PT <= step < BODY_PT] == [LABEL_PT]


def test_it_fires_on_a_quarter_of_a_corpus_and_not_on_the_rest(tmp_path: Path) -> None:
    """The sweep, as one deck: nine pages shaped like the ones it was measured over.

    Run over the 45 generated pages in /tmp/ab it reported 11, and 10 of those 11 were
    pages `type_floor` said nothing about at all. This is that shape held still -- a
    check that fires everywhere reports nothing, and the pages below are the ones it
    has to stay quiet on.
    """
    pages: list[tuple[str, list[tuple[str, float, float, float, float]], float]] = [
        # Fires: the body copy one point under what the deck calls body.
        ("15pt body", [(COPY, 1.0, 2.0, 6.0, 1.0)], 15.0),
        # Fires: four card bodies at once, one finding for the page.
        ("four cards at 15pt", [(COPY, 1.0 + n * 3.0, 2.0, 2.8, 1.4) for n in range(4)], 15.0),
        # Quiet: the ramp's own two steps for copy.
        ("16pt body", [(COPY, 1.0, 2.0, 6.0, 1.0)], 16.0),
        ("14pt body", [(COPY, 1.0, 2.0, 6.0, 1.0)], 14.0),
        ("20pt lead", [(COPY, 1.0, 2.0, 6.0, 1.0)], 20.0),
        # Quiet: under the floor, which `type_floor` reports by name.
        ("13pt body", [(COPY, 1.0, 2.0, 6.0, 1.0)], 13.0),
        ("9pt labels", [("flowChartAlternateProcess", 1.0 + n, 2.0, 0.9, 0.3) for n in range(6)], 9.0),
        # Quiet: a chart's marks and a page's kicker, set small on purpose.
        ("chart marks", [(f"{n + 1}月", 1.0 + n * 0.4, 5.0, 0.35, 0.3) for n in range(12)], 12.0),
        # Quiet: the two things a deck sets smallest on purpose.
        ("a source line", [("来源：TarViS 原论文（CVPR 2023），Table 2", 1.0, 2.0, 6.0, 0.4)], 15.0),
    ]
    built = _multi(tmp_path / "corpus", [boxes for _, boxes, _ in pages], [size for *_, size in pages])

    findings = scale_findings(built)

    assert [finding.page for finding in findings] == [1, 2]
    assert [len(finding.detail["sizes_pt"]) for finding in findings] == [1, 4]
    named = [name for name, *_ in pages]
    assert [named[finding.page - 1] for finding in findings] == ["15pt body", "four cards at 15pt"]


def _multi(stem: Path, pages: list[list[tuple[str, float, float, float, float]]], sizes: list[float]) -> Path:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    for boxes, size in zip(pages, sizes):
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        for text, left, top, width, height in boxes:
            box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
            run = box.text_frame.paragraphs[0].add_run()
            run.text = text
            run.font.size = Pt(size)
    path = stem.with_suffix(".pptx")
    presentation.save(str(path))
    return path
