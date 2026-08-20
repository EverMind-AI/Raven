"""One pass over a built deck, one list of findings, filtered by the caller.

The predecessor projected the same measurements three ways -- one function per
audience -- plus a fourth copy inlined in the polish tool, and which audience got
what was decided by which function a call site reached for. These tests pin the
replacement: every check states its own severity and audience, and the dispatch
table is asserted row by row against findings a real deck actually produces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from raven.ppt.contracts.brief import DeckBrief, PageBudget
from raven.ppt.contracts.build import BuildOutcome, PageSource
from raven.ppt.contracts.findings import Audience, Finding, Severity, blocking, warnings
from raven.ppt.contracts.outline import Outline, PagePlan
from raven.ppt.services.gates.citations import figure_labels, load_figure_catalog
from raven.ppt.services.gates.mapping import mapping_findings
from raven.ppt.services.gates.registry import (
    DISPATCH,
    DeckUnderReview,
    by_page,
    check_deck,
    checks,
    for_audience,
)
from raven.ppt.services.ingest.facts import build_source_index
from raven.ppt.services.measure.type_size import Span
from raven.ppt.services.measure.words import WordBox
from tests.ppt.conftest import DeckBuilder

pytest.importorskip("pptx")

# Everything the deck below says, so the fact gate has an index to check it
# against and only the one deliberate claim comes back unanchored.
MATERIAL = "Video panoptic segmentation results. Qualitative evidence in Figure 4 and Figure 5."

# The text a template writes into a slot it means the author to fill. A page still
# carrying it is a page whose own words were laid over the template's, not into them.
PLACEHOLDER = "\u5355\u51fb\u6b64\u5904\u6dfb\u52a0\u957f\u4e00\u70b9\u7684\u526f\u6807\u9898"


@dataclass(frozen=True)
class Sample:
    """A deck built to fail every check at once, and its ground truth."""

    deck: DeckUnderReview
    figures: Path


@pytest.fixture
def sample(tmp_path: Path, image) -> Sample:
    from pptx.util import Emu, Inches, Pt

    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "fig_two.png").write_bytes(image("fig_two.png", (40, 60, 200)).read_bytes())
    (tmp_path / "figures.json").write_text(
        json.dumps({"assets": {"fig_two": {"source_label": "Figure 5"}}}), encoding="utf-8"
    )

    builder = DeckBuilder(tmp_path)
    # The layout carries a panel down the right-hand side, so page 4's copy lands
    # on decoration the layout draws and the over-layout row fires.
    builder.layout_art()

    # 1: a full-width band with its title below it rather than on it.
    page = builder.page()
    builder.panel(page, left=0.0, top=0.3, width=13.333, height=0.9)
    builder.text(page, ("Results: Video Panoptic Segmentation", 28.0), top=1.5, width=9.0, height=0.5)

    # 2: a page carrying a talk's worth of copy, and a table nobody can read.
    page = builder.page()
    builder.text(page, ("word " * 251, 16.0), height=4.0)
    builder.table(page, 3, 9, top=5.0)

    # 3: a card for copy to escape, a hairline for it to be struck by, a shape
    # over the right edge, a label in a box too narrow to hold it, and a sentence
    # in a box too short for the lines it wraps to.
    page = builder.page()
    builder.text(
        page,
        ("a sentence with enough words in it to wrap several times over", 16.0),
        left=8.0,
        top=1.0,
        width=2.2,
        height=0.5,
        wrap=True,
    )
    builder.panel(page, left=1.0, top=1.0, width=4.0, height=2.0)
    rule = page.shapes.add_textbox(Inches(6.0), Emu(int(143.2 * 12700)), Inches(3.0), Pt(3.6))
    assert rule is not None
    builder.panel(page, left=13.0, top=4.0, width=1.0, height=1.0)
    builder.text(page, ("01", 18.0), left=1.0, top=6.5, width=0.3, height=0.4, wrap=True)
    # Cloned from the template and never replaced, so the placeholder and underlay
    # rows fire on this page. Page 4 stays unrelated, which is what the adherence
    # row needs.
    builder.text(page, (PLACEHOLDER, 18.0), left=1.0, top=4.5, width=6.0, height=0.5)
    # White on the template's orange, which is 2.5:1 -- legible, thin, and the template's
    # own choice on the agenda page every deck clones. Refusing a deck for it is what the
    # flat 3:1 floor did, so this is the row that warns.
    builder.text(page, ("白字压在模板的橙色上", 20.0), left=7.0, top=5.6, width=3.0, height=0.6, colour="FFFFFF")
    # Two groups stacked with no more air between them than inside them, which is what
    # `unseparated_blocks` reports.
    builder.text(page, ("组一的第一行", 16.0), left=8.0, top=1.0, width=4.0, height=0.6)
    builder.text(page, ("组二的开头", 16.0), left=8.0, top=1.7, width=4.0, height=0.4)
    # An escape that was meant to be a line break and came through as two characters,
    # which is what a JSON round trip does to a string on its way into a tool.
    builder.text(page, ("YTVIS：46.3 → 48.3\\nOVIS：29.8 → 31.1", 16.0), left=1.0, top=6.0, width=5.0, height=0.4)
    # An expression written with the prose helper: a relation, a grouping mark, and no
    # run raised or lowered, which is what `flat_formula` reports. Its subscripts are
    # flat and the box may break it after a comma.
    builder.text(page, ("Qin = concat(Qsem, Qinst, Qbg)", 16.0), left=1.0, top=6.6, width=3.6, height=0.4)
    # Two parallel claims in one box with nothing in front of either, which is what
    # `unmarked_points` reports -- they read as one paragraph with a line break.
    builder.text(
        page,
        ("分类不再走全连接头：类别被建模成网络的动态输入，语义表示只通过损失监督学到。", 16.0),
        ("同一套权重在推理时按需拼装查询集合即可热切换任务，无需任何任务特定微调。", 16.0),
        left=1.0,
        top=6.9,
        width=8.0,
        height=0.8,
    )
    # One slot repeated, and the renderer set the second copy smaller because the copy
    # in it is longer -- which is the state `type_drift` reports. Same declared size and
    # same box, so the two are copies of one slot rather than two different ones.
    builder.text(page, ("the slot, set as drawn", 18.0), left=1.0, top=5.4, width=2.0, height=0.5)
    builder.text(page, ("the same slot, shrunk to fit its copy", 18.0), left=4.0, top=5.4, width=2.0, height=0.5)

    # 4: copy set under the floor, a claim the materials never make, and a figure
    # cited as another figure.
    page = builder.page()
    builder.text(
        page,
        ("copy set too small to project on a screen", 11.0),
        ("the M5 chip", 11.0),
        ("benchmarked against RECIPE", 11.0),
        ("Qualitative evidence — Fig. 4", 11.0),
        height=2.0,
    )
    builder.picture(page, figures / "fig_two.png", top=3.0)
    # Type the reader cannot make out: #1A1A1A on the near-black ground the rendered page
    # below is painted with. A live deck set its title and four labels this way -- the
    # theme's `surface` reached for as if it were the ink -- and nothing measured it.
    builder.text(page, ("black on black", 20.0), left=7.0, top=1.0, width=4.0, height=0.6, colour="1A1A1A")
    # A title wider than the canvas in a box that does not wrap: it paints out of both
    # sides and off the page, and every other check passes it.
    builder.text(
        page,
        ("TarViS：把四类视频分割统一成一个模型的完整技术评审与工程建议与后续计划", 30.0),
        left=0.5,
        top=0.2,
        width=3.0,
        height=0.8,
        wrap=False,
    )
    # A figure nobody can see: two filled cards laid over it afterwards. Nothing on the
    # page collides and no word touches a word, which is why this needed its own check.
    builder.picture(page, figures / "fig_two.png", left=8.0, top=4.5, width=3.0, height=2.0)
    builder.panel(page, left=8.0, top=4.5, width=1.6, height=2.0)
    builder.panel(page, left=9.6, top=4.5, width=1.4, height=2.0)
    # Three more shapes, because the adherence check ignores pages under MIN_SHAPES --
    # a section divider matching a prototype by chance is not adherence. With these,
    # page 4 is a page built at coordinates no prototype uses, which is what that row
    # reports.
    for offset in range(3):
        builder.panel(page, left=2.0 + offset, top=6.2, width=0.6, height=0.5)

    # What the renderer set each box at, in points with the origin top left -- the same
    # system the .pptx uses, which is how a span is matched to the box it came out of.
    spans = [
        Span(page=3, size_pt=18.0, text="the slot, set as drawn", x0=75, y0=392, x1=200, y1=410),
        Span(page=3, size_pt=11.0, text="the same slot, shrunk to fit its copy", x0=291, y0=392, x1=420, y1=410),
    ]
    words = [
        # Flush against the bottom rim of the card the builder drew on page 3: inside it,
        # touching it, which is the state `crowded_panel` reports.
        WordBox(page=3, text="flush", x0=80, y0=196, x1=150, y1=214),
        # The two stacked groups, as the renderer set them: 22pt between the first
        # block's own lines and 26pt to the second block's first line.
        WordBox(page=3, text="组一的第一行", x0=580, y0=74, x1=700, y1=92),
        WordBox(page=3, text="换行继续", x0=580, y0=96, x1=700, y1=114),
        WordBox(page=3, text="组二的开头", x0=580, y0=122, x1=700, y1=140),
        # And a label the render broke one character short of fitting, in the box the
        # builder put at (1.0, 4.5) 6.0in wide on the same page.
        WordBox(page=3, text="单击此处添加长一点的副标", x0=76, y0=328, x1=500, y1=346),
        WordBox(page=3, text="题", x0=76, y0=350, x1=94, y1=368),
        WordBox(page=3, text="Source:", x0=400, y0=100, x1=460, y1=115),
        WordBox(page=3, text="OVIS", x0=405, y0=102, x1=450, y1=116),
        WordBox(page=3, text="identical", x0=100, y0=210, x1=180, y1=226),
        WordBox(page=3, text="Clip-PanoFCN", x0=500, y0=138, x1=580, y1=152),
    ]
    return Sample(
        deck=DeckUnderReview(
            pptx_path=builder.save(),
            outcome=BuildOutcome(ok=True, pages=4, sources=()),
            source_index=build_source_index(MATERIAL),
            figure_labels=figure_labels(figures, load_figure_catalog(tmp_path / "figures.json")),
            words=words,
            type_spans=spans,
            # A brief this deck also fails: it agreed to be a one-page deck in
            # Chinese, so the page budget and the language rows fire too.
            brief=DeckBrief(language="中文", audience="an internal review", pages=PageBudget(1, 1)),
            # And a template it was not built in, so the house-style row fires. Its
            # theme is the only thing compared, because that is what a template
            # cannot lose and a default cannot fake.
            template=_recoloured(tmp_path / "house.pptx"),
            # The same file as the prototype source: it ships pages, this deck's
            # pages match none of them, so the adherence row fires too. In production
            # the two differ -- `template` is the prepared copy with the example
            # pages removed, and prototypes is what the user handed over.
            prototypes=_shipping_pages(tmp_path / "prototypes.pptx", figures / "fig_two.png"),
            # And the plan this deck said it would follow. Page 4 names the template's
            # only page and was built nowhere near it, which is the state the
            # prototype-kept row reports: a promise made in the outline and not read
            # back off the file. Two live decks missed 3 of 12 and 11 of 12 pages.
            # A rendered page for the contrast check, painted the ground the deck sits on.
            # Handed over rather than rendered so the check runs without LibreOffice.
            rendered_pages=[
                _orange_page(tmp_path / "page-003.png"),
                _black_page(tmp_path / "page-004.png"),
            ],
            outline=Outline(
                takeaway="one model, four tasks",
                pages=(
                    PagePlan(page=1, claim="cover"),
                    PagePlan(page=2, claim="a talk's worth of copy"),
                    PagePlan(page=3, claim="a card, a rule and a label"),
                    PagePlan(page=4, claim="results", prototype=1),
                ),
            ),
        ),
        figures=figures,
    )


def _orange_page(path: Path) -> Path:
    """A rendered page with the template's accent where its numbered bubbles sit."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (960, 540), (10, 10, 10))
    ImageDraw.Draw(image).rectangle([500, 400, 730, 450], fill=(0xFF, 0x84, 0x00))
    image.save(path)
    return path


