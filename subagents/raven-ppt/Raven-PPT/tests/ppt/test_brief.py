"""What the deck is for, and the two checks that make agreeing it worth doing.

The point of this stage is not that the model asks a polite question. It is that
the answer binds: the page budget is checked against the built deck and the
language against what the pages say. A confirmed decision that binds nothing is
worse than never asking, because the user believes they decided something.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.ppt.contracts import DeckBrief, PageBudget, Severity, brief_path, load_brief, write_brief
from raven.ppt.contracts.project import Project
from raven.ppt.services.gates.brief import language_findings, material_findings, page_budget_findings
from raven.ppt.tools.brief import PptBriefTool

pytest.importorskip("pptx")


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    return Project(workspace=tmp_path, slug="tarvis")


def _brief(**kw) -> DeckBrief:
    base = {"language": "English", "audience": "a conference oral", "pages": PageBudget(16, 20)}
    return DeckBrief(**{**base, **kw})


# --- the contract ---------------------------------------------------------


def test_a_page_budget_is_a_range_and_knows_what_it_holds() -> None:
    budget = PageBudget(16, 20)
    assert budget.holds(16) and budget.holds(20) and not budget.holds(15) and not budget.holds(21)
    assert str(budget) == "16-20"
    assert str(PageBudget(18, 18)) == "18"


def test_a_backwards_or_empty_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="backwards"):
        PageBudget(20, 16)
    with pytest.raises(ValueError, match="at least one page"):
        PageBudget(0, 5)


def test_a_brief_needs_a_language_and_an_audience() -> None:
    for missing in ({"language": "  "}, {"audience": ""}):
        with pytest.raises(ValueError):
            _brief(**missing)


def test_a_brief_round_trips_through_disk(project: Project) -> None:
    brief = _brief(language="中文", notes=("keep the RWTH branding",))
    write_brief(brief, brief_path(project))
    assert load_brief(brief_path(project)) == brief


def test_no_brief_on_disk_reads_as_none_rather_than_a_default(project: Project) -> None:
    """A default would be a budget nobody agreed to, failed against silently."""
    assert load_brief(brief_path(project)) is None
    brief_path(project).parent.mkdir(parents=True, exist_ok=True)
    brief_path(project).write_text("not json", encoding="utf-8")
    assert load_brief(brief_path(project)) is None


def test_the_summary_reads_as_one_line_a_prompt_can_carry() -> None:
    said = _brief(language="中文", audience="内部技术评审", notes=("避免缩写",)).summary()
    assert "内部技术评审" in said and "中文" in said and "16-20" in said and "避免缩写" in said


# --- the tool -------------------------------------------------------------


async def _record(project: Project, **kw) -> dict:
    args = {
        "project": "tarvis",
        "language": "English",
        "audience": "a conference oral",
        "pages_low": 16,
        "pages_high": 20,
    }
    return json.loads(await PptBriefTool(project.workspace).execute(**{**args, **kw}))


@pytest.mark.asyncio
async def test_recording_a_brief_persists_it_and_says_what_binds(project: Project) -> None:
    body = await _record(project)

    assert body["ok"] is True
    assert body["brief"]["pages"] == {"low": 16, "high": 20}
    assert "checked against the finished file" in body["next_step"]
    assert load_brief(brief_path(project)) is not None


@pytest.mark.asyncio
async def test_an_impossible_budget_is_refused_with_the_reason(project: Project) -> None:
    body = await _record(project, pages_low=20, pages_high=16)
    assert body["ok"] is False and "backwards" in body["error"]
    assert load_brief(brief_path(project)) is None


@pytest.mark.asyncio
async def test_a_bad_project_name_is_refused_before_anything_is_written(project: Project) -> None:
    reply = await PptBriefTool(project.workspace).execute(
        project="../etc", language="English", audience="a review", pages_low=1, pages_high=2
    )
    body = json.loads(reply)
    assert body["ok"] is False and "usable project name" in body["error"]


@pytest.mark.asyncio
async def test_blank_notes_are_dropped_rather_than_recorded(project: Project) -> None:
    body = await _record(project, notes=["  ", "no acronyms"])
    assert body["brief"]["notes"] == ["no acronyms"]


# --- the page budget check ------------------------------------------------


def test_a_deck_the_agreed_length_produces_nothing() -> None:
    assert page_budget_findings(18, _brief()) == []


@pytest.mark.parametrize(("pages", "direction"), [(12, "more"), (24, "fewer")])
def test_a_deck_of_the_wrong_length_refuses_and_says_which_way(pages: int, direction: str) -> None:
    found = page_budget_findings(pages, _brief())
    assert len(found) == 1
    assert found[0].kind == "page_budget"
    assert direction in found[0].message
    # The one fix that must not be offered: this is not a text-fitting problem.
    assert "do not answer this by shrinking" in found[0].message


def test_with_no_brief_the_length_is_not_checked() -> None:
    assert page_budget_findings(3, None) == []


# --- the language check ---------------------------------------------------


def _deck(deck_builder, text: str, pages: int = 3) -> Path:
    for _ in range(pages):
        slide = deck_builder.page()
        deck_builder.text(slide, text, size=18)
    return deck_builder.save()


def test_a_chinese_brief_answered_with_an_english_deck_is_refused(deck) -> None:
    """Not a defect of degree: the audience cannot read it."""
    path = _deck(deck, "Unified target based video segmentation across four tasks and seven benchmarks. " * 3)
    found = language_findings(path, _brief(language="中文"))
    assert len(found) == 1 and found[0].kind == "language"
    assert "中文" in found[0].message


def test_a_chinese_brief_answered_in_chinese_passes(deck) -> None:
    path = _deck(deck, "统一的基于目标的视频分割方法，在四个任务与七个基准上联合训练，无需针对任务微调。" * 3)
    assert language_findings(path, _brief(language="中文")) == []


def test_an_english_deck_quoting_chinese_terms_is_not_refused(deck) -> None:
    """A wide threshold on purpose: quoting the other language is not the failure."""
    body = "Unified target based video segmentation across four tasks. " * 4 + "统一分割 "
    assert language_findings(_deck(deck, body), _brief(language="English")) == []


def test_a_deck_with_too_little_text_to_judge_is_left_alone(deck) -> None:
    assert language_findings(_deck(deck, "TarViS", pages=1), _brief(language="中文")) == []


def test_with_no_brief_the_language_is_not_checked(deck) -> None:
    assert language_findings(_deck(deck, "anything at all " * 30), None) == []


def test_material_that_carries_the_pages_agreed_says_nothing() -> None:
    """run5's numbers: 793 characters at 8 pages -- 99 per page -- and the one run of
    five that put no invented figure on a page."""
    brief = DeckBrief(language="中文", audience="内部评审", pages=PageBudget(low=8, high=8))
    assert material_findings(793, brief) == []


def test_material_thinner_than_the_pages_agreed_says_how_many_it_carries() -> None:
    """The same 793 characters at 12 pages, which is what put six invented numbers
    on a live deck's pages -- 4x and 1x deployment cost among them."""
    brief = DeckBrief(language="中文", audience="内部评审", pages=PageBudget(low=12, high=13))
    finding = material_findings(793, brief)[0]
    assert finding.kind == "thin_material"
    assert finding.severity is Severity.WARNING, "getting more material is a choice, not a refusal"
    assert finding.detail["carries"] == 9  # 793 // 80, and run5 built 8 good pages from it
    assert "web_search" in finding.message and "ppt_fetch" in finding.message


def test_a_paper_read_in_full_carries_a_deck() -> None:
    """run19 fetched the paper itself: 73989 characters, twelve pages, no complaint."""
    brief = DeckBrief(language="中文", audience="内部评审", pages=PageBudget(low=12, high=12))
    assert material_findings(73989, brief) == []


def test_no_index_yet_is_not_a_complaint() -> None:
    brief = DeckBrief(language="中文", audience="内部评审", pages=PageBudget(low=12, high=12))
    assert material_findings(None, brief) == []
    assert material_findings(0, brief) == []
    assert material_findings(793, None) == []
