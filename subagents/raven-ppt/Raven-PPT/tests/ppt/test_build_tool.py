"""What the model sees coming back from ppt_build.

Thin as the adapter is, this is where two measured failures were: batches of
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
from raven.ppt.tools.build import BATCH_VIEWS, PptBuildTool


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
        self.calls: list[str | None] = []
        self.drafts: list[bool] = []
        self.backend = None
        self.measure = None
        self.profile = registry.get("script_author")
        self.destination = lambda p: p.exports_dir / "deck.pptx"

    async def run(
        self,
        project: Project,
        script: str | None = None,
        *,
        slides=None,
        page_from: int = 1,
        draft: bool = False,
    ) -> StageResult:
        self.calls.append(script)
        self.drafts.append(draft)
        # The stage decides which pages the reply shows, because that is where the
        # deck is published and an unseen page has to refuse before delivery.
        pages = getattr(self.result.data.get("outcome"), "pages", 0)
        showing = (
            sorted(set(slides))[:BATCH_VIEWS]
            if slides
            else list(range(page_from, min(pages, page_from + BATCH_VIEWS - 1) + 1))
        )
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
    reply = await _tool(project, _ok(project)).execute(project="tarvis", slides=[1, 2])

    assert isinstance(reply, ToolResult)
    kinds = [block["type"] for block in reply.blocks or []]
    assert kinds == ["text", "image_url"]
    assert "Page 1 of 2" in (reply.blocks or [])[0]["text"]
    assert "Planned claim: It works" in (reply.blocks or [])[0]["text"]


@pytest.mark.asyncio
async def test_a_planned_table_is_shown_as_a_table_and_not_as_a_dict(project: Project) -> None:
    """The author was handed `{'columns': ('指标', '自研'), 'rows': ((...),)}` -- tuples,
    quotes and all -- two lines above the planned points, which are rendered as a list."""
    write_outline(
        Outline(
            takeaway="it works",
            pages=(
                PagePlan(
                    page=1,
                    claim="It works",
                    carries="drawn table",
                    table_plan={
                        "columns": ("指标", "自研"),
                        "rows": (("成本", "4.2"), ("延迟", "38ms")),
                        "reading": "compare cost",
                    },
                ),
            ),
        ),
        outline_path(project),
    )

    reply = await _tool(project, _ok(project, pages=1)).execute(project="tarvis", slides=[1])

    assert isinstance(reply, ToolResult)
    assert (
        "Planned table information shape:\n  指标 | 自研\n  --- | ---\n  成本 | 4.2\n  延迟 | 38ms\n  Read it for: compare cost"
        in (reply.blocks or [])[0]["text"]
    )


@pytest.mark.asyncio
async def test_a_page_s_findings_travel_with_that_page_s_picture(project: Project) -> None:
    finding = Finding(
        kind="card_overflow",
        severity=Severity.WARNING,
        message="'VIPSeg' runs 0.27in past the edge of its card",
        page=1,
    )
    reply = await _tool(project, _ok(project, [finding])).execute(project="tarvis", slides=[1])

    assert isinstance(reply, ToolResult)
    labels = [b["text"] for b in (reply.blocks or []) if b["type"] == "text"]
    assert "runs 0.27in past" in labels[0]


@pytest.mark.asyncio
async def test_the_reply_text_stands_on_its_own_without_the_pictures(project: Project) -> None:
    """Only providers that carry an image in a tool result ever see the blocks."""
    reply = await _tool(project, _ok(project, pages=1)).execute(project="tarvis", slides=[1])
    body = _body(reply)
    assert body["ok"] is True
    assert body["pptx_path"].endswith("deck.pptx")
    assert body["slides"] == 1


@pytest.mark.asyncio
async def test_every_ask_is_voiced_not_only_the_first(project: Project) -> None:
    findings = [
        Finding(kind="citation", severity=Severity.BLOCKING, message="page 11 cites Fig. 4, shows Fig. 5", page=11),
        Finding(kind="band", severity=Severity.BLOCKING, message="a filled bar", page=4),
        Finding(kind="band", severity=Severity.BLOCKING, message="another filled bar", page=6),
    ]
    body = _body(await _tool(project, _ok(project, findings)).execute(project="tarvis"))

    assert body["ok"] is False
    assert "citing one figure while showing another" in body["next_step"]
    assert "2 filled colour bar" in body["next_step"]


@pytest.mark.asyncio
async def test_a_content_warning_is_addressed_to_the_author_with_the_reason(project: Project) -> None:
    """It used to be written against `density`, a kind nothing has emitted for a release.

    The dead `_ASK` line was what kept the test green, so the one thing it was checking
    -- that a warning the author owns comes back with the move that answers it -- was
    being checked against a finding that could not arrive.
    """
    finding = Finding(
        kind="evidence",
        severity=Severity.WARNING,
        message="only 2 of 9 content pages show anything",
        page=7,
    )
    body = _body(await _tool(project, _ok(project, [finding])).execute(project="tarvis"))

    assert body["ok"] is True
    assert "put something on the pages that are all prose" in body["next_step"]
    assert [f["kind"] for f in body["measured"]["for_you"]] == ["evidence"]


@pytest.mark.asyncio
async def test_a_layout_warning_is_the_author_s_to_answer(project: Project) -> None:
    """`type_floor` went to a second actor, under a heading naming it, and was left
    alone: nobody else reads this reply now, so it comes back under `for_you` and is
    voiced in the next step like every other warning."""
    finding = Finding(kind="type_floor", severity=Severity.WARNING, message="11.5pt body", page=3)
    body = _body(await _tool(project, _ok(project, [finding])).execute(project="tarvis"))

    assert [f["kind"] for f in body["measured"]["for_you"]] == ["type_floor"]
    assert "for_the_design_pass" not in body["measured"]
    assert "type_floor" in body["next_step"]
    # And the reply names the call that finishes the deck. It used to end at "look at
    # every page", which is not a next step for a model that has already looked: one run
    # rebuilt the same finished deck eight times, each reply identical.
    assert body["next_step"].startswith("this deck is delivered at ")
    assert "nothing refuses it" in body["next_step"]
    assert "page(s) below" in body["next_step"]


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
    assert views.asked == [[1]]


@pytest.mark.asyncio
async def test_one_page_comes_back_unasked(project: Project) -> None:
    views = FakeViews(pages=18)
    await _tool(project, _ok(project, pages=18), views).execute(project="tarvis")
    assert views.asked == [[1]]


@pytest.mark.asyncio
async def test_a_build_has_no_switch_that_skips_a_check(project: Project) -> None:
    """`polish` was a parameter and it turned off the only stage that owned layout: a
    live run passed `polish=false` on eight consecutive finished builds. That stage is
    gone and so is the parameter, and no unknown argument may quietly stand in for it --
    `draft` is the whole of the choice a caller gets."""
    stage = FakeStage(_ok(project))
    tool = PptBuildTool(
        workspace=project.workspace, stage=stage, views=FakeViews(), profile=registry.get("script_author")
    )
    await tool.execute(project="tarvis", polish=False)
    await tool.execute(project="tarvis")
    assert stage.calls == [None, None], "both builds ran the same way"
    assert not {"polish", "design_pages", "design_setup"} & set(tool.parameters["properties"])


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


async def test_a_deck_the_publish_step_refused_is_not_reported_as_delivered(project: Project) -> None:
    """Two notes reach the reply and only one used to be read.

    `outcome.note` is the script's; `result.note` is the stage's, and the stage puts a
    publish refusal there -- an empty deck, a staged file that vanished, a deck that
    changed between the check and the copy, a write that failed. None of those is a
    blocking finding, so nothing in the reply was false one field at a time: it came
    back `"ok": true`, with no `pptx_path`, and the first thing it told the author was
    that the deck was delivered "at the export path above" and nothing refused it.
    Nothing had been written at all, and the next move it invited was to look at the
    pages of a file that did not exist.
    """
    refused = replace(_ok(project), ok=False, note="the built deck is empty")
    refused = replace(refused, data={k: v for k, v in refused.data.items() if k != "pptx_path"})

    reply = await _tool(project, refused).execute(project="tarvis")
    body = _body(reply)

    assert body["not_delivered"] == "the built deck is empty"
    assert "pptx_path" not in body
    assert "delivered" not in body["next_step"], body["next_step"]
    assert body["next_step"].startswith("nothing was published: the built deck is empty")


def test_the_slides_cap_cannot_exceed_the_batch_the_stage_renders() -> None:
    """Naming more pages than the stage shows drops the surplus without saying so.

    Two constants carry this name -- one here bounding what `slides` may hold, one in
    the stage deciding how many renders a reply carries -- and the stage truncates
    with `[:BATCH_VIEWS]`. Tighter here is a deliberate choice; looser is a request
    the reply silently answers in part, which is how a model concludes it has looked
    at a page nobody rendered.
    """
    from raven.ppt.stages.build import BATCH_VIEWS as RENDERED

    tool = PptBuildTool(workspace=Path("/tmp"), stage=None, views=None, profile=registry.get("script_author"))

    assert BATCH_VIEWS <= RENDERED
    assert tool.parameters["properties"]["slides"]["maxItems"] == BATCH_VIEWS
