"""What the model sees coming back from ppt_build.

Thin as the adapter is, this is where two measured failures were: twelve
unlabelled renders that the model could not pair with page numbers, and a reply
voicing one problem while two stood -- which cost a run seven rebuilds spent
re-checking numbers while seventeen colour bars went unmentioned.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from raven.agent.tools.base import ToolResult
from raven.ppt.contracts import (
    Audience,
    BuildOutcome,
    DeckBrief,
    Finding,
    IntakePlan,
    Outline,
    PageBudget,
    PagePlan,
    PageSource,
    Project,
    Severity,
    StageResult,
    brief_path,
    intake_path,
    outline_path,
    write_brief,
    write_outline,
    write_plan,
)
from raven.ppt.profiles import registry
from raven.ppt.tools.build import PptBuildTool


class FakeViews:
    def __init__(self, pages: int = 2) -> None:
        self.pages_count = pages
        self.asked: list[list[int]] = []

    async def pages(self, pptx: Path, out_dir: Path, numbers) -> dict[int, Path]:
        self.asked.append(list(numbers))
        out_dir.mkdir(parents=True, exist_ok=True)
        made = {}
        for n in numbers:
            if n <= self.pages_count:
                path = out_dir / f"page-{n:02d}.png"
                path.write_bytes(b"\x89PNG")
                made[n] = path
        return made

    def data_uri(self, png: Path, budget: int | None = None) -> str:
        return f"data:image/png;base64,{png.stem}"


class FakeStage:
    def __init__(self, result: StageResult) -> None:
        self.result = result
        self.calls: list[tuple[str | None, bool]] = []
        self.drafts: list[bool] = []
        self.backend = None
        self.measure = None
        self.profile = registry.get("script_author")
        self.design_pass = object()
        self.destination = lambda p: p.exports_dir / "deck.pptx"

    async def run(
        self,
        project: Project,
        script: str | None = None,
        *,
        polish: bool = True,
        slides=None,
        page_from: int = 1,
        draft: bool = False,
    ) -> StageResult:
        self.calls.append((script, polish))
        self.drafts.append(draft)
        # The stage decides which pages the reply shows, because that is where the
        # deck is published and an unseen page has to refuse before delivery.
        pages = getattr(self.result.data.get("outcome"), "pages", 0)
        showing = sorted(set(slides))[:12] if slides else list(range(page_from, min(pages, page_from + 11) + 1))
        return replace(self.result, data={**self.result.data, "showing": showing})


def _ok(project: Project, findings=(), pages: int = 2, **data) -> StageResult:
    deck = project.build_dir / "deck.pptx"
    deck.parent.mkdir(parents=True, exist_ok=True)
    deck.write_bytes(b"PK")
    outcome = BuildOutcome(
        ok=True, pptx_path=deck, pages=pages, sources=(PageSource(page=1, first_line=0, last_line=5),)
    )
    blocking = [f for f in findings if f.severity is Severity.BLOCKING]
    return StageResult(
        ok=not blocking,
        findings=tuple(findings),
        data={"outcome": outcome, "pptx_path": str(project.exports_dir / "deck.pptx"), **data},
    )


def _tool(project: Project, result: StageResult, views: FakeViews | None = None) -> PptBuildTool:
    return PptBuildTool(
        workspace=project.workspace,
        stage=FakeStage(result),  # type: ignore[arg-type]
        views=views or FakeViews(),
        profile=registry.get("script_author"),
    )


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    deck = Project(workspace=tmp_path, slug="tarvis")
    # Every case here is about the reply, and the build refuses outright without a
    # brief or an intake plan -- see test_a_deck_with_no_agreed_brief_is_refused and
    # test_a_deck_whose_task_was_never_read_is_refused for those two paths.
    write_brief(
        DeckBrief(language="English", audience="an internal review", pages=PageBudget(1, 40)),
        brief_path(deck),
    )
    write_plan(IntakePlan(topic="a deck", digest="x"), intake_path(deck))
    write_outline(Outline(takeaway="it works", pages=(PagePlan(page=1, claim="It works"),)), outline_path(deck))
    return deck


async def test_a_deck_with_no_outline_is_refused(tmp_path: Path) -> None:
    """The second thing that stops the front of the route being skipped. Without one,
    what a page said got decided while its geometry was being typed."""
    bare = Project(workspace=tmp_path, slug="unplanned")
    write_brief(DeckBrief(language="English", audience="a review", pages=PageBudget(1, 40)), brief_path(bare))
    write_plan(IntakePlan(topic="a deck", digest="x"), intake_path(bare))

    body = _body(await _tool(bare, _ok(bare)).execute(project="unplanned"))

    assert body["ok"] is False
    assert "no outline recorded" in body["error"]
    assert "ppt_outline" in body["hint"]


async def test_a_deck_whose_task_was_never_read_is_refused(tmp_path: Path) -> None:
    """The one thing that stops the front of the route being skipped. A model that
    went straight to write_file and ppt_build got a deck with no idea what it was
    for and no sources under it."""
    bare = Project(workspace=tmp_path, slug="unread")
    write_brief(
        DeckBrief(language="English", audience="a review", pages=PageBudget(1, 40)),
        brief_path(bare),
    )

    body = _body(await _tool(bare, _ok(bare)).execute(project="unread"))

    assert body["ok"] is False
    assert "has not been read" in body["error"]
    assert "ppt_prepare" in body["hint"]


def _body(reply: Any) -> dict[str, Any]:
    return json.loads(reply.model_text if isinstance(reply, ToolResult) else reply)


@pytest.mark.asyncio
async def test_each_render_is_preceded_by_the_line_that_names_its_page(project: Project) -> None:
    reply = await _tool(project, _ok(project)).execute(project="tarvis")

    assert isinstance(reply, ToolResult)
    kinds = [block["type"] for block in reply.blocks or []]
    assert kinds == ["text", "image_url", "text", "image_url"]
    assert "Page 1 of 2" in (reply.blocks or [])[0]["text"]


@pytest.mark.asyncio
async def test_a_page_s_findings_travel_with_that_page_s_picture(project: Project) -> None:
    finding = Finding(
        kind="card_overflow",
        severity=Severity.WARNING,
        message="'VIPSeg' runs 0.27in past the edge of its card",
        page=2,
        audience=Audience.DESIGNER,
    )
    reply = await _tool(project, _ok(project, [finding])).execute(project="tarvis")

    assert isinstance(reply, ToolResult)
    labels = [b["text"] for b in (reply.blocks or []) if b["type"] == "text"]
    assert "runs 0.27in past" in labels[1]
    assert "runs 0.27in past" not in labels[0]


@pytest.mark.asyncio
async def test_the_reply_text_stands_on_its_own_without_the_pictures(project: Project) -> None:
    """Only providers that carry an image in a tool result ever see the blocks."""
    reply = await _tool(project, _ok(project)).execute(project="tarvis")
    body = _body(reply)
    assert body["ok"] is True
    assert body["pptx_path"].endswith("deck.pptx")
    assert body["slides"] == 2


@pytest.mark.asyncio
async def test_every_ask_is_voiced_not_only_the_first(project: Project) -> None:
    findings = [
        Finding(kind="fact", severity=Severity.BLOCKING, message="48.3 is not in the sources", page=11),
        Finding(kind="band", severity=Severity.BLOCKING, message="a filled bar", page=4),
        Finding(kind="band", severity=Severity.BLOCKING, message="another filled bar", page=6),
    ]
    body = _body(await _tool(project, _ok(project, findings)).execute(project="tarvis"))

    assert body["ok"] is False
    assert "unanchored value" in body["next_step"]
    assert "2 filled colour bar" in body["next_step"]


@pytest.mark.asyncio
async def test_a_content_warning_is_addressed_to_the_author_with_the_reason(project: Project) -> None:
    finding = Finding(
        kind="density",
        severity=Severity.WARNING,
        message="the page carries 295 words",
        page=7,
        audience=Audience.AUTHOR,
    )
    body = _body(await _tool(project, _ok(project, [finding])).execute(project="tarvis"))

    assert body["ok"] is True
    assert "comes back compartmented" in body["next_step"]
    assert [f["kind"] for f in body["measured"]["for_you"]] == ["density"]


@pytest.mark.asyncio
async def test_designer_warnings_are_reported_but_not_asked_of_the_author(project: Project) -> None:
    finding = Finding(
        kind="type_floor", severity=Severity.WARNING, message="11.5pt body", page=3, audience=Audience.DESIGNER
    )
    body = _body(await _tool(project, _ok(project, [finding])).execute(project="tarvis"))

    assert [f["kind"] for f in body["measured"]["for_the_design_pass"]] == ["type_floor"]
    # And the reply names the call that finishes the deck. It used to end at "look at
    # every page", which is not a next step for a model that has already looked: one run
    # rebuilt the same finished deck eight times, each reply identical.
    assert body["next_step"].startswith("this deck is delivered at ")
    assert "nothing refuses it" in body["next_step"]
    assert "look at every page below" in body["next_step"]


@pytest.mark.asyncio
async def test_a_build_that_produced_no_deck_says_so_with_the_stderr(project: Project) -> None:
    failed = StageResult(ok=False, data={"outcome": BuildOutcome(ok=False, stderr="NameError: nope")})
    body = _body(await _tool(project, failed).execute(project="tarvis"))

    assert body["ok"] is False
    assert body["error"] == "the build script did not produce a deck"
    assert "NameError" in body["stderr"]


@pytest.mark.asyncio
async def test_a_bad_project_name_is_refused_before_anything_runs(project: Project) -> None:
    body = _body(await _tool(project, _ok(project)).execute(project="../etc"))
    assert body["ok"] is False and "usable project name" in body["error"]


@pytest.mark.asyncio
async def test_the_note_about_an_empty_submission_reaches_the_author(project: Project) -> None:
    deck = project.build_dir / "deck.pptx"
    deck.parent.mkdir(parents=True, exist_ok=True)
    deck.write_bytes(b"PK")
    result = StageResult(
        ok=True,
        data={"outcome": BuildOutcome(ok=True, pptx_path=deck, pages=1, note="build.py was left as it was")},
    )
    body = _body(await _tool(project, result).execute(project="tarvis"))
    assert body["note"] == "build.py was left as it was"


@pytest.mark.asyncio
async def test_only_the_requested_pages_come_back(project: Project) -> None:
    views = FakeViews(pages=18)
    tool = _tool(project, _ok(project, pages=18), views)
    await tool.execute(project="tarvis", slides=[3, 1, 2])
    assert views.asked == [[1, 2, 3]]


@pytest.mark.asyncio
async def test_at_most_twelve_pages_come_back_unasked(project: Project) -> None:
    views = FakeViews(pages=18)
    await _tool(project, _ok(project, pages=18), views).execute(project="tarvis")
    assert views.asked == [list(range(1, 13))]


@pytest.mark.asyncio
async def test_the_design_pass_summary_keeps_the_notes_and_drops_the_machinery(project: Project) -> None:
    design = {
        "rounds": [
            {"round": 1, "deck": {"notes": ["set a scale"]}, "pages": {2: {"notes": ["tightened"]}}, "refused": {}}
        ],
        "vocabulary": ["keep the rail"],
        "outcome": "an object nobody should serialise",
    }
    body = _body(await _tool(project, _ok(project, design_pass=design)).execute(project="tarvis"))

    assert body["design_pass"]["vocabulary"] == ["keep the rail"]
    assert body["design_pass"]["rounds"][0]["deck"] == ["set a scale"]
    assert "outcome" not in body["design_pass"]


@pytest.mark.asyncio
async def test_the_design_pass_cannot_be_switched_off(project: Project) -> None:
    """`polish` was a parameter and it was the one switch that turned off the only stage
    that owns layout. A live run passed `polish=false` on eight consecutive finished
    builds; a draft is not polished and a finished deck always is, so `draft` is the whole
    choice (design doc D8)."""
    stage = FakeStage(_ok(project))
    tool = PptBuildTool(
        workspace=project.workspace, stage=stage, views=FakeViews(), profile=registry.get("script_author")
    )
    await tool.execute(project="tarvis", polish=False)
    await tool.execute(project="tarvis")
    assert [polish for _script, polish in stage.calls] == [True, True]
    assert "polish" not in tool.parameters["properties"]


@pytest.mark.asyncio
async def test_a_deck_with_no_agreed_brief_is_refused_before_it_builds(tmp_path: Path) -> None:
    """Three things about a deck are the user's to decide, and all three are
    checked against the finished file. Defaulting them here would mean measuring
    the deck against a brief nobody agreed to."""
    bare = Project(workspace=tmp_path, slug="tarvis")
    stage = FakeStage(_ok(bare))
    tool = PptBuildTool(workspace=bare.workspace, stage=stage, views=FakeViews(), profile=registry.get("script_author"))
    body = _body(await tool.execute(project="tarvis"))

    assert body["ok"] is False
    assert "no brief recorded" in body["error"]
    assert "ppt_brief" in body["hint"] and "ask_user" in body["hint"]
    assert stage.calls == [], "nothing should have been built"


@pytest.mark.asyncio
async def test_draft_is_passed_through(project: Project) -> None:
    """A program still being written is an accepted state, not a short deck.

    The previous engine learned this from the same failure: requiring a whole deck in
    one submission meant "tens of thousands of tokens that took minutes and that the
    transport truncated". Holding a part-written deck to the agreed length pushes the
    author straight back into that one giant call.
    """
    tool = _tool(project, _ok(project))

    await tool.execute(project=project.slug, draft=True)

    assert tool.stage.drafts == [True]  # type: ignore[attr-defined]
