"""The order of build, measure, deliver -- and why it is that order."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from raven_ppt.backends.script import script_path
from raven_ppt.contracts import BuildOutcome, Finding, PageSource, Project, Severity
from raven_ppt.profiles import registry
from raven_ppt.stages.build import BuildStage

SCRIPT = textwrap.dedent(
    """
    import os
    from pptx import Presentation

    prs = Presentation()


    def new_slide():
        return prs.slides.add_slide(prs.slide_layouts[6])


    # SLIDE 1
    one = new_slide()

    # SLIDE 2
    two = new_slide()

    prs.save(os.environ["PPT_OUTPUT"])
    """
).lstrip()


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    p = Project(workspace=tmp_path, slug="tarvis")
    p.build_dir.mkdir(parents=True)
    p.state_dir.mkdir(parents=True)
    script_path(p).write_text(SCRIPT, encoding="utf-8")
    return p


def _outcome(project: Project, pages: int = 2, mapped: bool = True) -> BuildOutcome:
    deck = project.build_dir / "deck.pptx"
    deck.write_bytes(b"PK deck")
    lines = SCRIPT.splitlines(keepends=True)
    first = next(i for i, line in enumerate(lines) if "# SLIDE 1" in line)
    second = next(i for i, line in enumerate(lines) if "# SLIDE 2" in line)
    sources = (
        (
            PageSource(page=1, first_line=first, last_line=second),
            PageSource(page=2, first_line=second, last_line=len(lines) - 2),
        )
        if mapped
        else ()
    )
    return BuildOutcome(ok=True, pptx_path=deck, pages=pages, sources=sources, source_digest="x")


def _measure(findings):
    async def measure(_project: Project, _pptx: Path, _outcome=None) -> list[Finding]:
        return list(findings)

    return measure


def _stage(project: Project, *, findings=(), outcome=None, profile="script_author") -> BuildStage:
    measured = list(findings)

    async def backend(_project: Project, _script: str | None) -> BuildOutcome:
        return outcome or _outcome(_project)

    return BuildStage(
        backend=backend,
        measure=_measure(measured),
        profile=registry.get(profile),
    )


def _fact() -> Finding:
    return Finding(kind="fact", severity=Severity.BLOCKING, message="48.3 is not in the sources", page=11)


def _warning(kind: str = "type_floor") -> Finding:
    return Finding(kind=kind, severity=Severity.WARNING, message="11.5pt body", page=3)


@pytest.mark.asyncio
async def test_a_clean_deck_is_measured_and_delivered(project: Project) -> None:
    result = await _stage(project).run(project)

    assert result.ok, result.note
    assert Path(result.data["pptx_path"]).is_file()


@pytest.mark.asyncio
async def test_a_blocking_finding_refuses_the_export_whatever_it_is_about(project: Project) -> None:
    """Both kinds of refusal, because they used to be two different axes.

    A claim the materials do not make and a word painted over another word were once
    sorted into separate lists, and one waited on the other: a deck with 29 unreplaced
    placeholders also had 25 pairs of overlapping words, and nothing looked at the
    second list until the first cleared. There is one list now, and either entry on it
    stops the file being written.
    """
    collision = Finding(
        kind="word_collision",
        severity=Severity.BLOCKING,
        message="'31.1' is painted over 'VPS-only'",
        page=16,
    )
    for finding in (_fact(), collision):
        result = await _stage(project, findings=[finding]).run(project)

        assert not result.ok, finding.kind
        assert not (project.exports_dir / "deck.pptx").exists()


@pytest.mark.asyncio
async def test_a_warning_never_stops_the_deck(project: Project) -> None:
    """Shrinking the copy would satisfy the measurement and make the page worse."""
    result = await _stage(project, findings=[_warning()]).run(project)

    assert result.ok
    assert [f.kind for f in result.findings] == ["type_floor"]


@pytest.mark.asyncio
async def test_the_deck_is_measured_once(project: Project) -> None:
    """Measuring renders the whole deck, so a second pass over it costs minutes.

    It was measured twice, because a stage between the two rewrote the pages it had
    just looked at. Nothing rewrites them now, and the second render bought nothing.
    """
    seen: list[int] = []

    async def backend(_project: Project, _script: str | None) -> BuildOutcome:
        return _outcome(_project)

    async def measure(_p: Project, _pptx: Path, _outcome=None) -> list[Finding]:
        seen.append(1)
        return []

    stage = BuildStage(backend=backend, measure=measure, profile=registry.get("script_author"))
    await stage.run(project)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_pages_that_cannot_be_told_apart_are_blocking_and_carry_a_skeleton(project: Project) -> None:
    """Refused without one, an author rewrote the same shim three times and left."""
    looped = textwrap.dedent(
        """
        import os
        from pptx import Presentation
        prs = Presentation()
        for _ in range(5):
            prs.slides.add_slide(prs.slide_layouts[6])
        prs.save(os.environ["PPT_OUTPUT"])
        """
    ).lstrip()
    script_path(project).write_text(looped, encoding="utf-8")
    result = await _stage(project, outcome=_outcome(project, pages=5, mapped=False)).run(project)

    assert not result.ok
    unmapped = [f for f in result.findings if f.kind == "unmapped_page"]
    assert "# SLIDE 1" in unmapped[0].message


@pytest.mark.asyncio
async def test_a_mapped_deck_produces_no_mapping_finding(project: Project) -> None:
    result = await _stage(project).run(project)
    assert not any(f.kind == "unmapped_page" for f in result.findings)


@pytest.mark.asyncio
async def test_a_build_that_failed_stops_before_anything_else(project: Project) -> None:
    result = await _stage(project, outcome=BuildOutcome(ok=False, stderr="boom")).run(project)

    assert not result.ok
    assert result.data["outcome"].stderr == "boom"


@pytest.mark.asyncio
async def test_the_route_decides_which_warning_kinds_refuse_delivery(project: Project) -> None:
    """A page that traces to no block of its own code, so no render can be matched to it.

    The example was `band` until the gate was downgraded to a report and this test was
    one of the places still holding it fatal.
    """
    orphan = Finding(kind="unmapped_page", severity=Severity.WARNING, message="page 4 maps to no block", page=4)
    result = await _stage(project, findings=[orphan]).run(project)
    assert not result.ok

    other = Finding(kind="wrapped_label", severity=Severity.WARNING, message="a label wrapped", page=4)
    assert (await _stage(project, findings=[other]).run(project)).ok


@pytest.mark.asyncio
async def test_a_draft_is_measured_and_not_published(project: Project) -> None:
    """A half-written program is an accepted intermediate state: it is measured and
    handed back, and it is not held to a length nobody has finished writing to."""
    result = await _stage(project, findings=[_warning("page_budget")]).run(project, draft=True)

    assert result.ok and result.data["draft"] is True
    assert "pptx_path" not in result.data
    assert not (project.exports_dir / "deck.pptx").exists()
    assert [f.kind for f in result.findings] == [], "a draft is not held to the agreed length"


def _planned(project: Project, figures: dict[int, list[str]]) -> None:
    """An outline that gives these pages these figures."""
    import json

    from raven_ppt.contracts import outline_path

    outline_path(project).parent.mkdir(parents=True, exist_ok=True)
    outline_path(project).write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "page": n,
                        "claim": f"page {n}",
                        "carries": "prose",
                        "says": ["a", "b"],
                        "figures": figures.get(n, []),
                    }
                    for n in (1, 2)
                ]
            }
        ),
        encoding="utf-8",
    )


def _deck_with(path: Path, pictures: dict[int, bool]) -> None:
    """Two slides, each with a picture or without one."""
    from pptx import Presentation
    from pptx.util import Inches

    png = path.parent / "dot.png"
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6300010000050001-0d0a2db40000000049454e44ae426082".replace("-", "")
        )
    )
    prs = Presentation()
    for n in (1, 2):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        if pictures.get(n):
            slide.shapes.add_picture(str(png), Inches(1), Inches(1), Inches(1), Inches(1))
    prs.save(path)


def test_a_page_the_plan_gave_a_figure_and_the_build_left_bare_is_reported(project: Project) -> None:
    """Twenty pages were built carrying no picture while the plan named two."""
    from raven_ppt.stages.build import _unplaced_figure_findings

    deck = project.build_dir / "deck.pptx"
    _deck_with(deck, {1: False, 2: True})
    _planned(project, {1: ["mem0-logo-2afc7b9e29"], 2: ["arch-7979644ab7"]})
    found = _unplaced_figure_findings(project, BuildOutcome(ok=True, pptx_path=deck, pages=2, source_digest="x"))

    assert [f.page for f in found] == [1], "only the page built without one"
    assert "mem0-logo-2afc7b9e29" in found[0].message


def test_a_plan_that_promises_no_figure_is_not_asked_for_one(project: Project) -> None:
    from raven_ppt.stages.build import _unplaced_figure_findings

    deck = project.build_dir / "deck.pptx"
    _deck_with(deck, {1: False, 2: False})
    _planned(project, {})
    assert _unplaced_figure_findings(project, BuildOutcome(ok=True, pptx_path=deck, pages=2, source_digest="x")) == []


def _housed_deck(path: Path, content_pages: int, *, title_left: float = 0.72, wander: int = 0) -> None:
    """A cover and `content_pages` pages that put their title in the same box.

    `wander` moves that many of them somewhere else, which is how a deck that has not
    settled on a title row is built. The body copy sits below the title band and names
    its own size, so the record has both a row to report and a size to report.
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    cover = prs.slides.add_slide(prs.slide_layouts[6])
    cover.shapes.add_textbox(Inches(1), Inches(3), Inches(8), Inches(1)).text_frame.text = "A deck about deck houses"
    for number in range(content_pages):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        left = title_left if number >= wander else title_left + 1.5 + number * 0.3
        title = slide.shapes.add_textbox(Inches(left), Inches(0.4), Inches(8.5), Inches(0.6))
        run = title.text_frame.paragraphs[0].add_run()
        run.text = f"What page {number + 2} argues"
        run.font.size = Pt(24)
        run.font.bold = True
        run.font.name = "Arial"
        body = slide.shapes.add_textbox(Inches(title_left), Inches(3), Inches(8.5), Inches(3))
        line = body.text_frame.paragraphs[0].add_run()
        line.text = "A sentence long enough that nothing reads it as a divider or a label."
        line.font.size = Pt(18)
        line.font.name = "Arial"
    prs.save(path)