def _black_page(path: Path) -> Path:
    """One rendered page, the near-black a dark template paints its ground with."""
    from PIL import Image

    Image.new("RGB", (960, 540), (10, 10, 10)).save(path)
    return path


def _shipping_pages(path: Path, photo: Path) -> Path:
    """A template that ships one designed page, at coordinates this deck never uses.

    The adherence row needs a prototype to compare against, and an empty default
    presentation ships none -- which is a real state (a template whose design is all
    on its layouts) and one the check correctly says nothing about.
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    for index in range(5):
        page.shapes.add_textbox(Inches(11.1 + index * 0.03), Inches(6.9), Inches(0.21), Inches(0.17))
    # And the placeholder copy every real template ships in its slots.
    box = page.shapes.add_textbox(Inches(1.0), Inches(4.5), Inches(6.0), Inches(0.5))
    box.text_frame.text = PLACEHOLDER
    # And a photograph the deck then kept: 21% of a real template's images are this
    # size, the band a stock photo sits in, and keeping one is what the
    # template-picture row reports.
    page.shapes.add_picture(str(photo), Inches(1.0), Inches(2.0), width=Inches(4))
    # Two content pages agreeing on where the title goes, which is what makes a title row
    # a house rule rather than one page's idea -- and what the deck's own pages are then
    # held to.
    for _ in range(2):
        content = presentation.slides.add_slide(presentation.slide_layouts[6])
        heading = content.shapes.add_textbox(Inches(0.72), Inches(0.14), Inches(11.88), Inches(0.98))
        run = heading.text_frame.paragraphs[0].add_run()
        run.text = "单击此处添加页面标题"
        run.font.size = Pt(28)
        for index in range(4):
            body = content.shapes.add_textbox(Inches(0.72 + index * 3.0), Inches(2.4), Inches(2.7), Inches(1.2))
            body.text_frame.text = "单击此处添加文本，这一段足够长以算作正文而不是标记"
    presentation.save(str(path))
    return path


def _recoloured(path: Path) -> Path:
    """A template whose accent is not Office's, so a deck built without it differs.

    Rewritten inside the package rather than through python-pptx, which has no API
    for the theme -- which is also why the check reads it off the XML.
    """
    import zipfile

    from pptx import Presentation

    plain = path.with_name("plain.pptx")
    Presentation().save(str(plain))
    with zipfile.ZipFile(plain) as source, zipfile.ZipFile(path, "w") as target:
        for entry in source.infolist():
            body = source.read(entry.filename)
            if entry.filename == "ppt/theme/theme1.xml":
                body = body.replace(b'<a:accent1><a:srgbClr val="4F81BD"/>', b'<a:accent1><a:srgbClr val="BADA55"/>')
            target.writestr(entry, body)
    return path


def test_the_registry_and_the_dispatch_table_describe_the_same_checks() -> None:
    """A check with no row, or a row with no check, is a gap either way."""
    assert set(checks()) == set(DISPATCH)


def test_every_check_produces_the_severity_and_audience_it_declares(sample: Sample) -> None:
    """The dispatch table, asserted row by row, against two decks.

    This is the test that makes the table a specification rather than a comment:
    a check that quietly starts refusing a deck fails here instead of stalling a
    run.

    Two decks and not one, because four of the rows fire on exactly the opposite
    condition from the rest. The loaded deck trips every check there is; the bare
    one has no index, no labels, no brief and no render, so what fires there is
    the four that report which checks could not run. Between them they have to
    cover the table.
    """
    loaded = check_deck(sample.deck)
    bare = check_deck(DeckUnderReview(pptx_path=sample.deck.pptx_path))

    assert {finding.kind for finding in (*loaded, *bare)} == set(DISPATCH), "a row nothing produced"
    for finding in (*loaded, *bare):
        assert (finding.severity, finding.audience) == DISPATCH[finding.kind], finding.kind


def test_a_deck_with_nothing_behind_it_says_which_checks_did_not_run(sample: Sample) -> None:
    """The reply for a deck built with no sources used to be indistinguishable
    from the reply for one whose every number checked out. A run had exactly that
    shape: a directory was passed where a file was meant, the read raised, the
    raise was caught, and a deck with an invented number published clean."""
    findings = check_deck(DeckUnderReview(pptx_path=sample.deck.pptx_path))

    kinds = {"unchecked_facts", "unchecked_citations", "unchecked_agreement", "unrendered"}
    assert kinds <= {finding.kind for finding in findings}
    # An absent input is not a defect in the deck, so none of the four refuses
    # one -- that would be the refusal the checks they stand in for correctly
    # decline to invent.
    assert kinds & {finding.kind for finding in blocking(findings)} == set()
    said = " ".join(finding.message for finding in findings if finding.kind in kinds)
    assert "the author's word for it" in said
    assert "ppt_ingest" in said


def test_only_provenance_comprehension_and_what_was_agreed_refuse_a_deck(sample: Sample) -> None:
    """Design doc D3's blocking set, and nothing has crept into it.

    Twelve kinds, in four groups. Provenance: a claim the materials do not make.
    Comprehension: a deck the pipeline cannot tell the pages of. What was agreed
    with the user -- its length, its language, and the template it was to be built
    inside, which now includes two ways of not building inside it: pages still
    carrying the template's own placeholder copy, pages that cloned a template
    page for its background and laid new text boxes over it, and pages that did not come
    from the prototype their own outline named -- a promise the outline gate already
    enforces and that nothing read back off the finished file until now. Both are refused for the
    same reason as `house_style`: the user handed over a template, and a deck that
    ships "click here to add a title" is not the deck they asked for. Neither is
    answerable by the design pass, which may not rewrite a word. And one measurement:
    copy painted over copy in the render, and type a reader cannot make out
    against the ground it landed on -- #1A1A1A on #000000 is not a matter of degree.

    That last one is the deliberate exception to D2, added after three live runs
    delivered decks with overlapping text: the rest of the measured layout problems
    are satisfiable by shrinking the copy, so refusing on them could be answered by
    making the page worse -- but the type floor is measured too, so shrinking out of
    a collision only trades one finding for another, and the move a collision
    actually wants is a wider box, which costs the page nothing.
    """
    findings = check_deck(sample.deck)

    assert {finding.kind for finding in blocking(findings)} == {
        "fact",
        "citation",
        "band",
        "page_mapping",
        "page_budget",
        "language",
        "house_style",
        "placeholder_copy",
        "prototype_kept",
        "template_underlay",
        "unreadable",
        "word_collision",
        "covered_shape",
        "literal_escape",
    }
    assert {finding.kind for finding in warnings(findings)} == {
        "template_picture",
        "density",
        "evidence",
        "wide_table",
        "flat_formula",
        "unmarked_points",
        "type_floor",
        "type_drift",
        "clipped_copy",
        "thin_contrast",
        "rule_strike",
        "card_overflow",
        "crowded_panel",
        "orphan_line",
        "unseparated_blocks",
        "title_row",
        "wrapped_label",
        "overset_copy",
        "unfamiliar_name",
        "off_page",
        "spilled_copy",
        "over_layout_art",
        "template_adherence",
        # This sample has no render, so the four render-truth checks did not run
        # and the coverage check says so. The other three coverage kinds stay
        # quiet because the sample has an index, labels and a brief.
        "unrendered",
    }


def test_content_problems_reach_the_author_and_never_the_design_pass(sample: Sample) -> None:
    """The pass may rearrange a page and never rewrite it, so a page's density,
    its lack of evidence and its table width are the author's."""
    findings = check_deck(sample.deck)
    designer = {finding.kind for finding in for_audience(findings, Audience.DESIGNER)}

    assert {"density", "evidence", "wide_table"} & designer == set()
    assert {"density", "evidence", "wide_table", "fact", "citation", "page_mapping"} <= {
        finding.kind for finding in for_audience(findings, Audience.AUTHOR)
    }