def _house_of(project: Project, path: Path, pages: int) -> str:
    from raven_ppt.stages.build import _deck_house

    return _deck_house(project, BuildOutcome(ok=True, pptx_path=path, pages=pages, source_digest="x"))


def test_a_row_only_half_the_pages_use_is_not_named_as_the_decks(project: Project) -> None:
    """Half is not most, and half is where the measurement was measurably wrong.

    Three of six pages sharing a box named a right-hand panel on a real deck, and four
    of nine did on the same one -- a tie broken by insertion order. The record says the
    pages have not settled rather than naming a box they did not settle on, and rather
    than saying nothing: a deck whose pages disagree about where the title goes is a
    deck to look at.
    """
    deck = project.build_dir / "deck.pptx"
    _housed_deck(deck, 6, wander=3)
    said = _house_of(project, deck, 7)

    assert "have not settled on one" in said, said
    assert "0.60in" not in said, "the box itself is not named when it is not the deck's"


def test_a_deck_whose_pages_agree_carries_what_they_agreed(project: Project) -> None:
    deck = project.build_dir / "deck.pptx"
    _housed_deck(deck, 9)
    said = _house_of(project, deck, 10)

    assert "the title row 9 of them agree on" in said, said
    assert "(0.72, 0.40) 8.50x0.60in" in said, said
    assert "24pt bold" in said, said
    assert "18pt on 9 pages" in said, said
    # A record, not a finding: nothing in it asks for anything or refuses anything.
    assert "Nothing here is a finding" in said


def test_pages_that_have_not_settled_on_a_title_row_are_not_told_they_have(project: Project) -> None:
    """Nine content pages of which three share a box: a plurality, and not the deck's.

    This is the shape the relation exists for. On a real deck, three of six pages
    sharing a box was a right-hand panel rather than the title row those pages actually
    use, and naming it would have sent the author to move the titles. Three of nine is
    the same plurality at a different length, which is why the test is a comparison and
    not a count.
    """
    deck = project.build_dir / "deck.pptx"
    _housed_deck(deck, 9, wander=6)
    said = _house_of(project, deck, 10)

    assert "3 of them put a title row in the same place" in said, said
    assert "have not settled on one" in said, said
    assert "agree on is" not in said, "a plurality is not an agreement"


def test_a_deck_built_in_a_template_reports_no_house_of_its_own(project: Project) -> None:
    """The house is the template's then, and `title_row` already reports the drift per
    page against it. A second, deck-level house would read as permission to keep it."""
    from raven_ppt.services.template import prepared_path

    deck = project.build_dir / "deck.pptx"
    _housed_deck(deck, 9)
    assert _house_of(project, deck, 10) != ""

    prepared_path(project).parent.mkdir(parents=True, exist_ok=True)
    _housed_deck(prepared_path(project), 2)
    assert _house_of(project, deck, 10) == ""


def test_the_record_travels_on_the_note_the_reply_already_carries(project: Project) -> None:
    """`ppt_build` reads `outcome.note` into the reply; nothing else the stage returns
    reaches the author as prose while the build succeeded."""
    from raven_ppt.stages.build import _with_deck_house

    deck = project.build_dir / "deck.pptx"
    _housed_deck(deck, 9)
    outcome = BuildOutcome(ok=True, pptx_path=deck, pages=10, note="the script held no program", source_digest="x")
    carried = _with_deck_house(project, outcome)

    assert carried.note.startswith("the script held no program\n"), "the harness's own note is not displaced"
    assert "this deck's own house" in carried.note
    assert carried.pptx_path == outcome.pptx_path and carried.pages == outcome.pages