def test_a_caller_can_ask_for_one_check_alone(sample: Sample) -> None:
    assert {finding.kind for finding in check_deck(sample.deck, only=["band"])} == {"band"}


def test_page_numbered_findings_group_by_page_and_deck_wide_ones_do_not(sample: Sample) -> None:
    findings = check_deck(sample.deck)
    grouped = by_page(findings)

    assert set(grouped) == {1, 2, 3, 4}
    assert all(finding.page is not None for page in grouped.values() for finding in page)
    assert "evidence" not in {finding.kind for page in grouped.values() for finding in page}


def test_a_check_that_raises_does_not_silence_the_others(sample: Sample, monkeypatch) -> None:
    """The predecessor wrapped three checks in one `try`, so a crash in the first
    meant the other two reported a clean page."""
    from raven.ppt.services.gates import registry

    def exploding() -> dict:
        table = dict(original())
        table["band"] = lambda _deck: (_ for _ in ()).throw(RuntimeError("boom"))
        return table

    original = registry.checks
    monkeypatch.setattr(registry, "checks", exploding)
    seen: list[tuple[str, str]] = []

    findings = check_deck(sample.deck, on_error=lambda name, exc: seen.append((name, str(exc))))

    assert seen == [("band", "boom")]
    assert "band" not in {finding.kind for finding in findings}
    assert len(findings) > 5