def test_an_unreadable_deck_costs_the_record_and_not_the_build(project: Project) -> None:
    from raven_ppt.stages.build import _with_deck_house

    deck = project.build_dir / "deck.pptx"
    deck.write_bytes(b"not a deck")
    outcome = BuildOutcome(ok=True, pptx_path=deck, pages=10, source_digest="x")
    assert _with_deck_house(project, outcome) == outcome


@pytest.mark.asyncio
async def test_a_built_deck_hands_back_its_own_house(project: Project) -> None:
    """The whole way through `run`, because the record is only worth anything in a reply.

    `ppt_build` reads `outcome` out of the stage's data and its note into the payload,
    so the record has to be on the outcome the stage returns rather than beside it. Ten
    pages and three renders is also the deck that is refused for the seven nobody has
    looked at, which is the round the record matters in: the author builds again.
    """
    deck = project.build_dir / "deck.pptx"
    _housed_deck(deck, 9)
    outcome = BuildOutcome(
        ok=True,
        pptx_path=deck,
        pages=10,
        sources=tuple(PageSource(page=n, first_line=n, last_line=n + 1) for n in range(1, 11)),
        source_digest="x",
    )
    result = await _stage(project, outcome=outcome).run(project)

    assert [f.kind for f in result.findings] == ["unseen_page"]
    assert "this deck's own house" in result.data["outcome"].note


@pytest.mark.asyncio
async def test_a_draft_hands_back_its_house_too(project: Project) -> None:
    """A half-written program is exactly when knowing what the written half decided is
    worth something, so the record is taken before the draft returns."""
    deck = project.build_dir / "deck.pptx"
    _housed_deck(deck, 9)
    outcome = BuildOutcome(ok=True, pptx_path=deck, pages=10, source_digest="x")
    result = await _stage(project, outcome=outcome).run(project, draft=True)

    assert result.data["draft"] is True
    assert "this deck's own house" in result.data["outcome"].note


def _ten_pages(project: Project) -> BuildOutcome:
    deck = project.build_dir / "deck.pptx"
    deck.write_bytes(b"PK")
    return BuildOutcome(
        ok=True,
        pptx_path=deck,
        pages=10,
        sources=tuple(PageSource(page=n, first_line=n, last_line=n + 1) for n in range(1, 11)),
        source_digest="x",
    )


@pytest.mark.asyncio
async def test_a_draft_render_counts_as_a_page_seen(project: Project) -> None:
    """The record is about pages put in front of the author, and a draft render is that.

    Recording only on publication made the two halves disagree, and the disagreement
    is unwinnable rather than merely wasteful: a measured run walked its nineteen
    pages three times as drafts, was told on the first real build that eighteen had
    never been seen, and spent the rest of its budget walking them again one non-draft
    build at a time. It reasoned its way to the cause and said so -- "draft renders do
    not count towards the seen record" -- which is a fact about this code and not
    about the deck.
    """
    from raven_ppt.services import seen

    outcome = _ten_pages(project)
    stage = _stage(project, outcome=outcome)

    await stage.run(project, slides=[1, 2, 3], draft=True)

    recorded = set(json.loads(seen.seen_path(project).read_text())["pages"])
    assert recorded == {"1", "2", "3"}, "a draft render is a page seen"

    # And the deck built straight afterwards does not ask for them again.
    result = await stage.run(project, slides=[1, 2, 3])
    unseen = [f for f in result.findings if f.kind == "unseen_page"]
    assert unseen, "the seven nobody looked at still refuse"
    assert unseen[0].detail["pages"] == [4, 5, 6, 7, 8, 9, 10]


@pytest.mark.asyncio
async def test_a_draft_walk_leaves_nothing_for_the_real_build_to_refuse(project: Project) -> None:
    """The whole point, as a run actually does it: draft through the deck, then publish.

    Before this, the same walk left the record empty and the publishing build refused
    every page -- so the author had to repeat the entire walk with `draft` off.
    """
    outcome = _ten_pages(project)
    stage = _stage(project, outcome=outcome)

    for first in (1, 4, 7, 10):
        await stage.run(project, page_from=first, draft=True)

    result = await stage.run(project, page_from=1)

    assert [f.kind for f in result.findings if f.kind == "unseen_page"] == []


@pytest.mark.asyncio
async def test_a_render_that_did_not_happen_is_not_a_page_seen(project: Project) -> None:
    """The guard that survives the move. A page whose render failed was never put in
    front of anyone, and recording it would say the author saw code that drew nothing."""
    from raven_ppt.services import seen

    outcome = _ten_pages(project)
    stage = _stage(project, outcome=outcome, findings=[_warning("unrendered")])

    await stage.run(project, slides=[1, 2, 3], draft=True)

    assert not seen.seen_path(project).exists()