def test_without_a_render_the_rendered_checks_report_nothing(sample: Sample) -> None:
    """No PDF and no pdftotext are the same case: no signal, not a clean page."""
    findings = check_deck(DeckUnderReview(pptx_path=sample.deck.pptx_path))
    kinds = {finding.kind for finding in findings}

    assert kinds & {"word_collision", "rule_strike", "card_overflow"} == set()
    assert "band" in kinds  # the declared-geometry checks still run


def test_without_an_index_or_a_catalogue_the_provenance_gates_stay_quiet(sample: Sample) -> None:
    """Nothing ingested means nothing to check against, and a gate may only
    refuse on evidence."""
    findings = check_deck(DeckUnderReview(pptx_path=sample.deck.pptx_path))

    assert {finding.kind for finding in findings} & {"fact", "citation"} == set()


def test_the_render_is_read_once_for_the_three_checks_that_need_it(sample: Sample, monkeypatch) -> None:
    """The predecessor ran pdftotext separately for each, so every build
    extracted its whole deck three times."""
    from raven.ppt.services.gates import registry

    calls = []
    monkeypatch.setattr(registry, "words_from_pdf", lambda path: calls.append(path) or list(sample.deck.words or []))
    deck = DeckUnderReview(
        pptx_path=sample.deck.pptx_path,
        pdf_path=sample.deck.pptx_path.with_suffix(".pdf"),
    )

    findings = check_deck(deck, only=["word_collision", "rule_strike", "card_overflow"])

    assert len(calls) == 1
    assert {finding.kind for finding in findings} == {"word_collision", "rule_strike", "card_overflow"}


# --- the page-to-code mapping gate ------------------------------------------


def _outcome(pages: int, sources: tuple[PageSource, ...] = (), ok: bool = True) -> BuildOutcome:
    return BuildOutcome(ok=ok, pages=pages, sources=sources)


def test_a_deck_whose_pages_cannot_be_told_apart_is_refused() -> None:
    """Not because anything is wrong with it, but because nothing after this
    point can act on it -- and a deck that silently skips the design pass ships
    with every defect the pass exists to find."""
    findings = mapping_findings(_outcome(4))

    assert len(findings) == 1
    assert findings[0].kind == "page_mapping"
    assert findings[0].severity is Severity.BLOCKING
    assert findings[0].audience is Audience.AUTHOR
    assert findings[0].detail == {"pages": 4, "mapped": 0, "distinct_starts": 0}


def test_pages_drawn_from_one_line_are_refused() -> None:
    """A loop that draws every page from one call site maps them all together."""
    sources = tuple(PageSource(page=page, first_line=10, last_line=20) for page in (1, 2, 3))

    assert mapping_findings(_outcome(3, sources))


def test_a_complete_mapping_passes() -> None:
    sources = tuple(PageSource(page=page, first_line=10 * page, last_line=10 * page + 5) for page in (1, 2, 3))

    assert mapping_findings(_outcome(3, sources)) == []


def test_a_partial_mapping_is_refused() -> None:
    sources = (PageSource(page=1, first_line=10, last_line=20),)

    assert mapping_findings(_outcome(3, sources))


def test_a_single_page_deck_has_nothing_to_tell_apart() -> None:
    assert mapping_findings(_outcome(1)) == []


def test_a_build_that_produced_nothing_is_not_this_gates_problem() -> None:
    assert mapping_findings(_outcome(4, ok=False)) == []
    assert mapping_findings(None) == []


def test_findings_carry_a_message_a_model_can_act_on(sample: Sample) -> None:
    """Every gate has to leave the author a next step; a refusal with no move in
    it sends them out of the tool, where no gate runs at all."""
    for finding in check_deck(sample.deck):
        assert isinstance(finding, Finding)
        assert len(finding.message) > 20
